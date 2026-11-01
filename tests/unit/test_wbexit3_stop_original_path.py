"""SOURCE UNRUN: real C6 original-LIST/parser/lease/ceiling/sender controls.

Only SDK pages, positions, quote clock and typed broker replies are controlled.
The inherited fixture's permission callbacks are NOT used: release_count must
remain zero. Main must compose/review/fence before execution.
"""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from dataclasses import replace
from datetime import UTC, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BotControlAttempt, BrokerOrder, Fill, OmsManagedPosition, VirtualPosition
from project_mai_tai.oms import bot_control_atr as atr, wbexit3_stop
from tests.unit.test_killsw1_atr_stop import book as book, drive
from tests.unit.test_q_c6_original_list import original_case


def stop_port(book, monkeypatch, state):
    reports, reads = {}, []
    unknown = [False]
    native_read = book.broker.fetch_order_update  # Original children must stay LIST-only.
    async def submit(request):
        assert request.reason == atr.STOP_REASON and request.side == "sell" and request.intent_type == "close"
        assert request.quantity == 10 and state.broker_quantity >= Decimal(1010)
        assert request.order_type == "limit" and request.metadata["extended_hours"] == "true"
        with book.sessions() as db:
            order = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            audit = db.scalar(select(BotControlAttempt).where(BotControlAttempt.target_order_id == order.id,
                BotControlAttempt.operation == "SUBMIT_EXIT"))
            assert audit.status == "MAY_HAVE_SENT" and audit.dispatch_intent_at is not None
            assert db.scalar(select(VirtualPosition.quantity)) == 10
        assert any(kind == "list" for kind, _ in state.trace)
        assert state.trace[-1][0] == "positions"
        book.submits.append(request)
        status = "cancelled" if len(book.submits) == 1 else "filled"
        native = "CONTROLLED-stop-" + str(len(book.submits))
        report = ExecutionReport(status, request.client_order_id, broker_order_id=native,
            broker_fill_id=native + ":execution" if status == "filled" else None,
            symbol="ABC", side="sell", intent_type="close", quantity=request.quantity,
            filled_quantity=request.quantity if status == "filled" else Decimal(0),
            fill_price=Decimal(10) if status == "filled" else None,
            reported_at=book.clock.now(UTC), origin="broker")
        reports[request.client_order_id] = report
        if status == "filled":
            state.broker_quantity -= request.quantity
        return [report]
    async def fetch(request):
        if request.client_order_id not in reports:
            return await native_read(request)
        reads.append(request.client_order_id)
        if unknown[0]:
            raise TimeoutError("CONTROLLED exact original order read/429 UNKNOWN")
        return replace(reports[request.client_order_id], reported_at=book.clock.now(UTC))
    monkeypatch.setattr(book.broker, "submit_order", submit)
    monkeypatch.setattr(book.broker, "fetch_order_update", fetch)
    return reads, unknown


def fresh(book, state, seconds):
    book.clock.value = (book.clock.now(UTC) + timedelta(seconds=seconds)).isoformat()
    book.service._latest_quotes_by_symbol["ABC"]["received_at"] = book.clock.now(UTC)
    state.advance()


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("hours", [8, 21])
async def test_terminal_refusal_needs_separate_original_fetch_then_real_C6_retry(book, monkeypatch, hours):
    object.__setattr__(book.service.settings, "oms_webull_exit3_enabled", True)
    state = original_case(book, monkeypatch, broker_quantity=1010)
    reads, _ = stop_port(book, monkeypatch, state)
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert len(book.submits) == 1 and not reads
    assert book.release_count == 0
    with book.sessions() as db:
        order = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == book.submits[0].client_order_id))
        assert not order.payload.get(wbexit3_stop.READ_KEY)
        assert db.get(OmsManagedPosition, book.row).status == "open"
    # This pass ONLY reads the original native ID. It cannot send a new SELL.
    await drive(book)
    assert reads == [book.submits[0].client_order_id] and len(book.submits) == 1
    fresh(book, state, 11)
    await drive(book)
    assert [r.quantity for r in book.submits] == [Decimal(10), Decimal(10)]
    assert len({r.client_order_id for r in book.submits}) == 2
    assert len(book.submits[1].client_order_id) <= 40
    assert state.broker_quantity == Decimal(1000) and book.release_count == 0
    assert [kind for kind, _ in state.trace].count("cancel") == 1
    with book.sessions() as db:
        assert db.get(OmsManagedPosition, book.row).status == "closed"
        assert db.scalar(select(VirtualPosition.quantity)) == 0
        assert sum(db.scalars(select(Fill.quantity).where(Fill.side == "sell")), Decimal(0)) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("hours", [8, 21])
async def test_unknown_original_read_keeps_row_open_and_bounded_same_ID_retry(book, monkeypatch, hours):
    object.__setattr__(book.service.settings, "oms_webull_exit3_enabled", True)
    state = original_case(book, monkeypatch, broker_quantity=1010)
    reads, unknown = stop_port(book, monkeypatch, state)
    await drive(book)
    unknown[0] = True
    await drive(book)
    await drive(book)  # Same existing cadence: durable read backoff blocks query.
    assert reads == [book.submits[0].client_order_id]
    assert len(book.submits) == 1 and state.broker_quantity == Decimal(1010)
    with book.sessions() as db:
        assert db.get(OmsManagedPosition, book.row).status == "open"
        order = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == book.submits[0].client_order_id))
        assert not order.payload.get(wbexit3_stop.READ_KEY)
        assert order.payload[wbexit3_stop.READ_ATTEMPT_KEY]["count"] == 1
    assert book.release_count == 0
