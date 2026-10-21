"""Actual 556 dataclass/owner producers on the GAP-RESUME caller, all controlled."""
from dataclasses import fields, replace

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import SymbolState
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook
from project_mai_tai.v2_restart_rest_store import checkpoint_state
from tests.unit.test_slotcap_deferred_same_sell import advance, deferred
from tests.unit.test_v2_flip_owned_first_entry import _book, _leg, PRIMARY, WEBULL


def real_state_at_resume():
    strategy, original, clock, bar, identities, owners = deferred()
    # A new dataclass object copies ONLY declared production fields. Any dynamic
    # test-only attribute from a fixture is necessarily absent on this caller.
    state = replace(original)
    assert set(vars(state)) == {field.name for field in fields(SymbolState)}
    strategy._symbol_states[state.symbol] = state
    advance(strategy, state, clock, bar)
    return strategy, state, clock, bar, identities, owners


def test_actual_completed_bar_gap_resume_uses_declared_state_without_dispatch():
    strategy, state, _, bar, _, _ = real_state_at_resume()
    strategy._resting_entry_enabled = False  # Isolate cap release from order placement.
    observations = list(strategy.pending_atr_sell_observations())
    strategy._evaluate_completed_bar(
        state, is_new_bar=True, restored=True, restored_signal=None)
    assert not state.gap_hold_active
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.slotclear_last_fresh_sell_bar_ms == bar.timestamp_ms
    assert state.slotclear_deferred_sell is None
    assert list(strategy.pending_atr_sell_observations()) == observations
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
def test_real_unowned_managed_leg_producer_sets_unknown_and_resume_stays_held(account):
    strategy, state, clock, _, _, _ = real_state_at_resume()
    _book(strategy, clock, state.symbol, _leg(account, "unowned", entered_ms=clock[0]))
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_evidence_readable
    assert state.flip_owner_open_positions[account].managed_row_id == "unowned"
    assert strategy._maybe_resume_gap_hold(state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.slotclear_last_fresh_sell_bar_ms == 0


def test_service_missing_owner_store_does_not_become_readable_from_fresh_empty_book():
    strategy, state, clock, _, _, _ = real_state_at_resume()
    bot = object.__new__(SchwabV2BotService)
    bot.settings, bot.strategy, bot.session_factory = strategy.settings, strategy, None
    bot._configure_flip_entry_ownership_store({})
    assert not strategy._flip_owner_restore_readable
    _book(strategy, clock, state.symbol)
    assert state.flip_owner_evidence_readable and not state.flip_owner_open_positions
    assert strategy._maybe_resume_gap_hold(state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.slotclear_last_fresh_sell_bar_ms == 0


def test_active_durable_identity_without_owner_record_is_actual_unknown_not_idle():
    strategy, state, clock, _, _, _ = real_state_at_resume()
    strategy.configure_flip_entry_ownership(
        lambda *args: None, active_segments={state.symbol: 123}, restored={},
        restore_readable=True, retry_budget_persist=lambda *args: None)
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == 123
    _book(strategy, clock, state.symbol)
    assert strategy._maybe_resume_gap_hold(state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.flip_owner_phase == "unknown"


def test_actual_unreadable_position_book_keeps_cap_even_when_phase_is_idle():
    strategy, state, clock, _, _, _ = real_state_at_resume()
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], False, {}))
    assert state.flip_owner_phase == "idle"
    assert not state.flip_owner_evidence_readable
    assert strategy._maybe_resume_gap_hold(state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.slotclear_last_fresh_sell_bar_ms == 0


def test_real_native_restart_unknown_legs_retains_pending_with_fresh_flat_owner_book():
    strategy, state, clock, _, _, _ = real_state_at_resume()
    state.flip_owner_phase = "resting"
    state.flip_owner_opportunity_id = state.fanout_segment_id = 123
    state.resting_active = state.resting_is_broker_order = True
    state.resting_level = 1.0
    saved = checkpoint_state(state)
    state.resting_active = state.webull_resting_active = False
    assert not strategy.restore_restart_rest(state, saved, {})
    assert state.restart_rest_evidence_pending
    assert state.restart_rest_cancel_fields
    # A flat readable position book cannot settle unknown native order legs.
    _book(strategy, clock, state.symbol)
    assert strategy._maybe_resume_gap_hold(state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.restart_rest_evidence_pending
    assert state.slotclear_last_fresh_sell_bar_ms == 0
