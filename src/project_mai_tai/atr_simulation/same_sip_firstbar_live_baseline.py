"""C1 2026-10-28 explicit TODAY tie successor. Original sealed module unchanged."""
"""Actual aggregate-entry adapter; same FIRSTBAR exits, not a native ledger.

No FIRSTBAR entry planner, volume allocation, storage or broker calls. Recorded
aggregate fills are external facts, not new modeled buys on physical prints.
"""

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
import hashlib
from pathlib import Path

from ..backtest import atr_oracle
from .same_sip_firstbar import FirstBarMode
from .same_sip_firstbar_exits import FirstBarPosition
from .same_sip_eligibility import execution_status, note_unknown
from .same_sip_exits import causal_key
from .firstbar_study import (
    MINUTE_NS, ORACLE_SHA256, StudyBuyLeg, StudyTapePacket, _bars, _boundary,
    _local, parse_study_tape,
)
from .firstbar_target_rule import EXIT_POLICY_VERSION
from .same_sip import TieTape, TieState, enabled, report_key, LABEL
from .same_sip_types import (
    Boundary, Coverage, EntryFact, EventKey, EvidenceKind, ExecutionPolicy,
    FlipIdentity, PositionReceipt, SimExit, SimExitReason, SourceRef, TradePrint,
)


BASELINE_VERSION = "ATRSIM_RECORDED_AGGREGATE_ENTRY_BASELINE_V3_CONFIRMATION_EXIT"
BASELINE_POLICY_VERSION = "C1_20261024_0000_ENTRY_BAR_SHORT_CONFIRMATION_EXIT"
COHORT_POLICY = "INDEPENDENT_RECORDED_ACCOUNT_LEGS_SAME_ACCOUNT_OVERLAP_INFORMATIONAL"
BASELINE_VARIANT = "ACTUAL_AGGREGATE_ENTRY_SAME_EXIT_POLICY"
ENTRY_MODEL = "RECORDED_ORDER_AGGREGATE_AT_RECORDED_FILLED_NS"
AGGREGATE_ASSUMPTION = "COLLAPSE_RECORDED_ORDER_TO_ONE_ENTRY_AT_RECORDED_FILLED_NS"
BASELINE_LABEL = "RECORDED AGGREGATE-ENTRY REPLAY \u00b7 partial timing UNKNOWN"
TIE_POLICY = "OMIT_ALL_PRINTS_AT_RECORDED_AGGREGATE_SIP"
HISTORY_LABEL = "replay, in-sample; underlying capture completeness UNKNOWN"


class AggregateExitReason(str, Enum):
    """Aggregate-only serialized reason, not an amendment of sealed SIM types."""

    CONFIRMATION_EXIT = "CONFIRMATION_EXIT"


