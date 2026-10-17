"""Pure zero-execution branches only; positive-fill/native paths are deferred."""

from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
import threading

import pytest
from sqlalchemy import event, select

from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Fill, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import orb_country, webull_attach_riskoff as wb
from project_mai_tai.oms.service import ArmedHardStop
from tests.unit.test_wbattach1_riskoff import NOW, attach, make_case


async def prepared(make_case, site, account):
    case = make_case("schwab_1m_v2" if site == "atr" else "orb_schwab", account)
    service, factory, broker, entry_id, _ = case
    if site == "atr":
        assert await attach(case)
        with factory() as session:
            state = dict(session.get(BrokerOrder, entry_id).payload[wb.KEY]["attempts"][-1])
    else:
        await orb_country.reconcile_one(service, entry_id)
        with factory() as session:
            state = dict(session.get(BrokerOrder, entry_id).payload[orb_country.PROTECTION])
    coid = WebullBrokerAdapter._combo_leg_coid(state["base"], "T")
    report = broker.report(coid, filled=Decimal(0))

    async def record(reports):
        if site == "atr":
            await wb._record_atr_children(service, account, "ABC", "owned-entry", state, reports)
        else:
            await orb_country.record_protection_reports(service, entry_id, reports)

    return case, state, report, record


@contextmanager
def worker_probe(factory, *, fail_commit=False):
    loop_thread = threading.get_ident()
    engine = factory.kw["bind"]
    counts = {"sql": 0, "loaded": 0, "commit": 0}

    def cursor(*_args):
        assert threading.get_ident() != loop_thread
        counts["sql"] += 1

    def loaded(session, _instance):
        if session.get_bind() is engine:
            assert threading.get_ident() != loop_thread
            counts["loaded"] += 1

    def committing(session):
        if session.get_bind() is engine:
            assert threading.get_ident() != loop_thread
            if fail_commit:
                raise RuntimeError("controlled_zero_receipt_commit_failure")

    def committed(session):
        if session.get_bind() is engine:
            assert threading.get_ident() != loop_thread
            counts["commit"] += 1

    hooks = ((engine, "before_cursor_execute", cursor),
             (factory.class_, "loaded_as_persistent", loaded),
             (factory.class_, "before_commit", committing),
             (factory.class_, "after_commit", committed))
    for target, name, callback in hooks:
        event.listen(target, name, callback)
    try:
        yield counts
    finally:
        for target, name, callback in reversed(hooks):
            event.remove(target, name, callback)


def forbid_async_effects(service, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("zero-execution receipt invoked a broker/native/report/publication await")
    for name in ("_record_order_reports", "_manage_native_stop_after_fill", "_publish_order_event"):
        monkeypatch.setattr(service, name, forbidden)
    for name in ("submit_order", "fetch_order_update", "list_account_positions", "cancel_exit_pair"):
        monkeypatch.setattr(service.broker_adapter, name, forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("atr", "orb"))
@pytest.mark.parametrize("account", ("live:orb", "live:webull-alt"))
async def test_zero_execution_sql_and_commit_offloop_without_async_effects(make_case, monkeypatch, site, account):
    case, _, report, record = await prepared(make_case, site, account)
    service, factory, broker, entry_id, _ = case
    original_requests = tuple(broker.requests)
    forbid_async_effects(service, monkeypatch)
    with worker_probe(factory) as counts:
        assert await record([report]) is None
    assert counts["sql"] and counts["loaded"] and counts["commit"] == 1
    assert tuple(broker.requests) == original_requests
    with factory() as session:
        assert session.scalar(select(Fill).where(Fill.side == "sell")) is None
        assert session.scalar(select(VirtualPosition.quantity)) == 10
        if site == "orb":
            terminal = session.get(BrokerOrder, entry_id).payload[orb_country.PROTECTION]["terminal_children"]
            assert terminal[report.client_order_id]["filled_quantity"] == "0"
            assert terminal[report.client_order_id]["broker_order_id"] == report.broker_order_id


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("atr", "orb"))
async def test_zero_execution_commit_failure_rolls_back_without_publication(make_case, monkeypatch, site):
    case, _, report, record = await prepared(make_case, site, "live:orb")
    service, factory, _, entry_id, _ = case
    with factory() as session:
        before = dict(session.get(BrokerOrder, entry_id).payload)
    forbid_async_effects(service, monkeypatch)
    with worker_probe(factory, fail_commit=True) as counts:
        with pytest.raises(RuntimeError, match="controlled_zero_receipt_commit_failure"):
            await record([report])
    assert counts["sql"] and counts["commit"] == 0
    with factory() as session:
        assert session.get(BrokerOrder, entry_id).payload == before
        assert session.scalar(select(Fill).where(Fill.side == "sell")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("atr", "orb"))
