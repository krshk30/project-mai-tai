"""D2 rework component controls; source-authored, no broker RPCs."""

import asyncio
from datetime import UTC, datetime, timedelta
from copy import deepcopy
from decimal import Decimal
import json
import logging
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Strategy, TradeIntent
from project_mai_tai.oms.order_read_batch import MAX_CAPTURED_ORDERS, OrderReadBatch, persist_order_reads
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.reconciliation.order_read_evidence import SNAPSHOT_TYPE
from tests.unit.test_reconciliation_service import build_test_session_factory

NOW = datetime(2026, 10, 16, 19, 47, tzinfo=UTC)


def seed(factory, provider):
    with factory.begin() as session:
        account = BrokerAccount(name=f"live:synthetic-{provider}", provider=provider, environment="production")
        strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        if strategy is None:
            strategy = Strategy(code="schwab_1m_v2", name="synthetic", execution_mode="live", metadata_json={})
        session.add_all([account, strategy])
        session.flush()
        orders = []
        for index in range(2):
            intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
                symbol="SYN", side="buy", intent_type="open", quantity=Decimal(2),
                reason="controlled resting", status="accepted", payload={})
            session.add(intent)
            session.flush()
            row = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id, broker_account_id=account.id,
                client_order_id=f"synthetic-{provider}-order-{index}", broker_order_id=f"synthetic-{provider}-native-{index}",
                symbol="SYN", side="buy", order_type="STOP_LIMIT", time_in_force="day",
                quantity=Decimal(2), status="accepted", payload={"resting_entry": "true", "session": "NORMAL"},
                submitted_at=NOW-timedelta(minutes=4), updated_at=NOW-timedelta(minutes=4))
            session.add(row)
            orders.append(row)
        session.flush()
        return account, orders


def report_for(order):
    return ExecutionReport("accepted", order.client_order_id, broker_order_id=order.broker_order_id,
        symbol=order.symbol, side=order.side, quantity=order.quantity, origin="broker",
        metadata={"_webull_list_evidence": json.dumps({"account_id": "CONTROLLED",
            "client_order_id": order.client_order_id, "acquired_at": NOW.isoformat(), "truncated": False})})


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_batch_actual_run_db_after_session_close_three_reads_one_account_write(provider):
    factory = build_test_session_factory()
    account, orders = seed(factory, provider)
    loop_thread = get_ident()
    service = object.__new__(OmsRiskService)
    service.session_factory = factory
    service.settings = SimpleNamespace(oms_broker_sync_interval_seconds=5)
    service.logger = Mock(spec=logging.Logger)
    original_closed = [False]
    measured = []

    async def original_pass(*, account_names, diagnostic_batch):
        with factory():
            for order in orders:
                diagnostic_batch.capture(order=order, account=account, report=report_for(order),
                    started=NOW, completed=NOW, native_account="CONTROLLED")
        original_closed[0] = True
        return {"orders": 2, "terminal_orders": 0}

    service._sync_broker_orders_pass = original_pass

    def observe_sql(connection, cursor, statement, parameters, context, executemany):
        assert original_closed[0], "capture unexpectedly queried before trading session closed"
        assert get_ident() != loop_thread, "new diagnostic SQL ran on event loop"
        assert "SAVEPOINT" not in statement.upper() and "FOR UPDATE" not in statement.upper()
        measured.append(statement.lstrip().split()[0].upper())

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe_sql)
    try:
        result = await service.sync_broker_orders()
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)
    assert result == {"orders": 2, "terminal_orders": 0}
    assert measured.count("SELECT") == 3
    assert measured.count("INSERT") == 1
    assert measured.count("UPDATE") == 0
    with factory() as session:
        receipt = session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE))
        assert set(receipt.payload["orders"]) == {str(order.id) for order in orders}
        assert all(value["wire_read_at"] == NOW.isoformat() for value in receipt.payload["orders"].values())
        assert all(session.get(BrokerOrder, order.id).updated_at.replace(tzinfo=UTC) ==
                   NOW-timedelta(minutes=4) for order in orders)


