"""After-hours Step 2 measurement for the Momentum gateway hand-off.

This CLI remains standalone even though the runtime gateway and paper service
now import the measured hand-off primitive behind a default-off feature flag.
It reads Massive daily trade flat files, replays one measured peak minute, and
observes existing Redis streams with read-only ``XREAD`` calls.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import csv
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
import gzip
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time as clock
from typing import Awaitable, Callable, Mapping, Sequence, TypeVar
from urllib.parse import quote
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from project_mai_tai.events import (
    HeartbeatEvent,
    MarketDataSubscriptionEvent,
    QuoteTickEvent,
    SnapshotBatchEvent,
    stream_name,
)
from project_mai_tai.momentum_gateway_handoff import (
    BoundedPaperHandoff,
    connect_consumer_socket,
    CrossProcessPaperConsumer,
    drain_handoff_to_socket,
    SocketWriterCounters,
)


_ET = ZoneInfo("America/New_York")
_WINDOW_START = time(4, 0)
_WINDOW_END = time(9, 30)
_EXPECTED_COLUMNS = {
    "ticker",
    "conditions",
    "correction",
    "exchange",
    "id",
    "participant_timestamp",
    "price",
    "sequence_number",
    "sip_timestamp",
    "size",
    "tape",
    "trf_id",
    "trf_timestamp",
}
_POLICY_NEEDLE = b"1008 (policy violation)"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_STRICT_FLATNESS_PREFLIGHT = _REPO_ROOT / "ops/preflight/preflight_oms_restart.sh"
_REPLAY_START = time(16, 5)
_AFTER_HOURS_START = time(20)
_MASSIVE_FLAT_FILE_HOST = "files.massive.com"
_MASSIVE_FLAT_FILE_BUCKET = "flatfiles"
_MASSIVE_FLAT_FILE_ACCESS_KEY_ID = "ba57433b-db93-4501-a02d-6a22bf4b1856"
_MASSIVE_FLAT_FILE_REGION = "us-east-1"
_FLAT_FILE_SECRET_ENV = "MAI_TAI_MASSIVE_API_KEY"
_DEFAULT_FREE_SPACE_RESERVE_BYTES = 5 * 1024**3


_ReplaySpec = TypeVar("_ReplaySpec")
_ReplayOutput = TypeVar("_ReplayOutput")


@dataclass(frozen=True)
class PopulationResult:
    session_date: str
    source: str
    source_sha256: str
    source_complete: bool
    blind_spots: tuple[str, ...]
    total_prints: int
    seconds_in_window: int
    seconds_observed: int
    max_prints_1s: int
    p99_prints_1s: int
    busiest_60s_prints: int
    busiest_60s_start_utc: str
    busiest_60s_end_utc: str
    replay_tape: str


@dataclass(frozen=True)
class ExistingWorkResult:
    duration_seconds: float
    snapshot_count: int
    snapshot_p99_ms: float | None
    missed_snapshot_cycles: int
    quote_count: int
    quote_latency_p99_ms: float | None
    active_symbols: int
    heartbeat_active_symbols: int
    quote_latency_status: str
    raw_samples: str


@dataclass(frozen=True)
class ReplayResult:
    label: str
    speed: float
    consumer: str
    input_frames: int
    forwarded_frames: int
    queue_dropped_frames: int
    socket_would_block_drops: int
    parse_failures: int
    producer_pid: int
    consumer_pid: int
    consumed_frames: int
    handoff_p50_ms: float | None
    handoff_p95_ms: float | None
    handoff_p99_ms: float | None
    handoff_max_ms: float | None
    producer_offer_elapsed_ms: float
    producer_offer_schedule_delay_p99_ms: float
    producer_cpu_peak_pct_one_cpu: float
    producer_peak_rss_bytes: int
    consumer_cpu_peak_pct_one_cpu: float
    consumer_peak_rss_bytes: int
    gateway_cpu_peak_pct_one_cpu: float
    replay_access: ReplayAccessEvidence
    baseline: ExistingWorkResult
    during: ExistingWorkResult
    verdict: str
    reasons: tuple[str, ...]
    cpu_samples: str
    consumer_samples: str


@dataclass(frozen=True)
class ReplayAccessEvidence:
    checked_at_utc: str
    branch: str
    detail: str


@dataclass(frozen=True)
class FlatFileObject:
    key: str
    size_bytes: int


@dataclass(frozen=True)
class PacedReplayResult:
    writer: SocketWriterCounters
    offer_elapsed_ms: float
    offer_schedule_delay_p99_ms: float


class ReplayAborted(RuntimeError):
    pass


def require_replay_niceness(
    priority_reader: Callable[[], int] = lambda: os.getpriority(os.PRIO_PROCESS, 0),
) -> None:
    if os.name == "posix" and priority_reader() < 10:
        raise ReplayAborted("replay process nice value must be at least 10")


def require_replay_access(
    *,
    now: datetime | None = None,
    preflight_path: Path = _STRICT_FLATNESS_PREFLIGHT,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    priority_reader: Callable[[], int] = lambda: os.getpriority(os.PRIO_PROCESS, 0),
) -> ReplayAccessEvidence:
    """Prove the frozen flat-window-or-after-hours replay precondition."""

    require_replay_niceness(priority_reader)
    observed = now or datetime.now(_ET)
    local = observed.astimezone(_ET)
    if local.time() >= _AFTER_HOURS_START:
        return ReplayAccessEvidence(
            checked_at_utc=observed.astimezone(UTC).isoformat(),
            branch="AFTER_20_ET",
            detail="after 20:00 ET; flatness preflight not required by the frozen protocol",
        )
    if local.time() < _REPLAY_START:
        raise ReplayAborted("live-box replay is restricted to 16:05 ET or later")
    completed = runner(
        [str(preflight_path), "--require-all-account-positions-flat"],
        check=False,
        capture_output=True,
        text=True,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if completed.returncode != 0:
        raise ReplayAborted(
            "strict live-account flatness preflight refused "
            f"(rc={completed.returncode}): {output}"
        )
    required = (
        "zero open managed rows",
        "live:schwab_1m_v2 flat [",
        "live:orb flat [",
        "strict all-account-position flatness enabled",
    )
    missing = [marker for marker in required if marker not in output]
    if missing:
        raise ReplayAborted(
            "strict live-account flatness preflight omitted required evidence: "
            + ", ".join(missing)
        )
    return ReplayAccessEvidence(
        checked_at_utc=observed.astimezone(UTC).isoformat(),
        branch="FLAT_16_05_TO_20_ET",
        detail="zero open managed rows; both live accounts fresh and literally flat",
    )


async def run_guarded_replays(
    specs: Sequence[_ReplaySpec],
    *,
    access_checker: Callable[[], ReplayAccessEvidence],
    replay_runner: Callable[[_ReplaySpec], Awaitable[_ReplayOutput]],
) -> tuple[tuple[ReplayAccessEvidence, ...], tuple[_ReplayOutput, ...]]:
    """Re-check access immediately before every replay, including between runs."""

    checks: list[ReplayAccessEvidence] = []
    rows: list[_ReplayOutput] = []
    for spec in specs:
        checks.append(access_checker())
        rows.append(await replay_runner(spec))
    return tuple(checks), tuple(rows)


def verdict_exit_code(verdict: str) -> int:
    if verdict == "PASS":
        return 0
    if verdict == "FAIL":
        return 1
    if verdict == "UNMEASURED":
        return 2
    raise ValueError(f"unknown replay verdict: {verdict}")


def _sigv4_key(secret: str, day: str) -> bytes:
    dated = hmac.new(f"AWS4{secret}".encode(), day.encode(), hashlib.sha256).digest()
    region = hmac.new(dated, _MASSIVE_FLAT_FILE_REGION.encode(), hashlib.sha256).digest()
    service = hmac.new(region, b"s3", hashlib.sha256).digest()
    return hmac.new(service, b"aws4_request", hashlib.sha256).digest()


class MassiveFlatFileClient:
    """Minimal read-only S3 client for the offline Momentum measurement."""

    def __init__(self, *, secret: str) -> None:
        if not secret:
            raise RuntimeError(f"{_FLAT_FILE_SECRET_ENV} is empty")
        self._secret = secret

    @classmethod
    def from_environment(cls) -> MassiveFlatFileClient:
        try:
            secret = os.environ[_FLAT_FILE_SECRET_ENV]
        except KeyError as exc:
            raise RuntimeError(
                f"{_FLAT_FILE_SECRET_ENV} must be present in the measurement process environment"
            ) from exc
        return cls(secret=secret)

    def _signed_headers(self, method: str, key: str, *, now: datetime) -> tuple[str, dict[str, str]]:
        observed = now.astimezone(UTC)
        amz_date = observed.strftime("%Y%m%dT%H%M%SZ")
        day = observed.strftime("%Y%m%d")
        path = "/" + quote(f"{_MASSIVE_FLAT_FILE_BUCKET}/{key}", safe="/")
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_headers = (
            f"host:{_MASSIVE_FLAT_FILE_HOST}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            (method, path, "", canonical_headers, signed_headers, payload_hash)
        )
        scope = f"{day}/{_MASSIVE_FLAT_FILE_REGION}/s3/aws4_request"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            )
        )
        signature = hmac.new(
            _sigv4_key(self._secret, day),
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={_MASSIVE_FLAT_FILE_ACCESS_KEY_ID}/{scope},"
            f"SignedHeaders={signed_headers},Signature={signature}"
        )
        return path, {
            "Host": _MASSIVE_FLAT_FILE_HOST,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": authorization,
        }

    def _request(self, method: str, key: str) -> http.client.HTTPResponse:
        path, headers = self._signed_headers(method, key, now=datetime.now(UTC))
        connection = http.client.HTTPSConnection(_MASSIVE_FLAT_FILE_HOST, timeout=120)
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        # Keep the connection alive through streaming GETs; callers close the response.
        setattr(response, "_mai_tai_connection", connection)
        return response

    @staticmethod
    def _close_response(response: http.client.HTTPResponse) -> None:
        response.close()
        connection = getattr(response, "_mai_tai_connection", None)
        if connection is not None:
            connection.close()

    def head(self, key: str) -> FlatFileObject:
        response = self._request("HEAD", key)
        try:
            if response.status != 200:
                body = response.read(4_096).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"flat-file HEAD {key} returned HTTP {response.status}: {body}"
                )
            raw_size = response.getheader("Content-Length")
            if raw_size is None or not raw_size.isdigit():
                raise RuntimeError(f"flat-file HEAD {key} omitted a numeric Content-Length")
            return FlatFileObject(key=key, size_bytes=int(raw_size))
        finally:
            self._close_response(response)

    def download(self, object_: FlatFileObject, destination: Path) -> None:
        response = self._request("GET", object_.key)
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            if response.status != 200:
                body = response.read(4_096).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"flat-file GET {object_.key} returned HTTP {response.status}: {body}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with temporary.open("wb") as handle:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
                    written += len(chunk)
            if written != object_.size_bytes:
                raise RuntimeError(
                    f"flat-file GET {object_.key} wrote {written} bytes, expected "
                    f"{object_.size_bytes}"
                )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
            self._close_response(response)


class PolicyLogCounter:
    """Monotonic policy-violation count without rescanning the live log each second."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._device_inode: tuple[int, int] | None = None
        self._offset = 0
        self._count = 0

    def read(self) -> int:
        stat = self.path.stat()
        device_inode = (stat.st_dev, stat.st_ino)
        if self._device_inode != device_inode or stat.st_size < self._offset:
            self._device_inode = device_inode
            self._offset = 0
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()
        self._count += chunk.count(_POLICY_NEEDLE)
        return self._count


