"""Recorded FLYE boundary; positive cancellation receipts are explicit replay controls."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.fanout_outcome_consumer import FanoutOutcome
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.v2_flip_entry_ownership import (
    FlipEntryOwnershipRecord, FlipPositionBook, FlipPositionClose, FlipPositionLeg,
)
from project_mai_tai.v2_removed_wait import RemovedWaitProof
from tests.unit.test_retryleft1 import strategy_for_record

FLYE = json.loads((Path(__file__).parents[1] / "fixtures/flye-owner-20261008.json").read_text())
PRIMARY, WEBULL = "live:schwab_1m_v2", "live:orb"


def replay(*, account=PRIMARY, pm=False, phase="bound"):
    row = FLYE["position_ids"][PRIMARY]
    entry_ms = FLYE["position_entry_ms"][PRIMARY]
    record = FlipEntryOwnershipRecord(
        "FLYE", FLYE["opportunity_id"], phase, FLYE["flip_bar_ts"],
        FLYE["provisional_started_ms"], (account,), {account: row}, {account: entry_ms},
        FLYE["retry_segment_id"], 0,
    )
    strategy, state, _, clock, writes = strategy_for_record(
        record, datetime.fromisoformat(FLYE["close_poll_utc"]), pm=pm, quantities=(280, 140),
    )
    strategy._boot_ms = record.retry_segment_id - 60_000
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_sell_enabled = True
    strategy.settings.strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled = pm
    strategy._eh_resting_enabled = pm
    state.retry_one_watch_start_ms = strategy._boot_ms
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: pm
    strategy._liquidity_floor_ok = lambda _state: True
    strategy._resting_stop_ask_allows = lambda _state, _line, slot: True
    return strategy, state, record, clock, writes


def book(strategy, record, clock, *, reason="OCO_RESOLVED_FLAT", legs=(), readable=True):
    closes = tuple(FlipPositionClose(a, r, reason) for a, r in record.position_ids.items())
    strategy.apply_flip_position_book(FlipPositionBook(
        clock[0], readable, {"FLYE": legs} if legs else {}, closes_by_symbol={"FLYE": closes},
    ))


def sell(strategy, state, clock):
    clock[0] = FLYE["fresh_sell_poll_ms"]
    bar_ms = FLYE["fresh_sell_bar_ms"]
    state.bars.append(OHLCVBar(bar_ms, 2.33, 2.3499, 2.27, 2.285, 149257))
    state.atr_short_flip_bar_ts = bar_ms
    strategy._cw_v2_track(state, {"flip": "SELL", "observation_phase": "live"})


def settle(strategy, request, clock):
    # Positive receipt is a controlled prerequisite, not a claimed historical observation.
    strategy.apply_removed_wait_proofs([RemovedWaitProof(
        request, clock[0], True, "retry_leftovers_cancelled_owner_kept",
    )])


def confirm_controlled_next_entry(strategy, state, clock):
    """Counterfactual new fills, then the recorded 15:17 BUY bar; not historical trades."""
    opportunity = state.flip_owner_opportunity_id
    assert opportunity > FLYE["opportunity_id"]
    clock[0] = FLYE["next_buy_flip_poll_ms"] - 1_000
    if not state.resting_is_broker_order:
        # Controlled EH quote/dispatch, not a retained market print or a live trade.
        state.bars.append(OHLCVBar(FLYE["next_buy_flip_bar_ms"], 2.3, 2.4, 2.2, 2.3, 1))
        trigger = strategy._active_resting_trigger(state)
        quote = Quote("FLYE", trigger, trigger, trigger, clock[0], 1)
        entry = strategy._eh_resting_cross_check(state, quote)
        assert entry and entry.intent_type == "open" and entry.metadata["order_type"] == "limit"
        assert int(entry.metadata["fanout_segment_id"]) == opportunity
        assert strategy.drain_webull_fanout_intents()[0].intent_type == "open"
        assert strategy._eh_resting_cross_check(state, quote) is None
    strategy.update_position("FLYE", 2, held_qty=2)
    assert strategy.apply_fanout_outcome(FanoutOutcome(
        uuid4(), datetime.fromtimestamp(clock[0] / 1000, UTC), "FLYE", opportunity,
        "resting", fanout_slot_id(strategy_code="schwab_1m_v2", symbol="FLYE",
                                  segment_id=opportunity, slot="resting"),
        "test-next-entry", "filled", "test-fill-receipt", broker_account_name=WEBULL,
    )) == "consumed"
    legs = tuple(FlipPositionLeg(a, "test-next-" + a, clock[0], 1) for a in (PRIMARY, WEBULL))
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {"FLYE": legs}))
    clock[0] = FLYE["next_buy_flip_poll_ms"]
    # Open is unused by this tracker and is not retained by the source probe.
    state.bars.append(OHLCVBar(FLYE["next_buy_flip_bar_ms"], FLYE["next_buy_flip_close"],
        FLYE["next_buy_flip_high"], FLYE["next_buy_flip_low"], FLYE["next_buy_flip_close"],
        FLYE["next_buy_flip_volume"]))
    state.atr_short_flip_bar_ts = 0
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {"FLYE": legs}))
    strategy._cw_v2_track(state, {"flip": "BUY", "state": "long",
        "flip_level": FLYE["next_buy_flip_level"], "trail": FLYE["next_buy_flip_trail"],
        "observation_phase": "live"})
    assert state.flip_owner_phase == "bound"
    assert state.flip_owner_fill_accounts == {PRIMARY, WEBULL}
    assert state.flip_owner_opportunity_id == opportunity
    assert state.retry_one_segment_id == FLYE["fresh_sell_bar_ms"]
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    assert not strategy.drain_pending_intents()
    assert not strategy.drain_webull_direct_intents()
    assert not strategy.drain_webull_fanout_intents()


@pytest.mark.parametrize("pm", [False, True])
def test_controlled_new_entry_after_1410_rest_binds_at_recorded_1517_buy(pm):
    strategy, state, record, clock, _ = replay(pm=pm)
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    settle(strategy, request, clock)
    book(strategy, record, clock)
    strategy._queue_resting_place(state, 2.558685, slot="first")
    assert state.resting_active
    if not pm:
        assert strategy.drain_pending_intents()[0].intent_type == "open"
        assert strategy.drain_webull_direct_intents()[0].intent_type == "open"
    confirm_controlled_next_entry(strategy, state, clock)


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("reason", ["OCO_RESOLVED_FLAT", "CW_HARD_STOP", "CONFIRMATION_EXIT", "CW_FLIP"])
def test_fresh_sell_resumes_after_cancel_settlement_for_every_exit(account, pm, reason):
    strategy, state, record, clock, _ = replay(account=account, pm=pm)
    book(strategy, record, clock, reason=reason)
    request = strategy._removed_wait_requests["FLYE"]
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    assert state.flip_owner_phase == "bound"
    assert not strategy._strict_first_rest_admitted(state, slot="first")

    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock, reason=reason)
    sell(strategy, state, clock)
    assert state.flip_owner_phase == "awaiting_close"
    assert state.retry_one_segment_id == FLYE["fresh_sell_bar_ms"]
    assert state.retry_one_closes_in_segment == 0
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    settle(strategy, request, clock)
    book(strategy, record, clock, reason=reason)
    assert state.flip_owner_phase == "idle"
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.retry_one_closes_in_segment == 0
    strategy._queue_resting_place(state, 2.52, slot="first")
    if pm:
        assert state.resting_active
    else:
        primary, = strategy.drain_pending_intents()
        assert primary.intent_type == "open"
        assert int(primary.metadata["fanout_segment_id"]) > record.opportunity_id


def test_same_segment_receipt_keeps_owner_and_retry_off_consumed():
    strategy, state, record, clock, _ = replay()
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    settle(strategy, request, clock)
    book(strategy, record, clock)
    assert state.flip_owner_phase == "bound"
    assert not strategy._strict_first_rest_admitted(state, slot="first")


@pytest.mark.parametrize("fault", ["old", "seed", "missing_sell_identity"])
def test_only_genuine_fresh_sell_preserves_a_deferred_release(fault):
    strategy, state, record, clock, _ = replay()
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_sell_enabled = False
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    bar_ms = FLYE["fresh_sell_bar_ms"]
    state.bars.append(OHLCVBar(bar_ms, 2.33, 2.3499, 2.27, 2.285, 149257))
    state.atr_short_flip_bar_ts = bar_ms
    if fault == "old":
        clock[0] += 600_000
    elif fault == "seed":
        strategy._bar_observation_phase = "seed"
    else:
        state.atr_short_flip_bar_ts = 0
    strategy._cw_v2_track(state, {"flip": "SELL", "observation_phase": strategy._bar_observation_phase})
    assert state.flip_owner_phase in {"bound", "unknown"}
    clock[0] = FLYE["fresh_sell_poll_ms"]
    settle(strategy, request, clock)
    book(strategy, record, clock)
    assert state.flip_owner_phase == "bound"


def test_boundary_persistence_failure_cannot_release():
    strategy, state, record, clock, _ = replay()
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    def fail(*_):
        raise RuntimeError("owner store unavailable")
    strategy._flip_owner_persist = fail
    sell(strategy, state, clock)
    assert state.flip_owner_phase == "unknown"
    settle(strategy, request, clock)
    book(strategy, record, clock)
    assert state.flip_owner_phase != "idle"


@pytest.mark.parametrize("fault", ["unclear", "stale", "token", "wrong_purpose", "write_failed"])
def test_unknown_cancel_receipt_never_releases_after_sell(fault):
    strategy, state, record, clock, _ = replay()
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    proof = RemovedWaitProof(request, clock[0], True, "retry_leftovers_cancelled_owner_kept")
    if fault == "unclear":
        proof = replace(proof, clear=False, reason="broker_terminal_unproven")
    elif fault == "stale":
        proof = replace(proof, observed_at_ms=clock[0] - 600_000)
    elif fault == "token":
        proof = replace(proof, request=replace(request, token="wrong"))
    elif fault == "wrong_purpose":
        proof = replace(proof, reason="terminal_unfilled_removed_wait")
    else:
        def fail(*_):
            raise RuntimeError("store unavailable")
        strategy._removed_wait_persist = fail
    strategy.apply_removed_wait_proofs([proof])
    book(strategy, record, clock)
    assert state.flip_owner_phase == "awaiting_close"
    assert not strategy._strict_first_rest_admitted(state, slot="first")


@pytest.mark.parametrize("fault", ["sibling_open", "different_row_open", "union", "held", "unreadable"])
def test_deferred_release_never_releases_open_or_unknown_position(fault):
    strategy, state, record, clock, _ = replay()
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    settle(strategy, request, clock)
    legs = ()
    if fault in {"sibling_open", "different_row_open"}:
        account = WEBULL if fault == "sibling_open" else PRIMARY
        legs = (FlipPositionLeg(account, "different-open-row", clock[0], 1),)
    if fault == "union":
        state.position_qty = 1
    if fault == "held":
        state.position_qty_held = 1
    book(strategy, record, clock, legs=legs, readable=fault != "unreadable")
    assert state.flip_owner_phase != "idle"
    assert not strategy._strict_first_rest_admitted(state, slot="first")


@pytest.mark.parametrize("fault", ["union", "held", "sibling_open"])
def test_sell_itself_cannot_retire_an_open_position(fault):
    strategy, state, record, clock, _ = replay()
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    settle(strategy, request, clock)
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    if fault == "union":
        state.position_qty = 1
    elif fault == "held":
        state.position_qty_held = 1
    else:
        state.flip_owner_open_positions[WEBULL] = FlipPositionLeg(WEBULL, "sibling", clock[0], 1)
    sell(strategy, state, clock)
    assert state.flip_owner_phase == "awaiting_close"
    assert state.flip_owner_opportunity_id == record.opportunity_id


def test_sell_boundary_survives_restart_while_cancellation_is_pending():
    strategy, state, record, clock, _ = replay()
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    persisted = strategy._flip_owner_record(state)
    assert persisted.phase == "awaiting_close"
    restarted, restored, _, restart_clock, _ = strategy_for_record(
        persisted, datetime.fromtimestamp(clock[0] / 1000, UTC), quantities=(280, 140),
    )
    restored.position_qty = restored.position_qty_held = 0
    restored.retry_one_segment_id = FLYE["fresh_sell_bar_ms"]
    restarted.configure_removed_wait(lambda *_: None, restored={"FLYE": request}, readable=True)
    book(restarted, persisted, restart_clock)
    assert restored.flip_owner_phase == "awaiting_close"
    settle(restarted, request, restart_clock)
    book(restarted, persisted, restart_clock)
    assert restored.flip_owner_phase == "idle"


def test_repeated_flat_polls_do_not_write_owner_or_budget():
    strategy, state, record, clock, _ = replay()
    book(strategy, record, clock)
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    writes = []
    strategy._flip_owner_persist = lambda *args: writes.append(args)
    strategy._retry_one_budget_persist = lambda *args: writes.append(args)
    for _ in range(100):
        book(strategy, record, clock)
    assert writes == []


@pytest.mark.parametrize("phase", ["resting", "awaiting_fill"])
@pytest.mark.parametrize("fault", ["none", "union", "held", "open", "no_terminal"])
def test_unfilled_pending_union_recovers_only_after_exact_terminal_and_flat_book(phase, fault):
    from tests.unit.test_v2_flip_owned_first_entry import _book, _place_first, _strategy

    strategy, clock, _, _ = _strategy(dual=True)
    state, opportunity = _place_first(strategy, clock, "FLYE")
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    state.flip_owner_phase = phase
    state.position_qty = 1  # Conservative pending BUY union; no held shares or managed row.
    _book(strategy, clock, "FLYE")
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "unknown"
    state.position_qty = 1 if fault == "union" else 0
    state.position_qty_held = 1 if fault == "held" else 0
    legs = (FlipPositionLeg(WEBULL, "test-live-row", clock[0], 1),) if fault == "open" else ()
    _book(strategy, clock, "FLYE", *legs,
          terminal_unfilled_opportunities=frozenset() if fault == "no_terminal" else frozenset({opportunity}))
    assert (state.flip_owner_phase == "idle") is (fault == "none")


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("receipt_clear", [False, True])
@pytest.mark.parametrize("book_fault", ["none", "open", "unknown", "held"])
def test_fresh_sell_ends_prior_false_restore_only_after_its_barrier(pm, receipt_clear, book_fault):
    from tests.unit.test_falseflip1_runtime import ack, book as false_book, runtime

    # H's recorded FALSE entry facts use explicit controlled owner/receipt mappings.
    strategy, state, clock, proofs, _ = runtime(pm=pm)
    false_book(strategy, state, clock, proofs)
    ack(strategy)
    prior_budget = strategy._falseflip_budget(state)
    false_book(strategy, state, clock, proofs)
    request, = strategy.pending_falseflip_cancel_publications()
    strategy._removed_wait_persist(request, True)
    strategy.acknowledge_falseflip_cancel_publication(request)
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()

    clock[0] += 60_000
    state.bars.append(OHLCVBar(clock[0], 2.33, 2.3499, 2.27, 2.285, 149257))
    state.atr_short_flip_bar_ts = clock[0]
    false_book(strategy, state, clock, proofs)
    strategy._cw_v2_track(state, {"flip": "SELL", "observation_phase": "live"})
    assert state.flip_owner_phase == "awaiting_close"
    assert state.retry_one_segment_id > prior_budget.segment
    assert strategy._falseflip_budget(state).episodes == ()
    assert strategy._falseflip_budget(state).refunded_counts == ()
    strategy.apply_removed_wait_proofs((RemovedWaitProof(
        request, clock[0], receipt_clear,
        "false_flip_leftovers_cancelled_owner_kept" if receipt_clear else "broker_terminal_unproven",
    ),))
    legs = (FlipPositionLeg(WEBULL, "working-sibling", clock[0], 1),) if book_fault == "open" else ()
    if book_fault == "held":
        state.position_qty_held = 1
    strategy.apply_flip_position_book(FlipPositionBook(
        clock[0], book_fault != "unknown", {state.symbol: legs} if legs else {},
        closes_by_symbol={state.symbol: tuple(FlipPositionClose(
            p.identity.account, p.identity.managed_row_id, "CW_HARD_STOP") for p in proofs)},
        entry_classifications={state.symbol: tuple(p.as_payload() for p in proofs)},
        filled_opportunities={state.symbol: tuple((p.identity.account, p.identity.opportunity_id) for p in proofs)},
        closed_entry_rows=frozenset((p.identity.account, p.identity.managed_row_id) for p in proofs),
    ))
    released = receipt_clear and book_fault == "none"
    assert (state.flip_owner_phase == "idle") is released
    assert strategy._strict_first_rest_admitted(state, slot="first") is released
    assert strategy._falseflip_budgets[(prior_budget.symbol, prior_budget.segment)] == prior_budget
