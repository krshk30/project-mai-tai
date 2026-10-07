"""Recorded broker bodies; NFQ dispatch/ledger bindings are controlled, not live recovery."""
import asyncio
import json
import threading
from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event as sql_event, select

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, Strategy, TradeIntent
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms.eh_fresh_price import PREFIX
from project_mai_tai.oms import eh_fresh_price as nfq_module
from tests.unit import test_nfq2_eh_fresh_price as nfq_fixtures
from tests.unit.test_nfq2_eh_fresh_price import NOW, SEGMENT, biya, hold, quote, retry
from tests.unit.test_rpg1_buy_readback import recorded


@pytest.fixture
def service(monkeypatch):
    return nfq_fixtures.service.__wrapped__(monkeypatch)


class ReadOnlyBroker:
    def __init__(self, body, decode):
        self.body, self.decode, self.calls = body, decode, []
        self.during_read = None
        self.result = None

    async def read_atr_resting_buy_after_cancel(self, request):
        self.calls.append(request)
        if self.during_read:
            self.during_read()
        return self.result if self.result is not None else self.decode(request, self.body)

    async def submit_order(self, *args, **kwargs):
        pytest.fail("recovery must never send a cancel or buy")


def intent_for(service, event):
    with service.session_factory() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="v2", execution_mode="paper")
        account = service.store.ensure_broker_account(session, event.payload.broker_account_name,
                                                     provider="simulated", environment="paper")
        intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        session.commit()
        return intent.id


async def uncertain(service, broker="schwab", *, client_only=False, legacy=False, filled=False):
    request, body, decode = recorded(broker, filled=filled)
    event = biya(webull=broker == "webull")
    event.payload.symbol, event.payload.quantity = request.symbol, request.quantity
    md = event.payload.metadata
    md["fanout_slot_id"] = fanout_slot_id(strategy_code="schwab_1m_v2", symbol=request.symbol,
                                         segment_id=SEGMENT, slot="resting")
    FanoutSegmentIdentityStore(service.session_factory).record(
        symbol=request.symbol, segment_id=SEGMENT, active=True, reason="controlled_nfq_binding", now=NOW)
    held = hold(service, event)
    service._latest_quotes_by_symbol[request.symbol] = {"ask": Decimal("2.54"), "received_at": NOW}
    await service._evaluate_nfq2_holds(request.symbol)
    dispatched = retry(service)
    if broker == "webull":
        # Only the recorded client prefix is known, not the historical full event UUID.
        dispatched.event_id = UUID(request.client_order_id.rsplit("-", 1)[1] + "0" * 20)
    assert service._claim_nfq2_retry(dispatched)
    iid = intent_for(service, dispatched)
    with service.session_factory() as session:
        intent = session.get(TradeIntent, iid)
        account = session.get(BrokerAccount, intent.broker_account_id)
        account.provider = broker
        order = BrokerOrder(intent_id=iid, strategy_id=intent.strategy_id,
            broker_account_id=intent.broker_account_id, client_order_id=service._build_client_order_id(dispatched),
            broker_order_id=None if client_only else request.metadata["broker_order_id"],
            symbol=request.symbol, side="buy", order_type="stop_limit", time_in_force="day",
            quantity=request.quantity, status="accepted", payload=deepcopy(dispatched.payload.metadata))
        session.add(order)
        session.commit()
        oid = order.id
    service._finish_nfq2_retry(dispatched, completed=False)
    assert held.phase == "uncertain"
    if legacy:
        held.dispatch = None
        with service.session_factory() as session:
            service._nfq2_save(session, held)
            session.commit()
    adapter = ReadOnlyBroker(body, decode)
    service.broker_adapter = adapter
    return held, dispatched, oid, adapter


async def recover(service, held):
    await service._recover_nfq2_uncertain(service._nfq2_key(held.event), service._nfq2_copy(held))


@pytest.mark.asyncio
@pytest.mark.parametrize("broker,client_only,legacy", [
    ("schwab", False, False), ("webull", False, False), ("webull", True, False),
    ("schwab", False, True), ("webull", False, True)])
