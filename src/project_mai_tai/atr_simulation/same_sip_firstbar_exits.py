"""C1 2026-10-28 explicit TODAY tie successor. Original sealed module unchanged."""
"""Additive causal FIRSTBAR exit policy; never changes VOLBODY's Position.

Uses the existing pure position's VWAP, first-fill clock, boundary and actual
unmodeled-LIVE fallback receipts. C1 21:29: target uses exact A2 CEILING leaf;
stop threshold triggers execution at that first eligible round-lot print, not
at an invented threshold price. Neither is native execution/queue proof.
"""

from decimal import Decimal

from .same_sip_eligibility import execution_status, note_unknown
from .same_sip_exits import Position, TIME_NS, causal_key
from .same_sip_firstbar import FirstBarMode
from .firstbar_target_rule import load_target_rule
from .same_sip_types import SimExitReason, TradePrint
from .same_sip import LABEL


class FirstBarPosition(Position):
    """Same position lifecycle, no X4, optional X3. No storage or live hooks."""

    def __init__(self, *, mode: FirstBarMode, **position_args) -> None:
        if not isinstance(mode, FirstBarMode):
            raise ValueError("explicit_firstbar_mode_required")
        super().__init__(**position_args)
        self.mode = mode
        self._execution_uncertain = False
        self._target_limit_price = load_target_rule()

    @property
    def can_fill(self) -> bool:
        return super().can_fill and not self._execution_uncertain

    def on_trade(self, trade: TradePrint) -> None:
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
        if not eligible:
            return
        reason = None
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
                    note_unknown(self.issues, 'UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY')
                    self._execution_uncertain = True
                choice = self.same_sip_state.stop_choice(trade, stop=vwap * Decimal('0.92'),
                    target=self._target_limit_price(vwap * Decimal('1.05')))
            if choice is not None:
                self._close(SimExitReason.STOP_8, choice.key, choice.price,
                            'MODELED_FIRST_ELIGIBLE_ROUNDLOT_AT_OR_AFTER_STOP_TRIGGER_NOT_NATIVE; ' + LABEL)
                return
            deadline = self.first.event_ns + TIME_NS
            if trade.key.event_ns <= deadline and trade.price >= vwap * Decimal("1.01"):
                self.hit_one_in_time = True
            target = self._target_limit_price(vwap * Decimal("1.05"))
            if trade.price >= target:
                reason, price = SimExitReason.TARGET_5, target
            elif trade.price <= vwap * Decimal("0.92"):
                reason, price = SimExitReason.STOP_8, trade.price
            elif (self.mode == FirstBarMode.FIRSTBAR_X3
                    and trade.key.event_ns >= deadline and not self.hit_one_in_time):
                reason = SimExitReason.TIME_NO_1PCT_120S
            if reason is SimExitReason.TARGET_5:
                basis = "MODELED_A2_CEILING_TARGET_LIMIT_NOT_NATIVE_FILL"
            elif reason is SimExitReason.STOP_8:
                basis = "MODELED_FIRST_ELIGIBLE_ROUNDLOT_AT_OR_AFTER_STOP_TRIGGER_NOT_NATIVE"
        if reason is not None:
            self._close(reason, trade.key, price, basis)
