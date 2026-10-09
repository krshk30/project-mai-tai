"""Actual source caller and journal controls; controlled SDK, no broker HTTP."""

import asyncio
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
import threading
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.cancel_terminal_proof import UnboundCancelRequest
from project_mai_tai.db.models import TradeIntent
from project_mai_tai.oms import unbound_cancel_book as journal
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.integration import test_cancel_terminal_runtime as runtime
from tests.unit import test_buy_submission_journal as token_runtime

sdk = runtime.sdk
sessions = token_runtime.sessions


async def noop(*args, **kwargs):
    pass


def expected(intent):
    md = intent.payload["metadata"]
    requested = int(intent.created_at.replace(tzinfo=UTC).timestamp() * 1000)
    return UnboundCancelRequest(intent.symbol, md["clearwait_removal_token"], md["clearwait_removal_token"],
        md["clearwait_opportunity_id"], md["clearwait_purpose"], requested,
        {"schwab": "hash", "live:orb": "ACC1"}, {"schwab": "schwab", "live:orb": "webull"})


async def produce(sessions, monkeypatch, client, *, include_purpose=True):
    routed = runtime.adapter(client)
    service = OmsRiskService(settings=Settings(oms_adapter="simulated", broker_default_provider="webull",
        orb_broker_account_name="unused"), session_factory=sessions, redis_client=SimpleNamespace(),
        broker_adapter=routed)
    publications = []
    async def publish(event):
        publications.append(event)
    monkeypatch.setattr(service, "_publish_order_event", publish)
    monkeypatch.setattr(service, "_evaluate_risk", lambda event: (True, "controlled"))
    monkeypatch.setattr(service, "_reconcile_after_intent", noop)
    monkeypatch.setattr(broker, "now_ms", lambda: int(datetime.now(UTC).timestamp() * 1000))
    event = runtime.cancel_event(coid="").model_copy(update={"source_service": "schwab-1m-v2"})
    event.payload.reason = "retry exhausted cancellation barrier (webull)"
    event.payload.metadata.update({"fanout_segment_id": str(runtime.NOW), "clearwait_buy_only": "true"})
    if not include_purpose:
        event.payload.metadata.pop("clearwait_purpose")
    client.on_read = lambda: publications or pytest.fail("read before receipt publication")
    feedback = await service.process_trade_intent(event)
    assert feedback and publications
    await service._drain_cancel_terminal_evidence()
    return service


@pytest.mark.asyncio
async def test_previous_emitter_missing_purpose_is_unknown_without_adopting_history(sessions, sdk, monkeypatch):
    client = runtime.Client()
    await produce(sessions, monkeypatch, client, include_purpose=False)
    assert client.calls == []
    with sessions() as session:
        intent = session.scalar(select(TradeIntent))
        assert intent.status == "rejected" and journal.JOURNAL_KEY not in intent.payload


@pytest.mark.asyncio
async def test_actual_unbound_cancel_source_publishes_before_book_and_preserves_receipt(sessions, sdk, monkeypatch):
    client = runtime.Client(pages=[{"hasNext": False, "pageSize": 100, "orders": []}])
    await produce(sessions, monkeypatch, client)
    assert [kind for kind, _, _ in client.calls] == ["open"]  # No invented coid/detail.
    with sessions() as session:
        intent = session.scalar(select(TradeIntent))
        assert intent.status == "rejected" and journal.JOURNAL_KEY in intent.payload
        raw = intent.payload[journal.JOURNAL_KEY]
        assert raw["binding"]["observed_at_ms"] == journal._epoch(intent.updated_at)
        assert "client_order_id" not in raw["binding"]
        assert raw["binding"]["intent_reason"] == "retry exhausted cancellation barrier (webull)"
        assert raw["binding"]["metadata_reason"] == "retry_budget_exhausted"
        books = journal.load_unbound_request_working_books(session, [intent], expected(intent))
        assert set(books) == {"live:orb"} and books["live:orb"].complete
        assert journal.load_unbound_request_working_books(session, [intent], expected(intent),
            now_ms=books["live:orb"].finished_at_ms)
        assert not journal.load_unbound_request_working_books(session, [intent], expected(intent),
            now_ms=books["live:orb"].started_at_ms + 15_001)
        for request in (replace(expected(intent), token="foreign"), replace(expected(intent), generation="foreign"),
                        replace(expected(intent), purpose="foreign"),
                        replace(expected(intent), account_ids={"schwab": "hash", "live:orb": "foreign"})):
            assert not journal.load_unbound_request_working_books(session, [intent], request)


@pytest.mark.parametrize("change", ["token", "provider", "source", "pending", "newer_unanswered", "stale", "malformed"])
@pytest.mark.asyncio
async def test_changed_or_newer_receipt_cannot_reuse_unbound_journal(sessions, sdk, monkeypatch, change):
    client = runtime.Client()
    await produce(sessions, monkeypatch, client)
    with sessions() as session:
        intent = session.scalar(select(TradeIntent))
        request = expected(intent)
        payload = {**intent.payload, "metadata": dict(intent.payload["metadata"])}
        if change == "token":
            payload["metadata"]["clearwait_removal_token"] = "foreign"
        elif change == "source":
            payload["source_service"] = "foreign"
        elif change == "pending":
            intent.status = "pending"
        elif change == "provider":
            from project_mai_tai.db.models import BrokerAccount
            session.get(BrokerAccount, intent.broker_account_id).provider = "schwab"
        elif change == "newer_unanswered":
            session.add(TradeIntent(strategy_id=intent.strategy_id, broker_account_id=intent.broker_account_id,
                symbol=intent.symbol, side="buy", intent_type="cancel", quantity=intent.quantity,
                reason=intent.reason, status="pending", payload=dict(intent.payload)))
        elif change == "malformed":
            payload["metadata"] = []
        else:
            payload[journal.JOURNAL_KEY]["book"]["started_at_ms"] -= 60_000
        intent.payload = payload
        session.flush()
        assert not journal.load_unbound_request_working_books(session, list(session.scalars(select(TradeIntent))), request)


