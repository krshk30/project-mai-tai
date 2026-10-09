"""Real test PostgreSQL hybrid witnesses and journal visibility; no broker traffic."""

import asyncio
from dataclasses import replace
from decimal import Decimal
import threading
from time import monotonic
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.cancel_terminal_proof import (
    SchwabLocalCancelWitness, UnboundCancelFences, UnboundCancelRequest,
    evaluate_unbound_cancel_terminal,
)
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import cancel_terminal as journal
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
import test_cancel_terminal_runtime as runtime

NOW = runtime.NOW
sessions = runtime.sessions
sdk = runtime.sdk


def request(symbol="FLYE"):
    return UnboundCancelRequest(symbol, "token", "token", str(NOW), "retry_exhausted",
        NOW - 1000, {"schwab": "hash", "live:orb": "ACC1"},
        {"schwab": "schwab", "live:orb": "webull"}, "2026-10-08")


def witness(req, **changes):
    return replace(SchwabLocalCancelWitness(req, "schwab", "hash", req.session_key,
        NOW, "exact_cancel_chain", True, True, True, True, True), **changes)


def proof(req, book, local):
    return evaluate_unbound_cancel_terminal(req, {"live:orb": book},
        fences=UnboundCancelFences(req, True, True, True, True),
        schwab_witness=local, now_ms=NOW)


@pytest.mark.parametrize("symbol,side,schwab_live,terminal", [
    ("FLYE", None, False, True), ("DKI", None, False, True),
    ("FLYE", "BUY", False, False), ("FLYE", "SELL", False, True),
    ("FLYE", None, True, False),
])
@pytest.mark.asyncio
async def test_controlled_hybrid_replays(sessions, sdk, symbol, side, schwab_live, terminal):
    client = runtime.Client(pages=[{"hasNext": False, "orders": [] if side is None else [
        {"order_id": "operator", "items": [{"symbol": symbol, "side": side,
                                               "order_status": "SUBMITTED"}]}]}])
    req = request(symbol)
    book = await broker.acquire_complete_working_book(runtime.adapter(client), "live:orb",
                                                     after_ms=req.requested_at_ms)
    assert proof(req, book, witness(req, no_live_buy=not schwab_live)).terminal is terminal
    with sessions() as session:
        assert session.execute(text("SELECT CAST(:epoch AS bigint)"),
                               {"epoch": NOW}).scalar_one() == NOW


@pytest.mark.asyncio
async def test_schwab_never_reads_inventory_or_detail(sessions, sdk):
    leaf = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    leaf.accounts_by_name = {"schwab": SchwabAccountConfig(account_hash="hash")}
    async def deny(*args):
        pytest.fail("Schwab cancel proof must issue no HTTP")
    leaf._authorized_request_json = deny
    assert await broker.acquire_complete_working_book(leaf, "schwab") is None
    assert await broker.acquire_request_working_books(leaf, ["schwab"]) == {"schwab": None}


@pytest.mark.asyncio
async def test_ten_same_account_requests_coalesce_without_prior_raise_reuse(sdk, monkeypatch):
    client = runtime.Client()
    routed = runtime.adapter(client)
    req = request()
    books = await asyncio.gather(*(broker.acquire_complete_working_book(routed, "live:orb",
        after_ms=req.requested_at_ms) for _ in range(10)))
    assert all(book and book.complete for book in books)
    assert [kind for kind, _, _ in client.calls] == ["open"]
    assert all(book.started_at_ms == NOW for book in books)
    assert await broker.acquire_complete_working_book(routed, "live:orb", after_ms=NOW + 1) is None
    assert len(client.calls) == 1
    monkeypatch.setattr(broker, "now_ms", lambda: NOW + 15_001)
    client.pages.append({"hasNext": False, "orders": []})
    book = await broker.acquire_complete_working_book(routed, "live:orb", after_ms=NOW + 1)
    assert book and book.started_at_ms == NOW + 15_001 and len(client.calls) == 2


@pytest.mark.asyncio
async def test_failed_or_cancelled_flight_cannot_launch_duplicate_or_fake_complete(sdk):
    client = runtime.Client(pages=[{"orders": []}])
    routed = runtime.adapter(client)
    assert await asyncio.gather(*(broker.acquire_complete_working_book(routed, "live:orb")
                                 for _ in range(10))) == [None] * 10
    assert len(client.calls) == 1


