"""CONTROLLED lifecycle contracts; real SPRC/AIXI persisted replays remain separate."""

from dataclasses import replace

import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import FLIP_OWNER_EVIDENCE_MAX_AGE_MS
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook, FlipPositionLeg
from tests.unit.test_l11_c10719_sell_current_obligation import (
    ACCOUNTS, IDENTITY, NOW, probe, retired,
)


def awaiting(*, symbol="SPRC", keepwatch=True):
    strategy, state, writes = probe(symbol=symbol, keepwatch=keepwatch)
    state.flip_owner_phase = "awaiting_close"
    state.flip_owner_first_rest_placed = True
    state.flip_owner_fill_accounts = {ACCOUNTS[1]}
    assert not state.flip_owner_position_ids
    return strategy, state, writes


@pytest.mark.parametrize("symbol", ["SPRC", "AIXI"])
@pytest.mark.parametrize("keepwatch", [False, True])
def test_fresh_flat_unbound_sell_owner_retires_via_actual_position_callback(
        symbol, keepwatch, caplog):
    caplog.set_level("INFO")
    strategy, state, writes = awaiting(symbol=symbol, keepwatch=keepwatch)
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    retired(state, writes)
    assert "reason=sell_flip_flat_unknown_owner" in caplog.text


@pytest.mark.parametrize("clock", ["stale", "future", "zero", "unreadable"])
def test_unproven_flat_read_cannot_retire_empty_id_owner(clock):
    strategy, state, writes = awaiting()
    at = {"stale": NOW - FLIP_OWNER_EVIDENCE_MAX_AGE_MS - 1,
          "future": NOW + 1, "zero": 0, "unreadable": NOW}[clock]
    strategy.apply_flip_position_book(FlipPositionBook(at, clock != "unreadable", {}))
    assert state.flip_owner_phase == "awaiting_close"
    assert state.flip_owner_opportunity_id == IDENTITY
    assert not any(not active for _, _, active in writes)


def test_actual_managed_position_remains_held_then_flat_read_retires():
    strategy, state, writes = awaiting()
    # The persisted owner has no row ID; a newly read positive row still holds.
    leg = FlipPositionLeg(ACCOUNTS[1], "CONTROLLED_OPEN_ROW", NOW - 60000, 196)
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {state.symbol: (leg,)}))
    assert state.flip_owner_phase == "awaiting_close"
    assert state.flip_owner_opportunity_id == IDENTITY
    assert not state.flip_owner_position_ids
    assert not any(not active for _, _, active in writes)
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    retired(state, writes)


@pytest.mark.parametrize("blocker", [
    "quantity", "held_quantity", "resting", "webull_resting", "native_position",
    "working_schwab", "working_webull", "unanswered", "reservation", "restart",
])
def test_current_obligations_still_hold_empty_id_owner(blocker):
    strategy, state, writes = awaiting()
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    if blocker == "quantity":
        state.position_qty = 196
    elif blocker == "held_quantity":
        state.position_qty_held = 196
    elif blocker == "resting":
        state.resting_active = True
    elif blocker == "webull_resting":
        state.webull_resting_active = True
    elif blocker in {"native_position", "working_schwab", "working_webull"}:
        field = "positions" if blocker == "native_position" else "working"
        provider = "webull" if blocker == "working_webull" else "schwab"
        memory.apply(replace(snapshot, generation=2, books=tuple(
            replace(book, **{field: frozenset({state.symbol})})
            if book.provider == provider else book for book in snapshot.books)))
    elif blocker == "unanswered":
        memory.apply(replace(snapshot, generation=2, inflight=frozenset({state.symbol})))
    elif blocker == "reservation":
        memory.reserve(state.symbol, "CONTROLLED_UNANSWERED_SUBMIT")
    else:
        state.restart_rest_evidence_pending = True
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    assert state.flip_owner_phase == "awaiting_close"
    assert state.flip_owner_opportunity_id == IDENTITY
    assert not any(not active for _, _, active in writes)