async def test_recorded_terminal_zero_exact_binding_retires_without_resend(service, broker, client_only, legacy):
    held, dispatched, oid, adapter = await uncertain(service, broker, client_only=client_only, legacy=legacy)
    count = len(service.redis.entries)
    await recover(service, held)
    assert held.phase == "retired" and not service._nfq2_holds
    assert len(adapter.calls) == 1
    assert adapter.calls[0].metadata["nfq2_recovery_read_only"] == "true"
    assert adapter.calls[0].client_order_id == service._build_client_order_id(dispatched)
    with service.session_factory() as session:
        order = session.get(BrokerOrder, oid)
        assert order.status == "cancelled" and order.broker_order_id
        assert session.get(TradeIntent, order.intent_id).status == "cancelled"
        proof = session.get(DashboardSnapshot, held.row_id).payload
        assert proof["dispatch"]["generation"] == dispatched.payload.metadata["nfq2_retry_token"]
        assert proof["dispatch"]["recovery"]["filled_quantity"] == "0"
        audit = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == oid))
        assert audit.payload["reason"] == "nfq2_exact_terminal_zero_readback"
    service.__dict__["_eh_price_holds"] = {}
    service._restore_nfq2_holds()
    await service._evaluate_nfq2_holds(dispatched.payload.symbol)
    assert len(service.redis.entries) == count and len(adapter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["account", "strategy", "symbol", "side", "quantity", "slot", "segment",
    "client", "event", "generation", "hold", "intent_quantity", "intent_account", "dispatch_generation",
    "order_generation", "order_hold", "order_slot", "order_segment"])
async def test_exact_durable_identity_mismatch_blocks_without_read(service, field):
    held, _, oid, adapter = await uncertain(service)
    with service.session_factory() as session:
        order = session.get(BrokerOrder, oid)
        intent = session.get(TradeIntent, order.intent_id)
        if field == "account":
            session.get(BrokerAccount, order.broker_account_id).name = "another-account"
        elif field == "strategy":
            session.get(Strategy, order.strategy_id).code = "orb_schwab"
        elif field in {"symbol", "side", "client"}:
            setattr(order, {"client": "client_order_id"}.get(field, field), "ANOTHER" if field != "side" else "sell")
        elif field == "quantity":
            order.quantity += 1
        elif field == "intent_quantity":
            intent.quantity += 1
        elif field == "intent_account":
            intent.broker_account_id = service.store.ensure_broker_account(session, "other", provider="simulated",
                environment="paper").id
        elif field == "dispatch_generation":
            held.dispatch["generation"] = "another-generation"
        elif field.startswith("order_"):
            payload = deepcopy(order.payload)
            key = {"order_generation": "nfq2_retry_token", "order_hold": "nfq2_hold_id",
                   "order_slot": "fanout_slot_id", "order_segment": "fanout_segment_id"}[field]
            payload[key] = "another"
            order.payload = payload
        else:
            payload = deepcopy(intent.payload)
            if field == "event":
                payload["event_id"] = str(uuid4())
            else:
                key = {"slot": "fanout_slot_id", "segment": "fanout_segment_id",
                       "generation": "nfq2_retry_token", "hold": "nfq2_hold_id"}[field]
                payload["metadata"][key] = "another"
            intent.payload = payload
        session.commit()
    await recover(service, held)
    assert held.phase == "uncertain" and not adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("change", ["missing_fill", "unknown", "working", "ack", "wrong_id", "positive_fill"])
async def test_unproven_or_filled_body_never_releases(service, broker, change):
    held, _, _, adapter = await uncertain(service, broker)
    parent = adapter.body if broker == "schwab" else adapter.body["items"][0]
    fill_key = "filledQuantity" if broker == "schwab" else "filled_qty"
    status_key = "status" if broker == "schwab" else "order_status"
    if change == "missing_fill":
        parent.pop(fill_key)
    elif change == "unknown":
        parent[fill_key] = "NaN"
    elif change == "working":
        parent[status_key] = "WORKING"
    elif change == "ack":
        adapter.body = {"message": "cancel accepted"}
    elif change == "wrong_id":
        adapter.body["orderId" if broker == "schwab" else "order_id"] = "other"
    else:
        parent[fill_key] = "1"
    await recover(service, held)
    assert held.phase == "uncertain" and len(adapter.calls) == 1
    assert len(service.redis.entries) == 1


