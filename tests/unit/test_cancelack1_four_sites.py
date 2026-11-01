"""Synthetic all-site controls. No WORX/SAIQ/POM historical/native claims."""


from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from decimal import Decimal
import asyncio
from threading import Event, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from project_mai_tai.confirmation_exit import ConfirmationEntry, ConfirmationExitTracker
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill,
    OmsManagedPosition, Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.v2_removed_wait import DISPATCH_SNAPSHOT_TYPE
from tests.unit.test_cancelack1 import synthetic_case
from tests.unit.test_v2_fresh_sell_owner_release import MIRROR, PRIMARY, instant, refresh
from tests.unit.test_v2_fresh_sell_owner_release import commit_and_later_bar, sell


def terminal_buys(bot, request):
    with bot.session_factory() as session:
        for order in session.scalars(select(BrokerOrder)):
            stamp = order.updated_at
            flag_modified(order, "updated_at")
            order.broker_order_id = f"SYNTHETIC-native-{order.client_order_id}"
            session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled",
                event_source="broker", event_at=instant(request.requested_at_ms + 1),
                payload={"cancel_outcome": "confirmed"}))
            session.flush()
            session.refresh(order)
            assert order.updated_at.replace(tzinfo=None) == stamp.replace(tzinfo=None)
        session.commit()


def fault_history(bot, request, clock, fault):
    with bot.session_factory() as session:
        for row in session.scalars(select(TradeIntent)):
            flag_modified(row, "updated_at")
        if fault == "missing_history":
            for row in session.scalars(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == DISPATCH_SNAPSHOT_TYPE)):
                session.delete(row)
        elif fault == "pending_cancel":
            row = session.scalar(select(TradeIntent).where(TradeIntent.intent_type == "cancel"))
            row.status = "pending"
        elif fault == "unknown_cancel":
            row = session.scalar(select(TradeIntent).where(TradeIntent.intent_type == "cancel"))
            row.status = "cancelled"
            row.payload = {**row.payload, "metadata": {**row.payload["metadata"],
                                                       "cancel_outcome": "could_not_tell"}}
        elif fault == "other_orb_working":
            foreign = session.scalar(select(Strategy).where(Strategy.code == "orb_schwab"))
            primary = session.scalar(select(BrokerAccount).where(BrokerAccount.name == PRIMARY))
            session.add(BrokerOrder(strategy_id=foreign.id, broker_account_id=primary.id,
                symbol=request.symbol, side="buy", quantity=1, status="accepted",
                client_order_id="SYNTHETIC-other-ORB", order_type="limit", time_in_force="day",
                submitted_at=instant(clock[0] - 30_000), updated_at=instant(clock[0] - 20_000)))
        session.commit()
    if fault == "stale_positions":
        refresh(bot, clock, age=15_001)


@pytest.mark.asyncio
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("required", [(MIRROR,), (PRIMARY,), (PRIMARY, MIRROR)])
async def test_leftover_request_not_opened_for_proven_completed_obligations(monkeypatch, pm, required):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, pm=pm, required=required)
    try:
        bot.strategy._removed_wait_requests.clear()
        before = (state.flip_owner_phase, state.fanout_segment_id, state.retry_one_segment_id,
                  state.cw_resting_taken, state.cw_reclaim_taken, state.fanout_last_retired_opportunity_id)
        await bot._cancelack_obligations_poll()
        bot.strategy._cancel_retry_leftovers(state)
        assert not bot.strategy._removed_wait_requests and writes == []
        assert before == (state.flip_owner_phase, state.fanout_segment_id, state.retry_one_segment_id,
                          state.cw_resting_taken, state.cw_reclaim_taken, state.fanout_last_retired_opportunity_id)
        assert not bot.strategy.drain_pending_intents() and not bot.strategy.drain_webull_direct_intents()
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("required", [(MIRROR,), (PRIMARY,)])
@pytest.mark.parametrize("fault", ["missing_history", "other_orb_working", "stale_positions",
                                   "expired_cache", "active_leg"])
