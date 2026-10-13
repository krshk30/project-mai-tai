"""Final checkpoint controls; local fakes and SQLite, never provider calls."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from project_mai_tai.db.models import BrokerOrder, SystemIncident
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.oms import webull_order_identity as identity
from tests.unit.test_wbnullid1_identity import evidence, setup, snapshot


@pytest.mark.asyncio
async def test_fifo_offer_does_not_wait_for_blocked_metadata_writer(monkeypatch):
    _, _, order_id, report = setup()
    items = [replace(evidence(order_id, report), broker_order_id=f"CONTROLLED-{i}") for i in range(3)]
    writer = identity.IdentityWriter(None, None)
    started, release = Event(), Event()
    writes, threads = [], []

    def blocked(item):
        writes.append(item.broker_order_id)
        threads.append(get_ident())
        if len(writes) == 1:
            started.set()
            assert release.wait(2)

    monkeypatch.setattr(writer, "_write", blocked)
    try:
        writer.offer(items)
        assert writer.queue.qsize() == 3
        assert await asyncio.to_thread(started.wait, 1)
        assert not writer.task.done()
        assert writer.queue.qsize() == 2
        release.set()
        await writer.drain()
        assert writes == [item.broker_order_id for item in items]
        assert all(thread != get_ident() for thread in threads)
    finally:
        release.set()
        await writer.close()


@pytest.mark.asyncio
async def test_overflow_is_bounded_loud_and_never_resubmits_orders(monkeypatch):
    service, _, order_id, report = setup()
    writer = identity.IdentityWriter(None, None, capacity=2)
    items = [replace(evidence(order_id, report), broker_order_id=str(i)) for i in range(3)]
    writes = []
    monkeypatch.setattr(writer, "_write", lambda item: writes.append(item.broker_order_id))
    loud = Mock(wraps=identity.LOG.error)
    monkeypatch.setattr(identity.LOG, "error", loud)
    writer.offer(items)
    assert writer.queue.qsize() == writer.queue.maxsize == 2
    assert "queue_full" in loud.call_args.args[0]
    await writer.close()
    assert writes == ["0", "1"]
    assert service.broker_adapter.method_calls == []
    writer.offer([items[2]])
    assert "writer_closed" in loud.call_args.args[0]
    assert writes == ["0", "1"]


@pytest.mark.asyncio
async def test_write_failure_does_not_poison_next_item_or_next_offer(monkeypatch):
    _, _, order_id, report = setup()
    writer = identity.IdentityWriter(None, None)
    attempts = []
    items = [replace(evidence(order_id, report), broker_order_id=str(i)) for i in range(3)]

    def write(item):
        attempts.append(item.broker_order_id)
        if item.broker_order_id == "0":
            raise RuntimeError("CONTROLLED database failure")

    monkeypatch.setattr(writer, "_write", write)
    writer.offer(items[:2])
    await writer.drain()
    assert writer.queue.empty()
    writer.offer(items[2:])
    await writer.close()
    assert attempts == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_worker_thread_commit_dispatches_on_owned_loop():
    service, sessions, order_id, report = setup(broker_id="original")
    loop = asyncio.get_running_loop()

    def commit_from_thread():
        with sessions.begin() as session:
            order = session.get(BrokerOrder, order_id)
            service.store.update_order_from_report(order, report=report, metadata=order.payload)

    await asyncio.to_thread(commit_from_thread)
    assert service._webull_identity_loop is loop
    await service._drain_webull_order_identities()
    await service._webull_identity_writer.close()
    assert snapshot(sessions, order_id)["broker_order_id"] == "original"
    with sessions() as session:
        assert len(session.scalars(select(SystemIncident)).all()) == 1
    assert service.broker_adapter.method_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["sync", "cancel_verifier"])
async def test_real_consumer_returns_while_metadata_is_blocked_without_resubmit(path, monkeypatch):
    service, sessions, order_id, report = setup()
    before = snapshot(sessions, order_id)
    writer = identity.IdentityWriter(sessions, service._rpg_owns_old_order)
    service._webull_identity_writer = writer
    started, release = Event(), Event()

    def blocked(_item):
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(writer, "_write", blocked)
    try:
        writer.offer([evidence(order_id, report)])
        assert await asyncio.to_thread(started.wait, 1)
        if path == "sync":
            monkeypatch.setattr(service, "_should_refresh_working_order", lambda row: False)
            service.broker_adapter.fetch_order_update.return_value = report
            assert await service.sync_broker_orders(account_names=["live:orb"]) == {"orders":0,"terminal_orders":0}
        else:
            service.broker_adapter.fetch_order_update.return_value = replace(report, event_type="cancelled", intent_type="cancel")
            request = OrderRequest("CONTROLLED-client", "live:orb", "schwab_1m_v2", "FLYE",
                                   "buy", "cancel", Decimal(2), "CONTROLLED")
            assert await service._verify_cancel_landed(request=request, account_name="live:orb",
                symbol="FLYE", client_order_id=request.client_order_id, broker_order_id="",
                target_order_id=order_id) == "cancelled"
        assert not release.is_set() and not writer.task.done()
        assert snapshot(sessions, order_id) == before
        service.broker_adapter.fetch_order_update.assert_awaited_once()
        service.broker_adapter.submit_order.assert_not_awaited()
    finally:
        release.set()
        await writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", ["outer", "nested", "parent_of_committed_child"])
async def test_rolled_back_conflict_is_not_dispatched(rollback):
    service, sessions, order_id, report = setup(broker_id="original")
    with sessions() as session:
        order = session.get(BrokerOrder, order_id)
        if rollback == "outer":
            service.store.update_order_from_report(order, report=report, metadata=order.payload)
            session.rollback()
        else:
            with pytest.raises(ValueError):
                with session.begin_nested():
                    if rollback == "parent_of_committed_child":
                        with session.begin_nested():
                            service.store.update_order_from_report(order, report=report, metadata=order.payload)
                    else:
                        service.store.update_order_from_report(order, report=report, metadata=order.payload)
                    raise ValueError("CONTROLLED savepoint rollback")
            session.commit()
    await service._drain_webull_order_identities()
    writer = service.__dict__.get("_webull_identity_writer")
    if writer is not None:
        await writer.close()
    with sessions() as session:
        assert session.scalars(select(SystemIncident)).all() == []
    assert snapshot(sessions, order_id)["broker_order_id"] == "original"


@pytest.mark.asyncio
async def test_committed_savepoint_conflict_dispatches_after_outer_commit():
    service, sessions, order_id, report = setup(broker_id="original")
    with sessions() as session:
        order = session.get(BrokerOrder, order_id)
        with session.begin_nested():
            service.store.update_order_from_report(order, report=report, metadata=order.payload)
        assert service.__dict__.get("_webull_identity_writer") is None
        session.commit()
    await service._drain_webull_order_identities()
    await service._webull_identity_writer.close()
    with sessions() as session:
        assert len(session.scalars(select(SystemIncident)).all()) == 1


def test_after_commit_dispatch_failure_does_not_report_committed_fill_as_failed():
    service, sessions, order_id, report = setup(broker_id="original")

    def unavailable(_items):
        raise RuntimeError("CONTROLLED dispatch failure")

    service.store.webull_identity_dispatch = unavailable
    with sessions.begin() as session:
        order = session.get(BrokerOrder, order_id)
        service.store.update_order_from_report(order, report=report, metadata=order.payload)
        service.store.apply_fill_to_positions(session, strategy_id=order.strategy_id,
            broker_account_id=order.broker_account_id, symbol=order.symbol, side="buy",
            quantity=Decimal(2), price=Decimal(3), reported_at=datetime.now(UTC))
    with sessions() as session:
        order = session.get(BrokerOrder, order_id)
        assert service.store.get_virtual_position(session, strategy_id=order.strategy_id,
            broker_account_id=order.broker_account_id, symbol=order.symbol).quantity == 2


@pytest.mark.asyncio
async def test_nested_rollback_preserves_other_outer_transaction_evidence():
    service, sessions, order_id, report = setup(broker_id="original")
    with sessions() as session:
        order = session.get(BrokerOrder, order_id)
        service.store.update_order_from_report(order, report=replace(report, broker_order_id="outer-ID"), metadata=order.payload)
        with pytest.raises(ValueError):
            with session.begin_nested():
                service.store.update_order_from_report(order, report=report, metadata=order.payload)
                raise ValueError("CONTROLLED nested rollback")
        session.commit()
    await service._drain_webull_order_identities()
    await service._webull_identity_writer.close()
    with sessions() as session:
        incidents = session.scalars(select(SystemIncident)).all()
        assert len(incidents) == 1 and incidents[0].payload["incoming_id"] == "outer-ID"


@pytest.mark.asyncio
async def test_reused_session_after_rollback_dispatches_only_new_evidence():
    service, sessions, order_id, report = setup(broker_id="original")
    with sessions() as session:
        order = session.get(BrokerOrder, order_id)
        service.store.update_order_from_report(order, report=report, metadata=order.payload)
        session.rollback()
        order = session.get(BrokerOrder, order_id)
        service.store.update_order_from_report(order, report=replace(report, broker_order_id="new-ID"), metadata=order.payload)
        session.commit()
    await service._drain_webull_order_identities()
    await service._webull_identity_writer.close()
    with sessions() as session:
        incidents = session.scalars(select(SystemIncident)).all()
        assert len(incidents) == 1 and incidents[0].payload["incoming_id"] == "new-ID"


@pytest.mark.parametrize("broker_id", [True, 17, "", "   "])
def test_non_native_scalar_identity_is_refused(broker_id):
    _, _, order_id, report = setup()
    assert evidence(order_id, replace(report, broker_order_id=broker_id)) is None


@pytest.mark.asyncio
async def test_shutdown_deadline_does_not_cancel_running_metadata_commit(monkeypatch):
    _, _, order_id, report = setup()
    writer = identity.IdentityWriter(None, None)
    started, release, completed = Event(), Event(), Event()

    def write(_item):
        started.set()
        assert release.wait(2)
        completed.set()

    native_wait_for = asyncio.wait_for
    monkeypatch.setattr(writer, "_write", write)
    monkeypatch.setattr(identity.asyncio, "wait_for", lambda coro, timeout: native_wait_for(coro, timeout=0.01))
    try:
        writer.offer([evidence(order_id, report)])
        assert await asyncio.to_thread(started.wait, 1)
        await writer.close()
        assert writer.closed and not writer.task.done()
        release.set()
        await writer.drain()
        assert completed.is_set()
    finally:
        release.set()
        await writer.drain()


def test_postgresql_timeout_and_id_only_sql_receipt_not_database_execution():
    _, sessions, order_id, report = setup()
    receipt = []
    with sessions.begin() as native:
        class DialectReceipt:
            bind = SimpleNamespace(dialect=postgresql.dialect())

            def __getattr__(self, name):
                return getattr(native, name)

            def scalar(self, statement):
                receipt.append(str(statement.compile(dialect=self.bind.dialect)))
                return native.scalar(statement)

            def execute(self, statement):
                sql = str(statement.compile(dialect=self.bind.dialect))
                receipt.append(sql)
                return None if sql.startswith("SET LOCAL") else native.execute(statement)

        assert identity.persist_identities(DialectReceipt(), [evidence(order_id, report)],
            owner_guard=lambda *args: False) == ["saved"]
    assert receipt[:2] == ["SET LOCAL lock_timeout = '250ms'", "SET LOCAL statement_timeout = '2s'"]
    assert any("FOR UPDATE" in sql for sql in receipt)
    update = next(sql for sql in receipt if sql.startswith("UPDATE"))
    assert "broker_order_id IS NULL" in update and "updated_at=" in update
    assert "status=" not in update and "payload=" not in update
