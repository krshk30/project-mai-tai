"""Post-commit FIFO controls, with separate SQLite connections per lease."""
import asyncio
import logging
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, TradeIntent
from project_mai_tai.oms.buy_submission_journal import DurableBuyAdapter
from project_mai_tai.oms.report_audit_fifo import AuditItem, PRIOR
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_core_report_audit import order_in_session
from tests.unit.test_fanout_outcome_integration import _event

@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    @event.listens_for(engine, "connect")
    def transactions(connection, _):
        connection.isolation_level = None
    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()

def service_for(database):
    service = OmsRiskService(settings=Settings(oms_adapter="simulated"),
        redis_client=SimpleNamespace(), session_factory=database,
        broker_adapter=SimpleNamespace())
    service.broker_adapter = DurableBuyAdapter(SimpleNamespace(), database)
    service.logger = logging.getLogger(f"fifo-test-{uuid4()}")
    service.logger.propagate = True
    return service

def staged_order(service, session, metadata=None):
    _, order = order_in_session(session, metadata)
    report = ExecutionReport("accepted", order.client_order_id, metadata={})
    staged = []
    assert service._stage_ordinary_audit(staged, order, report,
        {"metadata": {}, "reason": "original"}, prior={}, request_metadata={})
    return staged, order

@pytest.mark.asyncio
async def test_fifo_parent_fk_copy_order_and_commit_accounting(database, monkeypatch, caplog):
    service = service_for(database)
    service.logger.addHandler(caplog.handler)
    entered, release = threading.Event(), threading.Event()
    original_commit = database.class_.commit
    original_append = service.store.append_order_event
    seen = []
    loop_thread = threading.get_ident()
    def append(session, **kwargs):
        assert threading.get_ident() != loop_thread
        assert session.get(BrokerOrder, kwargs["order"].id) is not None
        seen.append(kwargs["report"].reported_at)
        session.info["fifo_commit"] = True
        return original_append(session, **kwargs)
    def commit(session):
        if session.info.pop("fifo_commit", False):
            entered.set()
            assert release.wait(5)
        return original_commit(session)
    monkeypatch.setattr(service.store, "append_order_event", append)
    monkeypatch.setattr(database.class_, "commit", commit)
    with database() as session:
        staged, order = staged_order(service, session)
        first = staged[0]
        staged.append(AuditItem.capture(order, ExecutionReport("accepted", order.client_order_id),
            {"metadata": {}, "reason": "second"}))
        await service._commit_reports_and_transfer_audits(session, staged)
        order.payload = {"changed_after_capture": "true"}
    fifo = service._report_audit_fifo()
    drain = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert fifo.enqueued == 2 and fifo.written == fifo.failed == 0
        service._maybe_emit_order_event_census(interval_seconds=0)
        assert "enqueued=2 committed_written=0 failed=0 pending=1 inflight=1" in caplog.text
        with database() as session:
            assert list(session.scalars(select(BrokerOrderEvent))) == []
        drain = asyncio.create_task(service._drain_cancel_terminal_evidence())
        await asyncio.sleep(0.01)
        drain.cancel()
        await asyncio.sleep(0.01)
        assert not drain.done()
    finally:
        release.set()
        if drain is not None:
            with pytest.raises(asyncio.CancelledError):
                await drain
        await fifo.drain()
    assert fifo.written == 2 and fifo.failed == 0
    assert seen[0] == first.reported_at and seen == sorted(seen)
    with database() as session:
        assert sorted(row.payload["reason"] for row in session.scalars(select(BrokerOrderEvent))) == ["original", "second"]
    assert "previous_process_pending=UNKNOWN" in caplog.text

@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_parent_cancel_completion_or_rollback_before_transfer(database, monkeypatch, commit_fails):
    service = service_for(database)
    entered, release = threading.Event(), threading.Event()
    original = database.class_.commit
    def commit(session):
        if session.info.pop("parent_block", False):
            entered.set()
            assert release.wait(5)
            if commit_fails:
                raise RuntimeError("controlled parent rollback")
        return original(session)
    monkeypatch.setattr(database.class_, "commit", commit)
    with database() as session:
        staged, _ = staged_order(service, session)
        session.info["parent_block"] = True
        task = asyncio.create_task(service._commit_reports_and_transfer_audits(session, staged))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            assert not task.done() and "_ordinary_report_audit_fifo" not in service.__dict__
        finally:
            release.set()
            with pytest.raises(RuntimeError if commit_fails else asyncio.CancelledError):
                await task
            if commit_fails:
                session.rollback()
    await service._drain_cancel_terminal_evidence()
    with database() as session:
        assert len(list(session.scalars(select(BrokerOrderEvent)))) == int(not commit_fails)
        assert len(list(session.scalars(select(BrokerOrder)))) == int(not commit_fails)
    assert staged == []

@pytest.mark.asyncio
async def test_fifo_actual_fk_insert_failure_counted_after_parent_survives(database, caplog):
    service = service_for(database)
    service.logger.addHandler(caplog.handler)
    with database() as session:
        staged, _ = staged_order(service, session)
        item = staged[0]
        from dataclasses import replace
        staged[0] = replace(item, order_id=None)
        await service._commit_reports_and_transfer_audits(session, staged)
    await service._drain_cancel_terminal_evidence()
    fifo = service._report_audit_fifo()
    assert fifo.enqueued == fifo.failed == service._order_event_failures == 1
    assert fifo.written == 0 and service._order_event_attempts == 1
    with database() as session:
        assert session.scalar(select(BrokerOrder.status)) == "pending"
        assert list(session.scalars(select(BrokerOrderEvent))) == []
    assert "OMS-ORDER-EVENT-DROPPED" in caplog.text

