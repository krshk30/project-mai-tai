"""Cap provenance is not a fill; exact economic claims still fail closed."""

from dataclasses import replace

import pytest

from project_mai_tai.brokerdecides1_entry import (
    bind_reconstructed_opportunity, reconstructed_first_admitted,
)
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from tests.unit.test_brokerdecides1_entry_hooks import fixture as entry_fixture


def cap_case():
    strategy, state, draft = entry_fixture()
    state.flip_owner_phase = "idle"
    state.fanout_segment_id = state.flip_owner_opportunity_id = 0
    state.cw_seed_cap_watch_start_ms = 950
    state.cw_resting_taken = state.cw_reclaim_taken = True
    state.flip_owner_first_rest_placed = False
    state.flip_owner_fill_accounts = set()
    state.flip_owner_position_ids = {}
    state.flip_owner_open_positions = {}
    state.position_qty = state.position_qty_held = 0
    state.resting_active = state.webull_resting_active = False
    state.cw_v2_emit_claimed = state.fanout_webull_claimed = False
    state.fanout_claim_outcome = ""
    strategy._falseflip_entry_blocked = lambda s: False
    strategy._removed_wait_gate_closed = lambda symbol: False
    strategy._flip_owned_first_entry_enabled = True
    strategy._flip_owner_restore_readable = True
    strategy._flip_owner_evidence_fresh = lambda s: True
    strategy._removed_wait_enabled = True
    strategy._removed_wait_has_owner = lambda s: False
    strategy._retry_one_enabled = True
    strategy._retry_one_max_retries = 0
    strategy._falseflip_effective_closes = lambda s, count: count
    strategy._flip_owner_counts = {"admission_evaluated": 0, "admission_refused_unknown": 0}
    state.retry_one_budget_readable = True
    state.retry_one_segment_id = state.atr_short_flip_bar_ts
    state.retry_one_closes_in_segment = 0
    return strategy, state


def test_cap_only_native_flat_uses_canonical_gate_without_erasing_notes():
    strategy, state = cap_case()
    assert SchwabV2Strategy._strict_first_rest_admitted(strategy, state, slot="first")
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.cw_seed_cap_watch_start_ms == 950
    assert state.flip_owner_phase == "idle"
    assert not state.fanout_segment_id and not state.flip_owner_opportunity_id


def test_exact_new_identity_remains_admitted_across_country_allocation():
    strategy, state = cap_case()
    assert reconstructed_first_admitted(strategy, state)
    state.fanout_segment_id = state.flip_owner_opportunity_id = 1400
    bind_reconstructed_opportunity(state, 1400)
    assert reconstructed_first_admitted(strategy, state)
    # Refresh is assessed, not a cached grant and not a stranded old generation.
    memory = strategy._brokerdecides1_memory
    memory.apply(replace(memory.snapshot, generation=2))
    assert reconstructed_first_admitted(strategy, state)
    assert state.cw_resting_taken and state.cw_reclaim_taken


@pytest.mark.parametrize("identity", [(1400, 1400), (0, 1400), (1400, 0)])
def test_preexisting_identity_is_never_relabelled_cap_only(identity):
    strategy, state = cap_case()
    state.fanout_segment_id, state.flip_owner_opportunity_id = identity
    assert not reconstructed_first_admitted(strategy, state)
    assert not hasattr(state, "_brokerdecides1_cap_binding")


@pytest.mark.parametrize("change", ["foreign_identity", "other_segment", "other_watch", "unbound_candidate"])
def test_bound_provenance_cannot_follow_unrelated_identity(change):
    strategy, state = cap_case()
    assert reconstructed_first_admitted(strategy, state)
    state.fanout_segment_id = state.flip_owner_opportunity_id = 1400
    if change != "unbound_candidate":
        bind_reconstructed_opportunity(state, 1400)
    if change == "foreign_identity":
        state.fanout_segment_id = state.flip_owner_opportunity_id = 1401
    elif change == "other_segment":
        state.atr_short_flip_bar_ts = 901
    elif change == "other_watch":
        state.cw_seed_cap_watch_start_ms = 951
    assert not reconstructed_first_admitted(strategy, state)


