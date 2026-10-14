"""CAL: pure observed-cohort policy; incomplete minutes are not lateness measurements."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

NS = 10**9
MINUTE_NS = 60 * NS
POLICY = "complete-cohort-p99-two-v1"
LIMITS_NS = {"PM": 4_694_000_128, "RTH": 4_671_000_064, "AH": 3_785_000_192}
SEED_MARKERS = frozenset({"V2-DB-SEED-GAP", "V2-CW-SEED-CAP", "V2-WARMUP-LINE"})


def integer(value):
    return type(value) is int and value >= 0


def first_eligible_bar_ns(observed_join_or_seed_ns: int) -> int:
    """Exclude the partial bucket and the first FULL bucket after the observation.

    At 07:00:00 the excluded full bucket is [07:00,07:01); at 07:00:01
    it is [07:01,07:02). A bucket's key is its START, not its close.
    """
    if not integer(observed_join_or_seed_ns):
        raise ValueError("join/seed observation invalid")
    return ((observed_join_or_seed_ns + MINUTE_NS - 1) // MINUTE_NS + 1) * MINUTE_NS


def window(due_ns):
    if not integer(due_ns):
        raise ValueError("cohort due invalid")
    local = datetime.fromtimestamp(due_ns // NS, ZoneInfo("America/New_York"))
    minute = local.hour * 60 + local.minute
    if local.weekday() >= 5 or not 7 * 60 + 1 <= minute <= 20 * 60:
        return None
    return "PM" if minute < 570 else "RTH" if minute < 960 else "AH"


@dataclass(frozen=True)
class CohortObservation:
    due_ns: int
    observed_ns: int
    source: str
    known: bool
    eligible: tuple[str, ...]
    excluded: tuple[str, ...]
    missing: tuple[str, ...]
    first_receipts: tuple[tuple[str, int], ...]
    reason: str

    @property
    def status(self):
        return "INCOMPLETE" if not self.known or self.missing else "COMPLETE" if self.eligible else "EXCLUDED"

    @property
    def lateness_ns(self):
        return max(stamp for _, stamp in self.first_receipts) - self.due_ns if self.status == "COMPLETE" else None

    def payload(self):
        return {**asdict(self), "policy": POLICY, "status": self.status,
                "lateness_ns": self.lateness_ns, "window": window(self.due_ns)}

    @classmethod
    def read(cls, payload):
        try:
            if not isinstance(payload, dict) or payload.get("policy") != POLICY:
                raise ValueError("unreviewed cohort policy")
            value = cls(payload["due_ns"], payload["observed_ns"], payload["source"], payload["known"],
                        tuple(payload["eligible"]), tuple(payload["excluded"]), tuple(payload["missing"]),
                        tuple(tuple(pair) for pair in payload["first_receipts"]), payload["reason"])
            value.validate()
            if (payload.get("status") != value.status or payload.get("lateness_ns") != value.lateness_ns
                    or payload.get("window") != window(value.due_ns)):
                raise ValueError("cohort derived fields altered")
            return value
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError("cohort shape invalid") from exc

    def validate(self):
        if (not integer(self.due_ns) or self.due_ns == 0 or self.due_ns % MINUTE_NS
                or not integer(self.observed_ns) or self.due_ns > self.observed_ns
                or self.observed_ns // MINUTE_NS != self.due_ns // MINUTE_NS
                or self.source not in {"strategy-state-isolated", "retained_watchlist_log"}
                or type(self.known) is not bool or not isinstance(self.reason, str) or not self.reason):
            raise ValueError("cohort source/time invalid")
        for symbols in (self.eligible, self.excluded, self.missing):
            if (len(symbols) > 256 or len(set(symbols)) != len(symbols)
                    or any(not isinstance(s, str) or not s for s in symbols)):
                raise ValueError("cohort symbols invalid")
        if (set(self.eligible) & set(self.excluded) or not set(self.missing) <= set(self.eligible)
                or len(self.first_receipts) > 256 or len(dict(self.first_receipts)) != len(self.first_receipts)
                or set(dict(self.first_receipts)) != set(self.eligible) - set(self.missing)
                or any(not integer(t) or not self.due_ns <= t <= self.observed_ns for _, t in self.first_receipts)):
            raise ValueError("cohort completion evidence invalid")
        if not self.known and (self.eligible or self.excluded or self.missing or self.first_receipts):
            raise ValueError("unknown membership must not invent a cohort")
        if window(self.due_ns) is None:
            raise ValueError("cohort outside sourced first-due/trading window")


class CohortTracker:
    """Observed membership/seed history. Initial observation is a NEW join baseline."""

    def __init__(self):
        self.membership = []
        self.seeds = {}

    def observe_membership(self, symbols, observed_ns):
        if not integer(observed_ns):
            raise ValueError("membership observation invalid")
        if symbols is not None and (not isinstance(symbols, (list, tuple)) or len(symbols) > 256
                or len(set(symbols)) != len(symbols) or any(not isinstance(s, str) or not s for s in symbols)):
            raise ValueError("membership symbols invalid")
        previous = self.membership[-1] if self.membership else None
        selected = None if symbols is None else tuple(sorted(symbols))
        if previous and observed_ns < previous[0]:
            raise ValueError("membership observation regressed")
        if previous and selected == previous[1]:
            return
        joins = {s: previous[2].get(s, observed_ns) if previous and previous[1] is not None
                 else observed_ns for s in selected or ()}
        self.membership.append((observed_ns, selected, joins))
        cutoff = observed_ns - 2 * MINUTE_NS
        while len(self.membership) > 1 and self.membership[1][0] < cutoff:
            self.membership.pop(0)
        if len(self.membership) > 256:
            raise ValueError("membership history exceeds bound")

    def observe_seed(self, symbol, observed_ns, marker):
        if not isinstance(symbol, str) or not symbol or not integer(observed_ns) or marker not in SEED_MARKERS:
            raise ValueError("seed source invalid")
        items = self.seeds.setdefault(symbol, [])
        items.append(observed_ns)
        self.seeds[symbol] = [t for t in items if t >= observed_ns - 2 * MINUTE_NS]
        if len(self.seeds) > 1024 or len(self.seeds[symbol]) > 256:
            raise ValueError("seed history exceeds bound")

    def observation(self, due_ns, observed_ns, first_receipts, *, source):
        cohort = next((row for row in reversed(self.membership) if row[0] <= due_ns), None)
        if cohort is None or cohort[1] is None:
            result = CohortObservation(due_ns, observed_ns, source, False, (), (), (), (), "membership_UNKNOWN")
        else:
            eligible, excluded = [], []
            bar_start = due_ns - MINUTE_NS
            for symbol in cohort[1]:
                recent = max(cohort[2][symbol], max((t for t in self.seeds.get(symbol, ()) if t <= due_ns), default=0))
                (eligible if bar_start >= first_eligible_bar_ns(recent) else excluded).append(symbol)
            missing = tuple(s for s in eligible if s not in first_receipts)
            result = CohortObservation(due_ns, observed_ns, source, True, tuple(eligible), tuple(excluded), missing,
                tuple((s, first_receipts[s]) for s in eligible if s in first_receipts),
                "first_native_receipts" if not missing else "completion_missing_UNKNOWN")
        result.validate()
        return result


class CompleteCohortPolicy:
    """Consecutive COMPLETE measurements, not necessarily adjacent clock minutes.

    Unknown/excluded minutes neither trip nor reset. A later COMPLETE on-time
    observation resets. Repeated reads never count a completed minute twice.
    """

    def __init__(self):
        self.streak = self.complete_minutes = self.incomplete_minutes = self.excluded_minutes = 0
        self.gap_minutes = 0
        self.latest = None
        self.resolved = {}
        self.unknown_seen = set()
        self.last_due = None
        self.scope = None

    def observe(self, item):
        item.validate()
        if self.last_due is not None and item.due_ns < self.last_due:
            raise ValueError("cohort due regressed")
        local = datetime.fromtimestamp(item.due_ns // NS, ZoneInfo("America/New_York"))
        scope = (local.date().isoformat(), window(item.due_ns))
        if self.scope != scope:
            self.streak = 0
            self.last_due = None
            self.resolved.clear()
            self.unknown_seen.clear()
            self.scope = scope
        if self.last_due is not None and item.due_ns > self.last_due + MINUTE_NS:
            self.gap_minutes += (item.due_ns - self.last_due) // MINUTE_NS - 1
        self.last_due, self.latest = item.due_ns, item
        if item.status == "INCOMPLETE":
            if item.due_ns not in self.unknown_seen and item.due_ns not in self.resolved:
                self.incomplete_minutes += 1
                self.unknown_seen.add(item.due_ns)
        elif item.due_ns not in self.resolved:
            self.resolved[item.due_ns] = item
            if item.status == "COMPLETE":
                self.complete_minutes += 1
                self.streak = self.streak + 1 if item.lateness_ns > LIMITS_NS[window(item.due_ns)] else 0
            else:
                self.excluded_minutes += 1
        elif self.resolved[item.due_ns] != item:
            prior = self.resolved[item.due_ns]
            # Fresh sampling time can advance; immutable first completion/cohort cannot.
            if (prior.status, prior.eligible, prior.excluded, prior.first_receipts) != (
                    item.status, item.eligible, item.excluded, item.first_receipts):
                raise ValueError("resolved cohort altered")
        self.resolved = {k:v for k,v in self.resolved.items() if k >= item.due_ns - 2 * MINUTE_NS}
        self.unknown_seen = {k for k in self.unknown_seen if k >= item.due_ns - 2 * MINUTE_NS}
        return "v2_cohort_late_twice" if self.streak >= 2 else None

    def metrics(self):
        latest = self.latest
        return dict(policy=POLICY, complete_minutes=self.complete_minutes, incomplete_minutes=self.incomplete_minutes,
                    incomplete_sampled_minutes=self.incomplete_minutes,
                    incomplete_count_semantics="SAMPLED_PENDING_MAY_LATER_COMPLETE_NOT_CALIBRATION_DENOMINATOR",
                    streak_scope=list(self.scope) if self.scope else None,
                    unobserved_gap_minutes=self.gap_minutes, excluded_minutes=self.excluded_minutes,
                    late_streak=self.streak, status=latest.status if latest else "UNKNOWN_NOT_BASELINED",
                    due_ns=latest.due_ns if latest else None,
                    lateness_seconds=latest.lateness_ns / NS if latest and latest.lateness_ns is not None else None,
                    missing_symbols=len(latest.missing) if latest else None)


def replay_events(capture, *, first_due_ns, last_due_ns):
    """Exact policy over retained event evidence, not reconstructed live health.

    Completion selection is FIRST in capture/file order (native setdefault),
    never the latest receipt. Membership/seed timestamps are recorded markers;
    missing membership stays unknown. No prior monitor watch age is invented.
    """
    if (not isinstance(capture, dict) or not isinstance(capture.get("rows"), list)
            or len(capture["rows"]) > 150000 or not integer(capture.get("as_of_ns"))
            or not integer(first_due_ns) or not integer(last_due_ns)
            or first_due_ns % MINUTE_NS or last_due_ns % MINUTE_NS
            or not first_due_ns <= last_due_ns <= capture["as_of_ns"]):
        raise ValueError("replay bounds invalid")
    first, timeline = {}, []
    duplicates = ignored = 0
    for index, event in enumerate(capture["rows"]):
        stamp = event.get("observed_ns")
        if not integer(stamp) or stamp > capture["as_of_ns"]:
            raise ValueError("replay future/invalid event")
        if event["kind"] == "completion":
            bar_ms = event.get("bar_ms")
            if not integer(bar_ms) or bar_ms % 60000 or (bar_ms + 60000) * 10**6 > stamp:
                raise ValueError("replay unclosed/future bar")
            due = (bar_ms + 60000) * 10**6
            if stamp // MINUTE_NS != due // MINUTE_NS:
                ignored += 1
                continue
            key = event["symbol"], due
            if key in first:
                duplicates += 1
            first.setdefault(key, event)
        elif event["kind"] in {"membership", "seed_marker"}:
            timeline.append((stamp, index, event))
    timeline.sort(key=lambda item:(item[0], item[1]))
    tracker, policy, output = CohortTracker(), CompleteCohortPolicy(), []
    cursor = 0
    # The initial replay monitor has no observed inventory until a named marker.
    while cursor < len(timeline) and timeline[cursor][0] < first_due_ns:
        cursor += 1
    tracker.observe_membership(None, first_due_ns)
    trip = None
    for due in range(first_due_ns, last_due_ns + MINUTE_NS, MINUTE_NS):
        while cursor < len(timeline) and timeline[cursor][0] <= due:
            stamp, _, event = timeline[cursor]
            if event["kind"] == "membership":
                selected = event["symbols"] if event.get("complete") is True else None
                if selected is not None and len(selected) != event.get("count"):
                    raise ValueError("replay complete membership count changed")
                tracker.observe_membership(selected, stamp)
            else:
                tracker.observe_seed(event["symbol"], stamp, event["marker"])
            cursor += 1
        local = datetime.fromtimestamp(due // NS, ZoneInfo("America/New_York"))
        minute = local.hour * 60 + local.minute
        row = dict(due_ns=due, due_et=local.isoformat(), bar_start_ns=due-MINUTE_NS,
                   source="retained_watchlist_log", live_identity_freshness="UNMEASURED")
        if minute < 421:
            row.update(status="NOT_APPLICABLE", reason="sourced_before_first_due_0701", lateness_seconds=None)
        else:
            observed = min(due + MINUTE_NS - 1, capture["as_of_ns"])
            receipts = {s:event["observed_ns"] for (s,d), event in first.items()
                        if d == due and event["observed_ns"] <= observed}
            item = tracker.observation(due, observed, receipts, source="retained_watchlist_log")
            reason = policy.observe(item) if trip is None else None
            if reason:
                trip = dict(due_et=local.isoformat(), due_ns=due, reason=reason,
                            lateness_ns=item.lateness_ns, threshold_ns=LIMITS_NS[window(due)])
            row.update(item.payload(), lateness_seconds=item.lateness_ns / NS if item.lateness_ns is not None else None,
                       late_streak=policy.streak, incomplete_minutes=policy.incomplete_minutes,
                       checked_before_sticky_trip=trip is None or trip["due_ns"] == due,
                       trigger=reason,
                       A_off_declared=not (360 <= minute < 373 or 521 <= minute < 526))
        output.append(row)
    return dict(policy=POLICY, thresholds_ns=LIMITS_NS, rows=output, trips=[trip] if trip else [],
                duplicate_receipts_ignored=duplicates, non_current_due_receipts_ignored=ignored,
                completion_selection="FIRST_capture_file_order_setdefault_not_latest",
                monitor_initial_join="NEW_observation_no_prior_watch_age",
                native_health_identity_freshness="UNMEASURED_retained_logs_not_live_heartbeat")