@pytest.mark.parametrize("unknown", ["restore", "identity_conflict"])
def test_restore_or_identity_ambiguity_never_retires(unknown):
    strategy, state, writes = awaiting()
    if unknown == "restore":
        strategy._flip_owner_restore_readable = False
    else:
        state.fanout_segment_id = IDENTITY + 1
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    assert state.flip_owner_phase == "awaiting_close"
    assert state.flip_owner_opportunity_id == IDENTITY
    assert not any(not active for _, _, active in writes)


@pytest.mark.parametrize("store", ["identity", "owner"])
def test_empty_id_retirement_still_requires_durable_writes(store):
    strategy, state, writes = awaiting()

    def fail(*args):
        raise OSError("CONTROLLED retirement store failure")

    if store == "identity":
        strategy._fanout_identity_persist = fail
    else:
        strategy._flip_owner_persist = fail
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == IDENTITY


def test_nonempty_bound_id_keeps_existing_retirement_reason(caplog):
    caplog.set_level("INFO")
    strategy, state, writes = awaiting()
    state.flip_owner_position_ids = {ACCOUNTS[1]: "CONTROLLED_CLOSED_ROW"}
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    retired(state, writes)
    assert "reason=bound_flip_position_closed" in caplog.text


def test_unknown_before_sell_is_not_a_flatness_release():
    strategy, state, writes = awaiting()
    state.flip_owner_phase = "unknown"
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    assert state.flip_owner_opportunity_id == IDENTITY
    assert not any(not active for _, _, active in writes)


def test_actual_legacy_sell_then_unreadable_then_fresh_flat_callback():
    strategy, state, writes = awaiting(keepwatch=False)
    state.flip_owner_phase = "unknown"
    state.flip_owner_open_positions = {
        ACCOUNTS[1]: FlipPositionLeg(ACCOUNTS[1], "CONTROLLED_OPEN_ROW", NOW - 60000, 196)}
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "awaiting_close"
    assert not state.flip_owner_position_ids
    strategy.apply_flip_position_book(FlipPositionBook(NOW, False, {}))
    assert state.flip_owner_phase == "awaiting_close"
    assert not any(not active for _, _, active in writes)
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    retired(state, writes)
    before = tuple(writes)
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    assert tuple(writes) == before


def test_missing_opportunity_is_not_successful_retirement():
    strategy, state, writes = awaiting()
    state.flip_owner_opportunity_id = state.fanout_segment_id = 0
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    assert state.flip_owner_phase == "unknown"
    assert not any(not active for _, _, active in writes)


def test_existing_evidence_age_boundary_is_unchanged():
    strategy, state, writes = awaiting()
    strategy.apply_flip_position_book(FlipPositionBook(
        NOW - FLIP_OWNER_EVIDENCE_MAX_AGE_MS, True, {}))
    retired(state, writes)


def test_later_sell_already_has_flat_fallback_not_an_all_day_deadlock(caplog):
    caplog.set_level("INFO")
    strategy, state, writes = awaiting(keepwatch=False)
    # No intervening flat callback: this exercises the distinct existing SELL
    # fallback, consistent with the real 08:31 receipt, not an input replay.
    strategy._end_flip_owner_on_sell(state)
    retired(state, writes)
    assert "reason=sell_flip_flat" in caplog.text


def test_empty_awaiting_close_foreign_native_account_positive_book_does_not_count():
    strategy, state, writes = awaiting()
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    foreign = replace(
        snapshot.books[0], account="live:momentum_warn_live",
        positions=frozenset({state.symbol}), working=frozenset({state.symbol}))
    assert foreign.account not in strategy._flip_owner_accounts
    # Preserve positive foreign evidence without exceeding the actual bounded
    # native-book contract. This is NOT a complete bot-account flat card;
    # the separate real OMS book below supplies the typed flat callback.
    memory.apply(replace(snapshot, generation=2, books=(foreign, snapshot.books[1])))
    assert foreign in memory.snapshot.books
    assert state.symbol in foreign.positions and state.symbol in foreign.working
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    retired(state, writes)