async def test_leftover_unknown_does_not_skip_barrier(monkeypatch, pm, required, fault):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, pm=pm, required=required)
    try:
        bot.strategy._removed_wait_requests.clear()
        fault_history(bot, request, clock, fault)
        await bot._cancelack_obligations_poll()
        if fault == "expired_cache":
            clock[0] += 15_001
        elif fault == "active_leg":
            state.resting_active = True
        bot.strategy._cancel_retry_leftovers(state)
        pending = bot.strategy._removed_wait_requests[request.symbol]
        assert pending.purpose == "retry_exhausted" and pending.opportunity_id == request.opportunity_id
        assert bot.strategy._removed_wait_gate_closed(request.symbol)
        assert state.flip_owner_phase == "bound"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_new_dispatch_invalidates_cached_missing_leg_waiver(monkeypatch):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch)
    try:
        bot.strategy._removed_wait_requests.clear()
        await bot._cancelack_obligations_poll()
        key = (request.symbol, request.opportunity_id)
        assert key in bot.strategy._cancelack_obligations
        draft = SimpleNamespace(symbol=request.symbol, metadata={"fanout_segment_id": str(request.opportunity_id)})
        bot.strategy.track_removed_wait_dispatch(draft, PRIMARY)
        assert key not in bot.strategy._cancelack_obligations
        bot.strategy._cancel_retry_leftovers(state)
        assert bot.strategy._removed_wait_gate_closed(request.symbol)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_generation_change_during_read_does_not_restore_old_cache(monkeypatch):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch)
    try:
        bot.strategy._removed_wait_requests.clear()
        original = bot._fetch_cancelack_obligations
        def controlled_epoch_change(scopes):
            proofs = original(scopes)
            assert proofs  # An actually produced proof, not a manually invented success.
            bot.strategy._cancelack_dispatch_generation += 1
            return proofs
        monkeypatch.setattr(bot, "_fetch_cancelack_obligations", controlled_epoch_change)
        await bot._cancelack_obligations_poll()
        assert bot.strategy._cancelack_obligations == {}
        bot.strategy._cancel_retry_leftovers(state)
        assert bot.strategy._removed_wait_gate_closed(request.symbol)
    finally:
        engine.dispose()


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("required", [(MIRROR,), (PRIMARY,), (PRIMARY, MIRROR)])
@pytest.mark.parametrize("fault", ["none", "pending_cancel", "missing_history", "other_orb_working",
                                   "stale_positions", "unknown_cancel"])
def test_active_and_flag_off_receipt_readers_keep_other_gates(monkeypatch, pm, legacy, required, fault):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, pm=pm, required=required)
    try:
        terminal_buys(bot, request)
        fault_history(bot, request, clock, fault)
        reader = bot._removed_wait_store.proofs if legacy else bot._removed_wait_store.clear_rule_proofs
        proof, = reader((request,), set(request.account_names), now=instant(clock[0]))
        expected = fault == "none" or (fault == "missing_history" and len(required) == 2)
        assert proof.clear is expected, proof.reason
        if expected:
            assert proof.reason == "retry_leftovers_cancelled_owner_kept"
        assert state.flip_owner_phase == "bound" and bot.strategy._removed_wait_requests[request.symbol] == request
        assert writes == []  # Reading a cancellation receipt does not retire ownership.
    finally:
        engine.dispose()


@pytest.mark.parametrize("required", [(MIRROR,), (PRIMARY,)])
@pytest.mark.parametrize("fault", ["none", "pending_cancel", "missing_history"])
def test_flag_off_scanner_journal_keeps_universe_distinct_from_attempts(monkeypatch, required, fault):
    bot, state, clock, request, writes, engine = synthetic_case(
        monkeypatch, required=required, purpose="scanner_removal")
    try:
        terminal_buys(bot, request)
        with bot.session_factory() as session:
            owner = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership"))
            owner.payload = {**owner.payload, "phase": "idle", "active": False,
                             "fill_accounts": [], "position_ids": {}, "position_entry_ms": {}}
            session.commit()
        fault_history(bot, request, clock, fault)
        proof, = bot._removed_wait_store.proofs((request,), set(request.account_names), now=instant(clock[0]))
        assert proof.clear is (fault == "none"), proof.reason
        if proof.clear:
            assert proof.reason == "terminal_unfilled_removed_wait"
    finally:
        engine.dispose()


