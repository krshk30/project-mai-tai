"""Read-only G3 source adapter; no provider calls, writes or process controls."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import time
from zoneinfo import ZoneInfo

from project_mai_tai.momwarn_health_contract import OmsLogMonitor, proposed_health_v2
from project_mai_tai.momwarn_measurement_probes import read_bounded
from project_mai_tai.momwarn_measurements import NS, integer

BOUND = 65536
MAX_STATE_ROWS = 32
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) ")
PROBE = re.compile(r"\[V2-MACD-PROBE\] sym=(\S+) ts_ms=(\d+) ")


def newest_v2_state(entries: list) -> dict:
    if not isinstance(entries, list) or len(entries) > MAX_STATE_ROWS:
        raise ValueError("v2 state read bound")
    total = 0
    for _, fields in entries:
        raw = fields.get("data", fields.get(b"data"))
        if not isinstance(raw, (str, bytes)):
            raise ValueError("v2 stream data unreadable")
        total += len(raw.encode() if isinstance(raw, str) else raw)
        if len(raw) > BOUND or total > 4 * BOUND:
            raise ValueError("v2 state byte bound")
        event = json.loads(raw)
        if event.get("source_service") == "schwab-1m-v2":
            return event
    raise ValueError("no explicit v2 source in bounded newest-first state read")


class LiveWarningHealthSource:
    """Explicit reviewed configuration binds actual source instances and expectations.

    Configuration is setup provenance, never an externally restamped health
    measurement. Every sample actually reads OMS/probe/proc sources at EOF.
    """

    def __init__(self, config: dict, log_directory: Path, *, proc: Path = Path("/proc"),
                 wall_ns=time.time_ns):
        required = ("oms_instance_id", "v2_instance_id", "oms_cadence_source",
                    "source_timezone_ref", "v2_probe_source_ref", "v2_expectation_ref")
        if config.get("source_config_version") != 1 or any(
            not isinstance(config.get(key), str) or not config[key] for key in required
        ):
            raise ValueError("reviewed source setup/provenance required")
        for key in ("oms_pid", "v2_pid", "oms_started_ns", "v2_started_ns"):
            if not integer(config.get(key)) or config[key] == 0:
                raise ValueError("explicit source PID/start required")
        probes = config.get("v2_probe_symbols")
        if not isinstance(probes, list) or not probes or any(
            not isinstance(symbol, str) or not symbol for symbol in probes
        ):
            raise ValueError("explicit live probe setting required")
        if len(probes) > 256 or len(set(probes)) != len(probes):
            raise ValueError("probe bound/duplicates")
        self.config, self.proc, self.wall_ns = dict(config), proc, wall_ns
        self.zone = ZoneInfo(config["verified_timezone"])
        self.log = log_directory / "schwab-1m-v2.log"
        self.oms = OmsLogMonitor(
            log_directory / "oms.log", config["oms_pid"],
            verified_timezone=config["verified_timezone"],
            cadence_seconds=config["oms_sync_interval_seconds"],
            cadence_source=config["oms_cadence_source"], wall_ns=wall_ns, proc=proc,
        )
        self.identity = self.offset = None
        self.partial = b""
        self.covered_from_ns = None
        self.bars: dict[tuple[str, int], int] = {}

    def _check_v2_process(self) -> None:
        pid = self.config["v2_pid"]
        raw = read_bounded(self.proc / str(pid) / "stat").decode()
        fields = raw.rsplit(")", 1)[1].split()
        instance = f"{int(raw.split('(', 1)[0])}:{int(fields[19])}"
        if fields[0] in {"Z", "X"} or instance != self.config["v2_instance_id"]:
            raise ValueError("v2 process changed/dead")

    def _log_stamp(self, line: str) -> int:
        match = STAMP.match(line)
        if not match:
            raise ValueError("v2 log timestamp unknown")
        stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=self.zone)
        return int(stamp.timestamp()) * NS + int(match[2]) * 1_000_000

    def _read_probes(self, now_ns: int) -> None:
        fd = os.open(self.log, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            identity = info.st_dev, info.st_ino
            if not stat.S_ISREG(info.st_mode) or self.identity not in (None, identity):
                raise ValueError("v2 log unreadable/rotated")
            offset = max(0, info.st_size - BOUND) if self.offset is None else self.offset
            if info.st_size < offset or info.st_size - offset > BOUND:
                raise ValueError("v2 log truncation/backlog")
            os.lseek(fd, offset, os.SEEK_SET)
            raw = os.read(fd, BOUND + 1)
            covered = offset + len(raw)
            if len(raw) > BOUND or os.fstat(fd).st_size > covered:
                raise ValueError("v2 log EOF coverage exceeded")
            if self.offset is None and offset:
                raw = raw.partition(b"\n")[2]
            lines = (self.partial + raw).split(b"\n")
            observed_after_read = self.wall_ns()
            for line in lines[:-1]:
                text = line.decode()
                if not STAMP.match(text):
                    continue
                stamp = self._log_stamp(text)
                if stamp > observed_after_read:
                    raise ValueError("future v2 log evidence")
                if stamp < self.config["v2_started_ns"]:
                    continue
                if self.covered_from_ns is None:
                    self.covered_from_ns = stamp
                match = PROBE.search(text)
                if match:
                    key = match[1], int(match[2])
                    self.bars.setdefault(key, stamp)
            self.identity, self.offset, self.partial = identity, covered, lines[-1]
            if len(self.partial) > BOUND:
                raise ValueError("v2 partial line bound")
            cutoff = now_ns // 1_000_000 - 120_000
            self.bars = {key: stamp for key, stamp in self.bars.items() if key[1] >= cutoff}
            if len(self.bars) > 1024:
                raise ValueError("v2 probe map bound")
        finally:
            os.close(fd)

    def read(self, state: dict) -> dict:
        now = self.wall_ns()
        self._check_v2_process()
        receipt = self.oms.read()
        if receipt["oms_instance_id"] != self.config["oms_instance_id"]:
            raise ValueError("OMS process does not match reviewed setup")
        if receipt["oms_measured_wall_ns"] < self.config["oms_started_ns"]:
            raise ValueError("OMS end belongs to a preceding process")
        if receipt.get("oms_outcome") != "ok":
            raise ValueError("OMS completion did not successfully synchronize")
        self._read_probes(now)
        now = self.wall_ns()
        if state.get("source_service") != "schwab-1m-v2":
            raise ValueError("wrong v2 state producer")
        stamp = datetime.fromisoformat(state["produced_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("v2 state timezone unknown")
        state_ns = int(stamp.timestamp()) * NS + stamp.microsecond * 1000
        payload = state["payload"]
        watchlist = payload.get("watchlist")
        if (not 0 <= now - state_ns <= 10 * NS or state_ns < self.config["v2_started_ns"]
                or not isinstance(watchlist, list) or len(watchlist) > 256
                or any(not isinstance(symbol, str) or not symbol for symbol in watchlist)
                or len(set(watchlist)) != len(watchlist)):
            raise ValueError("v2 watchlist completeness/freshness unknown")
        receipt["observed_wall_ns"] = now
        receipt.update(v2_expectation_ref=self.config["v2_expectation_ref"])
        if not watchlist:
            if payload.get("restoration_complete") is not True:
                raise ValueError("empty startup is not a restored empty watchlist")
            receipt.update(v2_applicability="NOT_APPLICABLE",
                           v2_applicability_reason="sourced_empty_watchlist",
                           v2_watchlist=[], v2_watchlist_source="strategy-state-isolated",
                           v2_watchlist_complete=True, v2_watchlist_snapshot_ns=state_ns)
        else:
            if (self.config.get("v2_expectation_kind") != "recorded_first_same_day_0700"
                    or self.config.get("v2_expected_start_et") != "07:00"
                    or self.config.get("v2_source_lane") not in {
                        "schwab_anchored_current_day", "schwab_pricehistory_same_day"}):
                raise ValueError("no validated complete watched-bar expectation")
            local = datetime.fromtimestamp(now / NS, ZoneInfo("America/New_York"))
            if local.hour < 7:
                receipt.update(v2_applicability="NOT_APPLICABLE",
                               v2_applicability_reason="sourced_pre0700",
                               **{key: self.config[key] for key in (
                                   "v2_expectation_kind", "v2_expected_start_et", "v2_source_lane")})
            else:
                probes = self.config["v2_probe_symbols"]
                if "*" not in probes and not set(watchlist) <= set(probes):
                    raise ValueError("probe setting does not cover complete current watchlist")
                due = now // (60 * NS) * (60 * NS)
                bar_ms = due // 1_000_000 - 60_000
                if self.covered_from_ns is None or self.covered_from_ns > due:
                    raise ValueError("v2 log baseline does not cover current due minute")
                completed = [self.bars.get((symbol, bar_ms)) for symbol in watchlist]
                completion = max(completed) if all(item is not None for item in completed) else None
                if completion is not None and completion < due:
                    raise ValueError("probe observed unclosed/future bar")
                receipt.update(
                    v2_applicability="APPLICABLE", v2_source="completed_bar_due_monitor",
                    v2_instance_id=self.config["v2_instance_id"], v2_coverage="complete_current_due",
                    v2_measured_wall_ns=now, v2_due_wall_ns=due, v2_completed_wall_ns=completion,
                    v2_bar_lateness_seconds=((now if completion is None else completion) - due) / NS,
                    v2_probe_observation="strategy_evaluation_not_broker_ACK",
                )
        return proposed_health_v2(receipt, now_ns=now)