class AggregateBaselinePosition(FirstBarPosition):
    """V3 confirmation extension; sealed FIRSTBAR's normal paths stay inherited.

    SimExit is a pure dataclass, not a runtime enum-coercing validator. This
    aggregate-only string enum preserves .value, JSON/asdict and OPEN checks.
    The publication boundary separately validates its explicit V3 reason set.
    """

    def __init__(self, *, confirmation_exit: Boundary | None = None, **kwargs):
        super().__init__(**kwargs)
        if confirmation_exit is not None and not isinstance(confirmation_exit, Boundary):
            raise ValueError("typed_confirmation_close_required")
        self.confirmation_exit = confirmation_exit

    def _confirmation_first(self) -> bool:
        return (self.confirmation_exit is not None
            and self.confirmation_exit.available_ns <= self.session_end.available_ns
            and (self.sell_flip is None
                 or self.confirmation_exit.available_ns <= self.sell_flip.available_ns))

    def before(self, key) -> None:
        if (self.exit is None and self.remainder_cancel_reason is None
                and self._confirmation_first() and key.event_ns >= self.confirmation_exit.available_ns):
            self.remainder_cancel_reason = AggregateExitReason.CONFIRMATION_EXIT.value
        super().before(key)

    def finish(self):
        if (self.exit is None and self.remainder_cancel_reason is None
                and self._confirmation_first() and self.coverage.end_ns >= self.confirmation_exit.available_ns):
            self.remainder_cancel_reason = AggregateExitReason.CONFIRMATION_EXIT.value
        return super().finish()

    def on_trade(self, trade: TradePrint) -> None:
        if not self._confirmation_first() or trade.key.event_ns < self.confirmation_exit.available_ns:
            return super().on_trade(trade)
        # Only the new boundary path is local. Never call the parent's trade
        # handler a second time after advancing its strict causal cursor.
        if type(trade.size) is not int or trade.size < 0:
            raise ValueError("nonnegative_integer_size_required")
        if not trade.price.is_finite() or trade.price <= 0:
            raise ValueError("positive_finite_price_required")
        key = causal_key(trade.key)
        if self._last_trade is not None and key <= self._last_trade:
            raise ValueError("strict_causal_order_required")
        self._last_trade = key
        self.before(trade.key)
        if self.exit is not None or self.first is None or key <= causal_key(self.first):
            return
        eligible, _ = execution_status(trade)
        if eligible is None:
            note_unknown(self.issues, "UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY")
            self._execution_uncertain = True
            return
        if eligible:
            self._close(AggregateExitReason.CONFIRMATION_EXIT, trade.key, trade.price,
                "MODELED_CONFIRMATION_AT_FIRST_ELIGIBLE_PRINT_AFTER_ENTRY_BAR_CLOSE_NOT_NATIVE")


@dataclass(frozen=True)
class BaselineBarClose:
    bar_start_ns: int
    available_ns: int
    close: Decimal
    line: Decimal | None
    state: str | None
    source: SourceRef


@dataclass(frozen=True)
class BaselineDay:
    symbol: str
    day: str
    trades: tuple[TradePrint, ...]
    sell_flips: tuple[Boundary, ...]
    session_end: Boundary
    coverage: Coverage
    issues: tuple[str, ...]
    source: SourceRef
    all_flips: tuple[tuple[str, Boundary], ...] = ()
    bar_closes: tuple[BaselineBarClose, ...] = ()
    same_sip_policy: str | None = None


@dataclass(frozen=True)
class AggregateBaselineInput:
    reference_key: tuple[str, str, str]
    identity: FlipIdentity
    leg: StudyBuyLeg
    day: BaselineDay
    assumption: str


@dataclass(frozen=True)
class AggregateBaselineResult:
    version: str
    variant: str
    entry_model: str
    exit_policy_version: str
    label: str
    history_label: str
    clock_label: str
    reference_key: tuple[str, str, str]
    identity: FlipIdentity
    order_id: str
    recorded_aggregate: StudyBuyLeg
    aggregate_entry: EntryFact
    position: PositionReceipt
    exits: tuple[SimExit, ...]
    previous_sell: Boundary | None
    next_sell: Boundary | None
    decision: str
    modeled_gross_pnl: Decimal | None
    issues: tuple[str, ...]
    assumptions: tuple[str, ...]
    same_sip_print_sources: tuple[SourceRef, ...]
    evidence_limits: tuple[str, ...]
    flags: tuple[str, ...] = ()
    same_account_overlap_order_ids: tuple[str, ...] = ()
    cohort_policy: str = COHORT_POLICY
    baseline_policy_version: str = BASELINE_POLICY_VERSION
    entry_bar_close: BaselineBarClose | None = None
    confirmation_exit: Boundary | None = None