def nearest_rank(values: Sequence[float | int], percentile: int) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 1 <= percentile <= 100:
        raise ValueError("percentile must be in 1..100")
    ordered = sorted(float(value) for value in values)
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[rank - 1]


def measured_or_unmeasured(sample_count: int, *, absent_reason: str) -> str:
    return "MEASURED" if sample_count > 0 else f"UNMEASURED_{absent_reason}"


def replay_abort_reason(
    *,
    heartbeat_status: str,
    initial_policy_count: int,
    current_policy_count: int,
    load_average: float,
) -> str | None:
    if heartbeat_status != "healthy":
        return f"gateway heartbeat is {heartbeat_status!r}, expected 'healthy'"
    if current_policy_count > initial_policy_count:
        return (
            "market-data.log gained "
            f"{current_policy_count - initial_policy_count} policy-violation line(s)"
        )
    if load_average > 3.5:
        return f"one-minute load average {load_average:.3f} exceeds 3.5"
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _session_bounds(day: date) -> tuple[int, int]:
    start = datetime.combine(day, _WINDOW_START, tzinfo=_ET).astimezone(UTC)
    end = datetime.combine(day, _WINDOW_END, tzinfo=_ET).astimezone(UTC)
    return int(start.timestamp()), int(end.timestamp())


