#!/usr/bin/env python3
"""Supervise Option A's first paper session and stop only paper on harm or blindness."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time as clock_time, timedelta
from pathlib import Path
from urllib import request
from zoneinfo import ZoneInfo

from redis import Redis

from project_mai_tai.events import MarketDataSubscriptionEvent, MarketDataSubscriptionPayload
from project_mai_tai.events import stream_name
from project_mai_tai.settings import Settings


ET = ZoneInfo("America/New_York")
PAPER_UNIT = "project-mai-tai-momentum-paper.service"
LOW_PAGE_URL = "https://ntfy.sh/mai-tai-routine-112964cc8f26787132a29538"
GATEWAY_LOG = Path("/var/log/project-mai-tai/market-data.log")
V2_LOG = Path("/var/log/project-mai-tai/schwab-1m-v2.log")
OMS_LOG = Path("/var/log/project-mai-tai/oms.log")
PROBE = re.compile(r"\[V2-ATR-PROBE\] sym=([A-Z0-9.\-^]+) ts_ms=(\d+)")
MARKERS = (
    re.compile(r"schwab_1m_v2 db-seed: ([A-Z0-9.\-^]+) hydrated"),
    re.compile(r"\[V2-STREAMER-DRAIN\] replayed \d+ buffered bars for ([A-Z0-9.\-^]+)"),
    re.compile(r"\[V2-REST-WARMED\].* for ([A-Z0-9.\-^]+) "),
    re.compile(r"\[V2-DB-SEED-GAP\] ([A-Z0-9.\-^]+) "),
)
LOG_TIME = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})")
SYMBOL_BOUNDS = {
    "LGHL": (6.018, 6.288, 6.086),
    "NCI": (6.136, 6.632, 6.408),
    "VBIO": (6.222, 6.222, 6.222),
}
POOLED_BOUNDS = (6.096, 6.408, 6.086)
SNAPSHOT_BOUNDS = (14.076, 14.168, 15.106)
HEARTBEAT_AGE_BOUND = 30.819
REDIS_USED_MEMORY_STOP_BYTES = 1_600_000_000
MAX_SNAPSHOT_ID_REPLY_BYTES = 64
MAX_SNAPSHOT_ENTRY_BYTES = 20_000_000
MAX_SNAPSHOT_READS_PER_TICK = 3


class Blind(RuntimeError):
    pass


def parsed_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def log_time(line: str) -> datetime | None:
    match = LOG_TIME.match(line)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)


def nearest_p95(values: list[float]) -> float:
    if not values:
        raise Blind("p95 has no observations")
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def hour_index(now: datetime) -> int:
    hour = now.astimezone(ET).hour
    if hour not in (7, 8, 9):
        raise Blind(f"outside treatment hour: {hour}")
    return hour - 7


@dataclass
class SlowdownRules:
    snapshot_streak: int = 0
    oms_streak: int = 0
    symbol_streaks: dict[str, int] = field(default_factory=dict)
    heartbeat_unhealthy_streak: int = 0
    last_minute: datetime | None = None

    def heartbeat(self, status: str, age_s: float) -> str | None:
        self.heartbeat_unhealthy_streak = (
            self.heartbeat_unhealthy_streak + 1 if status != "healthy" else 0
        )
        if self.heartbeat_unhealthy_streak >= 2:
            return f"gateway_status={status} consecutive={self.heartbeat_unhealthy_streak}"
        if age_s > HEARTBEAT_AGE_BOUND:
            return f"gateway_heartbeat_age_s={age_s:.3f}>{HEARTBEAT_AGE_BOUND}"
        return None

    def minute(
        self,
        now: datetime,
        *,
        snapshot_intervals: list[float],
        probe_lags: dict[str, list[float]],
        oms_refusals: int,
        snapshot_warmup_complete: bool,
    ) -> tuple[str | None, dict]:
        minute = now.replace(second=0, microsecond=0)
        if self.last_minute is not None and minute - self.last_minute != timedelta(minutes=1):
            raise Blind(f"minute evaluation gap: {self.last_minute} -> {minute}")
        self.last_minute = minute
        index = hour_index(now)
        detail: dict = {"minute_et": now.astimezone(ET).isoformat(), "oms_refusals_5m": oms_refusals}
        if len(snapshot_intervals) < 20:
            self.snapshot_streak = 0
            if snapshot_warmup_complete:
                raise Blind(f"snapshot intervals={len(snapshot_intervals)}<20 in rolling 5m")
            detail["snapshot"] = "warming"
        else:
            snap_p95 = nearest_p95(snapshot_intervals)
            self.snapshot_streak = (
                self.snapshot_streak + 1 if snap_p95 > SNAPSHOT_BOUNDS[index] else 0
            )
            detail["snapshot"] = {"n": len(snapshot_intervals), "p95_s": snap_p95}
            if self.snapshot_streak >= 5:
                return f"snapshot_p95_s={snap_p95:.3f}>{SNAPSHOT_BOUNDS[index]}", detail

        self.oms_streak = self.oms_streak + 1 if oms_refusals >= 3 else 0
        if self.oms_streak >= 2:
            return f"oms_quote_refusals_5m={oms_refusals} consecutive=2", detail

        detail["v2"] = {}
        for symbol, values in sorted(probe_lags.items()):
            if len(values) < 3:
                self.symbol_streaks[symbol] = 0
                continue
            bound = SYMBOL_BOUNDS.get(symbol, POOLED_BOUNDS)[index]
            lag_p95 = nearest_p95(values)
            self.symbol_streaks[symbol] = (
                self.symbol_streaks.get(symbol, 0) + 1 if lag_p95 > bound else 0
            )
            detail["v2"][symbol] = {"n": len(values), "p95_s": lag_p95, "bound_s": bound}
            if self.symbol_streaks[symbol] >= 5:
                return f"v2_symbol={symbol} p95_s={lag_p95:.3f}>{bound}", detail
        for symbol in self.symbol_streaks.keys() - probe_lags.keys():
            self.symbol_streaks[symbol] = 0
        return None, detail


class LogTail:
    def __init__(self, path: Path, *, start_at_end: bool = True):
        stat = path.stat()
        self.path = path
        self.identity = stat.st_dev, stat.st_ino
        self.offset = stat.st_size if start_at_end else 0
        self.pending = b""

    def read(self) -> list[str]:
        before = self.path.stat()
        if (before.st_dev, before.st_ino) != self.identity or before.st_size < self.offset:
            raise Blind(f"log rotated/truncated: {self.path}")
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            appended = stream.read(before.st_size - self.offset)
        after = self.path.stat()
        if (after.st_dev, after.st_ino) != self.identity or after.st_size < before.st_size:
            raise Blind(f"log changed during read: {self.path}")
        self.offset = before.st_size
        parts = (self.pending + appended).split(b"\n")
        self.pending = parts[-1]
        return [item.decode("utf-8", errors="replace") for item in parts[:-1]]


class SamplerEvidence:
    def __init__(self, path: Path, *, start: datetime, end: datetime, launched_at: datetime):
        self.path = path
        self.start = start
        self.end = end
        self.last_seen = launched_at
        self.last_recorded: datetime | None = None
        self.tail: LogTail | None = None
        self.treatment_count = 0
        self.log_identity: tuple[int, int] | None = None
        self.last_size: int | None = None

    def read(self, now: datetime) -> str | None:
        if self.tail is None and self.path.exists():
            self.tail = LogTail(self.path, start_at_end=False)
        if self.tail is not None:
            for line in self.tail.read():
                try:
                    row = json.loads(line)
                    stamped = parsed_time(row["sampled_at_utc"])
                    status = row["status"]
                    identity = (row["device"], row["inode"])
                    offset = row["read_from_offset"]
                    size = row["size_bytes"]
                    new_1008 = row["new_1008_lines"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise Blind("malformed 1008 sampler row") from exc
                prior = self.last_recorded
                if (not all(type(item) is int and item >= 0 for item in (*identity, offset, size, new_1008))
                        or size < offset):
                    raise Blind("invalid 1008 sampler cursor fields")
                if prior is None and status != "BASELINE":
                    raise Blind("missing initial 1008 sampler baseline")
                if status == "BASELINE" and stamped >= self.start:
                    raise Blind("1008 sampler baseline at or after treatment start")
                if prior is None:
                    if offset != size:
                        raise Blind("1008 sampler baseline offset unconfirmed")
                    self.log_identity = identity
                elif identity != self.log_identity or offset != self.last_size:
                    raise Blind("1008 sampler cursor discontinuity")
                if prior is not None:
                    gap = (stamped - prior).total_seconds()
                    if not 0.5 <= gap <= 1.5:
                        raise Blind(f"1008 sampler row gap={gap:.3f}s")
                if (now - stamped).total_seconds() < -2:
                    raise Blind("1008 sampler row timestamp in future")
                self.last_recorded = stamped
                self.last_size = size
                self.last_seen = now
                if self.start <= stamped < self.end:
                    self.treatment_count += 1
                if status == "STOP_TRIGGER":
                    if new_1008 < 1:
                        raise Blind("1008 stop row has no new 1008 line")
                    return "gateway_1008"
                if status not in {"BASELINE", "OK"}:
                    raise Blind(f"1008 sampler status={status}")
                if new_1008:
                    raise Blind("1008 sampler suppressed a new 1008 line")
                if status == "BASELINE" and prior is not None:
                    raise Blind("unexpected repeated 1008 sampler baseline")
        if (now - self.last_seen).total_seconds() > 2.5:
            raise Blind("1008 sampler output stalled")
        return None


class LiveSignals:
    def __init__(self, redis: Redis, prefix: str):
        self.redis = redis
        self.prefix = prefix
        self.v2 = LogTail(V2_LOG)
        self.oms = LogTail(OMS_LOG)
        self.probes: list[tuple[datetime, str, float]] = []
        self.probe_keys: set[tuple[str, int]] = set()
        self.replay_seconds: set[tuple[str, int]] = set()
        self.refusals: list[datetime] = []
        self.last_snapshot_id: tuple[int, int] | None = None
        self.snapshot_stamps: list[datetime] = []

    def read_logs(self, start: datetime, now: datetime) -> None:
        for line in self.v2.read():
            stamp = log_time(line)
            if stamp is None or stamp < start or stamp > now + timedelta(seconds=2):
                continue
            for marker in MARKERS:
                match = marker.search(line)
                if match:
                    self.replay_seconds.add((match.group(1), int(stamp.timestamp())))
            match = PROBE.search(line)
            if match:
                key = match.group(1), int(match.group(2))
                if key in self.probe_keys:
                    continue
                self.probe_keys.add(key)
                bar_close = datetime.fromtimestamp(key[1] / 1000, UTC)
                bar_close += timedelta(minutes=1)
                if start <= bar_close < now:
                    self.probes.append((stamp, match.group(1), (stamp - bar_close).total_seconds()))
        for line in self.oms.read():
            stamp = log_time(line)
            if stamp and start <= stamp <= now and (
                "NO_FRESH_QUOTE" in line or "no valid OMS market snapshot" in line
            ):
                self.refusals.append(stamp)
        cutoff = now - timedelta(minutes=10)
        self.probes = [row for row in self.probes if row[0] >= cutoff]
        self.refusals = [stamp for stamp in self.refusals if stamp >= cutoff]
        self.replay_seconds = {
            item for item in self.replay_seconds if item[1] >= int(cutoff.timestamp())
        }

    def heartbeat(self, now: datetime) -> tuple[str, float, dict]:
        key = stream_name(self.prefix, "heartbeats")
        for _, fields in self.redis.xrevrange(key, count=25):
            event = json.loads(fields["data"])
            if event.get("source_service") != "market-data-gateway":
                continue
            stamp = parsed_time(event["produced_at"])
            age = (now - stamp).total_seconds()
            if age < -2:
                raise Blind("gateway heartbeat timestamp is in the future")
            return event["payload"]["status"], age, event
        raise Blind("no gateway heartbeat in latest 25 events")

    def sample_snapshot_id(self, now: datetime) -> None:
        key = stream_name(self.prefix, "snapshot-batches")
        if self.last_snapshot_id is None:
            entries = self.redis.xrevrange(key, max="+", min="-", count=1)
            if not entries:
                raise Blind("snapshot stream is empty or unavailable")
            raw = self._snapshot_reply_id(entries)
            del entries
            self._record_snapshot_id(raw, now)
            return
        # One payload at a time. Three nonempty reads mean we cannot prove caught-up.
        for _ in range(MAX_SNAPSHOT_READS_PER_TICK):
            cursor = "-".join(map(str, self.last_snapshot_id))
            entries = self.redis.xrange(key, min=f"({cursor}", max="+", count=1)
            if not entries:
                return
            raw = self._snapshot_reply_id(entries)
            del entries
            self._record_snapshot_id(raw, now)
        raise Blind(f"snapshot catch-up exceeded {MAX_SNAPSHOT_READS_PER_TICK} reads per tick")

    @staticmethod
    def _snapshot_reply_id(entries: list) -> str | bytes:
        if len(entries) != 1:
            raise Blind("snapshot reply exceeded COUNT 1")
        raw, fields = entries[0]
        if not isinstance(fields, dict):
            raise Blind("snapshot fields are unreadable")
        size = 0
        for name, value in fields.items():
            for part in (name, value):
                if not isinstance(part, (str, bytes)):
                    raise Blind("snapshot field is unreadable")
                size += len(part.encode("utf-8") if isinstance(part, str) else part)
        if size > MAX_SNAPSHOT_ENTRY_BYTES:
            raise Blind(f"snapshot entry bytes={size}>{MAX_SNAPSHOT_ENTRY_BYTES}")
        return raw

    def _record_snapshot_id(self, raw: str | bytes, now: datetime) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="strict")
        if not isinstance(raw, str) or len(raw.encode("ascii", errors="strict")) > MAX_SNAPSHOT_ID_REPLY_BYTES:
            raise Blind("snapshot last-generated-id reply is invalid or oversized")
        match = re.fullmatch(r"(\d+)-(\d+)", raw)
        if match is None:
            raise Blind("snapshot last-generated-id is malformed")
        snapshot_id = int(match.group(1)), int(match.group(2))
        if snapshot_id == (0, 0):
            raise Blind("snapshot id is zero")
        if self.last_snapshot_id is not None and snapshot_id <= self.last_snapshot_id:
            raise Blind("snapshot id moved backwards or repeated after exclusive cursor")
        stamp = datetime.fromtimestamp(snapshot_id[0] / 1000, UTC)
        if stamp > now + timedelta(seconds=2):
            raise Blind("snapshot last-generated-id is in the future")
        self.last_snapshot_id = snapshot_id
        self.snapshot_stamps.append(stamp)
        cutoff = now - timedelta(minutes=5)
        while len(self.snapshot_stamps) > 1 and self.snapshot_stamps[1] <= cutoff:
            self.snapshot_stamps.pop(0)

    def snapshot_intervals(self, now: datetime) -> list[float]:
        stamps = self.snapshot_stamps
        cutoff = now - timedelta(minutes=5)
        if not stamps or (now - stamps[-1]).total_seconds() > max(SNAPSHOT_BOUNDS):
            raise Blind("latest snapshot batch is too old or unavailable")
        return [
            (later - earlier).total_seconds()
            for earlier, later in zip(stamps, stamps[1:])
            if cutoff < later <= now
        ]

    def minute_values(self, now: datetime) -> tuple[dict[str, list[float]], int]:
        cutoff = now - timedelta(minutes=5)
        lags: dict[str, list[float]] = defaultdict(list)
        for stamp, symbol, lag in self.probes:
            if cutoff < stamp <= now and (symbol, int(stamp.timestamp())) not in self.replay_seconds:
                lags[symbol].append(lag)
        return dict(lags), sum(cutoff < stamp <= now for stamp in self.refusals)


class RedisSafety:
    def __init__(self, redis: Redis):
        self.redis = redis
        self.initial_evicted_keys: int | None = None

    def sample(self) -> tuple[str | None, dict[str, int]]:
        stats = self.redis.info("stats")
        memory = self.redis.info("memory")
        try:
            evicted = int(stats["evicted_keys"])
            used = int(memory["used_memory"])
        except (KeyError, TypeError, ValueError) as exc:
            raise Blind("Redis memory or eviction evidence unreadable") from exc
        if evicted < 0 or used < 0:
            raise Blind("Redis memory or eviction evidence invalid")
        if self.initial_evicted_keys is None:
            self.initial_evicted_keys = evicted
        if evicted < self.initial_evicted_keys:
            raise Blind("Redis eviction counter moved backwards")
        detail = {"evicted_keys": evicted, "used_memory_bytes": used}
        if evicted > self.initial_evicted_keys:
            return f"redis_evicted_keys={evicted}>{self.initial_evicted_keys}", detail
        if used > REDIS_USED_MEMORY_STOP_BYTES:
            return f"redis_used_memory_bytes={used}>{REDIS_USED_MEMORY_STOP_BYTES}", detail
        return None, detail

def _owners(redis: Redis, prefix: str) -> dict[str, set[str]]:
    encoded = redis.hgetall(stream_name(prefix, "market-data-subscription-owners"))
    if not encoded or encoded.get("_migration_complete") != "1":
        raise Blind("gateway owner hash absent or migration incomplete")
    owners = {}
    for name, value in encoded.items():
        if name.startswith("_"):
            continue
        symbols = json.loads(value)
        if not isinstance(symbols, list) or not all(isinstance(item, str) for item in symbols):
            raise Blind(f"malformed gateway owner {name}")
        owners[name] = set(symbols)
    return owners


def _healthy_union_count(redis: Redis, prefix: str, since: datetime) -> int | None:
    key = stream_name(prefix, "heartbeats")
    for _, fields in redis.xrevrange(key, count=25):
        event = json.loads(fields["data"])
        if event.get("source_service") != "market-data-gateway":
            continue
        if parsed_time(event["produced_at"]) <= since:
            return None
        if event["payload"]["status"] != "healthy":
            return None
        return int(event["payload"]["details"]["active_symbols"])
    return None


def _union_line_confirms(path: Path, offset: int, removed: set[str]) -> bool:
    with path.open("rb") as stream:
        stream.seek(offset)
        lines = stream.read().decode("utf-8", errors="replace").splitlines()
    for line in lines:
        if "[MARKET-DATA-SUBSCRIPTION-UNION] consumer=momentum-paper" not in line:
            continue
        match = re.search(r" removed=([^ ]+)", line)
        if match and (not removed or removed <= set(match.group(1).split(","))):
            return True
    return False


def _release_confirmed(
    redis: Redis, settings: Settings, *, before: dict[str, set[str]], stopped_at: datetime,
    log_offset: int,
) -> bool:
    current = _owners(redis, settings.redis_stream_prefix)
    if current.get("momentum-paper") != set() or "momentum-paper" not in current:
        return False
    for consumer, symbols in before.items():
        if consumer != "momentum-paper" and not symbols <= current.get(consumer, set()):
            return False
    remaining = set(settings.market_data_static_symbol_list)
    for name, symbols in current.items():
        if name != "momentum-paper":
            remaining |= symbols
    old_paper = before.get("momentum-paper", set())
    removed = old_paper - remaining
    count = _healthy_union_count(redis, settings.redis_stream_prefix, stopped_at)
    return count == len(remaining) and (
        not old_paper or _union_line_confirms(GATEWAY_LOG, log_offset, removed)
    )


def _page(title: str, body: str, *, priority: str = "low") -> bool:
    payload = request.Request(
        LOW_PAGE_URL, data=body.encode(),
        headers={"Title": title, "Priority": priority}, method="POST",
    )
    try:
        with request.urlopen(payload, timeout=15) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _page_with_audit(title: str, body: str, audit: Path) -> bool:
    try:
        delivered = _page(title, body)
    except Exception:
        delivered = False
    try:
        _audit(audit, {
            "action": "page_delivery", "title": title,
            "delivered": delivered, "at_utc": datetime.now(UTC).isoformat(),
        })
    except Exception:
        return False
    return delivered


def _notify_systemd(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(message.encode())


def stop_paper(reason: str, redis: Redis, settings: Settings, audit: Path) -> int:
    try:
        before = _owners(redis, settings.redis_stream_prefix)
        log_offset = GATEWAY_LOG.stat().st_size
    except Exception as exc:
        before = {}
        log_offset = 0
        reason += f"; before_owner_unreadable={type(exc).__name__}"
    try:
        stopped = subprocess.run(
            ["systemctl", "stop", PAPER_UNIT], check=False, capture_output=True, text=True,
            timeout=35,
        )
        stop_rc = stopped.returncode
    except (OSError, subprocess.TimeoutExpired):
        stop_rc = 2
    stopped_at = datetime.now(UTC)
    result = {
        "at_utc": stopped_at.isoformat(), "action": "stop_paper", "reason": reason,
        "systemctl_rc": stop_rc,
    }
    try:
        _audit(audit, result)
    except Exception as exc:
        _page_with_audit(
            "Option A audit UNKNOWN",
            f"paper stop rc={stop_rc}; audit unreadable: {type(exc).__name__}: {exc}; {reason}",
            audit,
        )
        return 2
    if stop_rc != 0:
        _page_with_audit("Option A paper STOP UNKNOWN", json.dumps(result, sort_keys=True), audit)
        return 2
    deadline = time.monotonic() + 35
    for attempt in range(2):
        while time.monotonic() < deadline:
            try:
                if before and _release_confirmed(
                    redis, settings, before=before, stopped_at=stopped_at,
                    log_offset=log_offset,
                ):
                    try:
                        _audit(audit, {"action": "owner_release_confirmed", "attempt": attempt})
                    except Exception as exc:
                        _page_with_audit(
                            "Option A audit UNKNOWN",
                            f"paper stopped, release confirmed but audit failed: "
                            f"{type(exc).__name__}: {exc}; {reason}", audit,
                        )
                        return 2
                    return 0 if _page_with_audit(
                        "Option A paper STOP", json.dumps(result, sort_keys=True), audit,
                    ) else 2
            except Exception:
                pass
            time.sleep(1)
        if attempt == 0:
            event = MarketDataSubscriptionEvent(
                source_service="option-a-guard",
                payload=MarketDataSubscriptionPayload(
                    consumer_name="momentum-paper", mode="replace", symbols=[],
                ),
            )
            try:
                event_id = redis.xadd(
                    stream_name(settings.redis_stream_prefix, "market-data-subscriptions"),
                    {"data": event.model_dump_json()},
                )
                with audit.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"action": "empty_replace", "event_id": event_id}) + "\n")
            except Exception:
                break
            deadline = time.monotonic() + 35
    _page_with_audit("Option A owner release UNKNOWN",
                     f"paper stopped, owner release unverified: {reason}", audit)
    return 2


def _audit(path: Path, item: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, sort_keys=True) + "\n")
        stream.flush()


def _cpu_contributors() -> list[str]:
    result = subprocess.run(
        ["ps", "-eo", "pid,comm,pcpu", "--sort=-pcpu"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    return result.stdout.splitlines()[1:9]


def _route_sampler_exit(returncode: int | None, stop: Callable[[str], int]) -> int | None:
    if returncode is None:
        return None
    if returncode == 3:
        return stop("gateway_1008")
    raise Blind(f"1008 sampler exited early rc={returncode}")


def finish_sampler(sampler, evidence, now):
    sampler.wait(timeout=3)
    if sampler.returncode != 0:
        raise Blind(f"1008 sampler ended rc={sampler.returncode}")
    # The fractional-phase last row may arrive after the guard's last integer tick.
    return evidence.read(now())


def run_guard(treatment_date: date, output_dir: Path) -> int:
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    audit = output_dir / "option-a-guard.jsonl"
    sampler_output = output_dir / "option-a-1008.jsonl"
    start = datetime.combine(treatment_date, clock_time(7), tzinfo=ET).astimezone(UTC)
    end = datetime.combine(treatment_date, clock_time(9, 40), tzinfo=ET).astimezone(UTC)
    snapshot_warmup_start = start - timedelta(minutes=5, seconds=30)
    if datetime.now(UTC) >= start:
        raise Blind("guard started at or after treatment start")
    signals = LiveSignals(redis, settings.redis_stream_prefix)
    redis_safety = RedisSafety(redis)
    rules = SlowdownRules()
    launched_at = datetime.now(UTC)
    sampler = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("sampler.py")),
            "--gateway-log", str(GATEWAY_LOG), "--output", str(sampler_output),
            "--treatment-date", treatment_date.isoformat(), "--end-utc", end.isoformat(),
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    _audit(audit, {"action": "start", "at_utc": datetime.now(UTC).isoformat(),
                   "sampler_pid": sampler.pid, "treatment_date": treatment_date.isoformat()})
    sampler_evidence = SamplerEvidence(
        sampler_output, start=start, end=end, launched_at=launched_at,
    )
    _notify_systemd("READY=1")
    next_minute = start + timedelta(minutes=1)
    last_load_at: datetime | None = None
    load_count = 0
    warning_load = False
    wall_now = datetime.now(UTC)
    next_tick = time.monotonic() + (1 - wall_now.microsecond / 1_000_000)
    def interrupted(_signum: int, _frame: object) -> None:
        raise Blind("guard interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        while True:
            wait = next_tick - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            now = datetime.now(UTC)
            if now >= end:
                break
            if now >= start and time.monotonic() - next_tick > 0.5:
                raise Blind(f"1Hz monitor tick late at {now.isoformat()}")
            next_tick += 1
            _notify_systemd("WATCHDOG=1")
            evidence_trigger = sampler_evidence.read(now)
            if evidence_trigger:
                return stop_paper(evidence_trigger, redis, settings, audit)
            sampler_result = _route_sampler_exit(
                sampler.poll(), lambda reason: stop_paper(reason, redis, settings, audit),
            )
            if sampler_result is not None:
                return sampler_result
            memory_trigger, redis_detail = redis_safety.sample()
            if memory_trigger:
                return stop_paper(memory_trigger, redis, settings, audit)
            if now >= snapshot_warmup_start:
                signals.sample_snapshot_id(now)
            if now >= start:
                if last_load_at and (now - last_load_at).total_seconds() > 2.5:
                    raise Blind(f"1Hz load sample gap after {last_load_at.isoformat()}")
                load1, load5, load15 = os.getloadavg()
                last_load_at = now
                load_count += 1
                warning_load |= load1 > 3.5
                _audit(audit, {
                    "action": "load_sample", "at_utc": now.isoformat(),
                    "load1": load1, "load5": load5, "load15": load15,
                    "over_3_5_warning_only": load1 > 3.5,
                })
                signals.read_logs(start, now)
                status, age, _event = signals.heartbeat(now)
                trigger = rules.heartbeat(status, age)
                if trigger:
                    return stop_paper(trigger, redis, settings, audit)
                if now >= next_minute + timedelta(seconds=1):
                    lags, refusals = signals.minute_values(now)
                    trigger, detail = rules.minute(
                        now, snapshot_intervals=signals.snapshot_intervals(now),
                        probe_lags=lags, oms_refusals=refusals,
                        snapshot_warmup_complete=now >= start + timedelta(minutes=5),
                    )
                    detail.update({"load1": os.getloadavg()[0], "heartbeat_age_s": age,
                                   "redis": redis_detail})
                    if warning_load:
                        detail["cpu_contributors"] = _cpu_contributors()
                        warning_load = False
                    _audit(audit, detail)
                    if trigger:
                        return stop_paper(trigger, redis, settings, audit)
                    next_minute += timedelta(minutes=1)
        final_trigger = finish_sampler(sampler, sampler_evidence, lambda: datetime.now(UTC))
        if final_trigger:
            return stop_paper(final_trigger, redis, settings, audit)
        expected = int((end - start).total_seconds())
        if load_count != expected or sampler_evidence.treatment_count != expected:
            raise Blind(
                f"coverage load={load_count}/{expected} "
                f"1008={sampler_evidence.treatment_count}/{expected}"
            )
        _audit(audit, {
            "action": "complete", "at_utc": datetime.now(UTC).isoformat(),
            "load_samples": load_count, "sampler_samples": sampler_evidence.treatment_count,
            "expected_samples": expected,
        })
        return 0
    except Exception as exc:
        return stop_paper(f"monitor_UNKNOWN:{type(exc).__name__}:{exc}", redis, settings, audit)
    finally:
        if sampler.poll() is None:
            sampler.terminate()
            sampler.wait(timeout=5)
        redis.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--treatment-date", type=date.fromisoformat)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--emergency-stop", action="store_true")
    args = parser.parse_args(argv)
    if not args.treatment_date or not args.output_dir:
        parser.error("--treatment-date and --output-dir are required")
    if args.emergency_stop:
        settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            return stop_paper("guard_process_died", redis, settings,
                              args.output_dir / "option-a-guard.jsonl")
        finally:
            redis.close()
    return run_guard(args.treatment_date, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