@pytest.mark.asyncio
async def test_readback_positive_fill_is_sticky_across_zero_and_restart(service):
    held, _, _, adapter = await uncertain(service)
    adapter.result = AtrBuyReadback(outcome="fills", reason="controlled_partial", cumulative_filled=Decimal(1),
                                   broker_status="CANCELED", broker_order_id=str(adapter.body["orderId"]))
    await recover(service, held)
    assert held.dispatch["fill_seen"] == "1"
    adapter.result = None
    service.__dict__["_eh_price_holds"] = {}
    service._restore_nfq2_holds()
    restored = service._nfq2_holds[service._nfq2_key(held.event)]
    await recover(service, restored)
    assert restored.phase == "uncertain" and len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_recorded_webull_filled_body_is_no_rebuy_not_fabricated_fill_accounting(service):
    held, _, _, adapter = await uncertain(service, "webull", filled=True)
    await recover(service, held)
    assert held.phase == "uncertain" and held.dispatch["fill_seen"] == "1"
    assert len(adapter.calls) == 1 and len(service.redis.entries) == 1
    with service.session_factory() as session:
        assert session.scalars(select(Fill)).all() == []


@pytest.mark.asyncio
async def test_runtime_generation_change_during_read_never_retires_durable_owner(service):
    held, _, oid, adapter = await uncertain(service)
    adapter.during_read = lambda: held.dispatch.update(generation="changed-runtime-owner")
    await recover(service, held)
    assert held.phase == "uncertain" and held.dispatch["generation"] == "changed-runtime-owner"
    with service.session_factory() as session:
        assert session.get(BrokerOrder, oid).status == "accepted"
        assert session.get(DashboardSnapshot, held.row_id).payload["phase"] == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["generation", "quantity", "fill", "fill_report", "malformed_fill_report"])
async def test_post_read_durable_races_never_release(service, race):
    held, _, oid, adapter = await uncertain(service)

    def change():
        with service.session_factory() as session:
            if race == "generation":
                row = session.get(DashboardSnapshot, held.row_id)
                payload = deepcopy(row.payload)
                payload["dispatch"]["generation"] = "new-owner-generation"
                row.payload = payload
            elif race == "quantity":
                session.get(BrokerOrder, oid).quantity += 1
            elif race == "fill":
                order = session.get(BrokerOrder, oid)
                session.add(Fill(order_id=oid, strategy_id=order.strategy_id,
                    broker_account_id=order.broker_account_id, symbol=order.symbol, side="buy",
                    quantity=Decimal(1), price=Decimal("2.54"), filled_at=NOW, payload={}))
            else:
                session.add(BrokerOrderEvent(order_id=oid, event_type="cancelled", event_source="broker",
                    event_at=NOW, payload={"filled_quantity": "1" if race == "fill_report" else "NaN"}))
            session.commit()

    adapter.during_read = change
    await recover(service, held)
    assert held.phase == "uncertain"
    with service.session_factory() as session:
        assert session.get(BrokerOrder, oid).status == "accepted"
        assert session.get(DashboardSnapshot, held.row_id).payload["phase"] == "uncertain"


@pytest.mark.asyncio
async def test_missing_order_never_expires_or_resends_and_off_still_blocks(service):
    held = hold(service, biya())
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    dispatched = retry(service)
    assert service._claim_nfq2_retry(dispatched)
    service._finish_nfq2_retry(dispatched, completed=True)
    assert held.phase == "uncertain"
    service.settings.oms_v2_eh_fresh_price_enabled = False
    event = biya()
    event.event_id = uuid4()
    iid = intent_for(service, event)
    with service.session_factory() as session:
        intent = session.get(TradeIntent, iid)
        assert service._nfq2_pre_submit(session, event, intent)
        assert intent.payload["refusal_code"] == PREFIX + "dispatch_uncertain"
        session.commit()
    for _ in range(3):
        await recover(service, held)
        await service._evaluate_nfq2_holds("BIYA")
    assert held.phase == "uncertain" and len(service.redis.entries) == 1


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("field,value", [("fanout_slot_id", None), ("fanout_segment_id", None),
    ("fanout_slot", None), ("fanout_slot_id", "invented"), ("fanout_segment_id", "invalid"),
    ("fanout_slot", "unknown"), ("fanout_slot_id", " ")])
def test_legacy_identity_refused_even_with_fresh_quote_no_guessed_migration(service, fresh, field, value):
    event = biya()
    if value is None:
        event.payload.metadata.pop(field)
    else:
        event.payload.metadata[field] = value
    if fresh:
        quote(service)
    iid = intent_for(service, event)
    with service.session_factory() as session:
        intent = session.get(TradeIntent, iid)
        assert service._nfq2_pre_submit(session, event, intent)
        assert intent.status == "rejected"
        assert intent.payload["refusal_code"] == PREFIX + "legacy_identity_unproven"
        session.commit()
    assert not service._nfq2_holds and not service.redis.entries


