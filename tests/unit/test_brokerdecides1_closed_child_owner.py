"""CONTROLLED episode ports; retained CLRO logs are separate, not native proof."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from project_mai_tai.brokerdecides1_closed_owner import (
    ClosedOwnerEpisode, closed_child_episodes, recover_closed_owner,
    retire_closed_owner_on_new_short,
)
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook
from tests.unit.test_brokerdecides1_unused_owner_sell import case, ACCOUNTS, NOW, SEGMENT, OPPORTUNITY


def episode_rows():
    when = datetime(2026, 10, 23, 12, 28, 10, tzinfo=UTC)
    account = NS(id="account", name=ACCOUNTS[0])
    entry = NS(id="entry", broker_account_id="account", strategy_id="v2", symbol="YMAT",
        side="buy", quantity=Decimal(195), status="filled", intent_id="intent",
        client_order_id="CONTROLLED-entry", payload={"source": "schwab_1m_v2",
            "resting_entry": "true", "fanout_segment_id": str(OPPORTUNITY),
            "fanout_slot": "resting", "cw_entry_slot": "first",
            "fanout_slot_id": fanout_slot_id(strategy_code="schwab_1m_v2", symbol="YMAT",
                segment_id=OPPORTUNITY, slot="resting")})
    child = NS(id="child", broker_account_id="account", strategy_id="v2", symbol="YMAT",
        side="sell", quantity=Decimal(195), status="filled", intent_id="intent",
        order_type="oco_exit", broker_order_id="1008414377527",
        client_order_id="CONTROLLED-entry-ocoexit-14377527")
    def fill(order, identity, stamp):
        return NS(id=identity, order_id=order.id, strategy_id="v2", broker_account_id="account",
            symbol="YMAT", side=order.side, quantity=Decimal(195), filled_at=stamp)
    buy = fill(entry, "buy-fill", when)
    sell = fill(child, "sell-fill", when + timedelta(seconds=27))
    row = NS(id="managed", strategy_code="schwab_1m_v2", broker_account_name=ACCOUNTS[0],
        symbol="YMAT", entry_order_id="entry", entry_client_order_id="CONTROLLED-entry",
        status="closed", current_quantity=0, original_quantity=195,
        entry_time=when, updated_at=when + timedelta(seconds=40))
    return row, entry, child, buy, sell, account


def build(values):
    row, entry, child, buy, sell, account = values
    return closed_child_episodes([row], [(buy, entry, account)], [(sell, child, account)])


def test_exact_full_child_and_linked_entry_produce_one_closed_episode():
    assert build(episode_rows()) == {"YMAT": (
        ClosedOwnerEpisode(ACCOUNTS[0], "managed", 1792758487033, 1792758490000, 1792758517000),)}


@pytest.mark.parametrize("fault", ["wrong_entry", "wrong_client", "foreign_account", "wrong_symbol",
    "wrong_slot", "wrong_op", "reclaim", "partial_buy", "partial_child", "unfilled_entry",
    "unfilled_child", "foreign_child_intent", "wrong_child_client", "open_row", "nonzero_row",
    "before_entry", "after_close", "no_child", "no_entry"])
def test_missing_or_conflicting_episode_proof_never_becomes_closed(fault):
    values = episode_rows()
    row, entry, child, buy, sell, account = values
    if fault == "wrong_entry": row.entry_order_id = "other"
    elif fault == "wrong_client": row.entry_client_order_id = "other"
    elif fault == "foreign_account": row.broker_account_name = ACCOUNTS[1]
    elif fault == "wrong_symbol": row.symbol = "OTHER"
    elif fault == "wrong_slot": entry.payload["fanout_slot_id"] = "other"
    elif fault == "wrong_op": entry.payload["fanout_segment_id"] = "unknown"
    elif fault == "reclaim": entry.payload["cw_entry_slot"] = "reclaim"
    elif fault == "partial_buy": buy.quantity = Decimal(194)
    elif fault == "partial_child": sell.quantity = Decimal(194)
    elif fault == "unfilled_entry": entry.status = "working"
    elif fault == "unfilled_child": child.status = "working"
    elif fault == "foreign_child_intent": child.intent_id = "other"
    elif fault == "wrong_child_client": child.client_order_id = "other"
    elif fault == "open_row": row.status = "open"
    elif fault == "nonzero_row": row.current_quantity = 1
    elif fault == "before_entry": sell.filled_at = row.entry_time - timedelta(seconds=1)
    elif fault == "after_close": sell.filled_at = row.updated_at + timedelta(seconds=1)
    entries = [] if fault == "no_entry" else [(buy, entry, account)]
    exits = [] if fault == "no_child" else [(sell, child, account)]
    assert closed_child_episodes([row], entries, exits) == {}


def owner_case():
    strategy, state, writes = case(phase="unknown")
    state.flip_owner_first_rest_placed = True
    state.flip_owner_fill_accounts = {ACCOUNTS[0]}
    state.flip_owner_retry_segment_id = SEGMENT - 600000
    strategy._entries_held = False
    proof = ClosedOwnerEpisode(ACCOUNTS[0], "managed", OPPORTUNITY, OPPORTUNITY + 3000, OPPORTUNITY + 30000)
    return strategy, state, writes, proof


def test_original_recovery_misses_closed_row_then_new_book_restores_consumed_owner():
    strategy, state, writes, proof = owner_case()
    assert not strategy._recover_unknown_flip_owner(state, (), (), frozenset())
    assert state.flip_owner_phase == "unknown" and not state.flip_owner_position_ids
    # Same-segment LONG stays OLD-unknown: no new owner write or retry budget.
    state.atr_state = "long"
    retry = (state.retry_one_segment_id, state.retry_one_closes_in_segment)
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {},
        closed_owner_episodes={"YMAT": (proof,)}))
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_position_ids == {} and not writes
    assert retry == (state.retry_one_segment_id, state.retry_one_closes_in_segment)
    assert not strategy._strict_first_rest_admitted(state, slot="first")


def test_later_clean_short_retires_normal_identity_then_owner_no_retry_reset():
    strategy, state, writes, proof = owner_case()
    retry = (state.retry_one_segment_id, state.retry_one_closes_in_segment)
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {},
        closed_owner_episodes={"YMAT": (proof,)}))
    assert state.flip_owner_phase == "idle"
    assert [(kind, active) for kind, _, active, _ in writes] == [
        ("identity", False), ("owner", False)]
    assert retry == (state.retry_one_segment_id, state.retry_one_closes_in_segment)


@pytest.mark.parametrize("fault", ["foreign_op", "missing_sibling", "duplicate_account", "foreign_row",
    "future_close", "restore_unknown", "book_stale", "book_unreadable", "flag_off"])
def test_reconstruction_fails_closed_without_exact_complete_owner_proof(fault):
    strategy, state, writes, proof = owner_case()
    proofs = (proof,)
    if fault == "foreign_op": proofs = (replace(proof, opportunity=OPPORTUNITY + 1),)
    elif fault == "missing_sibling": state.flip_owner_fill_accounts.add(ACCOUNTS[1])
    elif fault == "duplicate_account": proofs = (proof, proof)
    elif fault == "foreign_row": state.flip_owner_position_ids = {ACCOUNTS[0]: "foreign"}
    elif fault == "future_close": proofs = (replace(proof, closed_ms=NOW + 1),)
    elif fault == "restore_unknown": strategy._flip_owner_restore_readable = False
    elif fault == "book_stale": strategy._now_ms = lambda: NOW + 600000
    elif fault == "book_unreadable": state.flip_owner_evidence_readable = False
    else: strategy.settings.strategy_schwab_1m_v2_brokerdecides1_enabled = False
    assert not recover_closed_owner(strategy, state, proofs)
    assert state.flip_owner_phase == "unknown" and not writes


@pytest.mark.parametrize("fault", ["working_schwab", "working_webull", "position_schwab", "position_webull",
    "inflight", "fill_current", "incomplete", "stale20", "same_segment", "restart", "removed_wait",
    "boot", "gap", "line_unknown", "reservation"])
def test_new_segment_retirement_keeps_every_real_money_and_line_fence(fault):
    strategy, state, writes, proof = owner_case()
    assert recover_closed_owner(strategy, state, (proof,))
    writes.clear()
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    changes = dict(generation=2)
    if fault.startswith(("working", "position")):
        field, provider = fault.split("_")
        changes["books"] = tuple(replace(book, **{"working" if field == "working" else "positions":
            frozenset({"YMAT"})}) if book.provider == provider else book for book in snapshot.books)
    elif fault == "inflight": changes["inflight"] = frozenset({"YMAT"})
    elif fault == "fill_current": changes["filled_segments"] = frozenset({("YMAT", SEGMENT)})
    elif fault == "incomplete": changes["local_complete"] = False
    elif fault == "stale20": strategy._now_ms = lambda: NOW + 19900
    elif fault == "same_segment": state.atr_short_flip_bar_ts = state.flip_owner_retry_segment_id
    elif fault == "restart": state.restart_rest_evidence_pending = True
    elif fault == "removed_wait": strategy._removed_wait_requests[state.symbol] = object()
    elif fault == "boot": strategy._entries_held = True
    elif fault == "gap": strategy._gap_hold_enabled = state.gap_hold_active = True
    elif fault == "line_unknown": strategy._line_restoration_enabled = True; strategy._line_readiness = None
    else: memory.reserve("YMAT", "CONTROLLED-unknown-wire")
    memory.apply(replace(snapshot, **changes))
    assert not retire_closed_owner_on_new_short(strategy, state, (proof,))
    assert state.flip_owner_phase == "unknown" and not writes


def test_persistence_failure_keeps_unknown_and_original_row_evidence():
    strategy, state, writes, proof = owner_case()
    def fail(*args): raise OSError("CONTROLLED-write-failure")
    strategy._flip_owner_persist = fail
    assert recover_closed_owner(strategy, state, (proof,))
    assert not writes
    assert not retire_closed_owner_on_new_short(strategy, state, (proof,))
    assert state.flip_owner_phase == "unknown" and state.flip_owner_position_ids == {}


@pytest.mark.parametrize("phase", ["idle", "resting", "consumed"])
def test_outside_exception_closed_child_book_makes_no_new_owner_write_or_memory_read(phase):
    strategy, state, writes, proof = owner_case()
    state.flip_owner_phase = phase

    class NoRead:
        def assess(self, **kwargs):
            raise AssertionError("outside_exception_native_memory_read")

    strategy._brokerdecides1_memory = NoRead()
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {},
        closed_owner_episodes={"YMAT": (proof,)}))
    assert not writes
    assert state.flip_owner_phase == phase and state.flip_owner_position_ids == {}


@pytest.mark.parametrize("missing", ["memory", "snapshot", "complete"])
def test_current_unknown_owner_without_definite_card_stays_unknown_without_writes(missing):
    strategy, state, writes, proof = owner_case()
    if missing == "memory":
        strategy._brokerdecides1_memory = None
    elif missing == "snapshot":
        strategy._brokerdecides1_memory.snapshot = None
    else:
        memory = strategy._brokerdecides1_memory
        memory.apply(replace(memory.snapshot, generation=2, local_complete=False))
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {},
        closed_owner_episodes={"YMAT": (proof,)}))
    assert state.flip_owner_phase == "unknown" and state.flip_owner_position_ids == {}
    assert not writes
