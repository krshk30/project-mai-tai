"""C1 2026-10-28 explicit TODAY tie successor. Original sealed module unchanged."""
"""One VWAP position / first-fill clock, per C1 2026-10-23 02:31:20.

This pure offline event machine has no broker/OMS integration. A print supplies
its causal modeled allocation first, then a round-lot exit check uses the current
VWAP. The first fill's own print is not reused as an exit. Earlier SELL/session
boundaries or LIVE exit events close entry permission before any new allocation.
Threshold execution prices are modeled, never native fills.
"""

from decimal import Decimal
from .same_sip import LABEL

from .same_sip_types import (
    Boundary, Coverage, EntryFact, EventKey, LiveExitFact, LiveReason,
    PositionReceipt, SimExit, SimExitReason, TradePrint,
)


TIME_NS = 120_000_000_000
UNMODELED_LIVE_REASONS = frozenset((
    LiveReason.CONFIRMATION, LiveReason.BROKER_STOP, LiveReason.MANUAL, LiveReason.OTHER,
))


def causal_key(key: EventKey) -> tuple[int, int]:
    return key.event_ns, key.sequence


class Position:
    """One segment's position; accepted fills and earned latches never reset."""

    def __init__(
        self, *, sell_flip: Boundary | None, session_end: Boundary,
        coverage: Coverage, live_fallback: LiveExitFact | None = None,
        issues: tuple[str, ...] = (),
        same_sip_state=None,
    ) -> None:
        self.sell_flip = sell_flip
        self.session_end = session_end
        self.coverage = coverage
        self.fallback = live_fallback
        self.issues = list((*issues, *coverage.gaps))
        if coverage.complete is not True:
            self.issues.append("UNKNOWN_EXIT_TAPE_COVERAGE")
        if live_fallback is not None:
            if live_fallback.mapped_reason not in UNMODELED_LIVE_REASONS:
                raise ValueError("unmodeled_live_reason_required")
            if (
                isinstance(live_fallback.quantity, bool) or not isinstance(live_fallback.quantity, int)
                or live_fallback.quantity <= 0 or not live_fallback.price.is_finite() or live_fallback.price <= 0
            ):
                raise ValueError("valid_live_fallback_quantity_price_required")
        self.fills: list[EntryFact] = []
        self.ignored_fills: list[EntryFact] = []
        self.cost = Decimal(0)
        self.quantity = 0
        self.first: EventKey | None = None
        self.hit_one_in_time = False
        self.hit_two = False
        self.exit: SimExit | None = None
        self.remainder_cancel_reason: str | None = None
        self._last_trade: tuple[int, int] | None = None
        self.same_sip_state = same_sip_state

    @property
    def vwap(self) -> Decimal | None:
        return self.cost / self.quantity if self.quantity else None

    @property
    def can_fill(self) -> bool:
        return self.exit is None and self.remainder_cancel_reason is None

    def before(self, key: EventKey) -> None:
        """Apply separate, already-available boundary/LIVE events before a fill."""
        if self.exit is not None:
            return
        boundary = self.session_end
        reason = SimExitReason.SESSION_END
        if self.sell_flip is not None and self.sell_flip.available_ns <= boundary.available_ns:
            boundary, reason = self.sell_flip, SimExitReason.SELL_FLIP
        fallback = self.fallback
        if fallback is not None and causal_key(key) >= causal_key(fallback.at):
            if boundary.available_ns <= fallback.at.event_ns and self.remainder_cancel_reason is None:
                self.remainder_cancel_reason = reason.value
            self.fallback = None
            if self.remainder_cancel_reason is None:
                self.remainder_cancel_reason = SimExitReason.SAME_AS_LIVE.value
            if self.quantity:
                if self.coverage.end_ns < fallback.at.event_ns:
                    self.issues.append("UNKNOWN_TAPE_TO_LIVE_FALLBACK")
                self._close(
                    SimExitReason.SAME_AS_LIVE, fallback.at, fallback.price,
                    "MODELED_CLOSE_AT_ACTUAL_LIVE_EXIT_NOT_NEW_NATIVE_FILL", fallback,
                )
        if key.event_ns >= boundary.available_ns and self.remainder_cancel_reason is None:
            self.remainder_cancel_reason = reason.value

    def add_fill(self, entry: EntryFact) -> bool:
        if (
            isinstance(entry.quantity, bool) or not isinstance(entry.quantity, int)
            or entry.quantity <= 0 or not entry.price.is_finite() or entry.price <= 0
        ):
            raise ValueError("invalid_entry")
        if self.fills and causal_key(entry.at) <= causal_key(self.fills[-1].at):
            raise ValueError("strict_causal_fill_order_required")
        self.before(entry.at)
        if not self.can_fill:
            self.ignored_fills.append(entry)
            return False
        if self.first is None:
            self.first = entry.at
            if self.coverage.start_ns > entry.at.event_ns:
                self.issues.append("UNKNOWN_EXIT_PREFIX")
        self.fills.append(entry)
        self.cost += entry.price * entry.quantity
        self.quantity += entry.quantity
        return True

    def on_trade(self, trade: TradePrint) -> None:
        if isinstance(trade.size, bool) or not isinstance(trade.size, int) or trade.size < 0:
            raise ValueError("nonnegative_integer_size_required")
        if not trade.price.is_finite() or trade.price <= 0:
            raise ValueError("positive_finite_price_required")
        key = causal_key(trade.key)
        if self._last_trade is not None and key <= self._last_trade:
            raise ValueError("strict_causal_order_required")
        self._last_trade = key
        self.before(trade.key)
        if self.exit is not None or self.first is None or key <= causal_key(self.first) or trade.size < 100:
            return
        reason = None
        exit_at = trade.key
        price = trade.price
        basis = "MODELED_EXIT_AT_OBSERVED_ROUNDLOT_NOT_NATIVE"
        if self.sell_flip is not None and trade.key.event_ns >= self.sell_flip.available_ns:
            reason = SimExitReason.SELL_FLIP
        if trade.key.event_ns >= self.session_end.available_ns and (
            self.sell_flip is None or self.session_end.available_ns < self.sell_flip.available_ns
        ):
            reason = SimExitReason.SESSION_END
        if reason is None:
            vwap = self.vwap
            choice = None
            if self.same_sip_state is not None:
                if self.same_sip_state.tape.unresolved(trade):
                    self.issues.append('UNKNOWN_EXECUTION_EXIT_ELIGIBILITY')
                choice = self.same_sip_state.stop_choice(
                    trade, stop=vwap * Decimal('0.92'), target=vwap * Decimal('1.05'))
            if choice is not None:
                self._close(SimExitReason.STOP_8, choice.key, vwap * Decimal('0.92'),
                            'MODELED_THRESHOLD_PRICE_NOT_NATIVE_EXECUTION; ' + LABEL)
                return
            deadline = self.first.event_ns + TIME_NS
            if trade.key.event_ns <= deadline and trade.price >= vwap * Decimal("1.01"):
                self.hit_one_in_time = True
            self.hit_two = self.hit_two or trade.price >= vwap * Decimal("1.02")
            if trade.price >= vwap * Decimal("1.05"):
                reason, price = SimExitReason.TARGET_5, vwap * Decimal("1.05")
            elif self.hit_two and trade.price <= vwap * Decimal("0.995"):
                reason, price = SimExitReason.FLOOR_NEG05_AFTER_2, vwap * Decimal("0.995")
            elif trade.price <= vwap * Decimal("0.92"):
                reason, price = SimExitReason.STOP_8, vwap * Decimal("0.92")
            elif trade.key.event_ns >= deadline and not self.hit_one_in_time:
                reason = SimExitReason.TIME_NO_1PCT_120S
            if reason in (SimExitReason.TARGET_5, SimExitReason.STOP_8, SimExitReason.FLOOR_NEG05_AFTER_2):
                basis = "MODELED_THRESHOLD_PRICE_NOT_NATIVE_EXECUTION"
        if reason is not None:
            self._close(reason, exit_at, price, basis)

    def _close(self, reason: SimExitReason, at: EventKey, price: Decimal, basis: str, live: LiveExitFact | None = None) -> None:
        if self.exit is not None:
            raise ValueError("position_already_exited")
        if self.remainder_cancel_reason is None:
            self.remainder_cancel_reason = reason.value
        self.exit = SimExit(reason, at, price, self.quantity, basis, tuple(dict.fromkeys(self.issues)), live)

    def finish(self) -> tuple[SimExit, ...]:
        if self.fallback is not None:
            self.before(self.fallback.at)
        if self.remainder_cancel_reason is None and self.coverage.end_ns >= min(
            self.session_end.available_ns,
            self.sell_flip.available_ns if self.sell_flip is not None else self.session_end.available_ns,
        ):
            self.remainder_cancel_reason = (
                SimExitReason.SELL_FLIP.value if self.sell_flip is not None
                and self.sell_flip.available_ns <= self.session_end.available_ns else SimExitReason.SESSION_END.value
            )
        if not self.quantity:
            return ()
        if self.exit is not None:
            return (self.exit,)
        return (SimExit(
            SimExitReason.OPEN, None, None, self.quantity, "NO_EXIT_INVENTED",
            tuple(dict.fromkeys((*self.issues, "UNKNOWN_NO_QUALIFYING_EXIT_PRINT"))),
        ),)

    def receipt(self) -> PositionReceipt:
        return PositionReceipt(
            self.first.event_ns if self.first is not None else None,
            tuple(self.fills), self.vwap, self.quantity, self.remainder_cancel_reason,
            tuple(self.ignored_fills),
        )