@pytest.mark.parametrize("status", ["held", "filled", "pending"])
def test_unsafe_legacy_does_not_reset_owned_intent_or_existing_bound_hold(service, status):
    held = hold(service, biya())
    unsafe = biya()
    unsafe.event_id = uuid4()
    unsafe.payload.metadata.pop("fanout_slot_id")
    iid = intent_for(service, unsafe)
    with service.session_factory() as session:
        intent = session.get(TradeIntent, iid)
        intent.status = status
        if status == "pending":
            session.add(BrokerOrder(intent_id=iid, strategy_id=intent.strategy_id,
                broker_account_id=intent.broker_account_id, client_order_id="controlled-owned-legacy",
                broker_order_id="controlled-owned", symbol="BIYA", side="buy", order_type="limit",
                time_in_force="day", quantity=intent.quantity, status="accepted", payload={}))
            session.flush()
        assert service._nfq2_pre_submit(session, unsafe, intent)
        assert intent.status == status
        session.commit()
    service._nfq2_observe(unsafe)
    unsafe.payload.intent_type = "cancel"
    service._nfq2_observe(unsafe)
    assert service._nfq2_holds[service._nfq2_key(held.event)] is held and held.phase == "held"


def test_legacy_default_off_unchanged_and_modern_current_fresh_passes(service):
    event = biya()
    iid = intent_for(service, event)
    quote(service)
    with service.session_factory() as session:
        intent = session.get(TradeIntent, iid)
        assert not service._nfq2_pre_submit(session, event, intent)
        event.payload.metadata.pop("fanout_slot_id")
        service.settings.oms_v2_eh_fresh_price_enabled = False
        assert not service._nfq2_pre_submit(session, event, intent)
        assert intent.status == "pending"


def test_current_segment_is_required_even_when_canonical_identity_and_quote_are_fresh(service):
    event = biya()
    quote(service)
    FanoutSegmentIdentityStore(service.session_factory).record(symbol="BIYA", segment_id=SEGMENT,
        active=False, reason="controlled_closed", now=NOW.replace(microsecond=440000))
    iid = intent_for(service, event)
    with service.session_factory() as session:
        intent = session.get(TradeIntent, iid)
        assert service._nfq2_pre_submit(session, event, intent)
        assert intent.status == "rejected" and not service._nfq2_holds


@pytest.mark.asyncio
async def test_cancel_ack_without_explicit_zero_does_not_finish_or_recover(service):
    held, event, oid, adapter = await uncertain(service)
    with service.session_factory() as session:
        order = session.get(BrokerOrder, oid)
        order.status = "cancelled"
        session.add(BrokerOrderEvent(order_id=oid, event_type="cancelled", event_source="broker",
            event_at=NOW, payload={"client_order_id": order.client_order_id,
                                   "broker_order_id": order.broker_order_id}))
        session.commit()
    adapter.body = {"message": "cancel accepted"}
    await recover(service, held)
    assert held.phase == "uncertain"
    assert event.payload.metadata["nfq2_retry_token"] == held.dispatch["generation"]


