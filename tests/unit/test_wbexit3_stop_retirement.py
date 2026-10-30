"""SOURCE UNRUN: real C6 DB/journal/canonical-writer retirement component.

This is NOT native/whole-driver acceptance. The inherited fixture's release
callbacks are never called here; no owner, lease, proof or ceiling is patched to
permit a wire. Only the explicit external ExecutionReport is controlled.
"""
from datetime import UTC, timedelta
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import BotControlAttempt, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, Strategy, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import bot_control_atr as atr, wbexit3_stop
from project_mai_tai.oms.bot_control import ControlConflict
from tests.unit.test_killsw1_atr_stop import book as book


async def prior_attempt(book, status="cancelled", filled=Decimal(0)):
    now = book.clock.now(UTC)
    metadata = {"entry_order_id": str(book.entry), "managed_row_id": str(book.row),
        "oms_v2_managed_exit": "true", "killsw1_command": str(book.lease.command_id),
        "wbexit3_stop_retry_sequence": "0", "order_type": "limit", "session": "AM",
        "limit_price": "10.05", "extended_hours": "true"}
    coid = f"ks1-atr-{book.lease.command_id.hex[:16]}-{book.row.hex[:16]}"
    event = TradeIntentEvent(source_service="CONTROLLED_COMPONENT", payload=TradeIntentPayload(
        strategy_code=atr.STRATEGY, broker_account_name=book.account_name, symbol="ABC", side="sell",
        intent_type="close", quantity=Decimal(10), reason=atr.STOP_REASON, metadata=metadata))
    request = OrderRequest(coid, book.account_name, atr.STRATEGY, "ABC", "sell", "close", Decimal(10),
        atr.STOP_REASON, metadata, "limit", "day")
    with book.sessions.begin() as db:
        strategy, account = db.get(Strategy, book.strategy), db.get(BrokerAccount, book.account)
        intent = book.service.store.create_trade_intent(db, strategy=strategy, broker_account=account, event=event)
        order = book.service.store.get_or_create_order(db, intent=intent, strategy_id=book.strategy,
            broker_account_id=book.account, client_order_id=coid, symbol="ABC", side="sell", quantity=Decimal(10),
            metadata=metadata, status="pending", order_type="limit", time_in_force="day")
        order.submitted_at = None
        db.flush()
        order_id, intent_id = order.id, intent.id
    attempt = book.journal.prepare(book.lease, order_id=order_id, operation="SUBMIT_EXIT", sequence_no=0,
        owner_hash=atr._hash(metadata), owner_revision=book.lease.generation, now=now)
    book.journal.dispatch(book.lease, attempt.attempt_id, now=now)
    report = ExecutionReport(status, coid, broker_order_id="CONTROLLED-prior-native",
        broker_fill_id="CONTROLLED-prior-native:2" if filled else None, symbol="ABC", side="sell", intent_type="close",
        quantity=Decimal(10), filled_quantity=filled, fill_price=Decimal(10) if filled else None,
        reported_at=now, origin="broker")
    with book.sessions() as db:
        await atr._record_sell(book.service, db, book.lease, book.journal, attempt,
            db.get(BrokerOrder, order_id), db.get(TradeIntent, intent_id), event, request, report)
    book.prior_report = report
    return order_id


async def fetch_original(book, monkeypatch, order_id):
    # Change only the external broker response. The existing _read_exit path
    # must physically fetch before the NEW separate receipt can be persisted.
    calls = []
    async def fetch(request):
        calls.append(request)
        assert request.client_order_id == book.prior_report.client_order_id
        assert request.metadata["broker_order_id"] == book.prior_report.broker_order_id
        return replace(book.prior_report, reported_at=book.clock.now(UTC))
    monkeypatch.setattr(book.broker, "fetch_order_update", fetch)
    await atr._read_exit(book.service, book.lease, book.journal, order_id)
    assert len(calls) == 1


