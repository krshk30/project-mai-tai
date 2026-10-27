"""Controlled actual strategy transitions, NOT historical YMAT native proof."""

from dataclasses import replace

import pytest

from project_mai_tai.brokerdecides1 import AdmissionMemory, AdmissionSnapshot, NativeAccountBook
from project_mai_tai.brokerdecides1_entry import unused_owner_at_sell_definite
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

NOW = 1792765145000  # CONTROLLED 2026-10-23 14:19:05Z, not a supplied SELL bar ID.
SEGMENT = 1792765140000
OPPORTUNITY = 1792758487033  # Real recorded bound identity, native facts below CONTROLLED.
ACCOUNTS = ("live:schwab_1m_v2", "live:orb")


def case(*, phase="idle", flag=True):
    strategy = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_enabled=flag,
        strategy_schwab_1m_v2_brokerdecides1_max_age_ms=20_000,
        strategy_schwab_1m_v2_account_name=ACCOUNTS[0],
        strategy_schwab_1m_v2_webull_account_name=ACCOUNTS[1]))
    strategy._now_ms = lambda: NOW
    writes = []
    strategy.configure_fanout_identity_persistence(
        lambda symbol, identity, active, reason: writes.append(("identity", identity, active, reason)))
    strategy.configure_flip_entry_ownership(
        lambda record, active, reason: writes.append(("owner", record.opportunity_id, active, reason)),
        restore_readable=True)
    state = strategy.watchlist_state("YMAT")
    state.atr_state = "short"
    state.atr_short_flip_bar_ts = SEGMENT
    state.bars = [OHLCVBar(SEGMENT, 2.7, 2.8, 2.6, 2.7, 1000)]
    state.fanout_segment_id = state.flip_owner_opportunity_id = OPPORTUNITY
    state.flip_owner_phase = phase
    state.flip_owner_evidence_readable = True
    state.flip_owner_evidence_at_ms = NOW
    strategy._brokerdecides1_memory = AdmissionMemory()
    strategy._brokerdecides1_memory.apply(AdmissionSnapshot("CONTROLLED", 1, NOW - 200, NOW - 10,
        tuple(NativeAccountBook(account, provider, NOW - 100, NOW - 20,
            frozenset(), frozenset(), True, "CONTROLLED_NOT_NATIVE_FACTS")
            for account, provider in zip(ACCOUNTS, ("schwab", "webull"))),
        frozenset(), frozenset(), True, "CONTROLLED"))
    return strategy, state, writes


@pytest.mark.parametrize("phase", ["idle", "unknown"])
def test_unused_identity_at_new_sell_retires_through_normal_ordered_writes(phase):
    strategy, state, writes = case(phase=phase)
    retry = (state.retry_one_segment_id, state.retry_one_closes_in_segment, state.retry_one_budget_readable)
    assert unused_owner_at_sell_definite(strategy, state)
    strategy._end_flip_owner_on_sell(state)
    assert [(kind, identity, active) for kind, identity, active, _ in writes] == [
        ("identity", OPPORTUNITY, False), ("owner", OPPORTUNITY, False)]
    assert state.flip_owner_phase == "idle" and state.fanout_segment_id == state.flip_owner_opportunity_id == 0
    assert state.fanout_last_retired_opportunity_id == OPPORTUNITY
    assert retry == (state.retry_one_segment_id, state.retry_one_closes_in_segment, state.retry_one_budget_readable)


def test_original_flag_off_path_still_records_idle_identity_as_unknown():
    strategy, state, writes = case(flag=False)
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "unknown" and state.flip_owner_opportunity_id == OPPORTUNITY
    assert not any(not active for _, _, active, _ in writes)


@pytest.mark.parametrize("fault", ["working_schwab", "working_webull", "position_schwab", "position_webull",
    "inflight", "fill", "local_unknown", "native_unknown", "stale20", "reservation"])
def test_real_economic_or_unknown_card_does_not_retire_owner(fault):
    strategy, state, writes = case(phase="unknown")
    state.flip_owner_evidence_at_ms = 0  # Genuine OLD stale-owner blocker.
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    change = dict(generation=2)
    if fault.startswith(("working", "position")):
        provider = fault.split("_")[1]
        field = "working" if fault.startswith("working") else "positions"
        change["books"] = tuple(replace(book, **{field: frozenset({"YMAT"})})
            if book.provider == provider else book for book in snapshot.books)
    elif fault == "inflight":
        change["inflight"] = frozenset({"YMAT"})
    elif fault == "fill":
        change["filled_segments"] = frozenset({("YMAT", SEGMENT)})
    elif fault == "local_unknown":
        change["local_complete"] = False
    elif fault == "native_unknown":
        change["books"] = (replace(snapshot.books[0], complete=False), snapshot.books[1])
    elif fault == "stale20":
        strategy._now_ms = lambda: NOW + 19900
    else:
        memory.reserve("YMAT", "CONTROLLED_unknown_ACK")
    memory.apply(replace(snapshot, **change))
    assert not unused_owner_at_sell_definite(strategy, state)
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_opportunity_id == OPPORTUNITY
    assert not any(not active for _, _, active, _ in writes)


@pytest.mark.parametrize("fault", ["first_rest", "emit", "fill_account", "position_id", "rest", "mirror",
    "claim", "restore_unknown", "crossday_snapshot", "foreign_identity", "historical_bar"])
def test_unused_class_predicate_never_relabels_claimed_or_unproved_identity(fault):
    strategy, state, _ = case()
    if fault == "first_rest":
        state.flip_owner_first_rest_placed = True
    elif fault == "emit":
        state.cw_v2_emit_claimed = True
    elif fault == "fill_account":
        state.flip_owner_fill_accounts.add(ACCOUNTS[0])
    elif fault == "position_id":
        state.flip_owner_position_ids[ACCOUNTS[0]] = "CONTROLLED"
    elif fault == "rest":
        state.resting_active = True
    elif fault == "mirror":
        state.webull_resting_active = True
    elif fault == "claim":
        state.fanout_claim_attempt_id = "CONTROLLED"
    elif fault == "restore_unknown":
        strategy._flip_owner_restore_readable = False
    elif fault == "crossday_snapshot":
        strategy._now_ms = lambda: NOW + 86400000
    elif fault == "foreign_identity":
        state.fanout_segment_id += 1
    else:
        strategy._now_ms = lambda: NOW + 600000
    assert not unused_owner_at_sell_definite(strategy, state)


@pytest.mark.parametrize("failed", ["identity", "owner"])
def test_retirement_persistence_failure_keeps_unknown_and_identity(failed):
    strategy, state, writes = case()
    def fail(*args):
        raise OSError("CONTROLLED persistence failure")
    if failed == "identity":
        strategy._fanout_identity_persist = fail
    else:
        strategy._flip_owner_persist = fail
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == OPPORTUNITY
    assert not any(kind == "owner" and not active for kind, _, active, _ in writes)