def _date_from_flat_file(path: Path) -> date:
    name = path.name.removesuffix(".csv.gz").removesuffix(".csv")
    try:
        return date.fromisoformat(name)
    except ValueError as exc:
        raise ValueError(f"flat-file name must be YYYY-MM-DD.csv.gz: {path}") from exc


def _open_csv(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def _flat_trade_frame(row: Mapping[str, str]) -> dict[str, object]:
    conditions = [int(value) for value in row["conditions"].split(",") if value]
    trf = int(row["trf_id"] or 0)
    size = Decimal(row["size"])
    if size != size.to_integral_value():
        raise ValueError(f"flat-file trade size must be integral: {row['size']}")
    return {
        "ev": "T",
        "sym": row["ticker"],
        "t": int(row["sip_timestamp"]),
        "p": row["price"],
        "s": int(size),
        "c": conditions,
        "i": row["id"],
        "x": int(row["exchange"]),
        "trfi": trf or None,
    }


def summarize_massive_flat_file(path: Path, replay_dir: Path) -> PopulationResult:
    """Scan one complete Massive SIP trades flat file and retain its peak minute."""

    day = _date_from_flat_file(path)
    start_s, end_s = _session_bounds(day)
    counts: Counter[int] = Counter()
    with _open_csv(path) as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not _EXPECTED_COLUMNS.issubset(reader.fieldnames):
            missing = sorted(_EXPECTED_COLUMNS - set(reader.fieldnames or ()))
            raise RuntimeError(f"{path} is not a Massive trades_v1 flat file; missing={missing}")
        for row in reader:
            second = int(row["sip_timestamp"]) // 1_000_000_000
            if start_s <= second < end_s:
                counts[second] += 1
    if not counts:
        raise RuntimeError(f"{path} contains no prints in 04:00:00..09:29:59 ET")

    per_second = [counts.get(second, 0) for second in range(start_s, end_s)]
    rolling = sum(per_second[:60])
    peak_count = rolling
    peak_index = 0
    for index in range(60, len(per_second)):
        rolling += per_second[index] - per_second[index - 60]
        if rolling > peak_count:
            peak_count = rolling
            peak_index = index - 59
    peak_start_s = start_s + peak_index
    peak_end_s = peak_start_s + 60

    replay_rows: list[tuple[int, int, dict[str, object]]] = []
    with _open_csv(path) as handle:
        reader = csv.DictReader(handle)
        for ordinal, row in enumerate(reader):
            sip_ns = int(row["sip_timestamp"])
            if peak_start_s * 1_000_000_000 <= sip_ns < peak_end_s * 1_000_000_000:
                replay_rows.append((sip_ns, ordinal, _flat_trade_frame(row)))
    replay_rows.sort(key=lambda item: (item[0], item[1]))
    replay_dir.mkdir(parents=True, exist_ok=True)
    replay_path = replay_dir / f"momentum-gateway-peak-{day.isoformat()}.jsonl.gz"
    temporary = replay_path.with_name(f".{replay_path.name}.tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        for sip_ns, ordinal, frame in replay_rows:
            handle.write(
                json.dumps(
                    {"source_ns": sip_ns, "source_ordinal": ordinal, "frame": frame},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    temporary.replace(replay_path)
    return PopulationResult(
        session_date=day.isoformat(),
        source="Massive us_stocks_sip/trades_v1 daily SIP flat file",
        source_sha256=_sha256(path),
        source_complete=True,
        blind_spots=(
            "next-day source; files publish around 11:00 ET and cannot measure same-day availability",
            "SIP timestamps are UTC epoch values and are converted explicitly to ET for the window",
            "prices are unadjusted; no later split adjustment is applied",
            "SIP timestamps do not preserve websocket packet batching or host-arrival jitter",
            "cannot observe prints omitted or later corrected by the upstream provider",
        ),
        total_prints=sum(per_second),
        seconds_in_window=end_s - start_s,
        seconds_observed=len(counts),
        max_prints_1s=max(per_second),
        p99_prints_1s=int(nearest_rank(per_second, 99)),
        busiest_60s_prints=peak_count,
        busiest_60s_start_utc=datetime.fromtimestamp(peak_start_s, tz=UTC).isoformat(),
        busiest_60s_end_utc=datetime.fromtimestamp(peak_end_s, tz=UTC).isoformat(),
        replay_tape=str(replay_path),
    )


def _population_report_from_results(
    results: Sequence[PopulationResult], *, required_session: date
) -> dict[str, object]:
    if len(results) < 3:
        raise RuntimeError("population is UNMEASURED: at least three flat files are required")
    observed = [date.fromisoformat(row.session_date) for row in results]
    if len(set(observed)) != len(observed):
        raise RuntimeError("population is UNMEASURED: duplicate session files")
    if required_session not in observed:
        raise RuntimeError(
            f"population is UNMEASURED: required session {required_session} is missing"
        )
    busiest = max(results, key=lambda row: row.busiest_60s_prints)
    return {
        "verdict": "MEASURED",
        "required_session": required_session.isoformat(),
        "sessions": len(results),
        "session_results": [asdict(row) for row in results],
        "busiest_replay_tape": busiest.replay_tape,
        "busiest_session": busiest.session_date,
        "busiest_60s_prints": busiest.busiest_60s_prints,
        "source_documentation": "https://massive.com/docs/flat-files/stocks/trades",
    }


def population_report(
    flat_files: Sequence[Path], replay_dir: Path, *, required_session: date
) -> dict[str, object]:
    results = tuple(summarize_massive_flat_file(path, replay_dir) for path in flat_files)
    return _population_report_from_results(results, required_session=required_session)


def _decode_stream_data(fields: Mapping[object, object]) -> dict[str, object] | None:
    raw = fields.get("data") or fields.get(b"data")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        return None
    value = json.loads(raw)
    return value if isinstance(value, dict) else None


async def _latest_gateway_heartbeat(redis: Redis) -> HeartbeatEvent:
    rows = await redis.xrevrange(stream_name("mai_tai", "heartbeats"), count=200)
    for _entry_id, fields in rows:
        payload = _decode_stream_data(fields)
        if payload is None:
            continue
        event = HeartbeatEvent.model_validate(payload)
        if event.payload.service_name == "market-data-gateway":
            return event
    raise ReplayAborted("market-data-gateway heartbeat is missing")


async def _latest_subscription_symbols(redis: Redis) -> set[str]:
    rows = await redis.xrevrange(stream_name("mai_tai", "market-data-subscriptions"), count=1)
    if not rows:
        raise ReplayAborted("market-data-subscriptions state is missing")
    payload = _decode_stream_data(rows[0][1])
    if payload is None:
        raise ReplayAborted("latest market-data-subscriptions row has no data")
    event = MarketDataSubscriptionEvent.model_validate(payload)
    return {symbol.upper() for symbol in event.payload.symbols if symbol}


async def assert_replay_health(
    redis: Redis,
    *,
    policy_counter: PolicyLogCounter,
    initial_policy_count: int,
    load_reader: Callable[[], float] = lambda: os.getloadavg()[0],
) -> tuple[HeartbeatEvent, int, float]:
    heartbeat = await _latest_gateway_heartbeat(redis)
    policy_count = policy_counter.read()
    load = float(load_reader())
    reason = replay_abort_reason(
        heartbeat_status=heartbeat.payload.status,
        initial_policy_count=initial_policy_count,
        current_policy_count=policy_count,
        load_average=load,
    )
    if reason:
        raise ReplayAborted(reason)
    return heartbeat, policy_count, load


def _missed_snapshot_cycles(produced_ns: Sequence[int]) -> int:
    missed = 0
    for left, right in zip(produced_ns, produced_ns[1:], strict=False):
        slots = (right - left) // 5_000_000_000
        missed += max(0, int(slots) - 1)
    return missed


async def sample_existing_work(
    redis: Redis,
    *,
    duration_seconds: float,
    output_path: Path,
    policy_counter: PolicyLogCounter,
    initial_policy_count: int,
    clock_ns: Callable[[], int] = clock.time_ns,
    monotonic: Callable[[], float] = clock.monotonic,
) -> ExistingWorkResult:
    heartbeat, _, _ = await assert_replay_health(
        redis,
        policy_counter=policy_counter,
        initial_policy_count=initial_policy_count,
    )
    symbols = await _latest_subscription_symbols(redis)
    heartbeat_count = int(heartbeat.payload.details.get("active_symbols", "-1"))
    if len(symbols) != heartbeat_count:
        raise ReplayAborted(
            "subscription/heartbeat symbol count mismatch: "
            f"subscription={len(symbols)} heartbeat={heartbeat_count}"
        )
    streams = {
        stream_name("mai_tai", "snapshot-batches"): "$",
        stream_name("mai_tai", "market-data"): "$",
    }
    snapshot_ns: list[int] = []
    quote_latency_ms: list[float] = []
    started = monotonic()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as raw_output:
        while monotonic() - started < duration_seconds:
            await assert_replay_health(
                redis,
                policy_counter=policy_counter,
                initial_policy_count=initial_policy_count,
            )
            rows = await redis.xread(streams, count=5_000, block=1_000)
            received_ns = clock_ns()
            for raw_stream, entries in rows or ():
                stream = raw_stream.decode() if isinstance(raw_stream, bytes) else str(raw_stream)
                for raw_entry_id, fields in entries:
                    entry_id = (
                        raw_entry_id.decode()
                        if isinstance(raw_entry_id, bytes)
                        else str(raw_entry_id)
                    )
                    streams[stream] = entry_id
                    payload = _decode_stream_data(fields)
                    if payload is None:
                        continue
                    event_type = payload.get("event_type")
                    sample: dict[str, object] = {
                        "received_ns": received_ns,
                        "stream": stream,
                        "entry_id": entry_id,
                        "event_type": event_type,
                    }
                    if event_type == "snapshot_batch":
                        event = SnapshotBatchEvent.model_validate(payload)
                        produced_ns = int(event.produced_at.timestamp() * 1_000_000_000)
                        snapshot_ns.append(produced_ns)
                        sample["produced_ns"] = produced_ns
                    elif event_type == "quote_tick":
                        event = QuoteTickEvent.model_validate(payload)
                        symbol = event.payload.symbol.upper()
                        if symbol in symbols:
                            produced_ns = int(event.produced_at.timestamp() * 1_000_000_000)
                            latency_ms = (received_ns - produced_ns) / 1_000_000
                            quote_latency_ms.append(latency_ms)
                            sample.update(
                                produced_ns=produced_ns,
                                symbol=symbol,
                                latency_ms=latency_ms,
                            )
                    raw_output.write(json.dumps(sample, sort_keys=True) + "\n")
    await assert_replay_health(
        redis,
        policy_counter=policy_counter,
        initial_policy_count=initial_policy_count,
    )
    cadence_ms = [
        (right - left) / 1_000_000
        for left, right in zip(snapshot_ns, snapshot_ns[1:], strict=False)
    ]
    return ExistingWorkResult(
        duration_seconds=round(monotonic() - started, 3),
        snapshot_count=len(snapshot_ns),
        snapshot_p99_ms=nearest_rank(cadence_ms, 99) if cadence_ms else None,
        missed_snapshot_cycles=_missed_snapshot_cycles(snapshot_ns),
        quote_count=len(quote_latency_ms),
        quote_latency_p99_ms=(nearest_rank(quote_latency_ms, 99) if quote_latency_ms else None),
        active_symbols=len(symbols),
        heartbeat_active_symbols=heartbeat_count,
        quote_latency_status=measured_or_unmeasured(
            len(quote_latency_ms), absent_reason="NO_QUOTE_TICKS"
        ),
        raw_samples=str(output_path),
    )


def _read_replay_tape(path: Path) -> list[tuple[int, dict[str, object]]]:
    opener = gzip.open if path.name.endswith(".gz") else open
    rows: list[tuple[int, dict[str, object]]] = []
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            rows.append((int(payload["source_ns"]), dict(payload["frame"])))
    if not rows:
        raise RuntimeError(f"replay tape is empty: {path}")
    if any(left[0] > right[0] for left, right in zip(rows, rows[1:], strict=False)):
        raise RuntimeError(f"replay tape is not SIP-time ordered: {path}")
    return rows


def _proc_sample(pid: int) -> tuple[int, int]:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    ticks = int(stat[13]) + int(stat[14])
    status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    rss_kib = next(
        int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:")
    )
    return ticks, rss_kib * 1024


async def sample_processes(
    pids: Mapping[str, int], *, duration_seconds: float, output_path: Path
) -> dict[str, dict[str, float | int]]:
    hz = int(os.sysconf("SC_CLK_TCK"))
    started = clock.monotonic()
    previous: dict[str, tuple[float, int]] = {}
    peak_cpu = Counter()
    peak_rss = Counter()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        while clock.monotonic() - started < duration_seconds:
            sampled_at = clock.monotonic()
            for name, pid in pids.items():
                ticks, rss = _proc_sample(pid)
                cpu = 0.0
                if name in previous:
                    prior_at, prior_ticks = previous[name]
                    elapsed = sampled_at - prior_at
                    if elapsed > 0:
                        cpu = (ticks - prior_ticks) / hz / elapsed * 100
                previous[name] = (sampled_at, ticks)
                peak_cpu[name] = max(float(peak_cpu[name]), cpu)
                peak_rss[name] = max(int(peak_rss[name]), rss)
                handle.write(
                    json.dumps(
                        {
                            "sampled_at_ns": clock.time_ns(),
                            "name": name,
                            "pid": pid,
                            "cpu_pct_one_cpu": cpu,
                            "rss_bytes": rss,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
            await asyncio.sleep(1)
    return {
        name: {
            "peak_cpu_pct_one_cpu": float(peak_cpu[name]),
            "peak_rss_bytes": int(peak_rss[name]),
        }
        for name in pids
    }


async def _paced_replay(
    rows: Sequence[tuple[int, dict[str, object]]],
    *,
    speed: float,
    handoff: BoundedPaperHandoff,
    producer_socket: socket.socket,
) -> PacedReplayResult:
    if speed <= 0:
        raise ValueError("speed must be positive")
    origin_source = rows[0][0]
    origin_wall = clock.monotonic_ns()
    producer_done = asyncio.Event()
    writer = asyncio.create_task(
        drain_handoff_to_socket(handoff, producer_socket, producer_done)
    )
    offer_started = clock.monotonic_ns()
    schedule_delays_ms: list[float] = []
    try:
        for source_ns, frame in rows:
            target = origin_wall + int((source_ns - origin_source) / speed)
            while (remaining := target - clock.monotonic_ns()) > 0:
                await asyncio.sleep(min(remaining / 1_000_000_000, 0.01))
            handoff.offer(
                json.dumps(frame, sort_keys=True, separators=(",", ":")),
                received_ns=clock.time_ns(),
            )
            schedule_delays_ms.append(
                max(0.0, (clock.monotonic_ns() - target) / 1_000_000)
            )
    finally:
        producer_done.set()
    writer_counters = await writer
    offer_elapsed_ms = (clock.monotonic_ns() - offer_started) / 1_000_000
    return PacedReplayResult(
        writer=writer_counters,
        offer_elapsed_ms=offer_elapsed_ms,
        offer_schedule_delay_p99_ms=nearest_rank(schedule_delays_ms, 99),
    )


async def _gather_fail_fast(*awaitables: Awaitable[object]) -> tuple[object, ...]:
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    failure = next(
        (
            task.exception()
            for task in done
            if not task.cancelled() and task.exception() is not None
        ),
        None,
    )
    if failure is not None:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise failure
    return tuple(await asyncio.gather(*tasks))


def _within_baseline(baseline: float | None, replay: float | None) -> bool | None:
    if baseline is None or replay is None:
        return None
    return replay <= baseline + max(baseline * 0.10, 50.0)


def evaluate_replay(
    *,
    speed: float,
    consumer: str,
    handoff_p99_ms: float | None,
    producer_cpu_peak_pct_one_cpu: float,
    baseline_snapshot_p99_ms: float | None,
    replay_snapshot_p99_ms: float | None,
    baseline_quote_p99_ms: float | None,
    replay_quote_p99_ms: float | None,
    missed_snapshot_cycles: int,
    socket_would_block_drops: int,
    producer_offer_elapsed_ms: float,
    active_offer_elapsed_ms: float | None = None,
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    offer_unmeasured = False
    if speed == 3 and handoff_p99_ms is not None and handoff_p99_ms >= 250:
        reasons.append("3x p99 hand-off lag is not below 250 ms")
    if speed == 3 and producer_cpu_peak_pct_one_cpu >= 50:
        reasons.append("3x replay uses at least 50% of one CPU")
    snapshot_ok = _within_baseline(baseline_snapshot_p99_ms, replay_snapshot_p99_ms)
    quote_ok = _within_baseline(baseline_quote_p99_ms, replay_quote_p99_ms)
    if missed_snapshot_cycles:
        reasons.append("replay missed one or more five-second snapshot cycles")
    if snapshot_ok is False:
        reasons.append("snapshot cadence exceeded its baseline allowance")
    if quote_ok is False:
        reasons.append("quote latency exceeded its baseline allowance")
    if consumer == "dead":
        if not socket_would_block_drops:
            reasons.append("dead consumer did not exercise socket would-block drops")
        offer_ok = _within_baseline(active_offer_elapsed_ms, producer_offer_elapsed_ms)
        if offer_ok is False:
            reasons.append("dead consumer slowed the producer offer loop")
        if offer_ok is None:
            offer_unmeasured = True
    unmeasured = snapshot_ok is None or quote_ok is None or offer_unmeasured
    verdict = "FAIL" if reasons else "UNMEASURED" if unmeasured else "PASS"
    return verdict, tuple(reasons)


async def run_replay_once(
    *,
    redis: Redis,
    tape_path: Path,
    output_dir: Path,
    policy_counter: PolicyLogCounter,
    gateway_pid: int,
    speed: float,
    consumer: str,
    baseline_seconds: float,
    active_offer_elapsed_ms: float | None = None,
    access_checker: Callable[[], ReplayAccessEvidence] = require_replay_access,
) -> ReplayResult:
    require_replay_niceness()
    rows = _read_replay_tape(tape_path)
    policy_start = policy_counter.read()
    label = f"{speed:g}x-{consumer}"
    baseline = await sample_existing_work(
        redis,
        duration_seconds=baseline_seconds,
        output_path=output_dir / f"{label}-baseline-streams.jsonl",
        policy_counter=policy_counter,
        initial_policy_count=policy_start,
    )
    replay_access = access_checker()
    span_seconds = max(0.1, (rows[-1][0] - rows[0][0]) / 1_000_000_000 / speed + 0.1)
    handoff = BoundedPaperHandoff(capacity=10_000)
    cpu_path = output_dir / f"{label}-cpu.jsonl"
    consumer_samples = output_dir / f"{label}-consumer.jsonl"
    consumer_process = CrossProcessPaperConsumer(
        mode=consumer,
        raw_samples_path=consumer_samples,
    )
    producer_socket: socket.socket | None = None
    consumer_result = None
    try:
        consumer_pid = await asyncio.to_thread(consumer_process.start)
        producer_socket = connect_consumer_socket(consumer_process.socket_path)
        replay_result, during, cpu = await _gather_fail_fast(
            _paced_replay(
                rows,
                speed=speed,
                handoff=handoff,
                producer_socket=producer_socket,
            ),
            sample_existing_work(
                redis,
                duration_seconds=span_seconds,
                output_path=output_dir / f"{label}-replay-streams.jsonl",
                policy_counter=policy_counter,
                initial_policy_count=policy_start,
            ),
            sample_processes(
                {
                    "producer": os.getpid(),
                    "consumer": consumer_pid,
                    "gateway": gateway_pid,
                },
                duration_seconds=span_seconds,
                output_path=cpu_path,
            ),
        )
        consumer_result = await asyncio.to_thread(consumer_process.stop)
    finally:
        if producer_socket is not None:
            producer_socket.close()
        consumer_process.close()
    writer_counters = replay_result.writer
    offer_elapsed_ms = replay_result.offer_elapsed_ms
    lags = consumer_result.handoff_lags_ms
    counters = handoff.counters
    handoff_p99_ms = nearest_rank(lags, 99) if lags else None
    producer_cpu_peak = float(cpu["producer"]["peak_cpu_pct_one_cpu"])
    verdict, reasons = evaluate_replay(
        speed=speed,
        consumer=consumer,
        handoff_p99_ms=handoff_p99_ms,
        producer_cpu_peak_pct_one_cpu=producer_cpu_peak,
        baseline_snapshot_p99_ms=baseline.snapshot_p99_ms,
        replay_snapshot_p99_ms=during.snapshot_p99_ms,
        baseline_quote_p99_ms=baseline.quote_latency_p99_ms,
        replay_quote_p99_ms=during.quote_latency_p99_ms,
        missed_snapshot_cycles=during.missed_snapshot_cycles,
        socket_would_block_drops=writer_counters.would_block_drops,
        producer_offer_elapsed_ms=offer_elapsed_ms,
        producer_offer_schedule_delay_p99_ms=(
            replay_result.offer_schedule_delay_p99_ms
        ),
        active_offer_elapsed_ms=active_offer_elapsed_ms,
    )
    return ReplayResult(
        label=label,
        speed=speed,
        consumer=consumer,
        input_frames=counters.input_frames,
        forwarded_frames=counters.forwarded_frames,
        queue_dropped_frames=counters.dropped_frames,
        socket_would_block_drops=writer_counters.would_block_drops,
        parse_failures=counters.parse_failures,
        producer_pid=consumer_result.producer_pid,
        consumer_pid=consumer_result.consumer_pid,
        consumed_frames=consumer_result.consumed_frames,
        handoff_p50_ms=nearest_rank(lags, 50) if lags else None,
        handoff_p95_ms=nearest_rank(lags, 95) if lags else None,
        handoff_p99_ms=handoff_p99_ms,
        handoff_max_ms=max(lags) if lags else None,
        producer_offer_elapsed_ms=offer_elapsed_ms,
        producer_cpu_peak_pct_one_cpu=producer_cpu_peak,
        producer_peak_rss_bytes=int(cpu["producer"]["peak_rss_bytes"]),
        consumer_cpu_peak_pct_one_cpu=float(cpu["consumer"]["peak_cpu_pct_one_cpu"]),
        consumer_peak_rss_bytes=int(cpu["consumer"]["peak_rss_bytes"]),
        gateway_cpu_peak_pct_one_cpu=float(cpu["gateway"]["peak_cpu_pct_one_cpu"]),
        replay_access=replay_access,
        baseline=baseline,
        during=during,
        verdict=verdict,
        reasons=reasons,
        cpu_samples=str(cpu_path),
        consumer_samples=consumer_result.raw_samples,
    )


async def quote_precheck(
    redis: Redis,
    *,
    duration_seconds: float,
    output_path: Path,
    policy_counter: PolicyLogCounter,
) -> ExistingWorkResult:
    policy_start = policy_counter.read()
    return await sample_existing_work(
        redis,
        duration_seconds=duration_seconds,
        output_path=output_path,
        policy_counter=policy_counter,
        initial_policy_count=policy_start,
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def render_population_markdown(report: Mapping[str, object]) -> str:
    rows = [
        "# Momentum gateway population",
        "",
        f"Verdict: **{report['verdict']}**",
        "",
        "| Session | Prints | Seconds observed | Peak 1 s | p99 1 s | Busiest 60 s |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for raw in report["session_results"]:
        session = dict(raw)
        rows.append(
            f"| {session['session_date']} | {session['total_prints']} | "
            f"{session['seconds_observed']}/{session['seconds_in_window']} | "
            f"{session['max_prints_1s']} | {session['p99_prints_1s']} | "
            f"{session['busiest_60s_prints']} |"
        )
    rows.extend(
        [
            "",
            "Source: Massive `us_stocks_sip/trades_v1` daily SIP flat files.",
            "",
            "Blind spots: next-day availability; UTC epoch timestamps are converted explicitly "
            "to ET; prices are unadjusted; no websocket packet batching or host-arrival jitter; "
            "no visibility into upstream omissions or later corrections.",
            "",
        ]
    )
    return "\n".join(rows)


def render_replay_markdown(report: Mapping[str, object]) -> str:
    rows = [
        "# Momentum gateway replay",
        "",
        f"Verdict: **{report['verdict']}**",
        "",
        f"Quote precheck: **{report['quote_latency_row']}**",
        "",
        "| Replay | Input | Forwarded | Queue drops | Socket drops | p99 hand-off ms | "
        "Producer CPU | Snapshot row | Quote row | Verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for raw in report["replays"]:
        replay = dict(raw)
        during = dict(replay["during"])
        rows.append(
            f"| {replay['label']} | {replay['input_frames']} | "
            f"{replay['forwarded_frames']} | {replay['queue_dropped_frames']} | "
            f"{replay['socket_would_block_drops']} | {replay['handoff_p99_ms']} | "
            f"{replay['producer_cpu_peak_pct_one_cpu']:.2f}% | "
            f"{during['snapshot_count']} samples/{during['missed_snapshot_cycles']} missed | "
            f"{during['quote_latency_status']} ({during['quote_count']}) | "
            f"{replay['verdict']} |"
        )
    rows.append("")
    return "\n".join(rows)


def _population_command(args: argparse.Namespace) -> int:
    report = population_report(
        args.flat_file,
        args.replay_dir,
        required_session=date.fromisoformat(args.required_session),
    )
    _write_json(args.json, report)
    _write_text(args.markdown, render_population_markdown(report))
    print(json.dumps(report, sort_keys=True))
    return 0


def _flat_file_key(day: date) -> str:
    return (
        f"us_stocks_sip/trades_v1/{day.year:04d}/{day.month:02d}/"
        f"{day.isoformat()}.csv.gz"
    )


def _fetch_population_command(args: argparse.Namespace) -> int:
    try:
        # Flat-file population capture is offline REST/S3 work; the live replay keeps
        # its separate flat-book and time-window gates below in _suite_command_async.
        require_replay_niceness()
        sessions = tuple(date.fromisoformat(value) for value in args.session)
        if len(sessions) < 3:
            raise RuntimeError("population is UNMEASURED: at least three sessions are required")
        if len(set(sessions)) != len(sessions):
            raise RuntimeError("population is UNMEASURED: duplicate sessions requested")
        required_session = date.fromisoformat(args.required_session)
        if required_session not in sessions:
            raise RuntimeError(
                f"population is UNMEASURED: required session {required_session} is missing"
            )
        client = MassiveFlatFileClient.from_environment()
        objects = tuple(client.head(_flat_file_key(day)) for day in sessions)
        args.download_dir.mkdir(parents=True, exist_ok=True)
        required_bytes = sum(row.size_bytes for row in objects) + args.free_space_reserve_bytes
        free_bytes = shutil.disk_usage(args.download_dir).free
        if free_bytes < required_bytes:
            raise RuntimeError(
                "population is UNMEASURED: insufficient free disk "
                f"({free_bytes} available, {required_bytes} required including reserve)"
            )

        results: list[PopulationResult] = []
        for day, object_ in zip(sessions, objects, strict=True):
            raw_path = args.download_dir / f"{day.isoformat()}.csv.gz"
            client.download(object_, raw_path)
            result = summarize_massive_flat_file(raw_path, args.replay_dir)
            results.append(result)
            _write_json(
                args.replay_dir / f"momentum-gateway-population-{day.isoformat()}.json",
                asdict(result),
            )
            raw_path.unlink()
        report = _population_report_from_results(
            results,
            required_session=required_session,
        )
    except RuntimeError as exc:
        report = {"verdict": "UNMEASURED", "abort_reason": str(exc)}
        _write_json(args.json, report)
        _write_text(args.markdown, f"# Momentum gateway population\n\n{exc}\n")
        print(json.dumps(report, sort_keys=True))
        return verdict_exit_code("UNMEASURED")
    _write_json(args.json, report)
    _write_text(args.markdown, render_population_markdown(report))
    print(json.dumps(report, sort_keys=True))
    return 0


async def _suite_command_async(args: argparse.Namespace) -> int:
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    redis = Redis.from_url(args.redis_url, decode_responses=True)
    policy_counter = PolicyLogCounter(args.policy_log)
    try:
        initial_access = require_replay_access()
        precheck = await quote_precheck(
            redis,
            duration_seconds=args.quote_precheck_seconds,
            output_path=output_dir / "quote-precheck-streams.jsonl",
            policy_counter=policy_counter,
        )
        active_3x_offer_elapsed_ms: float | None = None

        async def replay(spec: tuple[float, str]) -> ReplayResult:
            nonlocal active_3x_offer_elapsed_ms
            speed, consumer = spec
            row = await run_replay_once(
                redis=redis,
                tape_path=args.tape,
                output_dir=output_dir,
                policy_counter=policy_counter,
                gateway_pid=args.gateway_pid,
                speed=speed,
                consumer=consumer,
                baseline_seconds=args.baseline_seconds,
                active_offer_elapsed_ms=(
                    active_3x_offer_elapsed_ms if consumer == "dead" else None
                ),
            )
            if speed == 3 and consumer == "active":
                active_3x_offer_elapsed_ms = row.producer_offer_elapsed_ms
            return row

        replay_access_checks, replay_rows = await run_guarded_replays(
            ((1.0, "active"), (3.0, "active"), (3.0, "dead")),
            access_checker=require_replay_access,
            replay_runner=replay,
        )
        access_checks = (initial_access, *replay_access_checks)
        rows = list(replay_rows)
    finally:
        await redis.aclose()
    report = {
        "access_checks": [asdict(row) for row in access_checks],
        "quote_precheck": asdict(precheck),
        "quote_latency_row": measured_or_unmeasured(
            precheck.quote_count, absent_reason="NO_QUOTE_TICKS"
        ),
        "replays": [asdict(row) for row in rows],
        "verdict": (
            "FAIL"
            if any(row.verdict == "FAIL" for row in rows)
            else "UNMEASURED"
            if precheck.quote_count == 0 or any(row.verdict == "UNMEASURED" for row in rows)
            else "PASS"
        ),
    }
    _write_json(output_dir / "replay-report.json", report)
    _write_text(output_dir / "replay-report.md", render_replay_markdown(report))
    print(json.dumps(report, sort_keys=True))
    return verdict_exit_code(str(report["verdict"]))


def _suite_command(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_suite_command_async(args))
    except ReplayAborted as exc:
        report = {"verdict": "UNMEASURED", "abort_reason": str(exc)}
        _write_json(args.output_dir / "replay-aborted.json", report)
        print(json.dumps(report, sort_keys=True))
        return verdict_exit_code("UNMEASURED")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(required=True)
    population = subparsers.add_parser("population")
    population.add_argument("--flat-file", action="append", type=Path, required=True)
    population.add_argument("--required-session", default="2026-09-17")
    population.add_argument("--replay-dir", type=Path, required=True)
    population.add_argument("--json", type=Path, required=True)
    population.add_argument("--markdown", type=Path, required=True)
    population.set_defaults(run=_population_command)

    fetch_population = subparsers.add_parser("fetch-population")
    fetch_population.add_argument("--session", action="append", required=True)
    fetch_population.add_argument("--required-session", default="2026-09-17")
    fetch_population.add_argument("--download-dir", type=Path, required=True)
    fetch_population.add_argument("--replay-dir", type=Path, required=True)
    fetch_population.add_argument("--json", type=Path, required=True)
    fetch_population.add_argument("--markdown", type=Path, required=True)
    fetch_population.add_argument(
        "--free-space-reserve-bytes",
        type=int,
        default=_DEFAULT_FREE_SPACE_RESERVE_BYTES,
    )
    fetch_population.set_defaults(run=_fetch_population_command)

    suite = subparsers.add_parser("replay-suite")
    suite.add_argument("--tape", type=Path, required=True)
    suite.add_argument("--output-dir", type=Path, required=True)
    suite.add_argument("--redis-url", required=True)
    suite.add_argument("--gateway-pid", type=int, required=True)
    suite.add_argument(
        "--policy-log", type=Path, default=Path("/var/log/project-mai-tai/market-data.log")
    )
    suite.add_argument("--baseline-seconds", type=float, default=600)
    suite.add_argument("--quote-precheck-seconds", type=float, default=120)
    suite.set_defaults(run=_suite_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
