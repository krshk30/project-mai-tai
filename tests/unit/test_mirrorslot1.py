"""Recorded identities/levels; delivery, fresh quotes and venue replies CONTROLLED."""
from datetime import datetime, timedelta
from decimal import Decimal
import json
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, DashboardSnapshot, TradeIntent
from project_mai_tai.fanout_outcome_consumer import FanoutOutcomeJournal, MIRROR_REFUSAL_PREFIX, OUTCOME_SNAPSHOT_TYPE
from project_mai_tai.oms.mirror_retained_hold import row_id
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from tests.unit.test_mirrorhold1_retained_hold import lane as _lane, event_for, quote, queued, restart, state, hold_and_queue

lane = _lane


CASES = json.loads((Path(__file__).parents[1] / "fixtures/mirrorslot1_recorded.json").read_text())["cases"]
HISTORY = json.loads((Path(__file__).parents[1] / "fixtures/mirrorslot1_history.json").read_text())


def cancel_for(event, clock, reason="liquidity_floor"):
    cancel = event.model_copy(deep=True)
    cancel.event_id = uuid4()
    cancel.produced_at = clock[0]
    cancel.payload.intent_type = "cancel"
    cancel.payload.metadata.update(reason=reason, resting_entry_cancel="true")
    return cancel


def record_cancel(lane, cancel):
    with lane[2]() as session:
        account = lane[0].store.ensure_broker_account(session, cancel.payload.broker_account_name,
            provider="webull", environment="live")
        strategy = lane[0].store.ensure_strategy(session, cancel.payload.strategy_code)
        session.add(TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
            symbol=cancel.payload.symbol, side="buy", intent_type="cancel", quantity=Decimal(1),
            reason="CONTROLLED delivery of captured cancel", status="rejected", created_at=cancel.produced_at,
            payload={"metadata": dict(cancel.payload.metadata)}))
        session.commit()