def identity(book):
    with book.sessions() as db:
        row, entry, account = db.get(OmsManagedPosition, book.row), db.get(BrokerOrder, book.entry), db.get(BrokerAccount, book.account)
        return wbexit3_stop.next_identity(book.service, db, book.lease, book.journal,
            row, entry, account, book.clock.now(UTC))


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [12, 22])
async def test_only_authoritative_terminal_retirement_can_get_distinct_retry_identity(book, hours, monkeypatch):
    object.__setattr__(book.service.settings, "oms_webull_exit3_enabled", True)
    prior = await prior_attempt(book)
    if book.provider == "schwab":
        assert identity(book)[1] == 0  # Existing Schwab identity/behavior unchanged.
        return
    # Original C6 audit source text cannot prove a later separate native READ.
    with book.sessions() as db:
        assert wbexit3_stop.retired(book.service, db, book.lease, book.journal,
            db.get(BrokerOrder, prior), book.clock.now(UTC)) is False
    with pytest.raises(ControlConflict, match="READ_FIRST"):
        identity(book)
    await fetch_original(book, monkeypatch, prior)
    with pytest.raises(ControlConflict, match="backoff"):
        identity(book)
    book.clock.value = (book.clock.now(UTC) + timedelta(seconds=11)).isoformat()
    coid, sequence = identity(book)
    assert sequence == 1 and len(coid) <= 40
    with book.sessions() as db:
        assert coid != db.get(BrokerOrder, prior).client_order_id
        assert len(list(db.scalars(select(BotControlAttempt)))) == 1
        assert len(list(db.scalars(select(Fill)))) == 1  # Original BUY only; component never creates a new fill/wire.
    assert not book.submits and book.release_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [12])
async def test_unknown_existing_ACK_remains_same_order_read_first(book, hours):
    object.__setattr__(book.service.settings, "oms_webull_exit3_enabled", True)
    prior = await prior_attempt(book, "accepted")
    if book.provider == "schwab":
        assert identity(book)[1] == 0
        return
    with book.sessions() as db:
        assert wbexit3_stop.retired(book.service, db, book.lease, book.journal,
            db.get(BrokerOrder, prior), book.clock.now(UTC)) is False
    with pytest.raises(ControlConflict, match="READ_FIRST"):
        identity(book)
    assert not book.submits


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [12])
async def test_partial_terminal_needs_actual_canonical_execution_amount(book, hours, monkeypatch):
    object.__setattr__(book.service.settings, "oms_webull_exit3_enabled", True)
    prior = await prior_attempt(book, "cancelled", Decimal(2))
    if book.provider == "schwab":
        return
    await fetch_original(book, monkeypatch, prior)
    with book.sessions() as db:
        assert wbexit3_stop.retired(book.service, db, book.lease, book.journal,
            db.get(BrokerOrder, prior), book.clock.now(UTC)) is True
    # Deliberately corrupt a private ledger AFTER real canonical attribution.
    # A terminal string cannot hide this missing execution.
    with book.sessions.begin() as db:
        for fill in db.scalars(select(Fill).where(Fill.order_id == prior)):
            db.delete(fill)
    with pytest.raises(ControlConflict, match="not_canonically_recorded"):
        identity(book)
    assert not book.submits


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [12])
async def test_terminal_reply_followed_by_unknown_fetch_never_retires(book, hours, monkeypatch):
    object.__setattr__(book.service.settings, "oms_webull_exit3_enabled", True)
    prior = await prior_attempt(book)
    if book.provider == "schwab":
        return
    async def unreadable(request):
        raise TimeoutError("CONTROLLED read budget/429 UNKNOWN")
    monkeypatch.setattr(book.broker, "fetch_order_update", unreadable)
    with pytest.raises(TimeoutError):
        await atr._read_exit(book.service, book.lease, book.journal, prior)
    with book.sessions() as db:
        order = db.get(BrokerOrder, prior)
        assert order.payload.get(wbexit3_stop.READ_KEY) is None
        assert wbexit3_stop.retired(book.service, db, book.lease, book.journal, order, book.clock.now(UTC)) is False
    assert not book.submits
