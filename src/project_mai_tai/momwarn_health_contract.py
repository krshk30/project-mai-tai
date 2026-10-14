"""Health v2 source candidate; local G1 wiring requires review, not installation."""

from __future__ import annotations

import codecs
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from project_mai_tai.momwarn_health import parse_oms_end
from project_mai_tai.momwarn_log_cursor import check_read_coverage, cursor_anchor, cursor_rewritten
from project_mai_tai.momwarn_measurement_probes import BOUND, read_bounded
from project_mai_tai.momwarn_measurements import NS, integer, nonnegative

_LONG_OMS_END = re.compile(
    rb"\[OMS-BROKER-SYNC-PASS\] id=\d+ phase=end outcome=(?:ok|failed|cancelled) "
    rb"duration_ms=\d+(?:\.\d+)?\b"
)


def _long_oms_fragment(state: dict, fragment: bytes, *, final: bool) -> bytes | None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    decoder.setstate(state.get("utf8", (b"", 0)))
    decoder.decode(fragment, final=final)
    state["utf8"] = decoder.getstate()
    combined = state["tail"] + fragment
    start = state["length"] - len(state["tail"])
    for match in _LONG_OMS_END.finditer(combined):
        end = start + match.end()
        if end <= state["parsed"] or (not final and match.end() == len(combined)):
            continue
        # The original greedy receipt parser selects the last complete end token.
        state["end"], state["parsed"] = match[0], end
    state["length"] += len(fragment)
    state["tail"] = combined[-BOUND:]
    return state["prefix"] + b" " + state["end"] if final and "end" in state else None


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
        cal_outside = False
        if receipt.get("v2_applicability_reason") == "sourced_cal_outside_trading_window":
            from project_mai_tai.momwarn_cohort_lateness import MINUTE_NS, POLICY, window

            due = receipt.get("v2_due_wall_ns")
            watchlist = receipt.get("v2_watchlist")
            cal_outside = (
                receipt.get("v2_lateness_policy") == POLICY
                and integer(due) and due == monitor // MINUTE_NS * MINUTE_NS
                and window(due) is None and window(now_ns // MINUTE_NS * MINUTE_NS) is None
                and receipt.get("v2_source") == "first_completed_bar_cohort_monitor"
                and isinstance(reference, str) and bool(reference)
                and isinstance(receipt.get("v2_instance_id"), str) and bool(receipt["v2_instance_id"])
                and receipt.get("v2_watchlist_source") == "strategy-state-isolated"
                and receipt.get("v2_watchlist_complete") is True
                and integer(watch_ns) and 0 <= monitor - watch_ns <= 10 * NS
                and isinstance(watchlist, list) and 0 < len(watchlist) <= 256
                and all(isinstance(symbol, str) and symbol for symbol in watchlist)
                and len(set(watchlist)) == len(watchlist)
            )
            if cal_outside:
                result.update({key: receipt[key] for key in (
                    "v2_lateness_policy", "v2_due_wall_ns", "v2_source", "v2_instance_id",
                    "v2_watchlist", "v2_watchlist_source", "v2_watchlist_complete", "v2_watchlist_snapshot_ns")})
        if not (pre07 or before_first_due or empty or cal_outside):
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
    elif applicability == "STARTUP_PENDING":
        started, due, deadline, watch_ns = (receipt.get(key) for key in (
            "v2_startup_started_ns", "v2_startup_first_due_ns", "v2_startup_deadline_ns",
            "v2_watchlist_snapshot_ns"))
        watchlist = receipt.get("v2_watchlist")
        baseline, activation = (receipt.get(key) for key in (
            "v2_startup_baseline_ns", "v2_startup_activation_ns"))
        first_due = sourced_first_due_ns(receipt, observed_ns=monitor)
        if (not integer(started) or started == 0 or started > monitor
                or not integer(due) or not integer(deadline)
                or not integer(baseline) or not integer(activation)
                or not started <= baseline <= activation <= monitor
                or due != max(first_due, (activation // (60 * NS) + 1) * (60 * NS))
                or deadline != due + 5 * NS or monitor < first_due
                or not isinstance(receipt.get("v2_startup_gateway_instance_id"), str)
                or not receipt["v2_startup_gateway_instance_id"]
                or not isinstance(receipt.get("v2_instance_id"), str) or not receipt["v2_instance_id"]
                or receipt.get("v2_watchlist_source") != "strategy-state-isolated"
                or receipt.get("v2_watchlist_complete") is not True
                or not integer(watch_ns) or not 0 <= monitor - watch_ns <= 10 * NS
                or not isinstance(watchlist, list) or not 0 < len(watchlist) <= 256
                or any(not isinstance(symbol, str) or not symbol for symbol in watchlist)
                or len(set(watchlist)) != len(watchlist)):
            raise ValueError("UNKNOWN:unsourced v2 startup readiness")
        result.update(v2_applicability=applicability, v2_bar_lateness_seconds=None,
                      v2_instance_id=receipt["v2_instance_id"],
                      v2_expectation_ref=receipt["v2_expectation_ref"])
        result.update({key: receipt[key] for key in (
            "v2_startup_gateway_instance_id", "v2_startup_started_ns",
            "v2_startup_first_due_ns", "v2_startup_deadline_ns",
            "v2_startup_baseline_ns", "v2_startup_activation_ns")})
        result["v2_applicability_provenance"] = {key: receipt.get(key) for key in (
            "v2_source_lane", "v2_expectation_kind", "v2_expected_start_et",
            "v2_watchlist_source", "v2_watchlist_snapshot_ns", "v2_watchlist_complete",
            "v2_watchlist", "v2_first_bar_start_et", "v2_bar_interval_seconds",
            "v2_first_bar_source_ref")}
    elif applicability == "CALIBRATED_COHORT":
        from project_mai_tai.momwarn_cohort_lateness import CohortObservation

        cohort = CohortObservation.read(receipt.get("v2_cohort"))
        watch_ns = receipt.get("v2_watchlist_snapshot_ns")
        watchlist = receipt.get("v2_watchlist")
        if (cohort.source != "strategy-state-isolated" or cohort.observed_ns != monitor
                or receipt.get("v2_source") != "first_completed_bar_cohort_monitor"
                or not isinstance(receipt.get("v2_instance_id"), str) or not receipt["v2_instance_id"]
                or receipt.get("v2_watchlist_source") != "strategy-state-isolated"
                or receipt.get("v2_watchlist_complete") is not True
                or not integer(watch_ns) or not 0 <= monitor - watch_ns <= 10 * NS
                or not isinstance(watchlist, list) or not 0 < len(watchlist) <= 256
                or any(not isinstance(symbol, str) or not symbol for symbol in watchlist)
                or len(set(watchlist)) != len(watchlist)):
            raise ValueError("UNKNOWN:CAL current source/watchlist evidence")
        result.update(v2_applicability=applicability, v2_source=receipt["v2_source"],
                      v2_instance_id=receipt["v2_instance_id"], v2_cohort=cohort.payload(),
                      v2_bar_lateness_seconds=cohort.lateness_ns / NS if cohort.lateness_ns is not None else None,
                      **{key:receipt[key] for key in ("v2_watchlist", "v2_watchlist_source",
                          "v2_watchlist_complete", "v2_watchlist_snapshot_ns")})
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

    MAX_LOG_PASS_BYTES = 2 * 1024 * 1024

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
        self._long_line = None

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
            rewritten = self.position is not None and cursor_rewritten(fd, position, self.anchor)
            partial = self.partial
            if rewritten:
                position, partial = 0, b""
            os.lseek(fd, position, os.SEEK_SET)
            latest = self.latest
            budget = self.MAX_LOG_PASS_BYTES
            first_fragment = self.position is None and position > 0
            long_line = None if rewritten else self._long_line
            oversized = False
            while budget:
                requested = min(BOUND, budget, max(0, info.st_size - position))
                data = os.read(fd, requested)
                if len(data) != requested:
                    raise ValueError("OMS log short read")
                covered = position + len(data)
                if first_fragment:
                    data = data.partition(b"\n")[2]
                    first_fragment = False
                text = partial + data
                pieces = text.split(b"\n")
                for index, line in enumerate(pieces):
                    final = index < len(pieces) - 1
                    if long_line is None and len(line) > BOUND:
                        long_line = dict(prefix=line[:29], tail=b"", length=0, parsed=0)
                    if long_line is not None:
                        oversized = True
                        candidate = _long_oms_fragment(long_line, line, final=final)
                        if final:
                            long_line = None
                        if candidate is None:
                            continue
                        line = candidate
                    elif not final:
                        continue
                    decoded = line.decode("utf-8")
                    if "[OMS-BROKER-SYNC-PASS]" in decoded and "phase=end" in decoded:
                        item = parse_oms_end(decoded, verified_timezone=self.timezone,
                                             instance_id=process)
                        if latest and item["oms_sync_seq"] != latest["oms_sync_seq"] + 1:
                            raise ValueError("OMS completion sequence gap/regression")
                        latest = item
                tail = b"" if long_line is not None else pieces[-1]
                check_read_coverage(fd, position, covered,
                                    max(BOUND, os.fstat(fd).st_size - covered), "OMS")
                anchor = cursor_anchor(fd, covered)
                # Commit parsed evidence even when this pass still has unread backlog.
                self.latest, self.process, self.identity = latest, process, identity
                self.position, self.partial, self.anchor = covered, tail, anchor
                self._long_line = long_line
                self.truncations += int(rewritten)
                rewritten = False
                position, partial = covered, tail
                budget -= requested
                if covered >= info.st_size:
                    break
            if position < info.st_size:
                raise ValueError("OMS log catch-up incomplete")
            if oversized:
                raise ValueError("OMS partial line bound; oversized evidence parsed")
            if latest is None:
                raise ValueError("no actual completed OMS duration in bounded log")
        finally:
            os.close(fd)
        # Stamps a bounded cursor/proc read, never changes the original end receipt time.
        return dict(self.latest, observed_wall_ns=self.wall_ns(),
                    oms_monitor_coverage="consecutive_observed_end_sequence",
                    oms_log_byte_coverage=("gap_unobserved_copytruncate" if self.truncations
                                           else "observed_cursor_reads_only"),
                    oms_log_copytruncate_count=self.truncations,
                    oms_sync_interval_seconds=self.cadence,
                    oms_cadence_source=self.cadence_source, oms_pass_allowance_seconds=5)