def native_claim(event):
    strategy = SchwabV2Strategy(Settings(strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name=event.payload.broker_account_name))
    s = strategy.watchlist_state(event.payload.symbol)
    md = event.payload.metadata
    s.fanout_segment_id = int(md["fanout_segment_id"])
    s.fanout_claim_slot_id = md["fanout_slot_id"]
    s.fanout_claim_slot = "resting"
    s.fanout_claim_attempt_id = md["fanout_attempt_id"]
    s.webull_resting_generation_id = md["webull_mirror_generation_id"]
    s.fanout_webull_claimed = s.webull_resting_active = True
    s.resting_active = s.resting_is_broker_order = True
    s.resting_level = 1.23
    return strategy, s


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["symbol"])
async def test_four_recorded_rearms_legacy_reason_proof_and_duplicate(lane, case):
    service, client, factory, clock = lane
    clock[0] = datetime.fromisoformat(case["old_at"])
    old = event_for(lane, symbol=case["symbol"], segment=case["segment"], stop=case["old_stop"])
    old.payload.metadata.update(fanout_slot_id=case["slot"], limit_price=case["old_limit"],
        webull_mirror_generation_id=case["generation"], rpg_resting_generation=case["rpg"])
    history = next(r for r in HISTORY["owners"] if r["identity"][2] == case["symbol"])
    old.event_id = UUID(history["event_id"])
    old.payload.quantity = Decimal(history["quantity"])
    # Preserve actual old orders and broker-sourced audits, including missing metadata keys.
    with factory() as session:
        account = service.store.ensure_broker_account(session, old.payload.broker_account_name,
            provider="webull", environment="live")
        strategy_row = service.store.ensure_strategy(session, old.payload.strategy_code)
        for raw in [r for r in HISTORY["orders"] if r["symbol"] == case["symbol"]]:
            assert raw["fill_count"] == 0 and raw["filled_quantity"] == 0
            prior = old.model_copy(deep=True)
            prior.event_id = UUID(raw["event_id"])
            prior.payload.quantity = Decimal(str(raw["quantity"]))
            prior.payload.metadata.update(raw["metadata"])
            intent = service.store.create_trade_intent(session, strategy=strategy_row, broker_account=account, event=prior)
            order = service.store.get_or_create_order(session, intent=intent, strategy_id=strategy_row.id,
                broker_account_id=account.id, client_order_id=raw["client"], symbol=raw["symbol"], side="buy",
                quantity=prior.payload.quantity, metadata=raw["metadata"], status=raw["status"], order_type="stop_limit")
            order.submitted_at = datetime.fromisoformat(raw["submitted_at"])
            for audit in [a for a in HISTORY["audits"] if a["client"] == raw["client"]]:
                session.add(BrokerOrderEvent(order_id=order.id, event_type=audit["event_type"],
                    event_source=audit["source"], event_at=datetime.fromisoformat(audit["event_at"]),
                    payload={"client_order_id":audit["event_client"], "reason":audit["reason"],
                             "metadata":audit["metadata"]}))
        session.commit()
    service._mirrorhold_observe_intent(old)
    with factory() as session:
        row = session.get(DashboardSnapshot, row_id(old))
        assert row.payload["wire_submissions"] == int(history["wire_submissions"])
        assert row.payload["wire_clients"] == history["wire_clients"]
        service._mirrorhold_write(session, row, {**row.payload, "phase":"retired", "reason":"v2_terminal_cancel",
            "dispatch_client":history["dispatch_client"], "dispatch_unresolved":False,
            "local_no_wire":history["local_no_wire"] == "true"})
        session.commit()
    clock[0] = datetime.fromisoformat(case["cancel_at"])
    cancel = cancel_for(old, clock)
    cancel.event_id = UUID(case["cancel_event"])
    record_cancel(lane, cancel)
    lane = restart(lane)
    service = lane[0]
    client._instrument = "CONTROLLED"
    service.broker_adapter._instrument_cache[case["symbol"]] = "CONTROLLED-" + case["symbol"]
    for index, (at, stop, limit) in enumerate(case["rearms"]):
        clock[0] = datetime.fromisoformat(at)
        fresh = event_for(lane, symbol=case["symbol"], segment=case["segment"], stop=stop)
        fresh.payload.metadata.update(fanout_slot_id=case["slot"], limit_price=limit)
        quote(lane, fresh, str(Decimal(stop) * Decimal("0.98")))
        if index:
            # CONTROLLED broker cancel confirmation between recorded reprice drafts.
            with factory() as session:
                order = session.scalar(select(BrokerOrder).where(
                    BrokerOrder.client_order_id == state(lane, fresh)["dispatch_client"]))
                order.status = "cancelled"
                session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled", event_source="broker",
                    payload={"client_order_id":order.client_order_id, "metadata":dict(order.payload)}))
                session.commit()
        result = await service.process_trade_intent(fresh)
        assert result[-1].payload.status == "accepted"
        assert state(lane, fresh)["event"]["payload"]["metadata"]["stop_price"] == stop
        assert state(lane, fresh)["wire_submissions"] == int(history["wire_submissions"]) + index + 1
        assert state(lane, fresh)["price_aggressive_refusals"] == 0
        await service.process_trade_intent(fresh)
        assert client.calls["place"] == index + 1
    print(f"MIRRORSLOT1 recorded={case['symbol']} old_wires={history['wire_submissions']} levels={[r[1] for r in case['rearms']]} controlled_new_wires={len(case['rearms'])} duplicate_wires=0")


@pytest.mark.asyncio
async def test_pause_is_cold_across_queued_delivery_ticks_and_restart(lane):
    service, client, _, clock = lane
    event, retry = await hold_and_queue(lane)
    clock[0] += timedelta(seconds=1)
    service._mirrorhold_observe_intent(cancel_for(event, clock))
    assert state(lane, event)["phase"] == "paused"
    lane = restart(lane)
    quote(lane, event, "5.2")
    await lane[0].process_trade_intent(retry)
    await lane[0].process_trade_intent(event)
    for _ in range(4):
        await lane[0]._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    assert client.calls.get("place", 0) == 0
    assert len(queued(lane)) == 0
    assert state(lane, event)["phase"] == "paused"
    clock[0] += timedelta(seconds=1)
    fresh = event_for(lane, stop="5.35")
    quote(lane, fresh, "5.2")
    await lane[0].process_trade_intent(fresh)
    assert client.calls["place"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["filled", "capped"])
async def test_cancel_never_downgrades_monotonic_fence(lane, phase):
    service, client, factory, clock = lane
    event, _ = await hold_and_queue(lane)
    with factory() as session:
        row = session.get(DashboardSnapshot, row_id(event))
        service._mirrorhold_write(session, row, {**row.payload, "phase":phase})
        session.commit()
    clock[0] += timedelta(seconds=1)
    service._mirrorhold_observe_intent(cancel_for(event, clock))
    assert state(lane, event)["phase"] == phase
    fresh = event_for(lane)
    await service.process_trade_intent(fresh)
    assert state(lane, event)["phase"] == phase
    assert client.calls.get("place", 0) == 0


