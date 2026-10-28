"""Actual restore API with controlled native card; not incident-native evidence."""

from dataclasses import replace

import pytest

from project_mai_tai.brokerdecides1_entry import recover_unused_current_short
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipRecord
from tests.unit.test_brokerdecides1_unused_owner_sell import case, ACCOUNTS, SEGMENT, OPPORTUNITY


def restored():
    strategy, state, writes = case(phase="unknown")
    strategy._entries_held = False
    strategy.configure_flip_entry_ownership(
        lambda record, active, reason: writes.append(("owner", record.opportunity_id, active, reason)),
        active_segments={"YMAT": OPPORTUNITY}, restored={"YMAT": FlipEntryOwnershipRecord(
            "YMAT", OPPORTUNITY, "unknown", 0, 0, (), {}, {})}, restore_readable=True)
    assert state.flip_owner_first_rest_placed is True  # Preserve original restore fact/bit.
    return strategy, state, writes


def test_real_restore_derived_placed_bit_is_not_a_permanent_empty_owner_lock():
    strategy, state, writes = restored()
    assert recover_unused_current_short(strategy, state)
    assert state.flip_owner_phase == "idle"
    assert [(kind, active) for kind, _, active, _ in writes] == [("identity", False), ("owner", False)]


@pytest.mark.parametrize("fault", ["no_restore_provenance", "different_owner", "fill", "position",
    "retry_changed", "working", "native_position", "inflight", "same_segment", "unreadable"])
def test_restored_note_cannot_replace_positive_or_uncertain_evidence(fault):
    strategy, state, writes = restored()
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    if fault == "no_restore_provenance": state._brokerdecides1_restored_empty_owner = None
    elif fault == "different_owner": state.fanout_segment_id = state.flip_owner_opportunity_id = OPPORTUNITY + 1
    elif fault == "fill": state.flip_owner_fill_accounts.add(ACCOUNTS[0])
    elif fault == "position": state.flip_owner_position_ids[ACCOUNTS[0]] = "managed"
    elif fault == "retry_changed": state.flip_owner_retry_segment_id = SEGMENT
    elif fault in {"working", "native_position"}:
        field = "working" if fault == "working" else "positions"
        memory.apply(replace(snapshot, generation=2, books=(replace(snapshot.books[0],
            **{field: frozenset({"YMAT"})}), snapshot.books[1])))
    elif fault == "inflight": memory.apply(replace(snapshot, generation=2, inflight=frozenset({"YMAT"})))
    elif fault == "same_segment": state.atr_short_flip_bar_ts = OPPORTUNITY // 60000 * 60000
    else: strategy._flip_owner_restore_readable = False
    assert not recover_unused_current_short(strategy, state)
    assert state.flip_owner_phase == "unknown" and not writes