async def test_zero_execution_still_validates_exact_protection_intent(make_case, monkeypatch, site):
    case, state, report, record = await prepared(make_case, site, "live:orb")
    service, factory, _, _, _ = case
    with factory.begin() as session:
        from uuid import UUID
        session.get(TradeIntent, UUID(state["intent_id"])).symbol = "FOREIGN"
    forbid_async_effects(service, monkeypatch)
    with worker_probe(factory) as counts:
        with pytest.raises(ValueError, match="protection_intent"):
            await record([report])
    assert counts["sql"] and counts["commit"] == 0


@pytest.mark.asyncio
async def test_orb_unknown_zero_sibling_cannot_become_terminal_proof(make_case, monkeypatch):
    case, _, report, record = await prepared(make_case, "orb", "live:orb")
    service, factory, _, entry_id, _ = case
    unknown = replace(report, origin="unknown", broker_order_id=None)
    forbid_async_effects(service, monkeypatch)
    with worker_probe(factory):
        await record([None, unknown])
    with factory() as session:
        state = session.get(BrokerOrder, entry_id).payload[orb_country.PROTECTION]
        assert unknown.client_order_id not in state.get("terminal_children", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("client", "symbol", "broker_id", "future"))
async def test_orb_zero_receipt_keeps_native_terminal_identity_guards(make_case, monkeypatch, mutation):
    case, _, report, record = await prepared(make_case, "orb", "live:orb")
    service, factory, _, _, clock = case
    if mutation == "client":
        report = replace(report, client_order_id="foreign-child")
    elif mutation == "symbol":
        report = replace(report, symbol="FOREIGN")
    elif mutation == "broker_id":
        report = replace(report, broker_order_id=None, metadata={"orbroute1_fresh_read": "true"})
    else:
        from datetime import timedelta
        report = replace(report, reported_at=clock[0] + timedelta(seconds=1))
    forbid_async_effects(service, monkeypatch)
    with worker_probe(factory) as counts:
        with pytest.raises(ValueError, match="orb_protection"):
            await record([report])
    assert counts["sql"] and counts["commit"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("atr", "orb"))
async def test_positive_execution_keeps_original_async_writer_onloop(make_case, monkeypatch, site):
    case, _, report, record = await prepared(make_case, site, "live:orb")
    service, factory, _, _, _ = case
    loop_thread = threading.get_ident()
    original = service._record_order_reports
    calls = []

    async def unchanged_writer(**kwargs):
        assert threading.get_ident() == loop_thread
        calls.append(kwargs["reports"])
        return await original(**kwargs)

    monkeypatch.setattr(service, "_record_order_reports", unchanged_writer)
    positive = replace(report, filled_quantity=Decimal(2), fill_price=Decimal(11),
                       broker_fill_id="positive-partial-two")
    await record([positive])
    assert len(calls) == 1
    with factory() as session:
        fills = session.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert len(fills) == 1 and fills[0].quantity == 2
        assert session.scalar(select(VirtualPosition.quantity)) == 8
        assert session.scalar(select(AccountPosition.quantity)) == 1008


