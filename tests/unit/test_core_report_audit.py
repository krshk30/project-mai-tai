"""The private Core audit keeps transaction and provenance semantics."""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrderEvent, DashboardSnapshot
from project_mai_tai.fanout_outcome_consumer import OUTCOME_SNAPSHOT_TYPE
from project_mai_tai.oms.buy_submission_journal import DurableBuyAdapter
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from tests.unit.test_fanout_outcome_integration import IDENTITY, _event, _factory


@pytest.fixture
def database():
    factory = _factory()
    yield factory
    factory.kw["bind"].dispose()


def order_in_session(session, metadata=None):
    store = OmsStore()
    strategy = store.ensure_strategy(session, "schwab_1m_v2")
    account = store.ensure_broker_account(session, "live:orb", provider="webull", environment="live")
    intent = store.create_trade_intent(session, strategy=strategy, broker_account=account,
        event=_event(account=account.name, metadata=metadata or {}))
    order = store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
        broker_account_id=account.id, client_order_id="audit-core", symbol="YYGH",
        side="buy", quantity=Decimal(1), metadata=metadata or {})
    return store, order


@pytest.mark.parametrize("use_core", [False, True])
@pytest.mark.parametrize("origin", ["broker", "client", "unknown"])
def test_audit_rows_and_fanout_identity_equal(database, use_core, origin):
    with database() as session:
        store, order = order_in_session(session, IDENTITY)
        report = ExecutionReport("accepted", order.client_order_id, origin=origin)
        payload = {"metadata": IDENTITY, "reason": "unchanged"}
        with session.begin_nested():
            result = store.append_order_event(session, order=order, report=report,
                payload=payload, use_core=use_core)
        assert inspect(result).persistent is not use_core
        event_id = result.id
        session.commit()
        row = session.get(BrokerOrderEvent, event_id)
        assert row.order_id == order.id and row.event_type == "accepted"
        assert row.event_source == origin and row.payload == payload
        written_at = row.event_at if row.event_at.tzinfo else row.event_at.replace(tzinfo=UTC)
        assert written_at == report.reported_at
        outcomes = list(session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE)))
        assert any(f"order_event:{event_id}" in str(item.payload) for item in outcomes)


@pytest.mark.parametrize("use_core", [False, True])
def test_nfq_terminal_report_keeps_only_audit_before_nfq_feedback(database, use_core):
    metadata = dict(IDENTITY, nfq_price_feedback_owned="true",
        fanout_source="rth_resting_mirror", fanout_leg="webull")
    with database() as session:
        store, order = order_in_session(session, metadata)
        before = list(session.scalars(select(DashboardSnapshot)))
        report = ExecutionReport("rejected", order.client_order_id, origin="broker")
        with session.begin_nested():
            result = store.append_order_event(session, order=order, report=report,
                payload={"metadata": metadata}, use_core=use_core)
        assert session.get(BrokerOrderEvent, result.id).event_source == "broker"
        assert list(session.scalars(select(DashboardSnapshot))) == before


def test_core_audit_flushes_pending_order_and_rolls_back(database):
    with database() as session:
        store, order = order_in_session(session)
        order.status = "accepted"
        with session.begin_nested():
            result = store.append_order_event(session, order=order,
                report=ExecutionReport("accepted", order.client_order_id), payload={}, use_core=True)
        assert session.get(BrokerOrderEvent, result.id) is not None
        session.rollback()
    with database() as session:
        assert list(session.scalars(select(BrokerOrderEvent))) == []


@pytest.mark.parametrize("use_core", [False, True])
def test_missing_timestamp_and_origin_keep_existing_defaults(database, use_core):
    with database() as session:
        store, order = order_in_session(session)
        before = datetime.now(UTC)
        report = SimpleNamespace(event_type="accepted", reported_at=None, origin=None, reason=None)
        with session.begin_nested():
            result = store.append_order_event(session, order=order, report=report,
                payload={}, use_core=use_core)
        written = session.get(BrokerOrderEvent, result.id)
        written_at = written.event_at if written.event_at.tzinfo else written.event_at.replace(tzinfo=UTC)
        assert before <= written_at <= datetime.now(UTC)
        assert written.event_source == "unknown"


@pytest.mark.asyncio
async def test_real_core_insert_failure_keeps_ledger_and_counts_drop(database, caplog):
    service = OmsRiskService.__new__(OmsRiskService)
    service.store = OmsStore()
    service.logger = logging.getLogger("core-audit-failure")
    service.broker_adapter = DurableBuyAdapter(SimpleNamespace(), None)
    service._order_event_attempts = service._order_event_failures = 0
    with database() as session:
        _, order = order_in_session(session)
        order.status = "accepted"
        assert not await service._append_order_event_isolated_awaited(session,
            order=SimpleNamespace(id=None, symbol="YYGH"),
            report=ExecutionReport("accepted", "audit-core"), payload={})
        session.commit()
        assert order.status == "accepted"
        assert list(session.scalars(select(BrokerOrderEvent))) == []
    assert service._order_event_attempts == service._order_event_failures == 1
    assert "OMS-ORDER-EVENT-DROPPED" in caplog.text