def simulate_position(
    entries: tuple[EntryFact, ...], trades: tuple[TradePrint, ...], *,
    sell_flip: Boundary | None, session_end: Boundary, coverage: Coverage,
    live_fallback: LiveExitFact | None = None, issues: tuple[str, ...] = (),
) -> Position:
    """Replay fills and tape in shared serialization; equal-key fill precedes risk.

    Later actual fills after a SIM close remain ignored receipts, never reopening
    the SIM position or modifying the separate original LIVE evidence.
    """
    position = Position(sell_flip=sell_flip, session_end=session_end, coverage=coverage, live_fallback=live_fallback, issues=issues)
    pending = iter(entries)
    entry = next(pending, None)
    for trade in trades:
        while entry is not None and causal_key(entry.at) <= causal_key(trade.key):
            position.add_fill(entry)
            entry = next(pending, None)
        position.on_trade(trade)
    while entry is not None:
        position.add_fill(entry)
        entry = next(pending, None)
    position.finish()
    return position


def simulate_exit(
    entry: EntryFact, trades: tuple[TradePrint, ...], *, sell_flip: Boundary | None,
    session_end: Boundary, coverage: Coverage, live_fallback: LiveExitFact | None = None,
) -> SimExit:
    """Backward-compatible single-entry wrapper around the one-position engine."""
    if live_fallback is not None:
        if live_fallback.quantity < entry.quantity:
            raise ValueError("valid_live_fallback_quantity_price_required")
        if causal_key(live_fallback.at) <= causal_key(entry.at):
            raise ValueError("live_fallback_must_follow_entry")
    position = simulate_position(
        (entry,), trades, sell_flip=sell_flip, session_end=session_end,
        coverage=coverage, live_fallback=live_fallback,
    )
    exits = position.finish()
    if not exits:
        raise ValueError("entry_outside_position_window")
    return exits[0]