def confirmation_case(bot, request, clock, required, fault):
    with bot.session_factory() as session:
        accounts = {a.name: a for a in session.scalars(select(BrokerAccount))}
        own = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.broker_account_id == accounts[required[0]].id))
        flag_modified(order, "updated_at")
        order.status = "filled"
        order.broker_order_id = "SYNTHETIC-confirmation-native"
        fill_id, native_fill = uuid4(), "SYNTHETIC-confirmation-fill"
        filled_at = instant(request.opportunity_id + 100)
        session.add(Fill(id=fill_id, order_id=order.id, strategy_id=own.id,
            broker_account_id=order.broker_account_id, symbol=request.symbol, side="buy",
            quantity=1, price=1, broker_fill_id=native_fill, filled_at=filled_at))
        session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=required[0],
            symbol=request.symbol, entry_order_id=order.id, entry_client_order_id=order.client_order_id,
            entry_price=1, original_quantity=1, current_quantity=0, status="closed"))
        for name in required:
            if fault == "missing_actual_vp" and name == required[0]:
                continue
            session.add(VirtualPosition(strategy_id=own.id, broker_account_id=accounts[name].id,
                symbol=request.symbol, quantity=Decimal(1 if fault == "held_actual_vp" else 0), average_price=1))
        if fault == "historical_other_leg":
            owner = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership"))
            owner.payload = {**owner.payload, "fill_accounts": [PRIMARY, MIRROR]}
        entry = ConfirmationEntry(order.id, fill_id, native_fill, order.broker_order_id, required[0],
            request.symbol, filled_at, clock[0] - 60_000, 1, None, filled_at,
            order.payload["fanout_slot_id"])
        session.commit()
    return entry


@pytest.mark.asyncio
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("required", [(MIRROR,), (PRIMARY,), (PRIMARY, MIRROR)])
@pytest.mark.parametrize("fault", ["none", "missing_actual_vp", "held_actual_vp", "historical_other_leg",
                                   "missing_history", "other_orb_working", "stale_positions"])
async def test_confirmation_missing_unused_row_is_not_missing_actual_owner(monkeypatch, pm, required, fault):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, pm=pm, required=required)
    try:
        entry = confirmation_case(bot, request, clock, required, fault)
        clock[0] += 24 * 60 * 60_000
        refresh(bot, clock)
        fault_history(bot, request, clock, fault)
        flat = bot._closed_confirmation_entry_ids([entry])
        # The complete-two-VP legacy path is unchanged; it waives no missing leg.
        expected = fault == "none" or (len(required) == 2 and fault in {
            "historical_other_leg", "missing_history", "stale_positions"})
        assert (entry.order_id in flat) is expected
        bot._confirmation_exit = ConfirmationExitTracker()
        assert bot._confirmation_exit.add(entry)
        await bot._retire_previous_confirmation_entries()
        assert bot._confirmation_exit.pending_count == (0 if expected else 1)
        assert writes == [] and state.flip_owner_phase == "bound"
    finally:
        engine.dispose()


def test_retry_unknown_diagnostic_is_not_silent_or_repeated(monkeypatch, caplog):
    from project_mai_tai.v2_removed_wait import RemovedWaitProof

    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch)
    try:
        proof = RemovedWaitProof(request, clock[0], False, "waiting_for_all_cancel_receipts")
        bot.strategy.apply_removed_wait_proofs((proof,))
        bot.strategy.apply_removed_wait_proofs((proof,))
        messages = [r.message for r in caplog.records if "receipt=UNKNOWN reason=waiting_for_all_cancel_receipts" in r.message]
        assert len(messages) == 1
        assert bot.strategy._removed_wait_gate_closed(request.symbol) and writes == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("required", [(MIRROR,), (PRIMARY,)])
