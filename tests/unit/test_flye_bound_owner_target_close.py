"""Recorded FLYE boundary; positive cancellation receipts are explicit replay controls."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
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
