"""Narrow core provenance controls; drafts/controlled ports are not native proof."""

import asyncio
import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from tests.unit.test_schwab_1m_v2_eh_stream_cross import (
    PRINT_2_RCV, PRINT_2_TS, _armed_clro, _at, _bot, _send_levelone, _settings,
)


def _dual_settings(confirm=False):
    return _settings(strategy_schwab_1m_v2_pm_print_ask_confirm_enabled=confirm,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_entry_notional_usd=600,
        strategy_schwab_1m_v2_webull_entry_notional_usd=300)


@pytest.mark.parametrize("confirm", [False, True])
def test_real_cross_retains_validated_ask_and_sizes_both_broker_drafts(monkeypatch, confirm):
    strategy = SchwabV2Strategy(_dual_settings(confirm))
    state = _armed_clro(strategy)
    _at(strategy, PRINT_2_RCV)
    seen = []
    original = strategy._eh_resting_cross_check

    def witness(state, quote, **kwargs):
        seen.append(quote.ask_price)
        return original(state, quote, **kwargs)

    monkeypatch.setattr(strategy, "_eh_resting_cross_check", witness)
    draft = strategy.on_stream_trade("CLRO", 5.5689, PRINT_2_TS,
        ask_price=5.57, bid_price=5.55, print_size=100, ask_age_ms=100)
    assert seen == [5.57]  # Neither zero, cap, line nor trade-price substitution.
    assert draft is not None and draft.quantity > 0
    assert draft.metadata["entry_notional_target_usd"] == "600"
    fanout = strategy.drain_webull_fanout_intents()
    assert len(fanout) == 1 and fanout[0].quantity > 0
    assert fanout[0].metadata["entry_notional_target_usd"] == "300"
    assert fanout[0].metadata["fanout_slot_id"] == draft.metadata["fanout_slot_id"]
    assert state.resting_flip_ms == PRINT_2_RCV
    assert state.position_qty == state.position_qty_held == 0
    assert strategy.on_stream_trade("CLRO", 5.5689, PRINT_2_TS, ask_price=5.57) is None
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("ask", [None, 0.0, float("nan"), float("inf"), 5.59, .99])
def test_flag_off_unknown_nonfinite_past_cap_subfloor_ask_never_takes_latch(ask):
    strategy = SchwabV2Strategy(_dual_settings())
    state = _armed_clro(strategy)
    _at(strategy, PRINT_2_RCV)
    assert strategy.on_stream_trade("CLRO", 5.5689, PRINT_2_TS, ask_price=ask) is None
    assert state.resting_flip_ms == 0
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("damage", ["boot_hold", "position", "line_unknown", "country_unknown"])
def test_valid_ask_does_not_bypass_other_core_admission_gates(damage):
    strategy = SchwabV2Strategy(_dual_settings())
    state = _armed_clro(strategy)
    _at(strategy, PRINT_2_RCV)
    if damage == "boot_hold":
        strategy._entries_held = True
    elif damage == "position":
        state.position_qty = 1
    elif damage == "line_unknown":
        strategy._line_restoration_enabled = True
        state.line_live_shadow = True
    else:
        strategy._country_restore_unknown = True
    assert strategy.on_stream_trade("CLRO", 5.5689, PRINT_2_TS, ask_price=5.57) is None
    assert state.resting_flip_ms == 0
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("age,emits", [(10_000, True), (10_001, False)])
async def test_real_service_cache_freshness_still_fences_flag_off(monkeypatch, age, emits):
    # _bot records the post-core draft at a CONTROLLED external publication port.
    # This is an actual parser/service/cache/core path, not native order acceptance.
    bot, written, emitted = _bot(monkeypatch,
        strategy_schwab_1m_v2_pm_print_ask_confirm_enabled=False)
    state = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV - age)
    await _send_levelone(bot, {"0": "CLRO", "2": 5.57}, PRINT_2_RCV - age)
    _at(bot.strategy, PRINT_2_RCV)
    await _send_levelone(bot,
        {"0": "CLRO", "3": 5.5689, "35": PRINT_2_TS}, PRINT_2_RCV)
    if bot._eh_stream_emit_tasks:
        await asyncio.gather(*tuple(bot._eh_stream_emit_tasks))
    assert len(emitted) == int(emits)
    assert state.resting_flip_ms == (PRINT_2_RCV if emits else 0)
    assert written and state.position_qty == state.position_qty_held == 0


def test_raw_subfloor_line_cannot_be_rescued_by_valid_ask_or_cap():
    strategy = SchwabV2Strategy(_dual_settings())
    state = _armed_clro(strategy)
    _at(strategy, PRINT_2_RCV)
    state.resting_level = .999
    state.resting_trigger = 1.004
    state.resting_wire_cap = 1.01
    assert strategy.on_stream_trade("CLRO", 1.005, PRINT_2_TS, ask_price=1.005) is None
    assert state.resting_flip_ms == 0
    assert strategy.drain_webull_fanout_intents() == []
