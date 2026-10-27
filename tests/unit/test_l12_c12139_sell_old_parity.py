"""CONTROLLED real SELL transitions; actual GIXI bisect remains separate."""

import pytest

from tests.unit.test_brokerdecides1_unused_owner_sell import case


class ForbiddenMemory:
    @property
    def snapshot(self):
        raise AssertionError("ordinary OLD SELL must not inspect L12 native memory")

    def assess(self, **kwargs):
        raise AssertionError("ordinary OLD SELL must not assess L12 native memory")


def outcome(state, writes):
    return (state.flip_owner_phase, state.flip_owner_opportunity_id,
        state.fanout_segment_id, state.fanout_last_retired_opportunity_id,
        state.retry_one_segment_id, state.retry_one_closes_in_segment,
        state.retry_one_budget_readable, writes)


@pytest.mark.parametrize("phase", ["unknown", "resting", "awaiting_fill", "consumed"])
@pytest.mark.parametrize("book", ["missing", "forbidden"])
def test_fresh_evidence_ordinary_sell_exact_flag_off_parity(phase, book):
    old, before, old_writes = case(phase=phase, flag=False)
    new, after, new_writes = case(phase=phase)
    new._brokerdecides1_memory = None if book == "missing" else ForbiddenMemory()
    old._end_flip_owner_on_sell(before)
    new._end_flip_owner_on_sell(after)
    assert outcome(after, new_writes) == outcome(before, old_writes)
    assert after.flip_owner_phase == "idle"
    assert after.flip_owner_opportunity_id == after.fanout_segment_id == 0
    assert [kind for kind, _, active, _ in new_writes if not active] == ["identity", "owner"]


@pytest.mark.parametrize("phase", ["unknown", "resting", "awaiting_fill", "consumed"])
def test_stale_local_evidence_missing_native_preserves_old_hold_and_writes(phase):
    old, before, old_writes = case(phase=phase, flag=False)
    new, after, new_writes = case(phase=phase)
    before.flip_owner_evidence_at_ms = after.flip_owner_evidence_at_ms = 0
    new._brokerdecides1_memory = None
    old._end_flip_owner_on_sell(before)
    new._end_flip_owner_on_sell(after)
    assert outcome(after, new_writes) == outcome(before, old_writes)
    assert after.flip_owner_opportunity_id > 0


def test_genuine_stale_unknown_owner_complete_card_keeps_ordered_exception():
    strategy, state, writes = case(phase="unknown")
    state.flip_owner_evidence_at_ms = 0
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "idle"
    assert [kind for kind, _, active, _ in writes if not active] == ["identity", "owner"]
    assert all(reason == "brokerdecides_unused_identity_sell" for _, _, _, reason in writes)


def test_idle_contradictory_owner_missing_card_preserves_old_unknown_transition():
    old, before, old_writes = case(flag=False)
    new, after, new_writes = case()
    new._brokerdecides1_memory = None
    old._end_flip_owner_on_sell(before)
    new._end_flip_owner_on_sell(after)
    assert outcome(after, new_writes) == outcome(before, old_writes)
    assert after.flip_owner_phase == "unknown"


def test_same_segment_ordinary_sell_reset_needs_no_native_permission():
    strategy, state, writes = case(phase="unknown")
    state.atr_short_flip_bar_ts = state.flip_owner_opportunity_id
    strategy._brokerdecides1_memory = ForbiddenMemory()
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "idle"
    assert state.flip_owner_opportunity_id == state.fanout_segment_id == 0
    assert len(writes) == 2