@pytest.mark.asyncio
async def test_actual_committed_refusal_clears_native_false_resting(lane, caplog):
    service, _, factory, clock = lane
    event, _ = await hold_and_queue(lane)
    clock[0] += timedelta(seconds=1)
    service._mirrorhold_observe_intent(cancel_for(event, clock, "window_closed"))
    fresh = event_for(lane)
    assert await service.process_trade_intent(fresh) == []
    strategy, s = native_claim(fresh)
    applied = []
    journal = FanoutOutcomeJournal(factory)
    journal.bootstrap(lambda record: applied.append(strategy.apply_fanout_outcome(record)),
        active_segments={fresh.payload.symbol: s.fanout_segment_id}, now=clock[0])
    assert "released" in applied
    assert not s.webull_resting_active and not s.fanout_webull_claimed
    assert s.resting_active and s.resting_is_broker_order and s.resting_level == 1.23
    assert "slot=" + fresh.payload.metadata["fanout_slot_id"] in caplog.text
    assert "owner_retired" in caplog.text
    with factory() as session:
        outcomes = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE)).all()
        assert any(r.payload["reason"].startswith(MIRROR_REFUSAL_PREFIX) for r in outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [True, False])
async def test_pause_old_order_requires_exact_broker_clear_preserves_history(lane, uncertain):
    service, client, factory, clock = lane
    old = event_for(lane)
    quote(lane, old, "5.2")
    await service.process_trade_intent(old)
    clock[0] += timedelta(seconds=1)
    service._mirrorhold_observe_intent(cancel_for(old, clock))
    with factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == state(lane, old)["dispatch_client"]))
        order.status = "cancelled"
        if not uncertain:
            session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled", event_source="broker",
                payload={"client_order_id":order.client_order_id, "metadata":dict(order.payload)}))
        session.commit()
    clock[0] += timedelta(seconds=1)
    fresh = event_for(lane, stop="5.30")
    quote(lane, fresh, "5.2")
    await service.process_trade_intent(fresh)
    assert client.calls["place"] == (1 if uncertain else 2)
    assert state(lane, fresh)["wire_submissions"] == (1 if uncertain else 2)
    if uncertain:
        assert state(lane, fresh)["phase"] == "paused"
        strategy, s = native_claim(fresh)
        journal = FanoutOutcomeJournal(factory)
        journal.bootstrap(strategy.apply_fanout_outcome,
            active_segments={fresh.payload.symbol:s.fanout_segment_id}, now=clock[0])
        assert s.fanout_claim_outcome == "mirror_uncertain"
        assert not s.webull_resting_active and s.fanout_webull_claimed
        # The unrelated Schwab-scoped zero transition cannot prove old Webull clearance.
        strategy._flip_owned_first_entry_enabled = False
        s.position_qty = 1
        strategy.update_position(fresh.payload.symbol, 0)
        assert s.fanout_webull_claimed and s.fanout_claim_outcome == "mirror_uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "reason", "generation", "account", "segment", "stale"])
async def test_legacy_retired_requires_real_matching_cancel(lane, change):
    service, client, factory, clock = lane
    old, _ = await hold_and_queue(lane)
    clock[0] += timedelta(seconds=1)
    cancel = cancel_for(old, clock)
    if change == "reason":
        cancel.payload.metadata["reason"] = "window_closed"
    elif change == "generation":
        cancel.payload.metadata["webull_mirror_generation_id"] = "other"
    elif change == "account":
        cancel.payload.broker_account_name = "live:other"
    elif change == "segment":
        cancel.payload.metadata["fanout_segment_id"] = "123"
    elif change == "stale":
        cancel.produced_at = old.produced_at - timedelta(seconds=1)
    if change != "missing":
        record_cancel(lane, cancel)
    with factory() as session:
        row = session.get(DashboardSnapshot, row_id(old))
        service._mirrorhold_write(session, row, {**row.payload, "phase":"retired", "token":"",
            "reason":"v2_terminal_cancel"})
        session.commit()
    clock[0] += timedelta(seconds=1)
    fresh = event_for(lane)
    quote(lane, fresh, "5.2")
    assert await service.process_trade_intent(fresh) == []
    assert state(lane, old)["phase"] == "retired"
    assert client.calls.get("place", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["same_generation", "deferred", "same_time", "inactive_segment"])
