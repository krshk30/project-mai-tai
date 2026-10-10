"""Recorded APUS shape; controlled replies prove RESERVE1's inline audit fence."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import threading

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms.report_audit_fifo import AuditItem
from tests.unit import test_reserve1 as recorded
from tests.unit.test_report_audit_fifo import database as controlled_database

database = controlled_database


def setup(database, monkeypatch, strategy="schwab_1m_v2"):
    # Keep the existing recorded caller fixture, but never share a StaticPool connection.
    monkeypatch.setattr(recorded, "create_engine", lambda *args, **kwargs: database.kw["bind"])
    monkeypatch.setattr(recorded, "sessionmaker", lambda **kwargs: database)
    service, sessions, adapter, symbol, now, stop = recorded.armed_harness(
        monkeypatch, strategy=strategy, reject=True)
    assert sessions is database
    adapter.reject_retry = True
    # Isolate the hard-stop quote caller from the fixture's unrelated pending CW flip.
    service._managed_v2_symbols.clear()
    return service, adapter, symbol, now, stop


async def block_fifo(service, database, monkeypatch):
    with database() as session:
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.side == "buy"))
        entry_id = entry.id
        item = AuditItem.capture(entry, ExecutionReport("accepted", entry.client_order_id),
            {"metadata": {}, "reason": "CONTROLLED FIFO blocker"})
    entered, release = threading.Event(), threading.Event()
    original = service.store.append_order_event

    def append(session, **kwargs):
        if kwargs["order"].id == entry_id and kwargs["report"].event_type == "accepted":
            entered.set()
            assert release.wait(10)
        return original(session, **kwargs)

    monkeypatch.setattr(service.store, "append_order_event", append)
    await service._report_audit_fifo().transfer((item,))
    assert await asyncio.to_thread(entered.wait, 5)
    return release


def committed_rejections(database, coid):
    with database() as session:
        return [(row.event_source, row.event_type) for row in session.scalars(
            select(BrokerOrderEvent).join(BrokerOrder).where(
                BrokerOrder.client_order_id == coid, BrokerOrderEvent.event_type == "rejected"))]


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_actual_recovery_reads_committed_rejection_while_fifo_blocked(database, monkeypatch, strategy):
    service, adapter, symbol, now, stop = setup(database, monkeypatch, strategy)
    release = await block_fifo(service, database, monkeypatch)
    observed = []
    consumer = service._reserve1_hard_stop_rejected_request

    async def read_proof(stop, events):
        coid = events[0].payload.client_order_id
        observed.append(committed_rejections(database, coid))
        return await consumer(stop, events)

    monkeypatch.setattr(service, "_reserve1_hard_stop_rejected_request", read_proof)
    try:
        await asyncio.wait_for(service._trigger_hard_stop(
            stop, trigger_price=Decimal("6.99"), trigger_source="bid"), 5)
        assert len(adapter.requests) == 2 and adapter.release_count == 2
        assert observed == [[("broker", "rejected")]]
        first, retry = adapter.requests
        assert retry.metadata["reserve1_retry"] == "1"
        assert retry.metadata["reserve1_retry_of"] == first.client_order_id
        assert retry.quantity == first.quantity == stop.quantity
        assert len(service._reserve1_retry_used) == 1
        assert committed_rejections(database, retry.client_order_id) == [("broker", "rejected")]
        fifo = service._report_audit_fifo()
        assert fifo.enqueued == 1 and fifo.written == fifo.failed == 0 and fifo.active is not None
        later = now + timedelta(seconds=3)
        monkeypatch.setattr(recorded.service_module, "utcnow", lambda: later)
        await asyncio.wait_for(service._handle_quote_tick_event(QuoteTickEvent(
            source_service="test", produced_at=later, payload=QuoteTickPayload(
                symbol=symbol, bid_price=Decimal("6.99"), ask_price=Decimal("7.00")))), 5)
        assert len(adapter.requests) == 3
        assert adapter.release_count == 3
        assert sum(request.metadata.get("reserve1_retry") == "1" for request in adapter.requests) == 1
        assert len(service._reserve1_retry_used) == 1
        assert fifo.written == fifo.failed == 0
    finally:
        release.set()
        await service._drain_cancel_terminal_evidence()
    assert fifo.written == 1 and fifo.failed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["client", "unknown_origin", "partial", "unknown_read", "filled_read"])
async def test_fake_uncertain_or_filled_rejection_never_admits_retry(database, monkeypatch, case):
    service, adapter, _, _, stop = setup(database, monkeypatch)
    original = adapter.submit_order

    async def submit(request):
        reports = await original(request)
        if case in {"client", "unknown_origin"}:
            return [replace(report, origin="client" if case == "client" else "unknown") for report in reports]
        if case == "partial":
            return [replace(report, filled_quantity=Decimal(1)) for report in reports]
        return reports

    monkeypatch.setattr(adapter, "submit_order", submit)
    if case.endswith("_read"):
        adapter.read_proof = "unknown" if case == "unknown_read" else "filled"
    release = await block_fifo(service, database, monkeypatch)
    try:
        await asyncio.wait_for(service._trigger_hard_stop(
            stop, trigger_price=Decimal("6.99"), trigger_source="bid"), 5)
        assert len(adapter.requests) == adapter.release_count == 1
        assert all("reserve1_retry" not in request.metadata for request in adapter.requests)
        if case in {"client", "unknown_origin", "partial"}:
            assert not any(method == "REJECTION-PROOF" for method, _ in adapter.calls)
        else:
            assert sum(method == "REJECTION-PROOF" for method, _ in adapter.calls) == 1
        if case != "partial":
            expected = "client" if case == "client" else "unknown" if case == "unknown_origin" else "broker"
            assert committed_rejections(database, adapter.requests[0].client_order_id) == [(expected, "rejected")]
        assert service._report_audit_fifo().written == service._report_audit_fifo().failed == 0
        assert service._report_audit_fifo().enqueued == (2 if case == "partial" else 1)
    finally:
        release.set()
        await service._drain_cancel_terminal_evidence()


@pytest.mark.asyncio
async def test_ordinary_accepted_hard_stop_sell_remains_fifo(database, monkeypatch):
    service, adapter, _, _, stop = setup(database, monkeypatch)
    original = adapter.submit_order

    async def submit(request):
        return [replace(report, event_type="accepted", reason="", filled_quantity=Decimal(0))
            for report in await original(request)]

    async def update(request):
        return ExecutionReport("accepted", request.client_order_id, origin="broker",
            broker_order_id=request.metadata["broker_order_id"], symbol=request.symbol,
            side=request.side, intent_type=request.intent_type, quantity=request.quantity,
            metadata=request.metadata)

    monkeypatch.setattr(adapter, "submit_order", submit)
    monkeypatch.setattr(adapter, "fetch_order_update", update)
    release = await block_fifo(service, database, monkeypatch)
    try:
        await asyncio.wait_for(service._trigger_hard_stop(
            stop, trigger_price=Decimal("6.99"), trigger_source="bid"), 5)
        assert len(adapter.requests) == 1
        fifo = service._report_audit_fifo()
        assert fifo.enqueued == 2 and fifo.written == fifo.failed == 0
        assert len(fifo.pending) == 1 and fifo.active is not None
        with database() as session:
            order = session.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == adapter.requests[0].client_order_id))
            assert order.status == "accepted"
            assert session.scalar(select(BrokerOrderEvent.id).where(BrokerOrderEvent.order_id == order.id)) is None
    finally:
        release.set()
        await service._drain_cancel_terminal_evidence()
    assert fifo.written == 2 and fifo.failed == 0
    with database() as session:
        assert session.scalar(select(BrokerOrderEvent.event_type).where(
            BrokerOrderEvent.order_id == order.id)) == "accepted"
