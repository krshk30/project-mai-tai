"""Health v2 source candidate; local G1 wiring requires review, not installation."""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from project_mai_tai.momwarn_health import parse_oms_end
from project_mai_tai.momwarn_log_cursor import cursor_anchor, cursor_rewritten
from project_mai_tai.momwarn_measurement_probes import BOUND, read_bounded
from project_mai_tai.momwarn_measurements import NS, integer, nonnegative


def sourced_first_due_ns(config: dict, *, observed_ns: int) -> int:
    """Only reviewed same-day start/interval evidence defines first closed due."""
    if (
        not integer(observed_ns)
        or config.get("v2_source_lane") != "schwab_pricehistory_same_day"
        or config.get("v2_expectation_kind") != "recorded_first_same_day_0700"
        or config.get("v2_expected_start_et") != "07:00"
        or config.get("v2_first_bar_start_et") != "07:00"
        or type(config.get("v2_bar_interval_seconds")) is not int
        or config["v2_bar_interval_seconds"] != 60
        or any(not isinstance(config.get(key), str) or not config[key]
               for key in ("v2_expectation_ref", "v2_first_bar_source_ref"))
    ):
        raise ValueError("UNKNOWN:reviewed first-bar start/interval/source expectation")
    local = datetime.fromtimestamp(observed_ns // NS, ZoneInfo("America/New_York"))
    if local.weekday() >= 5 or local.hour < 4:
        raise ValueError("UNKNOWN:first-due source outside PM trading day")
    start = local.replace(hour=7, minute=0, second=0, microsecond=0)
    return int(start.timestamp()) * NS + config["v2_bar_interval_seconds"] * NS


def proposed_health_v2(receipt: dict, *, now_ns: int) -> dict:
    """Preserve completion time; current monitor coverage, not reread time, must be fresh."""
    monitor, end = receipt.get("observed_wall_ns"), receipt.get("oms_measured_wall_ns")
    cadence = receipt.get("oms_sync_interval_seconds")
    coverage = receipt.get("oms_monitor_coverage")
    byte_fields = {"oms_log_byte_coverage", "oms_log_copytruncate_count"}
    legacy_coverage = coverage == "complete_since_baseline" and not byte_fields.intersection(receipt)
    gaps = receipt.get("oms_log_copytruncate_count")
    sequence_coverage = (
        coverage == "consecutive_observed_end_sequence"
        and integer(gaps)
        and receipt.get("oms_log_byte_coverage") == (
            "gap_unobserved_copytruncate" if gaps else "observed_cursor_reads_only")
    )
    if (
        not integer(monitor) or not integer(end) or not 0 <= now_ns - monitor <= 5 * NS
        or not nonnegative(cadence) or not 0 < cadence <= 15
        or not isinstance(receipt.get("oms_cadence_source"), str)
        or not receipt["oms_cadence_source"] or receipt.get("oms_pass_allowance_seconds") != 5
        or not 0 <= monitor - end <= (cadence + 5) * NS
        or not (legacy_coverage or sequence_coverage)
        or receipt.get("oms_source") != "OMS-BROKER-SYNC-PASS.phase=end"
        or not isinstance(receipt.get("oms_instance_id"), str) or not receipt["oms_instance_id"]
        or not integer(receipt.get("oms_sync_seq")) or receipt["oms_sync_seq"] == 0
        or not nonnegative(receipt.get("oms_sync_seconds"))
    ):
        raise ValueError("UNKNOWN:OMS cadence/source/monitor evidence")
    result = {key: receipt[key] for key in (
        "observed_wall_ns", "oms_measured_wall_ns", "oms_sync_seq", "oms_sync_seconds",
        "oms_sync_interval_seconds", "oms_pass_allowance_seconds", "oms_cadence_source",
        "oms_instance_id", "oms_source", "oms_monitor_coverage",
    )}
    result.update(schema_version=2, oms_source_age_seconds=(monitor - end) / NS)
    if sequence_coverage:
        result.update({key: receipt[key] for key in byte_fields})
    applicability = receipt.get("v2_applicability")
    if applicability == "NOT_APPLICABLE":
        local = datetime.fromtimestamp(monitor / NS, ZoneInfo("America/New_York"))
        reference = receipt.get("v2_expectation_ref")
        pre07 = (
            receipt.get("v2_applicability_reason") == "sourced_pre0700"
            and receipt.get("v2_source_lane") in {
                "schwab_anchored_current_day", "schwab_pricehistory_same_day"}
            and receipt.get("v2_expectation_kind") == "recorded_first_same_day_0700"
            and receipt.get("v2_expected_start_et") == "07:00"
            and isinstance(reference, str) and bool(reference)
            and local.weekday() < 5 and 4 <= local.hour < 7
        )
        before_first_due = False
        if receipt.get("v2_applicability_reason") == "sourced_before_first_due":
            first_due = sourced_first_due_ns(receipt, observed_ns=monitor)
            before_first_due = (
                receipt.get("v2_first_due_wall_ns") == first_due
                and monitor < first_due and now_ns < first_due
            )
            result["v2_first_due_wall_ns"] = first_due
        watch_ns = receipt.get("v2_watchlist_snapshot_ns")
        empty = (
            receipt.get("v2_applicability_reason") == "sourced_empty_watchlist"
            and receipt.get("v2_watchlist_source") == "strategy-state-isolated"
            and receipt.get("v2_watchlist_complete") is True
            and receipt.get("v2_watchlist") == [] and integer(watch_ns)
            and 0 <= monitor - watch_ns <= 10 * NS
        )
        if not (pre07 or before_first_due or empty):
            raise ValueError("UNKNOWN:unsourced v2 inapplicability")
        result.update(v2_applicability=applicability, v2_bar_lateness_seconds=None,
                      v2_applicability_reason=receipt["v2_applicability_reason"],
                      v2_expectation_ref=reference)
        result["v2_applicability_provenance"] = {key: receipt.get(key) for key in (
            "v2_source_lane", "v2_expectation_kind", "v2_expected_start_et",
            "v2_watchlist_source", "v2_watchlist_snapshot_ns", "v2_watchlist_complete",
            "v2_watchlist",
            "v2_first_bar_start_et", "v2_bar_interval_seconds", "v2_first_bar_source_ref",
        )}
    elif applicability == "APPLICABLE":
        measured, due, completed = (receipt.get(key) for key in (
            "v2_measured_wall_ns", "v2_due_wall_ns", "v2_completed_wall_ns"
        ))
        late = receipt.get("v2_bar_lateness_seconds")
        if (
            receipt.get("v2_source") != "completed_bar_due_monitor"
            or not isinstance(receipt.get("v2_instance_id"), str) or not receipt["v2_instance_id"]
            or receipt.get("v2_coverage") != "complete_current_due"
            or not integer(measured) or not 0 <= monitor - measured <= 5 * NS
            or not integer(due) or due == 0 or due > measured or not nonnegative(late)
            or completed is not None and (not integer(completed) or not due <= completed <= measured)
            or abs(((measured if completed is None else completed) - due) / NS - late) > .000001
        ):
            raise ValueError("UNKNOWN:applicable v2 due/completion evidence")
        result.update(v2_applicability=applicability, v2_bar_lateness_seconds=late,
                      v2_measured_wall_ns=measured, v2_due_wall_ns=due,
                      v2_completed_wall_ns=completed, v2_instance_id=receipt["v2_instance_id"],
                      v2_source=receipt["v2_source"], v2_coverage=receipt["v2_coverage"])
    else:
        raise ValueError("UNKNOWN:v2 applicability/source unreadable")
    return result


class OmsLogMonitor:
    """Actual bounded log/proc reader. No credentials, DB, provider or connection."""

    def __init__(self, log: Path, pid: int, *, verified_timezone: str, cadence_seconds: float,
                 cadence_source: str, wall_ns: Callable[[], int], proc: Path = Path("/proc")):
        if type(pid) is not int or pid <= 0 or not cadence_source:
            raise ValueError("explicit PID and live cadence provenance required")
        self.log, self.pid, self.proc = log, pid, proc
        self.timezone, self.cadence, self.cadence_source = (
            verified_timezone, cadence_seconds, cadence_source
        )
        self.wall_ns = wall_ns
        self.position = None
        self.identity = None
        self.process = None
        self.partial = b""
        self.latest = None
        self.truncations = 0
        self.anchor = None

    def read(self) -> dict:
        raw = read_bounded(self.proc / str(self.pid) / "stat").decode()
        fields = raw.rsplit(")", 1)[1].split()
        if int(raw.split("(", 1)[0]) != self.pid or fields[0] in {"Z", "X"}:
            raise ValueError("OMS process unreadable/dead")
        process = f"{self.pid}:{int(fields[19])}"
        if self.process is not None and process != self.process:
            raise ValueError("OMS PID reused/restarted; fresh baseline review required")
        fd = os.open(self.log, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            identity = info.st_dev, info.st_ino
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("OMS log not regular")
            if self.identity is not None and identity != self.identity:
                raise ValueError("OMS log rotated")
            position = max(0, info.st_size - BOUND) if self.position is None else self.position
            if self.position is not None and cursor_rewritten(fd, position, self.anchor):
                self.truncations += 1
                position, self.partial = 0, b""
            if info.st_size - position > BOUND:
                raise ValueError("OMS log truncated/backlog bound")
            os.lseek(fd, position, os.SEEK_SET)
            data = os.read(fd, BOUND + 1)
            if len(data) > BOUND:
                raise ValueError("OMS log read bound")
            covered = position + len(data)
            if os.fstat(fd).st_size > covered:
                raise ValueError("OMS log changed beyond read coverage")
            if self.position is None and position:
                data = data.partition(b"\n")[2]
            lines = (self.partial + data).split(b"\n")
            if len(lines[-1]) > BOUND:
                raise ValueError("OMS partial line bound")
            latest = self.latest
            for line in lines[:-1]:
                text = line.decode("utf-8")
                if "[OMS-BROKER-SYNC-PASS]" not in text or "phase=end" not in text:
                    continue
                item = parse_oms_end(text, verified_timezone=self.timezone, instance_id=process)
                if latest and item["oms_sync_seq"] != latest["oms_sync_seq"] + 1:
                    raise ValueError("OMS completion sequence gap/regression")
                latest = item
            if latest is None:
                raise ValueError("no actual completed OMS duration in bounded log")
            self.latest, self.process, self.identity = latest, process, identity
            self.position, self.partial = covered, lines[-1]
            self.anchor = cursor_anchor(fd, covered)
        finally:
            os.close(fd)
        # Stamps an actual EOF/proc monitor operation, never changes end receipt time.
        return dict(self.latest, observed_wall_ns=self.wall_ns(),
                    oms_monitor_coverage="consecutive_observed_end_sequence",
                    oms_log_byte_coverage=("gap_unobserved_copytruncate" if self.truncations
                                           else "observed_cursor_reads_only"),
                    oms_log_copytruncate_count=self.truncations,
                    oms_sync_interval_seconds=self.cadence,
                    oms_cadence_source=self.cadence_source, oms_pass_allowance_seconds=5)