@pytest.mark.asyncio
async def test_existing_async_partial_fill_cancels_live_ten_guard_and_rearms_eight(make_case, monkeypatch):
    """Pin the existing contract; NOT approval to substitute the retry queue."""
    service, factory, broker, entry_id, _ = make_case()
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        strategy = session.get(Strategy, entry.strategy_id)
        account = session.get(BrokerAccount, entry.broker_account_id)
        guard_event = TradeIntentEvent(source_service="CONTROLLED", payload=TradeIntentPayload(
            strategy_code=strategy.code, broker_account_name=account.name, symbol="ABC", side="sell",
            intent_type="close", quantity=Decimal(10), reason=service.NATIVE_STOP_GUARD_REASON,
            metadata={"native_stop_guard": "true", "order_type": "STOP", "stop_price": "9.5"}))
        guard_intent = service.store.create_trade_intent(session, strategy=strategy,
            broker_account=account, event=guard_event)
        guard_intent.status = "accepted"
        guard = service.store.get_or_create_order(session, intent=guard_intent,
            strategy_id=strategy.id, broker_account_id=account.id, client_order_id="original-guard-ten",
            broker_order_id="original-guard-native", symbol="ABC", side="sell", quantity=Decimal(10),
            metadata=dict(guard_event.payload.metadata), status="accepted", order_type="STOP",
            time_in_force="day")
        guard_id, strategy_id, account_id = guard.id, strategy.id, account.id
    key = service._hard_stop_key("schwab_1m_v2", broker.account, "ABC")
    service._armed_hard_stops[key] = ArmedHardStop("schwab_1m_v2", broker.account, "ABC",
        Decimal(10), Decimal(10), 5.0, Decimal("9.5"), 2000, 0.0)
    native_calls = []

    async def native_broker(request):
        native_calls.append(request)
        if request.intent_type == "cancel":
            assert request.client_order_id == "original-guard-ten"
            assert request.metadata["broker_order_id"] == "original-guard-native"
            status, broker_id = "cancelled", "original-guard-native"
        else:
            assert request.metadata["native_stop_guard"] == "true"
            assert request.order_type == "STOP" and request.quantity == 8
            status, broker_id = "accepted", "replacement-guard-eight"
        return [ExecutionReport(status, request.client_order_id, broker_order_id=broker_id,
            symbol="ABC", side="sell", intent_type=request.intent_type, quantity=request.quantity,
            reported_at=NOW, origin="broker", metadata=dict(request.metadata))]

    monkeypatch.setattr(broker, "submit_order", native_broker)
    with factory() as session:
        entry = session.get(BrokerOrder, entry_id)
        strategy = session.get(Strategy, strategy_id)
        account = session.get(BrokerAccount, account_id)
        report_event = TradeIntentEvent(source_service="CONTROLLED", payload=TradeIntentPayload(
            strategy_code=strategy.code, broker_account_name=account.name, symbol="ABC", side="sell",
            intent_type="close", quantity=Decimal(10), reason="CONTROLLED_PARTIAL_CLOSE",
            metadata={"entry_order_id": str(entry_id)}))
        intent = service.store.create_trade_intent(session, strategy=strategy,
            broker_account=account, event=report_event)
        request = OrderRequest("partial-close-two", account.name, strategy.code, "ABC", "sell", "close",
            Decimal(10), report_event.payload.reason, dict(report_event.payload.metadata))
        report = ExecutionReport("partially_filled", request.client_order_id,
            broker_order_id="owned-partial-close", broker_fill_id="owned-partial-two",
            symbol="ABC", side="sell", intent_type="close", quantity=Decimal(10),
            filled_quantity=Decimal(2), fill_price=Decimal(11), reported_at=NOW, origin="broker")
        await service._record_order_reports(session=session, intent=intent,
            strategy_id=strategy_id, broker_account_id=account_id,
            intent_event=report_event, request=request, reports=[report])
        session.commit()
    assert [request.intent_type for request in native_calls] == ["cancel", "close"]
    assert [request.quantity for request in native_calls] == [10, 8]
    assert service._armed_hard_stops[key].quantity == 8
    with factory() as session:
        assert session.get(BrokerOrder, guard_id).status == "cancelled"
        live_guard = service.store.find_open_native_stop_guard_order(session,
            strategy_id=strategy_id, broker_account_id=account_id, symbol="ABC")
        assert live_guard is not None and live_guard.quantity == 8
        assert live_guard.broker_order_id == "replacement-guard-eight"
        assert session.scalar(select(VirtualPosition.quantity)) == 8
    assert key not in service._native_guard_rearm_pending
