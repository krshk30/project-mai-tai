"""Real tick callbacks must not round or spend a false-flip cross without a line."""

from copy import deepcopy

import pytest

from project_mai_tai.falseflip1_runtime import FalseBudget
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import Quote, SchwabV2Strategy


@pytest.mark.parametrize("trail", [None, 0.0, -1.0, float("nan"), float("inf"), -float("inf")],
                         ids=["missing", "zero", "negative", "nan", "positive-infinity", "negative-infinity"])
@pytest.mark.parametrize("callback", ["quote", "stream", "cross"])
def test_invalid_atr_trail_bypasses_falseflip_before_trigger_without_spending(callback, trail, monkeypatch):
    strategy = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_false_flip_enabled=True,
        strategy_schwab_1m_v2_resting_buy_round_up_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True,
    ))
    now = 1791498000000
    monkeypatch.setattr(strategy, "_now_ms", lambda: now)
    state = strategy.watchlist_state("GRAN")
    state.atr_trail = trail
    state.retry_one_segment_id = now - 600000
    budget = FalseBudget(state.symbol, state.retry_one_segment_id).closed(1, 1).closed(2, 2)
    strategy.configure_falseflip({(budget.symbol, budget.segment): budget}, readable=True)
    quote = Quote(state.symbol, 2.0, 2.01, 2.01, now, now)
    # Normal on_quote freshness caching is intentionally unchanged by this fix.
    state.last_quote = quote
    before_state = repr(state)
    before_budget = deepcopy(strategy._falseflip_budgets)
    before_pending = deepcopy(strategy._falseflip_pending)
    before_cross = set(strategy._falseflip_cross_high)

    def forbidden_trigger(_line):
        pytest.fail("invalid ATR trail reached trigger computation before callback guard")

    monkeypatch.setattr(strategy, "_resting_trigger_for_line", forbidden_trigger)
    if callback == "quote":
        assert strategy.on_quote(state.symbol, quote) is None
    elif callback == "stream":
        assert strategy.on_stream_trade(state.symbol, 2.01, now, ask_price=2.01, ask_age_ms=0) is None
    else:
        assert strategy._falseflip_cross(state, 2.01, confirming=True) is False

    assert repr(state) == before_state
    assert strategy._falseflip_budgets == before_budget
    assert strategy._falseflip_pending == before_pending
    assert strategy._falseflip_cross_high == before_cross
    assert not strategy.pending_falseflip_budgets()
    assert not strategy.pending_falseflip_cancel_publications()
    assert not strategy.drain_pending_intents()
    assert not strategy.drain_webull_direct_intents()
    assert not strategy.drain_webull_fanout_intents()