@pytest.mark.parametrize("underlying_fresh", [False, True])
async def test_second_sell_does_not_reuse_aged_proof_or_relax_position_age(
    monkeypatch, caplog, pm, required, underlying_fresh,
):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, pm=pm, required=required)
    try:
        caplog.set_level("INFO")
        await bot._refresh_fresh_sell_before_live_bar(request.symbol, observation_phase="live")
        old = bot.strategy._fresh_sell_release_evidence[request.symbol]
        assert old.clear
        sell(bot, state)
        assert state.flip_owner_phase == "bound"
        clock[0] += 15_001
        refresh(bot, clock, age=0 if underlying_fresh else 15_001)
        sell(bot, state)
        assert state.flip_owner_phase == "bound"
        assert any("reason=cancel_evidence_stale" in r.message for r in caplog.records)
        await bot._refresh_fresh_sell_before_live_bar(request.symbol, observation_phase="live")
        fresh = bot.strategy._fresh_sell_release_evidence[request.symbol]
        assert fresh is not old and fresh.clear is underlying_fresh
        sell(bot, state)
        assert state.flip_owner_phase == "bound" and writes == []
        if underlying_fresh:
            await commit_and_later_bar(bot, state, clock)
            assert state.flip_owner_phase == "idle" and not bot.strategy._removed_wait_requests
        else:
            assert request.symbol in bot.strategy._removed_wait_requests
        assert not bot.strategy.drain_pending_intents() and not bot.strategy.drain_webull_direct_intents()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_replay_bar_boundary_does_not_query_fresh_sell_proof(monkeypatch):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch)
    try:
        def forbidden(*args):
            raise AssertionError("replay must not refresh live-SELL evidence")
        monkeypatch.setattr(bot, "_fetch_fresh_sell_release_evidence", forbidden)
        await bot._refresh_fresh_sell_before_live_bar(request.symbol, observation_phase="replay")
        assert bot.strategy._fresh_sell_release_evidence == {}
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_older_success_cannot_overwrite_newer_unknown_refresh(monkeypatch):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch)
    ready, release, lock = Event(), Event(), Lock()
    reads = [0]
    original = bot._fetch_fresh_sell_release_evidence
    def delayed_first_read(requests):
        with lock:
            reads[0] += 1
            first = reads[0] == 1
        result = original(requests)
        if first:
            assert result[0].clear
            ready.set()
            assert release.wait(3)
        return result
    monkeypatch.setattr(bot, "_fetch_fresh_sell_release_evidence", delayed_first_read)
    first_task = asyncio.create_task(bot._fresh_sell_owner_release_poll())
    try:
        assert await asyncio.to_thread(ready.wait, 2)
        assert request.symbol not in bot.strategy._fresh_sell_release_evidence
        fault_history(bot, request, clock, "pending_cancel")
        await bot._fresh_sell_owner_release_poll()
        assert not bot.strategy._fresh_sell_release_evidence[request.symbol].clear
        release.set()
        await first_task
        assert not bot.strategy._fresh_sell_release_evidence[request.symbol].clear
        sell(bot, state)
        assert state.flip_owner_phase == "bound" and writes == []
    finally:
        release.set()
        await first_task
        engine.dispose()


@pytest.mark.parametrize("fault", ["missing_stamp", "null_stamp", "false_stamp", "missing_kind",
                                   "missing_payload", "malformed_payload", "null_side", "sell", "unknown_side"])
def test_missing_or_contradictory_cancel_capability_is_not_default_buy(fault):
    from project_mai_tai.cancelack1 import buy_only_cancel, cancel_outcome_known

    row = SimpleNamespace(intent_type="cancel", payload={"metadata": {"clearwait_buy_only": "true"}})
    assert buy_only_cancel(row)  # The fault, not an unrelated missing field, must cause the HOLD.
    if fault == "missing_stamp":
        row.payload["metadata"].clear()
    elif fault in {"null_stamp", "false_stamp"}:
        row.payload["metadata"]["clearwait_buy_only"] = None if fault == "null_stamp" else "false"
    elif fault == "missing_kind":
        del row.intent_type
    elif fault == "missing_payload":
        del row.payload
    elif fault == "malformed_payload":
        row.payload = {"metadata": []}
    else:
        row.side = {"null_side": None, "sell": "sell", "unknown_side": "unknown"}[fault]
    assert not buy_only_cancel(row)
    if fault in {"missing_payload", "malformed_payload"}:
        assert not cancel_outcome_known(row)


@pytest.mark.parametrize("fault", ["missing_stamp", "null_side", "sell", "unknown_outcome"])
def test_legacy_projection_missing_safety_evidence_keeps_actual_assessor_held(fault):
    from tests.unit.test_clearwait1_removed_wait import _recorded
    from project_mai_tai.v2_removed_wait import assess_removed_wait

    request, rows = _recorded("AIXI", include_tickets=False)
    assert assess_removed_wait(request, **rows).clear
    cancel = rows["intents"][-1]
    assert not hasattr(cancel, "side") and cancel.payload["metadata"]["clearwait_buy_only"] == "true"
    if fault == "missing_stamp":
        del cancel.payload["metadata"]["clearwait_buy_only"]
    elif fault == "unknown_outcome":
        cancel.payload["metadata"]["cancel_outcome"] = "could_not_tell"
    else:
        cancel.side = None if fault == "null_side" else "sell"
    proof = assess_removed_wait(request, **rows)
    assert not proof.clear
