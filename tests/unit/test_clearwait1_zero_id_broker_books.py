"""Approved zero-ID rule, with explicit controlled complete broker responses."""

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.cancel_terminal_proof import BookOrder, CompleteWorkingBook
from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.webull_order_reads import shared_budget
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, OmsManagedPosition, TradeIntent
from project_mai_tai.v2_removed_wait import configured_removed_wait_bindings
from tests.unit.test_clearwait1_session_rollover import PRIMARY, WEBULL, NOW, ms, request, service, strategy
from tests.unit.test_clearwait1_session_rollover import db as controlled_db
from tests.unit.test_clearwait1_unbound import CONFIGURED_IDS, controlled_routing
from tests.integration.test_cancel_terminal_runtime import Client, sdk as controlled_sdk

db, sdk = controlled_db, controlled_sdk


def fixture(db):
    req = replace(request(), symbol="JZ", opportunity_id=0, requested_at_ms=ms(NOW))
    db[0].record(req, True)
    routing = controlled_routing()
    expected = configured_removed_wait_bindings(routing, req.account_names)
    with db[1]() as session:
        from project_mai_tai.db.models import BrokerAccount
        for name, account_id in db[2].items():
            session.get(BrokerAccount, account_id).external_account_id = CONFIGURED_IDS[name]
        session.commit()
    books = {name: CompleteWorkingBook(name, CONFIGURED_IDS[name], ms(NOW), ms(NOW),
        True, "all_working", (), "broker") for name in req.account_names}
    return req, routing, expected, books


@pytest.mark.parametrize("fault", ["none", "working_webull", "working_schwab", "broker_error",
    "unknown_side", "stale", "pre_request", "wrong_account", "incomplete",
    "recent_schwab_window", "full_schwab_page", "dispatch",
    "pending_buy", "working_db", "open_owner", "cas", "late_cas", "different_token"])
def test_zero_id_complete_books_clear_only_exact_no_dispatch_request(db, fault):
    req, _, expected, books = fixture(db)
    if fault.startswith("working_") and fault != "working_db":
        name = WEBULL if fault == "working_webull" else PRIMARY
        books[name] = replace(books[name], orders=(BookOrder("working", "JZ", "working", "buy"),))
    elif fault == "broker_error":
        books[WEBULL] = None
    elif fault == "unknown_side":
        books[WEBULL] = replace(books[WEBULL], orders=(BookOrder("unknown", "JZ", "working", "unknown"),))
    elif fault == "stale":
        books[WEBULL] = replace(books[WEBULL], started_at_ms=ms(NOW) - 15001)
    elif fault == "pre_request":
        books[WEBULL] = replace(books[WEBULL], started_at_ms=req.requested_at_ms - 1)
    elif fault == "wrong_account":
        books[PRIMARY] = replace(books[PRIMARY], account_id="not-configured")
    elif fault == "incomplete":
        books[PRIMARY] = replace(books[PRIMARY], complete=False)
    elif fault == "recent_schwab_window":
        # Exhausting an entered-time window does not cover older working GTCs.
        books[PRIMARY] = replace(books[PRIMARY], coverage="entered_time_window")
    elif fault == "full_schwab_page":
        books[PRIMARY] = replace(books[PRIMARY], orders=tuple(
            BookOrder("", "OTHER", "filled", "sell", str(index))
            for index in range(3000)))
    elif fault == "dispatch":
        db[0].record_dispatch({"schema_version": 1, "symbol": "JZ", "kind": "attempt"})
    with db[1]() as session:
        if fault == "pending_buy":
            session.add(TradeIntent(strategy_id=db[3], broker_account_id=db[2][WEBULL],
                symbol="JZ", side="buy", intent_type="open", quantity=1, status="submitting", reason="controlled"))
        elif fault == "working_db":
            session.add(BrokerOrder(strategy_id=db[3], broker_account_id=db[2][WEBULL],
                symbol="JZ", side="buy", order_type="limit", time_in_force="day", quantity=1,
                status="accepted", client_order_id="controlled-working"))
        elif fault == "open_owner":
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=PRIMARY,
                symbol="JZ", entry_price=1, original_quantity=1, current_quantity=1, status="open"))
        session.commit()
    if fault == "different_token":
        req = replace(req, token="changed-request")
    calls = []
    def current(_request):
        calls.append(True)
        return fault != "cas" and (fault != "late_cas" or len(calls) == 1)
    proof = db[0].retire_zero_id_no_dispatch(req, set(req.account_names), books=books,
        expected_bindings=expected, publication_current=current, now=NOW)
    assert proof.clear is (fault == "none")
    assert bool(db[0].restore()) is (fault != "none")
    with db[1]() as session:
        evidence = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_zero_id_terminal_broker_books"))
        assert (evidence is not None) is (fault == "none")
        if evidence is not None:
            assert evidence.payload["request"] == req.payload(active=True)
            assert set(evidence.payload["books"]) == {PRIMARY, WEBULL}
            assert evidence.payload["request_cas_current"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "working_webull", "broker_error", "schwab_unavailable", "emit_race"])
async def test_zero_id_actual_bot_caller_requires_both_complete_broker_readers(db, sdk, monkeypatch, fault):
    req, routing, _, books = fixture(db)
    strat = strategy(req)
    strat.configure_removed_wait(db[0].record, restored=db[0].restore(), readable=True,
                                dispatch_persist=db[0].record_dispatch)
    # Controlled no-dispatch setup: discard only the unsent boot cancel drafts.
    queued = (*strat.drain_pending_intents(), *strat.drain_webull_direct_intents())
    assert queued and all(d.intent_type == "cancel" for d in queued)
    bot = service(strat, db[0])
    bot._configure_flip_entry_ownership_store({})
    bot._removed_wait_adapter = routing
    orders = [{"client_order_id": "real-working-control", "order_id": "controlled-JZ",
        "items": [{"symbol": "JZ", "side": "BUY", "order_status": "WORKING"}]}] if fault == "working_webull" else []
    client = Client(pages=[{"has_next": False, "orders": orders}])
    if fault == "broker_error":
        client.on_read = lambda: (_ for _ in ()).throw(RuntimeError("controlled broker read failed"))
    elif fault == "emit_race":
        client.on_read = lambda: bot.__dict__.setdefault("_clearwait_emit_versions", {}).update(JZ=1)
    leaf = routing._adapter_for_account(WEBULL)
    leaf._get_client = lambda: client
    leaf._query_budget = shared_budget("controlled-zero-ID", uuid4().hex)
    monkeypatch.setattr(broker, "now_ms", lambda: ms(NOW))
    schwab_read = AsyncMock(return_value=books[PRIMARY])
    if fault != "schwab_unavailable":
        routing._adapter_for_account(PRIMARY).acquire_complete_working_book = schwab_read
    await bot._removed_wait_unbound_pass((req,), db[0], set(req.account_names))
    assert (not strat._removed_wait_requests) is (fault == "none")
    assert (not db[0].restore()) is (fault == "none")
    assert not strat.drain_pending_intents() and not strat.drain_webull_direct_intents()
    count = 0 if fault == "schwab_unavailable" else 1
    assert len(client.calls) == count
    assert not client.calls or client.calls[0][0] == "open"
    if fault != "emit_race":
        await bot._removed_wait_unbound_pass((req,), db[0], set(req.account_names))
        assert len(client.calls) == count, "no untriggered timer retry of a broker read"
