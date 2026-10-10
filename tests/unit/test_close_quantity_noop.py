"""Actual SELL caller; venue ACCEPTED answers are explicitly controlled."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event, inspect, select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.buy_submission_journal import DurableBuyAdapter
from tests.unit.test_report_audit_fifo import database as controlled_database, service_for

database = controlled_database
EQUAL = [("1.00000000", "1", "1"), ("0.125", "0.12500000", "0.125")]
CLAMP = [("3", "2", "1.5"), ("3", "1.25", "2")]


def setup(database, monkeypatch, quantities):
    requested, virtual, account = map(Decimal, quantities)
    service = service_for(database)
    wires, published = [], []

    async def accepted(request):
        wires.append(request)
        return [ExecutionReport("accepted", request.client_order_id,
            broker_order_id="CONTROLLED-ACCEPTED", symbol=request.symbol, side=request.side,
            intent_type=request.intent_type, quantity=request.quantity, metadata=request.metadata)]

    async def publish(result):
        published.append(result)

    async def noop(*args, **kwargs):
        pass

    service.broker_adapter = DurableBuyAdapter(SimpleNamespace(submit_order=accepted), database)
    monkeypatch.setattr(service, "_evaluate_risk", lambda event: (True, "controlled"))
    monkeypatch.setattr(service, "_market_is_fillable", lambda *args: True)
    monkeypatch.setattr(service, "_reconcile_after_intent", noop)
    monkeypatch.setattr(service, "_publish_order_event", publish)
    with database() as session:
        strategy = Strategy(code="macd_30s", name="controlled", execution_mode="live")
        broker = BrokerAccount(name="live:orb", provider="webull", environment="test")
        session.add_all([strategy, broker])
        session.flush()
        ids = strategy.id, broker.id
        session.add_all([
            VirtualPosition(strategy_id=strategy.id, broker_account_id=broker.id,
                symbol="EXIT", quantity=virtual, average_price=Decimal(2)),
            AccountPosition(broker_account_id=broker.id, symbol="EXIT",
                quantity=account, average_price=Decimal(2)),
        ])
        session.commit()
    intent = TradeIntentEvent(source_service="controlled-test", payload=TradeIntentPayload(
        strategy_code="macd_30s", broker_account_name="live:orb", symbol="EXIT",
        side="sell", quantity=requested, intent_type="close", reason="CONTROLLED_ACCEPTED",
        metadata={"reference_price": "2"}))
    return service, intent, wires, published, ids


def observe_lookup(service, monkeypatch, inject=None, fail_parent=False):
    original = service.store.get_or_create_order
    samples = []

    def observe(session, **kwargs):
        if inject is not None:
            inject(session)
        dirty = []
        for row in session.dirty:
            state = inspect(row)
            dirty.append((type(row).__name__, session.is_modified(row),
                {attr.key: (tuple(attr.history.added), tuple(attr.history.deleted))
                 for attr in state.attrs if attr.history.has_changes()}))
        queries, sql, flushes = [], [], []
        connection = session.connection()

        def query(state):
            queries.append(state.is_orm_statement)

        def statement(conn, cursor, text, parameters, context, many):
            sql.append(" ".join(text.lower().split()))

        def flush(session, context, instances):
            flushes.append((len(session.new), len(session.dirty), len(session.deleted)))

        event.listen(session, "do_orm_execute", query)
        event.listen(connection, "before_cursor_execute", statement)
        event.listen(session, "before_flush", flush)
        try:
            order = original(session, **kwargs)
            persisted = connection.scalar(select(TradeIntent.__table__.c.quantity).where(
                TradeIntent.__table__.c.id == kwargs["intent"].id))
            samples.append({"dirty": dirty, "queries": queries, "sql": sql,
                "flushes": flushes, "persisted": persisted, "quantity": order.quantity})
            if fail_parent:
                session.info["controlled_parent_failure"] = True
            return order
        finally:
            event.remove(session, "do_orm_execute", query)
            event.remove(connection, "before_cursor_execute", statement)
            event.remove(session, "before_flush", flush)

    monkeypatch.setattr(service.store, "get_or_create_order", observe)
    return samples


def assert_committed(database, expected):
    with database() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "EXIT"))
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "EXIT"))
        assert intent.quantity == order.quantity == expected
        assert order.intent_id == intent.id and order.status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("quantities", EQUAL)
async def test_equal_sell_quantity_does_not_dirty_intent_before_lookup(database, monkeypatch, quantities):
    service, intent, wires, published, _ = setup(database, monkeypatch, quantities)
    samples = observe_lookup(service, monkeypatch)
    try:
        result = await service.process_trade_intent(intent)
    finally:
        await service._drain_cancel_terminal_evidence()
    assert len(samples) == len(wires) == len(published) == len(result) == 1
    sample = samples[0]
    assert sample["dirty"] == [] and sample["queries"][0] is False
    assert not any(sql.startswith("update") and "trade_intents" in sql for sql in sample["sql"])
    expected = Decimal(quantities[0])
    assert sample["persisted"] == sample["quantity"] == wires[0].quantity == expected
    assert result[0].payload.status == "accepted"
    assert_committed(database, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("quantities", CLAMP)
async def test_real_clamp_remains_dirty_and_autoflushes_before_order_lookup(database, monkeypatch, quantities):
    service, intent, wires, published, _ = setup(database, monkeypatch, quantities)
    samples = observe_lookup(service, monkeypatch)
    try:
        result = await service.process_trade_intent(intent)
    finally:
        await service._drain_cancel_terminal_evidence()
    expected = min(map(Decimal, quantities))
    sample, = samples
    assert sample["dirty"] == [("TradeIntent", True,
        {"quantity": ((expected,), (Decimal(quantities[0]),))})]
    assert sample["queries"][0] is True
    update = next(i for i, sql in enumerate(sample["sql"])
        if sql.startswith("update") and "trade_intents" in sql)
    lookup = next(i for i, sql in enumerate(sample["sql"])
        if sql.startswith("select") and "broker_orders" in sql and "client_order_id" in sql)
    insert = next(i for i, sql in enumerate(sample["sql"])
        if sql.startswith("insert") and "broker_orders" in sql)
    assert update < lookup < insert
    assert sample["persisted"] == sample["quantity"] == wires[0].quantity == expected
    assert len(published) == len(result) == 1 and result[0].payload.status == "accepted"
    assert_committed(database, expected)


@pytest.mark.asyncio
async def test_postwire_parent_failure_rolls_back_clamped_intent_and_order(database, monkeypatch):
    service, intent, wires, published, _ = setup(database, monkeypatch, CLAMP[0])
    samples = observe_lookup(service, monkeypatch, fail_parent=True)

    def fail(session):
        if session.info.pop("controlled_parent_failure", False):
            raise RuntimeError("CONTROLLED postwire parent failure")

    event.listen(database, "before_commit", fail)
    try:
        with pytest.raises(RuntimeError, match="CONTROLLED postwire parent failure"):
            await service.process_trade_intent(intent)
    finally:
        await service._drain_cancel_terminal_evidence()
        event.remove(database, "before_commit", fail)
    assert len(wires) == 1 and wires[0].quantity == samples[0]["persisted"] == Decimal("1.5")
    assert published == [] and "_ordinary_report_audit_fifo" not in service.__dict__
    with database() as session:
        assert list(session.scalars(select(TradeIntent))) == []
        assert list(session.scalars(select(BrokerOrder))) == []
        assert session.scalar(select(VirtualPosition.quantity)) == Decimal(2)
        assert session.scalar(select(AccountPosition.quantity)) == Decimal("1.5")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pending", "dirty"])
async def test_unrelated_rows_still_require_original_autoflush(database, monkeypatch, kind):
    service, intent, wires, _, ids = setup(database, monkeypatch, EQUAL[0])

    def peer():
        return BrokerOrder(strategy_id=ids[0], broker_account_id=ids[1],
            client_order_id="CONTROLLED-unrelated", symbol="OTHER", side="buy",
            order_type="market", time_in_force="day", quantity=Decimal(1),
            status="accepted", payload={"original": "yes"})

    if kind == "dirty":
        with database() as session:
            session.add(peer())
            session.commit()

    def inject(session):
        if kind == "pending":
            row = peer()
            session.add(row)
        else:
            row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == "CONTROLLED-unrelated"))
            row.payload = {"modified": "yes"}

    samples = observe_lookup(service, monkeypatch, inject=inject)
    try:
        await service.process_trade_intent(intent)
    finally:
        await service._drain_cancel_terminal_evidence()
    sample, = samples
    assert all(row[0] != "TradeIntent" for row in sample["dirty"])
    assert sample["queries"][0] is True
    assert sample["flushes"][0] == ((1, 0, 0) if kind == "pending" else (0, 1, 0))
    with database() as session:
        row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == "CONTROLLED-unrelated"))
        assert row.payload == ({"original": "yes"} if kind == "pending" else {"modified": "yes"})
    assert len(wires) == 1
    assert_committed(database, Decimal(1))
