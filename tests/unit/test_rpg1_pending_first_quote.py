"""First-entry quote waits: recorded AMOD hold plus explicit counterfactual transitions.

AMOD's timestamp/age/trail/volume below are recorded 2026-10-02 facts. Subsequent
quotes are controlled inputs, not claims about unrecorded broker responses.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy


AT = int(datetime(2026, 10, 2, 13, 38, 2, 672000,
                  tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)


def setup_wait(monkeypatch, **overrides):
    options = dict(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_quote_max_age_ms=10_000,
        strategy_schwab_1m_v2_cw_v2_resting_entry_reprice_pct=0.5,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=False,
        strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct=0.5,
    )
    options.update(overrides)
    strategy = SchwabV2Strategy(Settings(**options))
    clock = [AT]
    monkeypatch.setattr(strategy, "_now_ms", lambda: clock[0])
    monkeypatch.setattr(strategy, "_resting_in_window", lambda now=None: True)
    monkeypatch.setattr(strategy, "_resting_session_is_eh", lambda now=None: False)
    state = strategy.watchlist_state("AMOD")
    state.atr_state = "short"
    state.atr_state_age = 31
    state.atr_trail = 3.360737
    state.atr_short_flip_bar_ts = AT - 31 * 60_000
    state.bars.append(OHLCVBar(
        timestamp_ms=AT - 62_672, open=3.25, high=3.27, low=3.25,
        close=3.2592, volume=40151,
    ))
    state.last_quote = Quote("AMOD", 3.25, 3.26, 3.2592, AT - 12_325)
    strategy._cw_v2_resting_track(state, None)
    assert strategy.drain_pending_intents() == []
    return strategy, state, clock


def fresh(strategy, clock, *, ask=3.26, age=0):
    clock[0] += 1000
    strategy.on_quote("AMOD", Quote("AMOD", ask - .01, ask, ask, clock[0] - age))
    return strategy.drain_pending_intents()


def test_amod_stale_first_entry_resumes_on_quote_without_another_bar(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    bar_count = len(state.bars)
    placed = fresh(strategy, clock)
    assert len(placed) == 1
    assert placed[0].intent_type == "open"
    assert placed[0].metadata["cw_entry_slot"] == "first"
    assert len(state.bars) == bar_count
    assert fresh(strategy, clock) == []


@pytest.mark.parametrize("age", [10_001, 42_018, 7 * 60 * 60 * 1000])
def test_repeated_old_quote_never_becomes_fresh_on_receipt(monkeypatch, age):
    strategy, state, clock = setup_wait(monkeypatch)
    assert fresh(strategy, clock, age=age) == []
    assert not state.resting_active


def test_trigger_at_ask_keeps_waiting_then_recovers_on_quote(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    assert fresh(strategy, clock, ask=3.40) == []
    assert not state.resting_active
    assert len(fresh(strategy, clock, ask=3.26)) == 1


@pytest.mark.parametrize("invalidate", ["flip", "segment", "floor", "window", "stale_bar"])
def test_invalidated_pending_first_entry_cannot_place(monkeypatch, invalidate, caplog):
    strategy, state, clock = setup_wait(monkeypatch)
    if invalidate == "flip":
        state.atr_state = "long"
    elif invalidate == "segment":
        state.atr_short_flip_bar_ts += 60_000
    elif invalidate == "floor":
        state.bars[-1].volume = 0
    elif invalidate == "window":
        monkeypatch.setattr(strategy, "_resting_in_window", lambda now=None: False)
    else:
        clock[0] += strategy._resting_max_bar_age_ms
    with caplog.at_level("INFO"):
        assert fresh(strategy, clock) == []
        assert fresh(strategy, clock) == []
    drops = [r.message for r in caplog.records if "action=gave_up" in r.message]
    assert len(drops) == 1


def test_watch_removal_drops_pending_without_resurrection(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    strategy.release_and_drop_symbol("AMOD")
    assert fresh(strategy, clock) == []


def test_rederived_line_places_only_latest_first_order(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    state.atr_trail = 3.30
    strategy._cw_v2_resting_track(state, None)
    placed = fresh(strategy, clock)
    assert len(placed) == 1
    assert placed[0].metadata["cw_flip_level"] == "3.3000"
    assert fresh(strategy, clock) == []


@pytest.mark.parametrize("fraction,changed", [(0.003, False), (0.0051, True)])
def test_pending_line_keeps_the_half_percent_reprice_boundary(monkeypatch, fraction, changed):
    strategy, state, clock = setup_wait(monkeypatch)
    original = state.atr_trail
    state.atr_trail *= 1 - fraction
    strategy._cw_v2_resting_track(state, None)
    placed = fresh(strategy, clock)
    expected = state.atr_trail if changed else original
    assert len(placed) == 1
    assert placed[0].metadata["cw_flip_level"] == f"{expected:.4f}"


def test_reclaim_ownership_blocks_pending_first_entry(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    state.resting_active = True
    state.resting_slot = "reclaim"
    assert fresh(strategy, clock) == []
    assert state.resting_slot == "reclaim"


@pytest.mark.parametrize("owner", ["resting", "held", "consumed"])
def test_occupied_first_slot_discards_wait_not_merely_blocks_order(monkeypatch, owner, caplog):
    strategy, state, clock = setup_wait(monkeypatch)
    if owner == "resting":
        state.resting_active = True
        state.resting_slot = "first"
    elif owner == "held":
        state.position_qty_held = 1
    else:
        state.cw_resting_taken = True
    with caplog.at_level("INFO"):
        assert fresh(strategy, clock) == []
    assert "AMOD" not in strategy._pending_first_rest_quotes
    assert sum("action=gave_up reason=slot_owned_or_consumed" in r.message
               for r in caplog.records) == 1
    state.resting_active = False
    state.position_qty_held = 0
    state.cw_resting_taken = False
    assert fresh(strategy, clock) == []  # A later bar must derive a new opportunity.


@pytest.mark.parametrize("hold", ["boot", "gap"])
def test_entry_hold_discards_wait_without_resuming_when_hold_clears(monkeypatch, hold, caplog):
    strategy, state, clock = setup_wait(monkeypatch)
    if hold == "boot":
        strategy._entries_held = True
    else:
        strategy._gap_hold_enabled = True
        state.gap_hold_active = True
    with caplog.at_level("INFO"):
        assert fresh(strategy, clock) == []
    assert "AMOD" not in strategy._pending_first_rest_quotes
    assert sum("action=gave_up reason=entry_or_gap_hold" in r.message
               for r in caplog.records) == 1
    strategy._entries_held = False
    state.gap_hold_active = False
    assert fresh(strategy, clock) == []


def test_watch_removal_discards_wait_even_when_flip_owner_state_is_retained(monkeypatch, caplog):
    strategy, state, clock = setup_wait(monkeypatch)
    strategy._flip_owned_first_entry_enabled = True
    state.flip_owner_phase = "provisional"
    state.position_qty = 1
    with caplog.at_level("INFO"):
        strategy.release_and_drop_symbol("AMOD")
        strategy.release_and_drop_symbol("AMOD")
    assert strategy.watchlist_state("AMOD") is state
    assert state.flip_owner_phase == "unknown"
    assert "AMOD" not in strategy._pending_first_rest_quotes
    assert sum("action=gave_up reason=watchlist-removed" in r.message
               for r in caplog.records) == 1
    assert fresh(strategy, clock) == []


def test_pending_first_entry_is_not_restored_into_a_new_strategy(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    restarted = SchwabV2Strategy(strategy.settings)
    monkeypatch.setattr(restarted, "_now_ms", lambda: clock[0])
    assert fresh(restarted, clock) == []


def test_ten_second_edge_is_preserved(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    assert len(fresh(strategy, clock, age=10_000)) == 1


@pytest.mark.parametrize("ask", [0.0, float("nan"), float("inf")])
def test_pending_never_uses_a_missing_or_invalid_price(monkeypatch, ask):
    strategy, state, clock = setup_wait(monkeypatch)
    assert fresh(strategy, clock, ask=ask) == []
    assert not state.resting_active


def test_missing_timestamp_stays_pending_until_a_timestamped_quote(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    strategy.on_quote("AMOD", Quote("AMOD", 3.25, 3.26, 3.26, 0))
    assert strategy.drain_pending_intents() == []
    assert len(fresh(strategy, clock)) == 1


def test_configured_1545_cutoff_drops_pending(monkeypatch):
    strategy, state, clock = setup_wait(
        monkeypatch, strategy_schwab_1m_v2_entry_window_end_hour_et=15,
        strategy_schwab_1m_v2_entry_window_end_minute_et=45,
    )
    clock[0] = int(datetime(2026, 10, 2, 15, 45,
                          tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    state.bars[-1].timestamp_ms = clock[0] - 1000
    assert fresh(strategy, clock) == []
    assert not strategy._pending_first_rest_quotes


def test_clock_close_sweep_clears_pending_even_without_quotes(monkeypatch, caplog):
    strategy, state, clock = setup_wait(monkeypatch)
    with caplog.at_level("INFO"):
        strategy.release_entry_state_at_window_close()
        strategy.release_entry_state_at_window_close()
    assert not strategy._pending_first_rest_quotes
    assert sum("action=gave_up" in r.message for r in caplog.records) == 1


def test_reclaim_stale_check_does_not_create_a_quote_wait(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    strategy._pending_first_rest_quotes.clear()
    assert not strategy._resting_stop_ask_allows(state, 3.36, slot="reclaim")
    assert strategy._pending_first_rest_quotes == {}
    assert fresh(strategy, clock) == []


def test_premarket_soft_rest_is_not_converted_to_a_pending_broker_order(monkeypatch):
    strategy, state, clock = setup_wait(monkeypatch)
    strategy._pending_first_rest_quotes.clear()
    strategy._eh_resting_enabled = True
    monkeypatch.setattr(strategy, "_resting_session_is_eh", lambda now=None: True)
    strategy._cw_v2_resting_track(state, None)
    assert state.resting_active
    assert not state.resting_is_broker_order
    assert strategy.drain_pending_intents() == []
    assert strategy._pending_first_rest_quotes == {}


@pytest.mark.parametrize("budget", ["unknown", "exhausted", "wrong_segment"])
def test_quote_resume_cannot_bypass_retry_one(monkeypatch, budget):
    strategy, state, clock = setup_wait(monkeypatch)
    strategy._flip_owned_first_entry_enabled = True
    strategy._flip_owner_restore_readable = True
    strategy._retry_one_enabled = True
    state.retry_one_segment_id = state.atr_short_flip_bar_ts
    state.flip_owner_evidence_readable = True
    state.flip_owner_evidence_at_ms = clock[0]
    if budget == "unknown":
        state.retry_one_budget_readable = False
    elif budget == "exhausted":
        state.retry_one_closes_in_segment = 2
    else:
        state.retry_one_segment_id -= 60_000
    assert fresh(strategy, clock) == []
    assert not state.resting_active


@pytest.mark.asyncio
async def test_quote_callback_emits_both_legs_without_waiting_for_bar(monkeypatch):
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService

    strategy, state, clock = setup_wait(
        monkeypatch,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
    )
    bot = SimpleNamespace(
        strategy=strategy, _last_tick_at={}, _last_quote_at_ms={},
        _last_quote_by_symbol={}, _gap_hold_enabled=False,
        _observe_halt_from_quote=lambda *_: None,
        _maybe_emit=AsyncMock(), _emit_webull_fanout_legs=AsyncMock(),
        intent_emitter=SimpleNamespace(emit=AsyncMock()),
        webull_intent_emitter=SimpleNamespace(emit=AsyncMock()),
    )
    bot._drain_direct_strategy_intents = lambda: SchwabV2BotService._drain_direct_strategy_intents(bot)
    bot._observe_line_trade = lambda *args: SchwabV2BotService._observe_line_trade(bot, *args)
    clock[0] += 1000
    quote = Quote("AMOD", 3.25, 3.26, 3.26, clock[0])
    await SchwabV2BotService._handle_quote(bot, "AMOD", quote)
    await SchwabV2BotService._handle_quote(bot, "AMOD", quote)
    bot.intent_emitter.emit.assert_awaited_once()
    bot.webull_intent_emitter.emit.assert_awaited_once()
    primary = bot.intent_emitter.emit.await_args.args[0]
    mirror = bot.webull_intent_emitter.emit.await_args.args[0]
    assert primary.metadata["fanout_slot_id"] == mirror.metadata["fanout_slot_id"]
    assert primary.metadata["fanout_segment_id"] == mirror.metadata["fanout_segment_id"]