@pytest.mark.asyncio
async def test_recovery_busy_fence_sends_one_read_and_never_queues(service):
    held, _, _, adapter = await uncertain(service)
    started, release = asyncio.Event(), asyncio.Event()
    original = adapter.read_atr_resting_buy_after_cancel

    async def blocked(request):
        started.set()
        await release.wait()
        return await original(request)

    adapter.read_atr_resting_buy_after_cancel = blocked
    first = asyncio.create_task(recover(service, held))
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(recover(service, held), timeout=0.1)
    release.set()
    await asyncio.wait_for(first, timeout=1)
    assert held.phase == "retired" and len(adapter.calls) == 1 and len(service.redis.entries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_reader", "raises", "wrong_type", "wrong_id", "timeout"])
async def test_read_errors_are_bounded_and_preserve_owner(service, failure, monkeypatch):
    held, _, oid, adapter = await uncertain(service)
    if failure == "missing_reader":
        service.broker_adapter = object()
    elif failure == "raises":
        async def broken(request):
            raise RuntimeError("controlled read failure")
        adapter.read_atr_resting_buy_after_cancel = broken
    elif failure == "wrong_type":
        adapter.result = {"status": "CANCELED", "filled": 0}
    elif failure == "wrong_id":
        adapter.result = AtrBuyReadback(outcome="cancelled_empty", reason="controlled_wrong_id",
            cumulative_filled=Decimal(0), terminal_cancel=True, broker_status="CANCELED", broker_order_id="other")
    else:
        original_wait = asyncio.wait_for

        async def bounded(awaitable, timeout):
            assert timeout == 2.0
            return await original_wait(awaitable, timeout=0.01)

        async def slow(request):
            await asyncio.sleep(1)
            pytest.fail("timed out read must not complete")

        adapter.read_atr_resting_buy_after_cancel = slow
        monkeypatch.setattr(asyncio, "wait_for", bounded)
    await recover(service, held)
    assert held.phase == "uncertain" and not service.__dict__["_eh_price_hold_inflight"]
    with service.session_factory() as session:
        assert session.get(BrokerOrder, oid).status == "accepted"


@pytest.mark.asyncio
async def test_uncertain_quotes_are_memory_only_and_symbol_account_isolated(service, monkeypatch):
    held, _, _, adapter = await uncertain(service)
    event = biya()
    event.event_id = uuid4()
    iid = intent_for(service, event)
    with service.session_factory() as session:
        assert service._nfq2_pre_submit(session, event, session.get(TradeIntent, iid))
        session.commit()
    # BIYA's independent hold may progress; AMOD uncertainty neither queries nor sends on ticks.
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    assert len(service.redis.entries) == 2 and held.phase == "uncertain"
    with monkeypatch.context() as guard:
        guard.setattr(service, "session_factory", lambda: pytest.fail("uncertain tick opened SQL session"))
        for symbol in ("AMOD", "UNRELATED"):
            await service._evaluate_nfq2_holds(symbol)
    assert not adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["quantity", "slot", "segment", "generation"])
async def test_serial_claim_rejects_changed_dispatch_shape(service, change):
    held = hold(service, biya())
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    event = retry(service)
    if change == "quantity":
        event.payload.quantity += 1
    else:
        event.payload.metadata[{"slot": "fanout_slot_id", "segment": "fanout_segment_id",
                                "generation": "nfq2_generation"}[change]] = "another"
    assert not service._claim_nfq2_retry(event)
    assert held.phase == "queued" and held.dispatch is None


@pytest.mark.asyncio
async def test_finish_durable_generation_race_never_overwrites_new_owner(service):
    held = hold(service, biya())
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    event = retry(service)
    assert service._claim_nfq2_retry(event)
    with service.session_factory() as session:
        row = session.get(DashboardSnapshot, held.row_id)
        payload = deepcopy(row.payload)
        payload["dispatch"]["generation"] = "new-owner"
        row.payload = payload
        session.commit()
    service._finish_nfq2_retry(event, completed=True)
    assert held.phase == "uncertain"
    with service.session_factory() as session:
        assert session.get(DashboardSnapshot, held.row_id).payload["dispatch"]["generation"] == "new-owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "quantity", "account", "strategy", "slot", "generation", "linked_order"])
async def test_only_exact_local_legacy_prewire_refusal_finishes(service, change):
    held = hold(service, biya())
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    event = retry(service)
    assert service._claim_nfq2_retry(event)
    FanoutSegmentIdentityStore(service.session_factory).record(symbol="BIYA", segment_id=SEGMENT,
        active=False, reason="controlled_closed_before_wire", now=NOW.replace(microsecond=440000))
    iid = intent_for(service, event)
    with service.session_factory() as session:
        intent = session.get(TradeIntent, iid)
        assert service._nfq2_pre_submit(session, event, intent)
        assert intent.status == "rejected" and intent.payload["refusal_code"] == PREFIX + "legacy_identity_unproven"
        if change == "quantity":
            intent.quantity += 1
        elif change == "account":
            intent.broker_account_id = service.store.ensure_broker_account(session, "other", provider="simulated",
                environment="paper").id
        elif change == "strategy":
            intent.strategy_id = service.store.ensure_strategy(session, "orb_schwab", name="ORB",
                execution_mode="paper").id
        elif change in {"slot", "generation"}:
            payload = deepcopy(intent.payload)
            payload["metadata"]["fanout_slot_id" if change == "slot" else "nfq2_retry_token"] = "other"
            intent.payload = payload
        elif change == "linked_order":
            session.add(BrokerOrder(intent_id=iid, strategy_id=intent.strategy_id,
                broker_account_id=intent.broker_account_id, client_order_id="different-client-linked-same-intent",
                symbol="BIYA", side="buy", order_type="limit", time_in_force="day", quantity=intent.quantity,
                status="accepted", payload={}))
        session.commit()
    service._finish_nfq2_retry(event, completed=True)
    assert held.phase == ("retired" if change is None else "uncertain")
    assert len(service.redis.entries) == 1