@pytest.mark.parametrize("claim,value", [
    ("flip_owner_phase", "unknown"), ("flip_owner_phase", "bound"),
    ("flip_owner_first_rest_placed", True), ("flip_owner_fill_accounts", {"live:schwab"}),
    ("flip_owner_position_ids", {"live:schwab": "actual-row"}),
    ("flip_owner_open_positions", {"live:schwab": object()}),
    ("position_qty", 1), ("position_qty_held", 1),
    ("resting_active", True), ("webull_resting_active", True),
    ("cw_v2_emit_claimed", True), ("fanout_webull_claimed", True),
    ("fanout_claim_outcome", "filled"), ("cw_seed_cap_watch_start_ms", 0),
    ("cw_resting_taken", False), ("cw_reclaim_taken", False),
])
def test_every_real_claim_or_missing_cap_provenance_holds(claim, value):
    strategy, state = cap_case()
    setattr(state, claim, value)
    assert not reconstructed_first_admitted(strategy, state)


@pytest.mark.parametrize("condition", ["working", "position", "inflight", "fill", "stale", "missing", "reserved"])
def test_each_call_rechecks_current_native_and_local_proof(condition):
    strategy, state = cap_case()
    assert reconstructed_first_admitted(strategy, state)
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    if condition in {"working", "position"}:
        field = "working" if condition == "working" else "positions"
        memory.apply(replace(snapshot, generation=2, books=(snapshot.books[0],
            replace(snapshot.books[1], **{field: frozenset({state.symbol})}))))
    elif condition == "inflight":
        memory.apply(replace(snapshot, generation=2, inflight=frozenset({state.symbol})))
    elif condition == "fill":
        memory.apply(replace(snapshot, generation=2, filled_segments=frozenset({(state.symbol, 900)})))
    elif condition == "stale":
        strategy._now_ms = lambda: 1501
    elif condition == "missing":
        memory.snapshot = None
    else:
        memory.reserve(state.symbol, "persisted-token")
    assert not reconstructed_first_admitted(strategy, state)


@pytest.mark.parametrize("guard", ["falseflip", "removed", "reclaim", "flag_off"])
def test_existing_non_cap_guards_not_waived(guard):
    strategy, state = cap_case()
    if guard == "falseflip":
        strategy._falseflip_entry_blocked = lambda s: True
    elif guard == "removed":
        strategy._removed_wait_gate_closed = lambda symbol: True
    elif guard == "flag_off":
        strategy.settings.strategy_schwab_1m_v2_brokerdecides1_enabled = False
    assert not reconstructed_first_admitted(strategy, state, slot="reclaim" if guard == "reclaim" else "first")


@pytest.mark.parametrize("guard", [
    "restore_unknown", "retry_unreadable", "segment_unknown", "segment_mismatch",
    "exhausted", "owner_evidence_stale", "removed_wait_filled_owner",
])
@pytest.mark.parametrize("after_binding", [False, True])
def test_native_flat_never_waives_historical_hard_guard(guard, after_binding):
    strategy, state = cap_case()
    if after_binding:
        assert reconstructed_first_admitted(strategy, state)
        state.fanout_segment_id = state.flip_owner_opportunity_id = 1400
        bind_reconstructed_opportunity(state, 1400)
    prior = getattr(state, "_brokerdecides1_cap_binding", None)
    if guard == "restore_unknown":
        strategy._flip_owner_restore_readable = False
    elif guard == "retry_unreadable":
        state.retry_one_budget_readable = False
    elif guard == "segment_unknown":
        state.retry_one_segment_id = 0
    elif guard == "segment_mismatch":
        state.retry_one_segment_id = 899
    elif guard == "exhausted":
        state.retry_one_closes_in_segment = 1
    elif guard == "owner_evidence_stale":
        strategy._flip_owner_evidence_fresh = lambda s: False
    else:
        strategy._removed_wait_has_owner = lambda s: True
    assert not reconstructed_first_admitted(strategy, state)
    assert not SchwabV2Strategy._strict_first_rest_admitted(strategy, state, slot="first")
    assert getattr(state, "_brokerdecides1_cap_binding", None) == prior
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.cw_seed_cap_watch_start_ms == 950
