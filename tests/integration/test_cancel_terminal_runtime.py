"""Real PostgreSQL OMS journal and adapter endpoint controls, never live broker traffic."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import UTC, datetime
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from project_mai_tai.cancel_terminal_proof import evaluate_cancel_terminal
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, Strategy, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import cancel_terminal as journal
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

NOW = 1_791_466_919_000


@pytest.fixture
def sessions():
    url = os.environ.get("MAI_TAI_DATABASE_URL")
    assert url, "required real PostgreSQL service: MAI_TAI_DATABASE_URL"
    engine = create_engine(url)
    assert engine.dialect.name == "postgresql", "cancel evidence requires real PostgreSQL"
    schema = "cancel_terminal_" + uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(isolated)
    threads = []
    event.listen(engine, "before_cursor_execute", lambda *args: threads.append(threading.get_ident()))
    factory = sessionmaker(bind=isolated, expire_on_commit=False)
    factory.test_threads = threads
    yield factory
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


@pytest.fixture
def sdk(monkeypatch):
    class Request:
        kind = ""

        def __init__(self):
            self.values = {}

        def __getattr__(self, name):
            if name.startswith("set_"):
                return lambda value: self.values.__setitem__(name[4:], value)
            raise AttributeError(name)

    for path, cls_name, kind in (
        ("get_open_orders_request", "OpenOrdersListRequest", "open"),
        ("get_order_detail_request", "OrderDetailRequest", "detail"),
    ):
        name = "webull.trade.request." + path
        module = ModuleType(name)
        setattr(module, cls_name, type(cls_name, (Request,), {"kind": kind}))
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(broker, "now_ms", lambda: NOW)


class Client:
    def __init__(self, pages=None, detail=None):
        self.pages = pages or [{"has_next": False, "orders": []}]
        self.detail = detail
        self.calls = []
        self.on_read = None

    def get_response(self, request):
        self.calls.append((request.kind, dict(request.values), threading.get_ident()))
        if self.on_read:
            self.on_read()
        if request.kind == "detail":
            return SimpleNamespace(status_code=404 if self.detail is None else 200, body=self.detail)
        assert request.kind == "open", "today/cache cannot prove complete working book"
        index = sum(kind == "open" for kind, _, _ in self.calls) - 1
        return SimpleNamespace(status_code=200, body=self.pages[index])


def adapter(client):
    leaf = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    leaf.accounts_by_name = {"live:orb": WebullAccountConfig(account_id="ACC1")}
    leaf._get_client = lambda: client
    return RoutingBrokerAdapter(default_provider="webull", provider_by_account={"live:orb": "webull"},
                                factories_by_provider={"webull": lambda: leaf})


def cancel_event(*, coid="exact-coid", symbol="DKI"):
    md = {"clearwait_removal_token": "exact-token", "clearwait_opportunity_id": str(NOW),
          "clearwait_purpose": "retry_exhausted", "reason": "retry_budget_exhausted"}
    if coid:
        md["target_client_order_id"] = coid
    return TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb", symbol=symbol,
        side="buy", intent_type="cancel", quantity=Decimal(1), reason="retry_budget_exhausted",
        metadata=md,
    ))


def seed(sessions, routed, *, coid="exact-coid"):
    ev = cancel_event(coid=coid)
    with sessions() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        account = BrokerAccount(name="live:orb", provider="webull", environment="test",
                                external_account_id="ACC1")
        session.add_all([strategy, account])
        session.flush()
        intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
            symbol="DKI", side="buy", intent_type="cancel", quantity=Decimal(1),
            reason="retry_budget_exhausted", status="rejected",
            updated_at=datetime.fromtimestamp((NOW - 20_000) / 1000, UTC),
            payload={"event_id": str(ev.event_id), "metadata": dict(ev.payload.metadata),
                     "refusal_origin": "skipped_before_submit", "refusal_code": "cancel_target_not_found"})
        journal.bind_cancel_target(intent, ev, routed)
        session.add(intent)
        session.commit()
        return intent.id


def read(sessions, intent_id):
    with sessions() as session:
        intent = session.get(TradeIntent, intent_id)
        account = session.get(BrokerAccount, intent.broker_account_id)
        receipt = journal.receipt_from_intent(intent, account)
        evidence = journal.load_cancel_terminal_evidence(session, [intent])
        proof = evaluate_cancel_terminal(receipt, evidence.get(receipt.scope.event_id), now_ms=NOW)
        return intent, proof


@pytest.mark.asyncio
async def test_offloop_real_pg_exact_journal_and_every_page(sessions, sdk):
    client = Client(pages=[{"has_next": True, "orders": [
        {"client_order_id": "other", "symbol": "OTHER", "order_status": "SUBMITTED"}]},
        {"has_next": False, "orders": []}])
    routed = adapter(client)
    intent_id = seed(sessions, routed)
    sessions.test_threads.clear()
    await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    assert sessions.test_threads and all(t != threading.get_ident() for t in sessions.test_threads)
    assert all(t != threading.get_ident() for _, _, t in client.calls)
    intent, proof = read(sessions, intent_id)
    assert proof.terminal and proof.scope.account_id == "ACC1"
    assert intent.updated_at == datetime.fromtimestamp((NOW - 20_000) / 1000, UTC)
    assert client.calls[-1][1]["last_client_order_id"] == "other"
    assert intent.payload[journal.JOURNAL_KEY]["binding"]["generation"] == str(NOW)
    assert intent.payload[journal.JOURNAL_KEY]["binding"]["token"] == "exact-token"
    with sessions() as session:
        raw = session.execute(text(
            f'SELECT (payload::jsonb->\'{journal.JOURNAL_KEY}\'->\'evidence\'->\'book\' '
            f'->>\'started_at_ms\')::bigint FROM "{session.bind.get_execution_options()["schema_translate_map"][None]}".trade_intents'
        )).scalar_one()
        assert raw == NOW


@pytest.mark.parametrize("pages", [
    [{"has_next": True, "orders": []}], [{"orders": []}], [{"has_next": 0, "orders": []}],
    [{"has_next": False, "orders": [{"client_order_id": "exact-coid", "symbol": "DKI",
                                      "order_status": "SUBMITTED"}]}],
    [{"has_next": False, "orders": [{"client_order_id": "exact-coid", "symbol": "FOREIGN",
                                      "order_status": "SUBMITTED"}]}],
])
@pytest.mark.asyncio
async def test_partial_unknown_and_real_working_books_never_clear(sessions, sdk, pages):
    routed = adapter(Client(pages=pages))
    intent_id = seed(sessions, routed)
    await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    assert not read(sessions, intent_id)[1].terminal


@pytest.mark.parametrize("status,filled,clear", [
    ("CANCELLED", "0", True), ("REJECTED", "0", True), ("EXPIRED", "0", True),
    ("CANCELLED", "1", False), ("FILLED", "1", False), ("PENDING_CANCEL", "0", False),
])
@pytest.mark.asyncio
async def test_exact_broker_terminal_target_not_local_rejected_order(sessions, sdk, status, filled, clear):
    detail = {"account_id": "ACC1", "client_order_id": "exact-coid", "order_id": "broker-id",
              "items": [{"symbol": "DKI", "order_status": status, "filled_qty": filled}]}
    routed = adapter(Client(detail=detail))
    intent_id = seed(sessions, routed)
    await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    assert read(sessions, intent_id)[1].terminal is clear


@pytest.mark.parametrize("field", ["token", "purpose", "generation"])
@pytest.mark.asyncio
async def test_request_cas_change_during_http_never_publishes_evidence(sessions, sdk, field):
    client = Client()
    routed = adapter(client)
    intent_id = seed(sessions, routed)

    def change_request():
        client.on_read = None
        with sessions() as session:
            intent = session.get(TradeIntent, intent_id)
            payload = dict(intent.payload)
            payload[journal.BINDING_KEY] = {**payload[journal.BINDING_KEY], field: "foreign"}
            intent.payload = payload
            session.commit()

    client.on_read = change_request
    assert not await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    with sessions() as session:
        assert journal.JOURNAL_KEY not in session.get(TradeIntent, intent_id).payload


@pytest.mark.asyncio
async def test_missing_recorded_dki_coid_never_invented(sessions, sdk):
    client = Client()
    routed = adapter(client)
    intent_id = seed(sessions, routed, coid="")
    assert not await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    assert client.calls == []


@pytest.mark.parametrize("field,value", [("account_id", "foreign"), ("client_order_id", "foreign")])
@pytest.mark.asyncio
async def test_mismatched_broker_detail_cannot_fall_back_to_empty_book(sessions, sdk, field, value):
    detail = {"account_id": "ACC1", "client_order_id": "exact-coid", "order_id": "broker-id",
              "items": [{"symbol": "DKI", "order_status": "CANCELLED", "filled_qty": "0"}]}
    detail[field] = value
    client = Client(detail=detail)
    routed = adapter(client)
    intent_id = seed(sessions, routed)
    await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    assert not read(sessions, intent_id)[1].terminal
    assert [kind for kind, _, _ in client.calls] == ["detail"]


@pytest.mark.parametrize("field", ["clearwait_removal_token", "clearwait_purpose", "clearwait_opportunity_id"])
@pytest.mark.asyncio
async def test_journal_loader_rejects_changed_request_metadata(sessions, sdk, field):
    routed = adapter(Client())
    intent_id = seed(sessions, routed)
    await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    with sessions() as session:
        intent = session.get(TradeIntent, intent_id)
        payload = dict(intent.payload)
        payload["metadata"] = {**payload["metadata"], field: "foreign"}
        intent.payload = payload
        session.flush()
        assert journal.load_cancel_terminal_evidence(session, [intent]) == {}


@pytest.mark.asyncio
async def test_real_oms_cancel_caller_produces_committed_book(sessions, sdk, monkeypatch):
    # Exercise the real source caller, not a test-only invocation of the producer.
    monkeypatch.setattr(broker, "now_ms", lambda: int(datetime.now(UTC).timestamp() * 1000))
    client = Client()
    service = OmsRiskService(settings=Settings(oms_adapter="simulated", webull_account_id="ACC1"),
        redis_client=SimpleNamespace(), session_factory=sessions, broker_adapter=adapter(client))

    async def publish(_event):
        pass

    monkeypatch.setattr(service, "_publish_order_event", publish)
    await service.process_trade_intent(cancel_event())
    with sessions() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.intent_type == "cancel"))
        assert intent.status == "rejected"
        evidence = journal.load_cancel_terminal_evidence(session, [intent])
        assert len(evidence) == 1
        receipt = next(iter(evidence.values())).receipt
        assert evaluate_cancel_terminal(receipt, evidence[receipt.scope.event_id],
                                        now_ms=broker.now_ms()).terminal
    assert [kind for kind, _, _ in client.calls] == ["detail", "open"]


@pytest.mark.asyncio
async def test_schwab_exact_target_receipt_without_relabelled_account_list(sessions, sdk):
    leaf = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    leaf.accounts_by_name = {"live:orb": SchwabAccountConfig(account_hash="ACC1")}
    calls = []

    async def get(method, path):
        calls.append((method, path))
        return 200, {}, {"orderId": "broker-id", "status": "CANCELED", "filledQuantity": 0,
                         "orderLegCollection": [{"instrument": {"symbol": "DKI"}}]}

    leaf._authorized_request_json = get
    intent_id = seed(sessions, leaf)
    request = await asyncio.to_thread(journal._read_request, sessions, intent_id)
    evidence = await broker.acquire_broker_cancel_evidence(leaf, request.receipt,
                                                         broker_order_id="broker-id")
    assert evaluate_cancel_terminal(request.receipt, evidence, now_ms=NOW).terminal
    assert calls == [("GET", "/trader/v1/accounts/ACC1/orders/broker-id")]
    unknown = await broker.acquire_broker_cancel_evidence(leaf, request.receipt)
    assert not evaluate_cancel_terminal(request.receipt, unknown, now_ms=NOW).terminal
