"""Pure MOMLATCH1 policy; gateway owns health validation, gates and socket actions.

The 1s read/2s publication budgets, CAL two-minute rule and boot-error filter stay
external. Initial PAUSED requires three actual clean reads, not a constructor
health receipt. Call clean only after a ready, nonpending, reason-free read.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import time


MONITOR_REASONS = frozenset({
    "monitor_unreadable", "monitor_stale", "monitor_invalid",
    "monitor_sync_regressed", "monitor_sync_gap", "v2_startup_pending",
    "v2_startup_timeout", "publish_lag", "v2_bar_late",
})
HARM_REASONS = frozenset({
    "oms_sync_slow_twice", "v2_cohort_late_twice", "load", "ram", "live_error",
})
RECOVERY_READS = 3
PAUSE_HISTORY_LIMIT = 32
FLAPPING_THRESHOLD = 6
HOUR_NS = 3_600_000_000_000
LONG_PAUSE_NS = 600_000_000_000


class ReasonClass(str, Enum):
    MONITOR = "monitor"
    HARM = "harm"
    UNKNOWN = "unknown"


def classify_reason(reason: str) -> ReasonClass:
    """Only exact approved noise may resume; HARM/unknown stays sticky.

    v2_bar_late is UNCALIBRATED. Validated CAL harm is v2_cohort_late_twice.
    Watched T/Q lag, disconnect/1008, publication/metrics failures and new reasons
    are sticky by default. No prefix matching or invented source classification.
    """
    if isinstance(reason, str) and reason in MONITOR_REASONS:
        return ReasonClass.MONITOR
    if isinstance(reason, str) and reason in HARM_REASONS:
        return ReasonClass.HARM
    return ReasonClass.UNKNOWN


@dataclass(frozen=True)
class PauseDecision:
    state: str
    action: str
    reason: str | None
    clean_reads: int
    alert_reasons: tuple[str, ...]
    new_alert_reasons: tuple[str, ...]

    @property
    def reason_class(self) -> ReasonClass | None:
        return classify_reason(self.reason) if self.reason is not None else None


class WarningPausePolicy:
    """Bounded, I/O-free single-loop policy with explicit nanosecond time inputs.

    observe returns a PauseDecision. trip/clean/snapshot are compatibility aliases.
    trip returns 'pause'/'sticky'. clean returns permission after an ACTUAL clean
    read: immediately if active, after three if paused, never if off. Caller
    deduplicates socket changes against actual state; this module sends nothing.

    pause_count counts REASON SEGMENTS. pause_episode_count counts actual observed
    policy intervals (including initial readiness); wire_pause_count counts only
    active->paused transitions. Noise after partial recovery increments interruptions,
    not episode starts. Alert at >6 actual episodes OR >6 interruptions per hour;
    changed reasons alone never trigger flapping. Continuous bad polls use >10min.
    """

    def __init__(self, *, clock_ns: Callable[[], int] = time.monotonic_ns):
        self._clock_ns = clock_ns
        self._last_now_ns: int | None = None
        self.state = "paused"
        self.reason: str | None = "v2_startup_pending"
        self.clean_streak = 0
        self.pause_count = 0
        self.pause_episode_count = 0
        self.wire_pause_count = 0
        self.pause_events = 0
        self.recovery_interruptions = 0
        self._history: deque[dict] = deque(maxlen=PAUSE_HISTORY_LIMIT)
        self._current: dict | None = None
        self._pause_starts: deque[int] = deque(maxlen=FLAPPING_THRESHOLD+1)
        self._flap_events: deque[int] = deque(maxlen=FLAPPING_THRESHOLD+1)
        self._pause_reason_counts: dict[str, int] = {}
        self._noise_reason_counts: dict[str, int] = {}
        self._last_alerts: set[str] = set()

    def _now(self, now_ns: int | None) -> int:
        value = self._clock_ns() if now_ns is None else now_ns
        if type(value) is not int or value < 0:
            raise ValueError("policy time must be nonnegative integer monotonic nanoseconds")
        if self._last_now_ns is not None and value < self._last_now_ns:
            raise ValueError("policy clock regressed")
        self._last_now_ns = value
        return value

    def _finish_pause(self, now_ns: int, outcome: str) -> None:
        if self._current is not None:
            self._current.update(end_ns=now_ns,
                                 duration_ns=now_ns-self._current["start_ns"], outcome=outcome)
            self._current = None

    def _begin_pause(self, reason: str, now: int) -> None:
        self.pause_count += 1
        self.pause_episode_count += 1
        self.pause_events += 1
        self._pause_starts.append(now)
        self._pause_reason_counts[reason] = self._pause_reason_counts.get(reason, 0)+1
        self._current = {"reason": reason, "latest_reason": reason,
                         "start_ns": now, "end_ns": None, "duration_ns": 0,
                         "outcome": "paused"}
        self._history.append(self._current)

    def trip(self, reason: str, now_ns: int | None = None) -> str:
        now = self._now(now_ns)
        if self.state == "off":
            return "sticky"
        classification = classify_reason(reason)
        normalized = reason[:128] if isinstance(reason, str) and reason else "unknown_invalid_reason"
        if classification != ReasonClass.MONITOR:
            self._finish_pause(now, "sticky")
            self.state, self.reason, self.clean_streak = "off", normalized, 0
            return "sticky"
        self._noise_reason_counts[reason] = self._noise_reason_counts.get(reason, 0)+1
        if self.state == "active":
            self.wire_pause_count += 1
        if self._current is None:
            self._begin_pause(reason, now)
        else:
            if reason != self._current["latest_reason"]:
                self.pause_count += 1
                self._pause_reason_counts[reason] = self._pause_reason_counts.get(reason, 0)+1
        if self.state == "paused" and self.clean_streak:
            self.recovery_interruptions += 1
            self.pause_events += 1
            self._flap_events.append(now)
        if self._current is not None:
            self._current["latest_reason"] = reason
        self.state, self.reason, self.clean_streak = "paused", reason, 0
        return "pause"

    def clean(self, now_ns: int | None = None) -> bool:
        """Pending/unreadable reads MUST call trip instead; readiness is external."""
        now = self._now(now_ns)
        if self.state == "off":
            return False
        if self.state == "paused" and self._current is None:
            self._begin_pause("v2_startup_pending", now)
        self.clean_streak = min(RECOVERY_READS, self.clean_streak+1)
        if self.state == "paused":
            if self.clean_streak < RECOVERY_READS:
                return False
            self._finish_pause(now, "resumed")
            self.state, self.reason = "active", None
        return True

    def snapshot(self, now_ns: int | None = None) -> dict:
        now = self._now(now_ns)
        for events in (self._pause_starts, self._flap_events):
            while events and events[0] <= now-HOUR_NS:
                events.popleft()
        alerts = []
        if (len(self._pause_starts) > FLAPPING_THRESHOLD
                or len(self._flap_events) > FLAPPING_THRESHOLD):
            alerts.append("pause_flapping")
        if self._current is not None and now-self._current["start_ns"] > LONG_PAUSE_NS:
            alerts.append("pause_over_10min")
        history = []
        for item in self._history:
            row = dict(item)
            if row["end_ns"] is None:
                row["duration_ns"] = now-row["start_ns"]
            row["duration_seconds"] = row["duration_ns"]/1_000_000_000
            history.append(row)
        return {
            "state": self.state, "reason": self.reason, "clean_streak": self.clean_streak,
            "reason_class": classify_reason(self.reason).value if self.reason is not None else None,
            "required_clean_reads": RECOVERY_READS,
            "pause_count": self.pause_count, "pause_events": self.pause_events,
            "pause_count_semantics": "reason_segments_not_wire_unsubscribes",
            "pause_episode_count": self.pause_episode_count,
            "wire_pause_count": self.wire_pause_count,
            "pause_reason_counts": dict(self._pause_reason_counts),
            "noise_reason_counts": dict(self._noise_reason_counts),
            "recovery_interruptions": self.recovery_interruptions,
            "pauses_last_hour_lower_bound": len(self._pause_starts),
            "pause_events_last_hour_lower_bound": len(self._pause_starts)+len(self._flap_events),
            "pause_events_semantics": "episode_starts_plus_interrupted_recovery",
            "recovery_interruptions_last_hour_lower_bound": len(self._flap_events),
            "recent_counts_saturated": (len(self._flap_events) == FLAPPING_THRESHOLD+1
                                        or len(self._pause_starts) == FLAPPING_THRESHOLD+1),
            "history": history, "history_limit": PAUSE_HISTORY_LIMIT,
            "alert_reasons": alerts, "clock_domain": "monotonic_ns",
        }

    def metrics(self, now_ns: int | None = None) -> dict:
        return self.snapshot(now_ns)

    def observe(self, reason: str | None = None, *, ready: bool = False,
                exception: Exception | None = None, now_ns: int | None = None) -> PauseDecision:
        if exception is not None:
            if not isinstance(exception, Exception):
                raise exception
            if reason is None:
                reason = "monitor_unreadable"
        prior = self.state
        if reason is None and not ready:
            reason = "v2_startup_pending"
        if reason is not None:
            outcome = self.trip(reason, now_ns)
            action = "sticky_off" if outcome == "sticky" and prior != "off" else (
                "pause" if outcome == "pause" and prior == "active" else "none")
        else:
            resumed = self.clean(now_ns)
            action = "resume" if resumed and prior == "paused" else "none"
        snapshot = self.snapshot(now_ns)
        alerts = set(snapshot["alert_reasons"])
        new_alerts = alerts-self._last_alerts
        self._last_alerts = alerts
        return PauseDecision(self.state, action, self.reason, self.clean_streak,
                             tuple(sorted(alerts)), tuple(sorted(new_alerts)))