@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["fanout_source", "nfq_price_feedback_owned", "clearwait_removal_token", None])
async def test_old_payload_and_nested_reports_stay_inline(database, marker):
    service = service_for(database)
    with database() as session:
        _, order = order_in_session(session, {marker: "true"} if marker else {})
        intent = session.get(TradeIntent, order.intent_id)
        request = OrderRequest(order.client_order_id, "live:orb", "schwab_1m_v2", order.symbol,
            "buy", "open", order.quantity, "controlled", {})
        report = ExecutionReport("accepted", order.client_order_id, metadata={})
        staged = [] if marker else None
        await service._record_order_reports(session=session, strategy_id=order.strategy_id,
            broker_account_id=order.broker_account_id, intent=intent,
            intent_event=_event(account="live:orb", metadata={}), request=request,
            reports=[report], ordinary_audits=staged)
        assert staged in (None, [])
        assert len(list(session.scalars(select(BrokerOrderEvent)))) == 1
        assert "_ordinary_report_audit_fifo" not in service.__dict__
        assert PRIOR not in session.info
        session.commit()

@pytest.mark.asyncio
async def test_background_terminal_evidence_waits_for_committed_fifo(database, monkeypatch):
    service = service_for(database)
    entered, release = threading.Event(), threading.Event()
    original = service.store.append_order_event
    def append(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.store, "append_order_event", append)
    cleared = []
    async def evidence(*args, **kwargs):
        with database() as session:
            assert session.scalar(select(BrokerOrderEvent.id)) is not None
        cleared.append(True)
    monkeypatch.setattr("project_mai_tai.oms.service.acquire_cancel_terminal_evidence", evidence)
    with database() as session:
        staged, _ = staged_order(service, session)
        await service._commit_reports_and_transfer_audits(session, staged)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        service._schedule_cancel_terminal_evidence("live:orb", uuid4())
        await asyncio.sleep(0.01)
        assert cleared == []
    finally:
        release.set()
        await service._drain_cancel_terminal_evidence()
    assert cleared == [True]

@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["canceled", "filled"])
async def test_terminal_cancel_refusal_stays_inline(database, terminal):
    service = service_for(database)
    with database() as session:
        staged, order = staged_order(service, session)
        staged.clear()
        report = ExecutionReport("rejected", order.client_order_id,
            reason=f" order   in state {terminal} cannot be canceled ", origin="broker", metadata={})
        assert not service._stage_ordinary_audit(staged, order, report, {}, prior={}, request_metadata={})
        assert await service._append_order_event_isolated_awaited(session, order=order, report=report,
            payload={"reason": report.reason})
        assert service.store.count_terminal_cancel_refusals(session, order_id=order.id) == 1
        assert staged == []


@pytest.mark.asyncio
async def test_concurrent_capacity_waiters_recheck_no_drop_one_writer(database, monkeypatch):
    service = service_for(database)
    with database() as session:
        staged, order = staged_order(service, session)
        items = [AuditItem.capture(order, ExecutionReport("accepted", order.client_order_id),
            {"sequence": i}) for i in range(8)]
        session.commit()
    entered, release = threading.Event(), threading.Event()
    original = service.store.append_order_event
    seen, concurrent = [], []
    fifo = service._report_audit_fifo()
    fifo.limit = 1
    def append(*args, **kwargs):
        concurrent.append(len(fifo.pending) + int(fifo.active is not None))
        seen.append(kwargs["payload"]["sequence"])
        if len(seen) == 1:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)
    fifo.append = append
    await fifo.transfer(items[:1])
    tasks = []
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        tasks = [asyncio.create_task(fifo.transfer((item,))) for item in items[1:]]
        await asyncio.sleep(0.01)
        assert fifo.enqueued == 1 and all(not task.done() for task in tasks)
    finally:
        release.set()
        await asyncio.gather(*tasks)
        await fifo.drain()
    assert max(concurrent) == 1
    assert seen == list(range(8))
    assert fifo.enqueued == fifo.written == 8 and fifo.failed == 0


@pytest.mark.asyncio
async def test_whole_shutdown_fences_producer_settlement_and_private_commit(database, monkeypatch):
    service = service_for(database)
    settling, settled = asyncio.Event(), asyncio.Event()
    entered, release = threading.Event(), threading.Event()
    original = service.store.append_order_event
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.store, "append_order_event", blocked)
    async def producer():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            settling.set()
            await settled.wait()
            with database() as session:
                staged, _ = staged_order(service, session)
                await service._commit_reports_and_transfer_audits(session, staged)
    task = asyncio.create_task(producer())
    await asyncio.sleep(0)
    shutdown = asyncio.create_task(service._shutdown_runtime_producers(asyncio.Event(), task))
    try:
        await settling.wait()
        shutdown.cancel()
        await asyncio.sleep(0.01)
        assert not shutdown.done() and not task.done()
        settled.set()
        assert await asyncio.to_thread(entered.wait, 5)
        shutdown.cancel()
        await asyncio.sleep(0.01)
        assert not shutdown.done() and service._report_audit_fifo().written == 0
    finally:
        settled.set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
    assert task.done() and service._report_audit_fifo().written == 1
    with database() as session:
        assert session.scalar(select(BrokerOrderEvent.order_id)) is not None