def service(sessions, adapter, **settings):
    return OmsRiskService(settings=Settings(oms_adapter="simulated", orb_broker_account_name="unused",
        broker_default_provider="webull", **settings), redis_client=SimpleNamespace(),
        session_factory=sessions, broker_adapter=adapter)


async def noop(*args, **kwargs):
    pass


@pytest.mark.asyncio
async def test_real_oms_stalled_30s_read_does_not_delay_receipt_quote_or_close(sessions, sdk, monkeypatch):
    client = runtime.Client(detail=runtime.EMPTY_DETAIL)
    routed = runtime.adapter(client)
    oms = service(sessions, routed)
    started, release = threading.Event(), threading.Event()
    publications = []
    async def publish(event):
        publications.append(event)
    def hang():
        if client.calls[-1][0] == "open":
            assert publications, "broker read preceded normal cancel receipt publication"
            started.set()
            assert release.wait(30), "controlled broker read was not released"
    client.on_read = hang
    monkeypatch.setattr(oms, "_publish_order_event", publish)
    monkeypatch.setattr(oms, "_evaluate_risk", lambda event: (True, "controlled"))
    monkeypatch.setattr(oms, "_reconcile_after_intent", noop)
    monkeypatch.setattr(oms, "_market_is_fillable", lambda *a: True)
    leaf = broker.broker_binding(routed, "live:orb")[0]
    async def submit(order):
        assert order.side == "sell" and order.intent_type == "close"
        return [ExecutionReport(event_type="accepted", client_order_id=order.client_order_id,
            broker_order_id="protective-close", symbol=order.symbol, side=order.side,
            intent_type=order.intent_type, quantity=order.quantity, metadata=order.metadata)]
    monkeypatch.setattr(leaf, "submit_order", submit)
    # Operational position only: exercise real close admission, not a terminal
    # ownership certificate or a bypass of the no-position SELL refusal.
    with sessions() as session:
        strategy = Strategy(code="macd_30s", name="controlled", execution_mode="live")
        account = BrokerAccount(name="live:orb", provider="webull", environment="test")
        session.add_all([strategy, account])
        session.flush()
        session.add_all([
            VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                symbol="DKI", quantity=Decimal(1), average_price=Decimal(2)),
            AccountPosition(broker_account_id=account.id, symbol="DKI",
                quantity=Decimal(1), average_price=Decimal(2)),
        ])
        session.commit()
    try:
        begin = monotonic()
        feedback = await oms.process_trade_intent(runtime.cancel_event())
        assert (monotonic() - begin) * 1000 < 50
        assert feedback and publications
        assert await asyncio.to_thread(started.wait, 2)
        with sessions() as session:
            pending = session.scalar(select(TradeIntent).where(TradeIntent.intent_type == "cancel"))
            assert pending.status == "rejected" and journal.JOURNAL_KEY not in pending.payload
        quote = QuoteTickEvent(source_service="test", payload=QuoteTickPayload(symbol="DKI",
            bid_price=Decimal(2), ask_price=Decimal("2.01")))
        begin = monotonic()
        await oms._handle_quote_tick_event(quote)
        assert (monotonic() - begin) * 1000 < 50
        assert oms._latest_quotes_by_symbol["DKI"]["bid"] == 2
        close = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
            strategy_code="macd_30s", broker_account_name="live:orb", symbol="DKI", side="sell",
            quantity=Decimal(1), intent_type="close", reason="PROTECTIVE_CLOSE",
            metadata={"reference_price": "2"}))
        begin = monotonic()
        closed = await oms.process_trade_intent(close)
        assert (monotonic() - begin) * 1000 < 50
        assert closed and closed[0].payload.status == "accepted" and not release.is_set()
    finally:
        release.set()
        await oms._drain_cancel_terminal_evidence()