def prepare_baseline_day(packet: StudyTapePacket, policy: ExecutionPolicy) -> BaselineDay:
    """Full supplied day -> all causal SELL closes; never reuse proxy's next SELL."""
    if hashlib.sha256(Path(atr_oracle.__file__).read_bytes()).hexdigest() != ORACLE_SHA256:
        raise ValueError("pinned_pure_atr_oracle_required")
    trades = parse_study_tape(packet, policy)
    bars, uncertain = _bars(trades)
    oracle_bars = [atr_oracle.Bar(start // 1_000_000, float(o), float(h), float(low), float(c), volume)
                   for start, o, h, low, c, volume in bars]
    rows = atr_oracle.compute_atr_trail(oracle_bars, seed="sma5", period=5, factor=3.5)
    all_flips = tuple((row["flip"], Boundary(bar[0] + MINUTE_NS, replace(packet.source,
        kind=EvidenceKind.DERIVED,
        pointer=f"oracle-{row['flip']}-bar:{bar[0]};available-at-close;oracle:{ORACLE_SHA256}"), bar[0]))
        for bar, row in zip(bars, rows) if row["flip"] in ("BUY", "SELL"))
    flips = tuple(event for side, event in all_flips if side == "SELL")
    closes = tuple(BaselineBarClose(bar[0], bar[0] + MINUTE_NS, bar[4],
        Decimal(str(row["trail"])) if row["trail"] is not None else None, row["state"],
        replace(packet.source, kind=EvidenceKind.DERIVED,
            pointer=f"oracle-close-bar:{bar[0]};available-at-close;oracle:{ORACLE_SHA256}"))
        for bar, row in zip(bars, rows))
    issues = list(packet.coverage.gaps)
    if (packet.coverage.complete is not True
            or packet.coverage.start_ns > _boundary(packet.day, 4)
            or packet.coverage.end_ns < _boundary(packet.day, 20)):
        issues.append("UNKNOWN_SUPPLIED_0400_2000_ENVELOPE")
    if uncertain:
        issues.append("UNKNOWN_DAY_SELL_BAR_EXECUTION_ELIGIBILITY")
    if not bars or not any(row["state"] is not None for row in rows):
        issues.append("UNKNOWN_DAY_ORACLE_SEED")
    return BaselineDay(packet.symbol, packet.day, trades, flips,
        Boundary(_boundary(packet.day, 20), replace(packet.source, kind=EvidenceKind.DERIVED,
                 pointer="SUPPLIED_STUDY_2000_SESSION_BOUNDARY_NOT_NATIVE_EXIT")),
        packet.coverage, tuple(dict.fromkeys(issues)), packet.source, all_flips, closes)


def entry_bar_confirmation(day: BaselineDay, filled_ns: int):
    """Only the actual entry minute's completed close, never a prior SELL state."""
    start = filled_ns // MINUTE_NS * MINUTE_NS
    closes = day.bar_closes
    if (any(type(row.bar_start_ns) is not int or row.bar_start_ns % MINUTE_NS
            or row.available_ns != row.bar_start_ns + MINUTE_NS for row in closes)
            or any(a.bar_start_ns >= b.bar_start_ns for a, b in zip(closes, closes[1:]))):
        raise ValueError("unique_ascending_completed_oracle_closes_required")
    row = next((row for row in closes if row.bar_start_ns == start), None)
    if row is None or row.line is None or row.state not in ("long", "short"):
        return row, None, ("UNKNOWN_ENTRY_BAR_ORACLE_CLOSE",)
    if not row.close.is_finite() or not row.line.is_finite() or row.close <= 0 or row.line <= 0:
        raise ValueError("positive_finite_entry_bar_close_line_required")
    if row.state == "short" and row.close < row.line:
        return row, Boundary(row.available_ns, row.source, row.bar_start_ns), ()
    if ((row.state == "long" and row.close < row.line)
            or (row.state == "short" and row.close > row.line)):
        return row, None, ("UNKNOWN_ENTRY_BAR_ORACLE_STATE_LINE_CONFLICT",)
    return row, None, ()


def sell_window(day: BaselineDay, filled_ns: int):
    previous = next((event for event in reversed(day.sell_flips) if event.available_ns < filled_ns), None)
    following = next((event for event in day.sell_flips if event.available_ns > filled_ns), None)
    equal = any(event.available_ns == filled_ns for event in day.sell_flips)
    return previous, following, equal


def require_fresh_sell_transition(day: BaselineDay, filled_ns: int, selected: Boundary | None) -> None:
    """A prior closed SELL state is never a post-entry SELL exit event."""
    if selected is not None and (
            selected.available_ns <= filled_ns or selected not in day.sell_flips
            or not any(side == "SELL" and event == selected for side, event in day.all_flips)):
        raise ValueError("selected_SELL_must_be_new_transition_after_actual_entry")


def simulate_aggregate_entry(item: AggregateBaselineInput, *, same_sip_reports=None) -> AggregateBaselineResult:
    if item.assumption != AGGREGATE_ASSUMPTION:
        raise ValueError("explicit_approved_aggregate_entry_assumption_required")
    leg, day, identity = item.leg, item.day, item.identity
    use_ties = enabled(day.same_sip_policy)
    state = TieState(TieTape(day.trades)) if use_ties else None
    if (identity.symbol != leg.symbol or day.symbol != leg.symbol
            or identity.account_id != leg.account or identity.trading_day != day.day
            or item.reference_key[:2] != (leg.symbol, day.day)
            or identity.broker not in ("webull", "schwab") or not identity.strategy
            or not leg.order_id or _local(leg.filled_ns).date().isoformat() != day.day):
        raise ValueError("exact_actual_leg_day_account_identity_required")
    if (type(leg.filled_ns) is not int or type(leg.quantity) is not int or leg.quantity <= 0
            or not leg.price.is_finite() or leg.price <= 0):
        raise ValueError("positive_actual_aggregate_price_quantity_required")
    if (tuple(sorted(day.sell_flips, key=lambda event: event.available_ns)) != day.sell_flips
            or len({event.available_ns for event in day.sell_flips}) != len(day.sell_flips)):
        raise ValueError("unique_ascending_day_SELL_events_required")
    if (any(side not in ("BUY", "SELL") for side, _ in day.all_flips)
            or any(left[1].available_ns >= right[1].available_ns
                   for left, right in zip(day.all_flips, day.all_flips[1:]))
            or tuple(event for side, event in day.all_flips if side == "SELL") != day.sell_flips):
        raise ValueError("complete_consistent_day_BUY_SELL_event_sequence_required")
    issues = list(day.issues)
    close, confirmation, close_issues = entry_bar_confirmation(day, leg.filled_ns)
    issues.extend(close_issues)
    previous, following, equal = sell_window(day, leg.filled_ns)
    require_fresh_sell_transition(day, leg.filled_ns, following)
    if equal:
        issues.append("UNKNOWN_AGGREGATE_ENTRY_ON_SELL_BOUNDARY")
    # Recorded entries are external facts, not re-admitted through the oracle's
    # last closed BUY state. Previous SELL remains provenance, not permission.
    if not _boundary(day.day, 4) <= leg.filled_ns < day.session_end.available_ns:
        issues.append("UNKNOWN_ACTUAL_ENTRY_OUTSIDE_STUDY_SESSION")
    # Sequence 0 is adapter serialization ONLY. No physical print is assigned
    # to an order aggregate; ALL same-SIP prints are omitted from exit checks.
    aggregate_source = replace(leg.source, kind=EvidenceKind.DERIVED,
                               pointer=leg.source.pointer + ";" + AGGREGATE_ASSUMPTION)
    event = EventKey(leg.filled_ns, 0, f"ACTUAL_ORDER_AGGREGATE:{leg.account}:{leg.order_id}", aggregate_source)
    entry = EntryFact(event.raw_id, event, leg.price, leg.quantity, aggregate_source)
    position = AggregateBaselinePosition(mode=FirstBarMode.FIRSTBAR, sell_flip=following,
        session_end=day.session_end, coverage=day.coverage, issues=tuple(issues),
        confirmation_exit=confirmation, same_sip_state=state)
    same_sip = []
    if not position.add_fill(entry):
        issues.append("UNKNOWN_RECORDED_AGGREGATE_ENTRY_REJECTED_BY_LIFECYCLE")
    if not issues:
        prior = None
        seen = set()
        for trade in day.trades:
            current = (trade.key.event_ns, trade.key.sequence)
            if trade.symbol != day.symbol or trade.key.raw_id in seen or (prior is not None and current <= prior):
                raise ValueError("unique_causal_actual_day_tape_required")
            seen.add(trade.key.raw_id)
            prior = current
            if trade.key.event_ns == leg.filled_ns:
                same_sip.append(trade.key.source)
            if trade.key.event_ns > leg.filled_ns:
                position.on_trade(trade)
    exits = position.finish()
    issues.extend(position.issues)
    issues.extend(issue for exit in exits for issue in exit.issues)
    if not exits or any(exit.price is None or exit.reason is SimExitReason.OPEN for exit in exits):
        issues.append("UNKNOWN_NO_MODELED_BASELINE_EXIT")
    issues = tuple(dict.fromkeys(issues))
    pnl = None if issues else sum((Decimal(exit.quantity) * (exit.price - position.vwap)
                                 for exit in exits), Decimal(0))
    if state is not None and same_sip_reports is not None:
        same_sip_reports[report_key(identity, 'AGG_ENTRY')] = state.report()
    return AggregateBaselineResult(BASELINE_VERSION, BASELINE_VARIANT, ENTRY_MODEL,
        EXIT_POLICY_VERSION, BASELINE_LABEL, HISTORY_LABEL,
        "reconstructed SIP clock; source-export millisecond precision/file order",
        item.reference_key, identity, leg.order_id, leg, entry, position.receipt(), exits,
        previous, following, "UNKNOWN" if issues else "MODELED_BASELINE_CLOSED",
        pnl, issues, (AGGREGATE_ASSUMPTION, TIE_POLICY,
            "ACTUAL_RECORDED_ENTRIES_NOT_PRINT_VOLUME_ALLOCATED_OR_NEW_BUYS",
            "FULL_DAY_TYPED_ELIGIBLE_SELL_ORACLE_PERIOD5_FACTOR3.5_SMA5_CLOSE_PLUS60S") + ((LABEL,) if state is not None and state.touched else ()),
        tuple(same_sip), ("Original individual partial-fill path/first-fill clock UNKNOWN",
            "Native arrival/book/queue/market completeness UNKNOWN",
            "Modeled exits are not actual realized LIVE exits or realized PnL"),
        baseline_policy_version=BASELINE_POLICY_VERSION, entry_bar_close=close,
        confirmation_exit=confirmation)


def simulate_aggregate_cohort(items: tuple[AggregateBaselineInput, ...], *, same_sip_reports=None) -> tuple[AggregateBaselineResult, ...]:
    ids = [(item.leg.account, item.leg.order_id) for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_actual_account_order_binding")
    results = [simulate_aggregate_entry(item, same_sip_reports=same_sip_reports) for item in items]
    peers = {index: set() for index in range(len(results))}
    for index, left in enumerate(results):
        for other in range(index + 1, len(results)):
            right = results[other]
            if (left.identity.account_id, left.identity.symbol, left.identity.strategy) != (
                    right.identity.account_id, right.identity.symbol, right.identity.strategy):
                continue
            def interval(result, item):
                end = next((exit.at.event_ns for exit in result.exits if exit.at is not None),
                           item.day.session_end.available_ns)
                return result.recorded_aggregate.filled_ns, end
            a, b = interval(left, items[index]), interval(right, items[other])
            if max(a[0], b[0]) <= min(a[1], b[1]):
                peers[index].add(right.order_id)
                peers[other].add(left.order_id)
    for index, orders in peers.items():
        if not orders:
            continue
        value = results[index]
        # Every recorded account leg has its own position. Overlap describes
        # both participants, never changes their exits, issues or economics.
        results[index] = replace(value, flags=("SAME_ACCOUNT_OVERLAP",),
                                  same_account_overlap_order_ids=tuple(sorted(orders)))
    return tuple(results)


def independent_leg_totals(results: tuple[AggregateBaselineResult, ...]) -> dict:
    def total(values):
        values = tuple(values)
        known = tuple(row for row in values if row.modeled_gross_pnl is not None)
        return dict(legs=len(values), unknown_legs=len(values) - len(known),
            gross_pnl=None if len(known) != len(values) else sum((row.modeled_gross_pnl for row in known), Decimal(0)),
            known_component_gross_pnl=sum((row.modeled_gross_pnl for row in known), Decimal(0)))
    return dict(all_legs=total(results),
        excluding_same_account_overlap=total(row for row in results if "SAME_ACCOUNT_OVERLAP" not in row.flags),
        same_account_overlap=total(row for row in results if "SAME_ACCOUNT_OVERLAP" in row.flags))