@pytest.mark.parametrize("other_observed", [False, True], ids=["unobserved-account", "observed-account"])
def test_overflow_clears_old_scalar_receipts_and_failed_batch_cannot_publish_positive(other_observed):
    factory = build_test_session_factory()
    account, orders = seed(factory, "schwab")
    other_account, other_orders = seed(factory, "webull")
    batch = OrderReadBatch()
    for order in orders:
        batch.capture(order=order, account=account, report=report_for(order), started=NOW, completed=NOW)
    for order in other_orders:
        batch.capture(order=order, account=other_account, report=report_for(order),
                      started=NOW, completed=NOW, native_account="CONTROLLED")
    with factory.begin() as session:
        persist_order_reads(session, batch, cadence_seconds=5)
    with factory() as session:
        unaffected_before = deepcopy(next(row.payload for row in session.scalars(
            select(DashboardSnapshot)).all() if row.payload["account_id"] == str(other_account.id)))
    # Bound memory using distinct synthetic IDs; these are not additional trades.
    from dataclasses import replace
    from uuid import uuid4
    exemplar = next(iter(batch.reads.values()))
    batch.reads = {}
    for _ in range(MAX_CAPTURED_ORDERS - int(other_observed)):
        identity = uuid4()
        batch.reads[identity] = replace(exemplar, order_id=identity)
    if other_observed:
        # Even when B was observed, A overflow must not erase B's proof.
        batch.capture(order=other_orders[0], account=other_account, report=report_for(other_orders[0]),
                      started=NOW, completed=NOW, native_account="CONTROLLED")
    batch.capture(order=orders[0], account=account, report=None, started=NOW, completed=NOW)
    assert batch.overflow and len(batch.reads) == MAX_CAPTURED_ORDERS
    assert batch.revoked_accounts == {account.id}
    with factory.begin() as session:
        persist_order_reads(session, batch, cadence_seconds=5)
    with factory() as session:
        receipts = {row.payload["account_id"]: row.payload
                    for row in session.scalars(select(DashboardSnapshot)).all()}
        assert not receipts[str(account.id)]["orders"]
        assert receipts[str(other_account.id)] == unaffected_before
    failed = OrderReadBatch()
    failed.capture(order=orders[0], account=account, report=report_for(orders[0]), started=NOW, completed=NOW)
    failed.failed = True
    with factory.begin() as session:
        persist_order_reads(session, failed, cadence_seconds=5)
    with factory() as session:
        receipts = {row.payload["account_id"]: row.payload
                    for row in session.scalars(select(DashboardSnapshot)).all()}
        assert not receipts[str(account.id)]["orders"]
        assert receipts[str(other_account.id)] == unaffected_before


def test_bad_diagnostic_dto_is_account_scoped_uncertainty_not_a_trading_exception():
    factory = build_test_session_factory()
    account, orders = seed(factory, "schwab")
    service = object.__new__(OmsRiskService)
    batch = OrderReadBatch()

    class BadDiagnosticOrder:
        def __getattr__(self, name):
            if name == "quantity":
                raise ValueError("controlled diagnostic DTO quantity accessor")
            return getattr(orders[0], name)

    assert service._capture_passive_order_read(batch, order=BadDiagnosticOrder(),
        account=account, beginning=True) is None
    assert batch.revoked_accounts == {account.id}
    assert not batch.reads


@pytest.mark.asyncio
async def test_ordinary_capture_fault_preserves_original_fetch_and_success_result(monkeypatch):
    factory = build_test_session_factory()
    account, orders = seed(factory, "schwab")
    service = object.__new__(OmsRiskService)
    service.session_factory = factory
    service.settings = SimpleNamespace(oms_broker_sync_interval_seconds=5)
    service.logger = Mock(spec=logging.Logger)
    service.broker_adapter = SimpleNamespace(fetch_order_update=AsyncMock(return_value=report_for(orders[0])))

    def broken_capture(self, **kwargs):
        raise ValueError("controlled optional instrumentation fault")

    monkeypatch.setattr(OrderReadBatch, "capture", broken_capture)

    async def original_pass(*, account_names, diagnostic_batch):
        started = service._capture_passive_order_read(diagnostic_batch,
            order=orders[0], account=account, beginning=True)
        report = await service.broker_adapter.fetch_order_update("original request")
        service._capture_passive_order_read(diagnostic_batch, order=orders[0], account=account,
            report=report, started=started)
        return {"orders": 1, "terminal_orders": 0}

    service._sync_broker_orders_pass = original_pass
    assert await service.sync_broker_orders() == {"orders": 1, "terminal_orders": 0}
    service.broker_adapter.fetch_order_update.assert_awaited_once_with("original request")


@pytest.mark.asyncio
async def test_original_broker_error_is_not_replaced_by_capture_fault(monkeypatch):
    factory = build_test_session_factory()
    account, orders = seed(factory, "schwab")
    service = object.__new__(OmsRiskService)
    service.session_factory = factory
    service.settings = SimpleNamespace(oms_broker_sync_interval_seconds=5)
    service.logger = Mock(spec=logging.Logger)
    failure = OSError("original broker failure")
    service.broker_adapter = SimpleNamespace(fetch_order_update=AsyncMock(side_effect=failure))

    def broken_capture(self, **kwargs):
        raise ValueError("diagnostic only")

    monkeypatch.setattr(OrderReadBatch, "capture", broken_capture)

    async def original_pass(*, account_names, diagnostic_batch):
        service._capture_passive_order_read(diagnostic_batch, order=orders[0], account=account, beginning=True)
        await service.broker_adapter.fetch_order_update("original request")

    service._sync_broker_orders_pass = original_pass
    with pytest.raises(OSError) as caught:
        await service.sync_broker_orders()
    assert caught.value is failure


def test_optional_capture_does_not_swallow_cancellation(monkeypatch):
    factory = build_test_session_factory()
    account, orders = seed(factory, "schwab")
    service = object.__new__(OmsRiskService)
    batch = OrderReadBatch()
    cancellation = asyncio.CancelledError("original cancellation")

    def cancelled_capture(self, **kwargs):
        raise cancellation

    monkeypatch.setattr(OrderReadBatch, "capture", cancelled_capture)
    with pytest.raises(asyncio.CancelledError) as caught:
        service._capture_passive_order_read(batch, order=orders[0], account=account, beginning=True)
    assert caught.value is cancellation