@pytest.mark.asyncio
async def test_periodic_recovery_sql_is_offloop_and_no_hold_sweep_is_zero_sql(service):
    held, _, _, adapter = await uncertain(service)
    engine = service.session_factory.kw["bind"]
    loop_thread = threading.get_ident()
    threads = []

    def observe(*args):
        threads.append(threading.get_ident())

    sql_event.listen(engine, "before_cursor_execute", observe)
    try:
        await service._sweep_nfq2_holds()
        assert held.phase == "retired" and len(adapter.calls) == 1
        assert threads and loop_thread not in threads
        count = len(threads)
        service.__dict__.pop("_eh_price_hold_last_sweep", None)
        await service._sweep_nfq2_holds()
        assert len(threads) == count
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)


@pytest.mark.asyncio
async def test_claim_persists_exact_generation_before_any_wire_and_refuses_durable_race(service):
    held = hold(service, biya())
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    event = retry(service)
    with service.session_factory() as session:
        row = session.get(DashboardSnapshot, held.row_id)
        before = deepcopy(row.payload)
        assert before["phase"] == "queued" and before["dispatch"] is None
        row.payload = {**before, "token": "another-owner"}
        session.commit()
    assert not service._claim_nfq2_retry(event)
    with service.session_factory() as session:
        row = session.get(DashboardSnapshot, held.row_id)
        assert row.payload["token"] == "another-owner"
        row.payload = before
        session.commit()
    assert service._claim_nfq2_retry(event)
    with service.session_factory() as session:
        dispatch = session.get(DashboardSnapshot, held.row_id).payload["dispatch"]
        assert dispatch == {"event_id": str(event.event_id), "client_order_id": service._build_client_order_id(event),
                            "generation": event.payload.metadata["nfq2_retry_token"], "hold_id": str(held.row_id)}
        assert session.scalars(select(BrokerOrder)).all() == []


@pytest.mark.asyncio
async def test_unknown_read_budget_five_second_sweep_and_zero_tick_sql(service, monkeypatch):
    held, _, _, adapter = await uncertain(service)
    assert service.settings.oms_broker_sync_interval_seconds == 5
    adapter.result = AtrBuyReadback(outcome="unknown", reason="controlled_unproven")
    clock = [0.0]
    monkeypatch.setattr(nfq_module, "monotonic", lambda: clock[0])
    engine = service.session_factory.kw["bind"]
    sql_threads, transactions, commits = [], [], []

    def sql(*args):
        sql_threads.append(threading.get_ident())

    def begin(*args):
        transactions.append(threading.get_ident())

    def commit(*args):
        commits.append(threading.get_ident())

    for name, callback in (("before_cursor_execute", sql), ("begin", begin), ("commit", commit)):
        sql_event.listen(engine, name, callback)
    try:
        samples = []
        for now, expected in ((0, 1), (0.1, 1), (4.999, 1), (5, 2), (5.1, 2), (10, 3)):
            clock[0] = now
            await service._sweep_nfq2_holds()
            assert len(adapter.calls) == expected
            samples.append({"elapsed": now, "reads": expected, "sql": len(sql_threads),
                            "transactions": len(transactions), "commits": len(commits)})
        before = (len(sql_threads), len(transactions), len(commits))
        for _ in range(200):
            await service._evaluate_nfq2_holds(held.event.payload.symbol)
            await service._evaluate_nfq2_holds("UNRELATED")
        assert before == (len(sql_threads), len(transactions), len(commits))
        assert threading.get_ident() not in sql_threads
        assert held.phase == "uncertain" and len(service.redis.entries) == 1
        print("NFQ_RECOVERY_BUDGET " + json.dumps({"samples": samples, "tick_events": 400,
              "tick_sql": 0, "tick_transactions": 0, "tick_commits": 0, "reads": len(adapter.calls)}))
    finally:
        for name, callback in (("before_cursor_execute", sql), ("begin", begin), ("commit", commit)):
            sql_event.remove(engine, name, callback)