@pytest.mark.parametrize("prewire", [False, True])
@pytest.mark.asyncio
async def test_real_oms_uncommitted_buy_visibility_is_never_local_terminal(sessions, sdk, monkeypatch, prewire):
    # Actual OMS dispatch is withheld on the broker side; an independent PG
    # connection observes whether the existing conditional durable lane ran.
    leaf = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    leaf.accounts_by_name = {"schwab": SchwabAccountConfig(account_hash="hash")}
    oms = service(sessions, leaf, strategy_schwab_1m_v2_account_name="schwab")
    monkeypatch.setattr(oms.settings, "broker_default_provider", "schwab")
    monkeypatch.setattr(oms, "_evaluate_risk", lambda event: (True, "controlled"))
    monkeypatch.setattr(oms, "_market_is_fillable", lambda *a: True)
    monkeypatch.setattr(oms, "_publish_order_event", noop)
    monkeypatch.setattr(oms, "_reconcile_after_intent", noop)
    async def no_refusal(*args, **kwargs):
        return None
    monkeypatch.setattr(oms, "_rpg_fresh_open_refusal", no_refusal)
    monkeypatch.setattr(oms, "_rpg_external_retry", lambda event: False)
    wb = await broker.acquire_complete_working_book(runtime.adapter(runtime.Client()), "live:orb")
    req = request()
    reached = False
    async def submit(order):
        nonlocal reached
        reached = True
        assert order.side == "buy"
        with sessions() as independent:
            intents = list(independent.scalars(select(TradeIntent)))
            orders = list(independent.scalars(select(BrokerOrder)))
            assert bool(intents) is prewire and bool(orders) is prewire
            if prewire:
                assert orders[0].client_order_id == order.client_order_id and orders[0].status == "pending"
            # Neither empty journal nor a visible live BUY authorizes release.
            assert not proof(req, wb, witness(req, no_unjournaled_buy=prewire,
                                               no_live_buy=not prewire)).terminal
        return [ExecutionReport(event_type="accepted", client_order_id=order.client_order_id,
            broker_order_id="accepted-on-wire", symbol=order.symbol, side="buy", intent_type="open",
            quantity=order.quantity, metadata=order.metadata)]
    leaf.submit_order = submit
    event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="schwab", symbol="FLYE", side="buy",
        quantity=Decimal(1), intent_type="open", reason="ENTRY",
        metadata={"reference_price": "2", "entry_size_price": "2",
                  **({"rpg_handoff_token": "token"} if prewire else {})}))
    await oms.process_trade_intent(event)
    assert reached
    with sessions() as independent:
        assert independent.scalar(select(BrokerOrder)).status == "accepted"


@pytest.mark.parametrize("when", ["before_read", "during_read"])
@pytest.mark.asyncio
async def test_journal_provider_mismatch_cannot_publish_evidence(sessions, sdk, when):
    client = runtime.Client()
    routed = runtime.adapter(client)
    intent_id = runtime.seed(sessions, routed)
    def mismatch():
        client.on_read = None
        with sessions() as session:
            intent = session.get(TradeIntent, intent_id)
            session.get(BrokerAccount, intent.broker_account_id).provider = "schwab"
            session.commit()
    if when == "before_read":
        mismatch()
    else:
        client.on_read = mismatch
    assert await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id]) == {}
    assert bool(client.calls) is (when == "during_read")


@pytest.mark.parametrize("terminal_receipt", [True, False])
@pytest.mark.asyncio
async def test_same_request_receipt_reused_but_old_book_reacquired(sessions, sdk, monkeypatch, terminal_receipt):
    detail = {"account_id": "ACC1", "client_order_id": "exact-coid", "order_id": "broker-id",
              "items": [{"symbol": "DKI", "order_status": "CANCELLED", "filled_qty": "0"}]}
    client = runtime.Client(detail=detail if terminal_receipt else None,
        pages=[{"hasNext": False, "orders": []}, {"hasNext": False, "orders": []}])
    routed = runtime.adapter(client)
    clock = [0.0]
    broker.broker_binding(routed, "live:orb")[0]._query_budget.clock = lambda: clock[0]
    intent_id = runtime.seed(sessions, routed)
    assert await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    count = len(client.calls)
    advanced = NOW + 8 * 3_600_000
    clock[0] = 8 * 3600
    monkeypatch.setattr(broker, "now_ms", lambda: advanced)
    evidence = await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    assert len(client.calls) == count + (0 if terminal_receipt else 2)
    with sessions() as session:
        intent = session.get(TradeIntent, intent_id)
        receipt = journal.receipt_from_intent(intent, session.get(BrokerAccount, intent.broker_account_id))
        assert runtime.evaluate_cancel_terminal(receipt, evidence[receipt.scope.event_id], now_ms=advanced).terminal