async def test_pause_new_nonce_alone_is_not_permission(lane, change):
    service, client, factory, clock = lane
    old, _ = await hold_and_queue(lane)
    clock[0] += timedelta(seconds=1)
    service._mirrorhold_observe_intent(cancel_for(old, clock))
    if change != "same_time":
        clock[0] += timedelta(seconds=1)
    fresh = event_for(lane)
    if change == "same_generation":
        fresh.payload.metadata["webull_mirror_generation_id"] = old.payload.metadata["webull_mirror_generation_id"]
    elif change == "deferred":
        fresh.payload.metadata["webull_deferred_resubmit"] = "true"
    elif change == "inactive_segment":
        from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
        FanoutSegmentIdentityStore(factory).record(fresh.payload.symbol,
            int(fresh.payload.metadata["fanout_segment_id"]), False, "CONTROLLED end",
            now=clock[0] + timedelta(milliseconds=1))
    quote(lane, fresh, "5.2")
    assert await service.process_trade_intent(fresh) == []
    assert state(lane, old)["phase"] == "paused"
    assert client.calls.get("place", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["attempt", "generation", "slot", "filled"])
async def test_native_refusal_exactness_and_filled_monotonic(lane, change):
    service, _, factory, clock = lane
    event, _ = await hold_and_queue(lane)
    with factory() as session:
        service._mirrorhold_feedback(session, event, "controlled_refusal")
        session.commit()
    strategy, s = native_claim(event)
    if change == "attempt":
        s.fanout_claim_attempt_id = "newer"
    elif change == "generation":
        s.webull_resting_generation_id = "newer"
    elif change == "slot":
        s.fanout_claim_slot_id = "other"
    else:
        s.fanout_claim_outcome = "filled"
    journal = FanoutOutcomeJournal(factory)
    journal.bootstrap(strategy.apply_fanout_outcome,
        active_segments={event.payload.symbol:s.fanout_segment_id}, now=clock[0])
    assert s.fanout_webull_claimed and s.webull_resting_active
    if change == "filled":
        assert s.fanout_claim_outcome == "filled"


@pytest.mark.asyncio
async def test_previous_session_unknown_terminal_does_not_poison_feedback(lane, caplog):
    service, _, factory, clock = lane
    old = event_for(lane)
    quote(lane, old, "5.2")
    await service.process_trade_intent(old)
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        order.status = "cancelled"  # Deliberately no terminal audit.
        order.submitted_at = clock[0] - timedelta(days=2)
        session.commit()
    fresh = event_for(lane, segment="1791297000000")
    with factory() as session:
        service._mirrorhold_feedback(session, fresh, "controlled_new_refusal")
        session.commit()
    strategy, s = native_claim(fresh)
    journal = FanoutOutcomeJournal(factory)
    journal.bootstrap(strategy.apply_fanout_outcome,
        active_segments={fresh.payload.symbol:s.fanout_segment_id}, now=clock[0])
    assert not s.webull_resting_active and not s.fanout_webull_claimed
    assert "orders_considered=0" in caplog.text


@pytest.mark.asyncio
async def test_existing_store_local_refusal_applies_native_and_reports_account(lane, caplog, monkeypatch):
    service, _, factory, clock = lane
    event = event_for(lane)
    monkeypatch.setattr(service, "_evaluate_risk", lambda event: (False, "CONTROLLED risk refusal"))
    await service.process_trade_intent(event)
    strategy, s = native_claim(event)
    journal = FanoutOutcomeJournal(factory)
    journal.bootstrap(strategy.apply_fanout_outcome,
        active_segments={event.payload.symbol:s.fanout_segment_id}, now=clock[0])
    assert not s.webull_resting_active and not s.fanout_webull_claimed
    assert "CONTROLLED risk refusal" in caplog.text and "slot=" in caplog.text
    assert state(lane, event)["phase"] == "paused"


@pytest.mark.asyncio
async def test_fill_derived_refusal_never_fabricates_broker_fill(lane):
    service, _, factory, clock = lane
    old = event_for(lane)
    quote(lane, old, "5.2")
    await service.process_trade_intent(old)
    with factory() as session:
        session.scalar(select(BrokerOrder)).status = "filled"
        session.commit()
    fresh = event_for(lane)
    await service.process_trade_intent(fresh)
    strategy, s = native_claim(fresh)
    journal = FanoutOutcomeJournal(factory)
    journal.bootstrap(strategy.apply_fanout_outcome,
        active_segments={fresh.payload.symbol:s.fanout_segment_id}, now=clock[0])
    assert not s.webull_resting_active and s.fanout_webull_claimed
    assert s.fanout_claim_outcome == "mirror_uncertain"
    assert not s.fanout_webull_resting_taken  # Only actual broker fill transport may consume it.
    with factory() as session:
        records = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE,
            DashboardSnapshot.payload["reason"].as_string().startswith(MIRROR_REFUSAL_PREFIX))).all()
        assert records and all(r.payload["event_source"] == "client" for r in records)
        assert all(r.payload["outcome"] == "could_not_tell" for r in records)


