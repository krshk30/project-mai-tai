"""Unresolved restore provenance must survive flat-book recovery and boundaries."""
import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipRecord
from tests.unit.test_slotcap_real_owner_state import real_state_at_resume
from tests.unit.test_v2_flip_owned_first_entry import _book


def pending_restore():
    strategy, state, clock, bar, identities, owners = real_state_at_resume()
    strategy.configure_flip_entry_ownership(
        lambda *args: None, active_segments={state.symbol: 123}, restored={},
        restore_readable=True, retry_budget_persist=lambda *args: None)
    assert state.flip_owner_phase == "unknown" and state.flip_owner_opportunity_id == 0
    assert state.symbol in strategy._restored_flip_owners
    assert strategy._restored_flip_owners[state.symbol] is None
    return strategy, state, clock, bar, identities, owners


@pytest.mark.parametrize("boundary", ["repeated_flat", "unrelated_terminal", "live_sell", "session_roll"])
def test_unresolved_restore_keeps_unknown_and_both_slots_across_real_owner_callers(boundary):
    strategy, state, clock, _, identities, owners = pending_restore()
    writes = (len(identities), len(owners))
    if boundary == "repeated_flat":
        for _ in range(3):
            clock[0] += 1000
            _book(strategy, clock, state.symbol)
    elif boundary == "unrelated_terminal":
        _book(strategy, clock, state.symbol, terminal_unfilled_opportunities=frozenset({123}))
    elif boundary == "live_sell":
        _book(strategy, clock, state.symbol)
        strategy._end_flip_owner_on_sell(state)
    else:
        clock[0] += 86400000
        _book(strategy, clock, state.symbol)
        strategy._apply_session_anchor_reset(
            state, session_start_ts_ms(clock[0]), owner_boundary_is_current=True)
    assert strategy._maybe_resume_gap_hold(state)
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == state.fanout_segment_id == 0
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.slotclear_last_fresh_sell_bar_ms == 0
    assert state.symbol in strategy._restored_flip_owners
    assert (len(identities), len(owners)) == writes
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []


def test_benign_empty_unknown_without_pending_restore_still_uses_existing_release():
    strategy, state, clock, bar, _, _ = real_state_at_resume()
    assert state.symbol not in strategy._restored_flip_owners
    strategy._set_flip_owner_unknown(state, reason="controlled_ownerless_unknown", persist=False)
    _book(strategy, clock, state.symbol)
    assert state.flip_owner_phase == "idle"
    assert strategy._maybe_resume_gap_hold(state)
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.slotclear_last_fresh_sell_bar_ms == bar.timestamp_ms


def test_explicit_valid_restore_consumes_pending_marker_and_keeps_actual_durable_owner():
    strategy, state, _, _, _, _ = pending_restore()
    strategy.configure_fanout_identity_persistence(
        lambda *args: None, restored={state.symbol: 123})
    record = FlipEntryOwnershipRecord(
        symbol=state.symbol, opportunity_id=123, phase="resting", flip_bar_ts=0,
        provisional_started_ms=0, fill_accounts=(), position_ids={}, position_entry_ms={})
    strategy.configure_flip_entry_ownership(
        lambda *args: None, active_segments={state.symbol: 123}, restored={state.symbol: record},
        restore_readable=True, retry_budget_persist=lambda *args: None)
    assert state.symbol not in strategy._restored_flip_owners
    assert state.flip_owner_phase == "resting" and state.flip_owner_opportunity_id == 123
    assert strategy._maybe_resume_gap_hold(state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.flip_owner_opportunity_id == 123
