"""Read-only G3 source adapter; no provider calls, writes or process controls."""

from __future__ import annotations

from datetime import datetime
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat
import time
from zoneinfo import ZoneInfo

from project_mai_tai.momwarn_health_contract import (
    OmsLogMonitor, proposed_health_v2, sourced_first_due_ns,
)
from project_mai_tai.momwarn_measurement_probes import read_bounded
from project_mai_tai.momwarn_log_cursor import check_read_coverage, cursor_anchor, cursor_rewritten
from project_mai_tai.momwarn_measurements import NS, integer
from project_mai_tai.momwarn_cohort_lateness import CohortTracker, POLICY

BOUND = 65536
MAX_STATE_ROWS = 32
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) ")
PROBE = re.compile(r"\[V2-MACD-PROBE\] sym=(\S+) ts_ms=(\d+) ")
LINE_PUBLICATION = re.compile(
    r"\[V2-LINE-RESTORE\] sym=(\S+) [^\n]*\bcurrent_bar_ms=(\d+)\b"
    r"[^\n]*\boutcome=published(?:\s|$)"
)
SEED = re.compile(r"\[(V2-DB-SEED-GAP|V2-CW-SEED-CAP|V2-WARMUP-LINE)\] (\S+)")


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
    measurement. Every sample actually reads bounded OMS/probe/proc sources.
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
        self.truncations = 0
        self.anchor = None
        policy = config.get("v2_lateness_policy")
        if policy is not None and policy != POLICY:
            raise ValueError("unreviewed v2 lateness policy")
        self.cohort = CohortTracker() if policy == POLICY else None

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
            rewritten = self.offset is not None and cursor_rewritten(fd, offset, self.anchor)
            partial = self.partial
            if rewritten:
                offset, partial = 0, b""
            if info.st_size - offset > BOUND:
                raise ValueError("v2 log truncation/backlog")
            os.lseek(fd, offset, os.SEEK_SET)
            raw = os.read(fd, BOUND + 1)
            covered = offset + len(raw)
            if len(raw) > BOUND:
                raise ValueError("v2 log EOF coverage exceeded")
            check_read_coverage(fd, offset, covered, BOUND, "v2")
            if self.offset is None and offset:
                raw = raw.partition(b"\n")[2]
            lines = (partial + raw).split(b"\n")
            observed_after_read = self.wall_ns()
            covered_from = self.covered_from_ns
            bars = dict(self.bars)
            cohort = deepcopy(self.cohort)
            for line in lines[:-1]:
                text = line.decode()
                if not STAMP.match(text):
                    continue
                stamp = self._log_stamp(text)
                if stamp > observed_after_read:
                    raise ValueError("future v2 log evidence")
                if stamp < self.config["v2_started_ns"]:
                    continue
                if covered_from is None:
                    covered_from = stamp
                # The first restored bar can publish without a MACD diagnostic.
                # Only successful publication of that exact minute is evidence.
                match = PROBE.search(text) or LINE_PUBLICATION.search(text)
                if match:
                    key = match[1], int(match[2])
                    if cohort is not None and key[1] % 60000:
                        raise ValueError("CAL probe bucket is malformed")
                    bars.setdefault(key, stamp)
                seed = SEED.search(text)
                if seed and cohort is not None:
                    cohort.observe_seed(seed[2], stamp, seed[1])
            if rewritten:
                # Lost bytes cannot prove the past interval. Future due minutes can
                # use the new contiguous EOF baseline, not a lifetime gap taint.
                covered_from = observed_after_read
            if len(lines[-1]) > BOUND:
                raise ValueError("v2 partial line bound")
            cutoff = now_ns // 1_000_000 - 120_000
            bars = {key: stamp for key, stamp in bars.items() if key[1] >= cutoff}
            if len(bars) > 1024:
                raise ValueError("v2 probe map bound")
            anchor = cursor_anchor(fd, covered)
            self.identity, self.offset, self.partial = identity, covered, lines[-1]
            self.covered_from_ns, self.bars, self.anchor = covered_from, bars, anchor
            self.cohort = cohort
            self.truncations += int(rewritten)
        finally:
            os.close(fd)

    def read(self, state: dict, *, startup: dict | None = None) -> dict:
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
        if self.cohort is not None:
            self.cohort.observe_membership(watchlist, now)
        receipt["observed_wall_ns"] = now
        receipt.update(v2_expectation_ref=self.config["v2_expectation_ref"])
        if not watchlist:
            if payload.get("restoration_complete") is not True:
                raise ValueError("empty startup is not a restored empty watchlist")
            if startup is not None and not startup["empty_seen"]:
                startup.update(baseline_ns=now, activation_ns=None, ready=None, empty_seen=True)
            receipt.update(v2_applicability="NOT_APPLICABLE",
                           v2_applicability_reason="sourced_empty_watchlist",
                           v2_watchlist=[], v2_watchlist_source="strategy-state-isolated",
                           v2_watchlist_complete=True, v2_watchlist_snapshot_ns=state_ns)
        else:
            first_due = sourced_first_due_ns(self.config, observed_ns=now)
            if now < first_due:
                receipt.update(v2_applicability="NOT_APPLICABLE",
                               v2_applicability_reason="sourced_before_first_due",
                               v2_first_due_wall_ns=first_due,
                               **{key: self.config[key] for key in (
                                   "v2_expectation_kind", "v2_expected_start_et", "v2_source_lane",
                                   "v2_first_bar_start_et", "v2_bar_interval_seconds",
                                   "v2_first_bar_source_ref")})
            else:
                probes = self.config["v2_probe_symbols"]
                if "*" not in probes and not set(watchlist) <= set(probes):
                    raise ValueError("probe setting does not cover complete current watchlist")
                due = now // (60 * NS) * (60 * NS)
                bar_ms = due // 1_000_000 - 60_000
                completed = [self.bars.get((symbol, bar_ms)) for symbol in watchlist]
                completion = max(completed) if all(item is not None for item in completed) else None
                if startup is not None:
                    startup["empty_seen"] = False
                    ready = startup.get("ready")
                    if ready is not None and ready["v2_instance_id"] != self.config["v2_instance_id"]:
                        raise ValueError("v2 startup readiness belongs to another source instance")
                    if ready is None:
                        started = startup["started_ns"]
                        baseline = startup["baseline_ns"]
                        if startup["activation_ns"] is None:
                            startup["activation_ns"] = now
                        activation = startup["activation_ns"]
                        startup_due = max(first_due, (activation // (60 * NS) + 1) * (60 * NS))
                        deadline = startup_due + 5 * NS
                        if (completion is not None and now <= deadline
                                and all(baseline < item and due <= item <= due + 5 * NS
                                        for item in completed)):
                            startup["ready"] = dict(v2_instance_id=self.config["v2_instance_id"],
                                                    due_ns=due, completed_ns=completion)
                        else:
                            # Pending is missing readiness, never healthy/zero lateness.
                            receipt.update(
                                v2_applicability="STARTUP_PENDING",
                                v2_startup_gateway_instance_id=startup["gateway_instance_id"],
                                v2_startup_started_ns=started, v2_startup_first_due_ns=startup_due,
                                v2_startup_baseline_ns=baseline, v2_startup_activation_ns=activation,
                                v2_startup_deadline_ns=deadline,
                                v2_instance_id=self.config["v2_instance_id"],
                                v2_watchlist=watchlist, v2_watchlist_source="strategy-state-isolated",
                                v2_watchlist_complete=True, v2_watchlist_snapshot_ns=state_ns,
                                **{key: self.config[key] for key in (
                                    "v2_expectation_kind", "v2_expected_start_et", "v2_source_lane",
                                    "v2_first_bar_start_et", "v2_bar_interval_seconds",
                                    "v2_first_bar_source_ref")},
                            )
                            return proposed_health_v2(receipt, now_ns=now)
                if self.truncations:
                    if completion is None and (self.covered_from_ns is None or self.covered_from_ns >= due):
                        raise ValueError("v2 current-due completion unknown after copytruncate gap")
                elif self.covered_from_ns is None or self.covered_from_ns > due:
                    raise ValueError("v2 log baseline does not cover current due minute")
                if completion is not None and completion < due:
                    raise ValueError("probe observed unclosed/future bar")
                if self.cohort is not None:
                    if any(minute >= bar_ms and (minute + 60000)*1_000_000 > stamp
                           for (_, minute), stamp in self.bars.items()):
                        raise ValueError("CAL current/future receipt precedes bar close")
                    observed = self.cohort.observation(due, now,
                        {s:t for (s, minute), t in self.bars.items() if minute == bar_ms},
                        source="strategy-state-isolated")
                    receipt.update(
                        v2_applicability="CALIBRATED_COHORT", v2_source="first_completed_bar_cohort_monitor",
                        v2_instance_id=self.config["v2_instance_id"],
                        v2_watchlist=watchlist, v2_watchlist_source="strategy-state-isolated",
                        v2_watchlist_snapshot_ns=state_ns, v2_watchlist_complete=True,
                        v2_cohort=observed.payload())
                    return proposed_health_v2(receipt, now_ns=now)
                receipt.update(
                    v2_applicability="APPLICABLE", v2_source="completed_bar_due_monitor",
                    v2_instance_id=self.config["v2_instance_id"], v2_coverage="complete_current_due",
                    v2_measured_wall_ns=now, v2_due_wall_ns=due, v2_completed_wall_ns=completion,
                    v2_bar_lateness_seconds=((now if completion is None else completion) - due) / NS,
                    v2_probe_observation="strategy_evaluation_or_line_publication_not_broker_ACK",
                )
        return proposed_health_v2(receipt, now_ns=now)
