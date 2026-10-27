"""Controlled next-day/missed-SELL transitions, not actual historical native books."""

from dataclasses import replace

import pytest

from project_mai_tai.brokerdecides1_entry import recover_unused_current_short, recover_unused_at_buy, unused_owner_at_sell_definite
from tests.unit.test_brokerdecides1_unused_owner_sell import case, NOW, SEGMENT, OPPORTUNITY


def ready_case():
    strategy, state, writes = case(phase="unknown")
    strategy._entries_held = False
    strategy._bar_observation_phase = "live"
    return strategy, state, writes


def refresh(strategy, now):
    strategy._now_ms = lambda: now
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    memory.apply(replace(snapshot, generation=snapshot.generation + 1, started_ms=now - 200,
        completed_ms=now - 10, books=tuple(replace(book, wire_started_ms=now - 100,
            wire_completed_ms=now - 20) for book in snapshot.books)))


@pytest.mark.parametrize("days", [1, 3])
def test_prior_day_unused_identity_does_not_gate_current_clean_short(days):
    strategy, state, writes = ready_case()
    old = OPPORTUNITY - days * 86400000
    state.fanout_segment_id = state.flip_owner_opportunity_id = old
    retry = (state.retry_one_segment_id, state.retry_one_closes_in_segment, state.retry_one_budget_readable)
    assert recover_unused_current_short(strategy, state)
    assert [(kind, identity, active) for kind, identity, active, _ in writes] == [("identity", old, False), ("owner", old, False)]
    assert state.flip_owner_phase == "idle" and state.flip_owner_opportunity_id == 0
    assert retry == (state.retry_one_segment_id, state.retry_one_closes_in_segment, state.retry_one_budget_readable)


def test_missed_sell_can_recover_on_later_live_short_bar_without_new_fill_or_order():
    strategy, state, writes = ready_case()
    now = NOW + 120000
    refresh(strategy, now)
    state.bars[-1] = replace(state.bars[-1], timestamp_ms=SEGMENT + 60000)
    assert unused_owner_at_sell_definite(strategy, state)
    assert recover_unused_current_short(strategy, state)
    assert len(writes) == 2 and state.flip_owner_phase == "idle"


def buy_case():
    strategy, state, writes = ready_case()
    refresh(strategy, NOW + 120000)
    state.bars[-1] = replace(state.bars[-1], timestamp_ms=SEGMENT + 60000)
    state.atr_state = "long"
    state.atr_short_flip_bar_ts = 0  # Original ATR callback already clears it.
    return strategy, state, writes, dict(flip="BUY", observation_phase="live", decision_short_segment_id=SEGMENT)


def test_actual_causal_short_argument_survives_new_buy_retirement_without_retry_reset():
    strategy, state, writes, signal = buy_case()
    state.retry_one_budget_readable = False
    state.retry_one_segment_id = SEGMENT - 600000
    state.retry_one_closes_in_segment = 2
    strategy._retry_one_enabled = True
    retry = (state.retry_one_budget_readable, state.retry_one_segment_id, state.retry_one_closes_in_segment)
    assert recover_unused_at_buy(strategy, state, signal)
    assert state.flip_owner_phase == "idle" and len(writes) == 2
    assert state.slotclear_fresh_buy_segment_id == SEGMENT
    assert state.slotclear_fresh_buy_bar_ms == SEGMENT + 60000
    assert retry == (state.retry_one_budget_readable, state.retry_one_segment_id, state.retry_one_closes_in_segment)
    assert not strategy._strict_first_rest_admitted(state, slot="first")  # Retry unreadable stays a HARD guard.


@pytest.mark.parametrize("fault", ["missing_short", "future_short", "old_day_short", "replay", "held_boot", "gap", "line_unknown", "real_fill"])
def test_new_buy_recovery_does_not_replace_line_boot_or_current_fill_proof(fault):
    strategy, state, writes, signal = buy_case()
    if fault == "missing_short":
        signal.pop("decision_short_segment_id")
    elif fault == "future_short":
        signal["decision_short_segment_id"] += 600000
    elif fault == "old_day_short":
        signal["decision_short_segment_id"] -= 86400000
    elif fault == "replay":
        signal["observation_phase"] = "replay"
    elif fault == "held_boot":
        strategy._entries_held = True
    elif fault == "gap":
        strategy._gap_hold_enabled = True
        state.gap_hold_active = True
    elif fault == "line_unknown":
        strategy._line_restoration_enabled = True
        strategy._line_readiness = None
    else:
        memory = strategy._brokerdecides1_memory
        memory.apply(replace(memory.snapshot, generation=3, filled_segments=frozenset({("YMAT", SEGMENT)})))
    assert not recover_unused_at_buy(strategy, state, signal)
    assert writes == [] and state.flip_owner_opportunity_id == OPPORTUNITY


def test_cross04_publication_cannot_clear_old_owner_from_partial_session_fill_scope():
    strategy, state, writes = ready_case()
    anchor = 1792742400000  # 2026-10-23 08:00Z, literal controlled04ET boundary.
    strategy._now_ms = lambda: anchor + 1000
    state.atr_short_flip_bar_ts = anchor
    state.bars[-1] = replace(state.bars[-1], timestamp_ms=anchor)
    state.fanout_segment_id = state.flip_owner_opportunity_id = anchor - 86400000
    memory = strategy._brokerdecides1_memory
    memory.apply(replace(memory.snapshot, generation=2, started_ms=anchor - 100,
        completed_ms=anchor + 100, books=tuple(replace(book, wire_started_ms=anchor - 50,
            wire_completed_ms=anchor + 50) for book in memory.snapshot.books)))
    assert not recover_unused_current_short(strategy, state)
    assert writes == []
