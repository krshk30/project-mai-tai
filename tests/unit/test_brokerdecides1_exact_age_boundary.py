"""Exact 20s card expiry; source-authored, not native cadence evidence."""

from dataclasses import replace

import pytest

from project_mai_tai.brokerdecides1_entry import (
    card_fill_segment_definite, entry_decision, reconstructed_first_admitted,
)
from tests.unit.test_brokerdecides1_card_only import NOW, card_case


@pytest.mark.parametrize("account_index", [0, 1])
@pytest.mark.parametrize("age_ms,allowed", [(19_999, True), (20_000, False), (20_001, False)])
def test_oldest_account_wire_start_expires_at_exact_bound(account_index, age_ms, allowed):
    strategy, state = card_case()
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    books = list(snapshot.books)
    books[account_index] = replace(books[account_index], wire_started_ms=NOW - age_ms)
    memory.apply(replace(snapshot, generation=3, started_ms=NOW - age_ms,
        books=tuple(books)))
    before = (state.retry_one_segment_id, state.retry_one_closes_in_segment,
        state.flip_owner_phase, state.flip_owner_opportunity_id,
        state.cw_seed_cap_watch_start_ms, state.cw_resting_taken, state.cw_reclaim_taken)

    decision = entry_decision(strategy, state.symbol)
    assert decision.allowed is allowed
    assert decision.oldest_age_ms == age_ms
    assert decision.reason == ("broker_flat_no_inflight_no_segment_fill"
        if allowed else "snapshot_stale")
    assert card_fill_segment_definite(strategy, state) is allowed
    assert reconstructed_first_admitted(strategy, state) is allowed
    assert before == (state.retry_one_segment_id, state.retry_one_closes_in_segment,
        state.flip_owner_phase, state.flip_owner_opportunity_id,
        state.cw_seed_cap_watch_start_ms, state.cw_resting_taken, state.cw_reclaim_taken)


@pytest.mark.parametrize("block", ["working", "position", "inflight", "filled"])
def test_just_inside_age_boundary_never_overrides_economic_evidence(block):
    strategy, state = card_case()
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    books = tuple(replace(book, wire_started_ms=NOW - 19_999) for book in snapshot.books)
    changes = {}
    if block in {"working", "position"}:
        field = "working" if block == "working" else "positions"
        books = (books[0], replace(books[1], **{field: frozenset({state.symbol})}))
    elif block == "inflight":
        changes["inflight"] = frozenset({state.symbol})
    else:
        changes["filled_segments"] = frozenset({(state.symbol, state.atr_short_flip_bar_ts)})
    memory.apply(replace(snapshot, generation=3, started_ms=NOW - 19_999,
        books=books, **changes))

    assert not entry_decision(strategy, state.symbol).allowed
    assert not card_fill_segment_definite(strategy, state)
    assert not reconstructed_first_admitted(strategy, state)
    assert state.retry_one_segment_id == 0
    assert state.retry_one_closes_in_segment == 0
