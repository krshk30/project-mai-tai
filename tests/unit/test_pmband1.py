"""Recorded Oct12 cap/ask facts; arm freshness and later callbacks are controlled."""

import logging
from decimal import Decimal

import pytest

from test_pmprint1_pmflip1 import armed, cross, ms

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.line_restore_acceptance_factory import make_line_restore_case


CASES = [
    ("MNDR", "2026-10-12T11:55:06.817Z", 2.40, 2.44, 2.41),
    ("YMAT", "2026-10-12T12:48:28.839Z", 2.79, 2.83, 2.80),
]


@pytest.mark.parametrize("symbol,at,trigger,ask,cap", CASES)
def test_recorded_rest_past_cap_keeps_latch_and_both_legs_clear(symbol, at, trigger, ask, cap, caplog):
    strategy, state, clock = armed((symbol, at, trigger, ask, ask))
    state.resting_wire_cap = cap
    with caplog.at_level(logging.INFO):
        assert cross(strategy, state, clock, ask, ask, stream=False) is None
    assert state.resting_active and state.resting_flip_ms == 0
    assert state.last_entry_price is None
    assert strategy.drain_webull_fanout_intents() == []
    assert "reason=ask_past_band" in caplog.text
    assert "source=rest_quote latch_taken=0" in caplog.text


@pytest.mark.parametrize("stream", [False, True])
def test_controlled_later_in_band_callback_emits_once_on_both_accounts(stream):
    strategy, state, clock = armed(CASES[0][:4] + (2.44,))
    state.resting_wire_cap = 2.41
    assert cross(strategy, state, clock, 2.44, 2.44, stream=False) is None
    clock[0] += 1000
    primary = cross(strategy, state, clock, 2.41, 2.41, stream=stream)
    mirror, = strategy.drain_webull_fanout_intents()
    assert primary is not None and primary.side == mirror.side == "buy"
    assert Decimal(primary.metadata["pm_confirming_ask"]) == Decimal("2.41")
    assert Decimal(mirror.metadata["pm_confirming_ask"]) == Decimal("2.41")
    assert cross(strategy, state, clock, 2.41, 2.41, stream=not stream) is None
    assert strategy.drain_webull_fanout_intents() == []


def test_recorded_ymat_exact_wire_cap_rest_still_crosses():
    strategy, state, clock = armed(("YMAT", "2026-10-12T11:06:06.527Z", 3.30, 3.32, 3.32))
    state.resting_wire_cap = 3.32
    assert cross(strategy, state, clock, 3.32, 3.32, stream=False) is not None
    assert len(strategy.drain_webull_fanout_intents()) == 1


def test_mndr_recorded_later_quote_returns_to_band_and_crosses():
    strategy, state, clock = armed(CASES[0][:4] + (2.44,))
    state.resting_wire_cap = 2.41
    assert cross(strategy, state, clock, 2.44, 2.44, stream=False) is None
    # Read-only market_quote_ticks: original event and receive clocks retained.
    # Arm state and offering this captured quote to the callback are controlled.
    event_ms = ms("2026-10-12T11:55:09.285Z")
    clock[0] = ms("2026-10-12T11:55:11.309213Z")
    draft = strategy.on_quote("MNDR", Quote("MNDR", 2.42, 2.41, 2.41, event_ms))
    assert draft is not None
    mirror, = strategy.drain_webull_fanout_intents()
    assert draft.side == mirror.side == "buy"
    assert draft.metadata["pm_confirming_ask_age_ms"] == "2024"
    assert Decimal(draft.metadata["pm_confirming_ask"]) == Decimal("2.41")
    assert state.resting_flip_ms == clock[0]


def test_recorded_lnks_stream_cross_unchanged():
    strategy, state, clock = armed(("LNKS", "2026-10-12T11:02:54.501Z", 1.53, 1.53, 1.53))
    state.resting_wire_cap = 1.54
    assert cross(strategy, state, clock, 1.53, 1.53, stream=True) is not None
    assert len(strategy.drain_webull_fanout_intents()) == 1


def test_exact_decimal_band_without_wire_cap_admits_boundary():
    strategy, state, clock = armed(("MNDR", "2026-10-12T11:55:06.817Z", 2.6, 2.613, 2.613))
    assert state.resting_wire_cap == 0
    assert cross(strategy, state, clock, 2.613, 2.613, stream=False) is not None


def test_regular_hours_eh_cross_unchanged_and_no_latch():
    strategy, state, clock = armed(("MNDR", "2026-10-12T14:55:06.817Z", 2.4, 2.44, 2.44))
    assert strategy._eh_resting_cross_check(state, state.last_quote) is None
    assert state.resting_flip_ms == 0


def test_all_on_native_readiness_rest_skip_and_later_cross():
    flags = {
        "strategy_schwab_1m_v2_line_chart_restoration_enabled": True,
        "strategy_schwab_1m_v2_gap_line_carry_enabled": True,
        "strategy_schwab_1m_v2_false_flip_enabled": True,
        "strategy_schwab_1m_v2_slotclear_fresh_flip_enabled": True,
        "strategy_schwab_1m_v2_slotclear_fresh_sell_enabled": True,
        "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": False,
        "strategy_schwab_1m_v2_flip_owned_first_entry_enabled": True,
        "strategy_schwab_1m_v2_retry_one_enabled": True,
        "strategy_schwab_1m_v2_retry_one_max_retries": 0,
        "oms_v2_webull_mirror_retained_hold_enabled": True,
        "strategy_schwab_1m_v2_pm_print_ask_confirm_enabled": True,
        "strategy_schwab_1m_v2_dual_broker_fanout_enabled": True,
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": True,
    }
    settings = Settings(_env_file=None, **flags)
    now = ms(CASES[0][1])
    case = make_line_restore_case(symbol="MNDR", now_ms=now, settings_overrides={
        key: getattr(settings, key) for key in Settings.model_fields
    })
    strategy = case.strategy
    strategy.configure_falseflip({}, readable=True)
    state = strategy.watchlist_state("MNDR")
    # Controlled already-warmed soft rest, using the real bot readiness predicate.
    strategy._entries_held = False
    state.line_live_fallback = True
    state.atr_state, state.atr_trail = "short", 2.38
    state.resting_active = True
    state.resting_is_broker_order = False
    state.resting_trigger, state.resting_level, state.resting_wire_cap = 2.4, 2.38, 2.41
    state.bars.append(OHLCVBar(now // 60000 * 60000 - 60000, 2.38, 2.4, 2.37, 2.38, 17480))
    with case.clock():
        assert strategy._line_readiness == case.bot._line_buy_ready
        assert strategy.line_buy_ready("MNDR")
        assert strategy.on_quote("MNDR", Quote("MNDR", 2.43, 2.44, 2.44, now)) is None
        assert state.resting_flip_ms == 0
        assert strategy.drain_webull_fanout_intents() == []
    with case.clock(ms("2026-10-12T11:55:11.309213Z")):
        assert strategy.line_buy_ready("MNDR")
        draft = strategy.on_quote("MNDR", Quote("MNDR", 2.42, 2.41, 2.41, ms("2026-10-12T11:55:09.285Z")))
        assert draft is not None
        assert len(strategy.drain_webull_fanout_intents()) == 1
    for key, value in flags.items():
        assert getattr(strategy.settings, key) == value