@pytest.mark.asyncio
async def test_pm_state_rth_owner_inapplicable_and_no_new_stop_wire(lane):
    service, client, _, clock = lane
    clock[0] = clock[0].replace(hour=12)
    event = event_for(lane)
    quote(lane, event, "5.2")
    await service.process_trade_intent(event)
    assert state(lane, event)["phase"] == "retired"
    assert client.calls.get("place", 0) == 0
    eh = event.model_copy(deep=True)
    eh.payload.metadata["fanout_source"] = "eh_resting"
    assert not service._mirrorhold_scope(eh)
    assert service._mirrorhold_admit(eh)  # Existing EH path, not a new retained STOP mechanism.


@pytest.mark.asyncio
async def test_paused_ticks_are_memory_only(lane, monkeypatch):
    service, _, _, clock = lane
    event, _ = await hold_and_queue(lane)
    clock[0] += timedelta(seconds=1)
    service._mirrorhold_observe_intent(cancel_for(event, clock))
    monkeypatch.setattr(service, "session_factory", lambda: pytest.fail("paused quote opened DB"))
    for _ in range(1000):
        service._mirrorhold_schedule(event.payload.symbol)
    assert not service.__dict__.get("_symbol_tick_work")


@pytest.mark.asyncio
@pytest.mark.parametrize("off", [False, True])
async def test_cancelled_accepted_wire_fences_nonretained_buy_until_actual_clear(lane, off):
    service, _, factory, clock = lane
    old = event_for(lane)
    quote(lane, old, "5.2")
    await service.process_trade_intent(old)
    clock[0] += timedelta(seconds=1)
    service._mirrorhold_observe_intent(cancel_for(old, clock))
    assert state(lane, old)["phase"] == "paused" and state(lane, old)["dispatch_unresolved"]
    service.settings.oms_v2_webull_mirror_retained_hold_enabled = not off
    lane = restart(lane)
    event = old.model_copy(deep=True)
    event.event_id = uuid4()
    event.payload.metadata["fanout_source"] = "eh_resting"
    event.produced_at = clock[0]
    assert not lane[0]._mirrorhold_admit(event)
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        order.status = "cancelled"
        order.payload = {k:v for k,v in order.payload.items()
                         if k not in {"mirrorhold_id", "mirrorhold_dispatch_generation"}}
        session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled", event_source="broker",
            payload={"client_order_id":order.client_order_id,"metadata":dict(order.payload)}))
        session.commit()
    assert lane[0]._mirrorhold_admit(event)
    assert state(lane, old)["phase"] == "paused" and not state(lane, old)["dispatch_unresolved"]


@pytest.mark.asyncio
async def test_final_size_refusal_off_loop_durable_native_and_cold(lane, monkeypatch, caplog):
    service, client, factory, clock = lane
    event = event_for(lane)
    quote(lane, event, "5.2")
    loop_thread = threading.get_ident()
    original = service._mirrorhold_finalize_refusal
    calls = []
    admission_threads = []
    admit = service._mirrorhold_admit
    def admission(event):
        admission_threads.append(threading.get_ident())
        return admit(event)
    def finalize(session, event, results):
        calls.append(threading.get_ident())
        return original(session, event, results)
    monkeypatch.setattr(service, "_mirrorhold_finalize_refusal", finalize)
    monkeypatch.setattr(service, "_mirrorhold_admit", admission)
    monkeypatch.setattr(service, "_finalize_v2_entry_quantity", lambda event, intent: "CONTROLLED final_size")
    await service.process_trade_intent(event)
    assert calls and all(t != loop_thread for t in calls)
    assert admission_threads and all(t != loop_thread for t in admission_threads)
    assert state(lane, event)["phase"] == "paused"
    assert client.calls.get("place", 0) == 0
    assert "CONTROLLED final_size" in caplog.text and "slot=" in caplog.text
    strategy, s = native_claim(event)
    journal = FanoutOutcomeJournal(factory)
    journal.bootstrap(strategy.apply_fanout_outcome,
        active_segments={event.payload.symbol:s.fanout_segment_id}, now=clock[0])
    assert not s.webull_resting_active and not s.fanout_webull_claimed
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    assert not queued(lane)