@pytest.mark.asyncio
async def test_book_read_request_revision_cas_and_malformed_inventory_fail_closed(sessions, sdk, monkeypatch):
    client = runtime.Client(pages=[{"orders": []}])  # Missing measured hasNext.
    service = await produce(sessions, monkeypatch, client)
    with sessions() as session:
        intent = session.scalar(select(TradeIntent))
        assert journal.JOURNAL_KEY not in intent.payload
        intent_id = intent.id
    snapshot = journal._read(sessions, service.broker_adapter, intent_id)
    client.pages = [{"hasNext": False, "orders": []}]
    # Clear only this controlled malformed flight to test an independent CAS.
    leaf = broker.broker_binding(service.broker_adapter, "live:orb")[0]
    del leaf._cancel_terminal_book_cycle
    client.calls.clear()
    book = await broker.acquire_complete_working_book(service.broker_adapter, "live:orb")
    with sessions() as session:
        intent = session.get(TradeIntent, intent_id)
        intent.payload = {**intent.payload, "metadata": {**intent.payload["metadata"], "clearwait_removal_token": "new"}}
        session.commit()
    assert not journal._write(sessions, service.broker_adapter, snapshot, book)


@pytest.mark.asyncio
async def test_new_explicit_receipt_refreshes_prior_book_once_not_its_timestamps(sdk, monkeypatch):
    client = runtime.Client(pages=[{"hasNext": False, "orders": []}] * 2)
    routed = runtime.adapter(client)
    first = await broker.acquire_complete_working_book(routed, "live:orb")
    monkeypatch.setattr(broker, "now_ms", lambda: runtime.NOW + 10)
    books = await asyncio.gather(*(broker.acquire_complete_working_book(routed, "live:orb",
        after_ms=runtime.NOW + 1) for _ in range(10)))
    assert first.started_at_ms == runtime.NOW
    assert all(book and book.started_at_ms == runtime.NOW + 10 for book in books)
    assert len(client.calls) == 2  # Actual get_response counts, no hidden retries.
    assert await broker.acquire_complete_working_book(routed, "live:orb", after_ms=runtime.NOW + 11) is None
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_latest_receipt_uses_submillisecond_precision_not_older_journal(sessions, sdk, monkeypatch):
    from project_mai_tai.db.models import BrokerAccount
    service = await produce(sessions, monkeypatch, runtime.Client())
    with sessions() as session:
        older = session.scalar(select(TradeIntent))
        request = expected(older)
        account = session.get(BrokerAccount, older.broker_account_id)
        at = journal._utc(older.updated_at)
        older.updated_at = at.replace(microsecond=(at.microsecond // 1000) * 1000 + 100)
        old_raw = older.payload[journal.JOURNAL_KEY]
        older.payload = {**older.payload, journal.JOURNAL_KEY: {
            **old_raw, "binding": journal._binding(older, account, "ACC1")}}
        newer = TradeIntent(strategy_id=older.strategy_id, broker_account_id=older.broker_account_id,
            symbol=older.symbol, side="buy", intent_type="cancel", quantity=older.quantity,
            reason=older.reason, status=older.status, created_at=older.created_at,
            updated_at=older.updated_at + timedelta(microseconds=100),
            payload={**older.payload, "event_id": str(uuid4())})
        newer.payload = {key: value for key, value in newer.payload.items() if key != journal.JOURNAL_KEY}
        session.add(newer)
        session.flush()
        assert journal._epoch(older.updated_at) == journal._epoch(newer.updated_at)
        assert not journal.load_unbound_request_working_books(session, [older, newer], request)
    await service._drain_cancel_terminal_evidence()


@pytest.mark.asyncio
async def test_new_receipt_during_prior_physical_flight_waits_then_coalesces(sdk, monkeypatch):
    clock = [runtime.NOW]
    monkeypatch.setattr(broker, "now_ms", lambda: clock[0])
    started, release = threading.Event(), threading.Event()
    client = runtime.Client(pages=[{"hasNext": False, "orders": []}] * 2)
    def wait_first():
        if len(client.calls) == 1:
            started.set()
            assert release.wait(3)
    client.on_read = wait_first
    routed = runtime.adapter(client)
    first = asyncio.create_task(broker.acquire_complete_working_book(routed, "live:orb"))
    assert await asyncio.to_thread(started.wait, 2)
    clock[0] += 10
    requests = [asyncio.create_task(broker.acquire_complete_working_book(routed, "live:orb",
        after_ms=runtime.NOW + 1)) for _ in range(10)]
    try:
        await asyncio.sleep(0.01)
        assert len(client.calls) == 1 and not any(task.done() for task in requests)
    finally:
        release.set()
    prior = await first
    books = await asyncio.gather(*requests)
    assert prior.started_at_ms == runtime.NOW
    assert all(book and book.started_at_ms == runtime.NOW + 10 for book in books)
    assert len(client.calls) == 2
