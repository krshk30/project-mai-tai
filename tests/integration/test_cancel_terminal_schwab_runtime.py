"""Schwab filtered acquisition controls; no live broker traffic."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.cancel_terminal_proof import (
    CancelReceipt, CancelScope, evaluate_cancel_terminal,
)
import test_cancel_terminal_runtime as runtime

NOW = runtime.NOW
sessions = runtime.sessions
sdk = runtime.sdk


def order(*, oid=1, status="WORKING", side="BUY", children=None):
    return {"orderId": oid, "status": status, "filledQuantity": 0,
            "enteredTime": datetime.fromtimestamp((NOW - 1000) / 1000, UTC).isoformat(),
            "orderLegCollection": [{"instruction": side,
                "instrument": {"symbol": "FLYE", "assetType": "EQUITY"}}],
            "childOrderStrategies": children or []}


def adapter(code, rows, calls):
    leaf = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    leaf.accounts_by_name = {"schwab": SchwabAccountConfig(account_hash="actual-hash")}

    async def get(method, path):
        calls.append((method, path))
        query = parse_qs(urlsplit(path).query)
        start, stop = (datetime.fromisoformat(query[key][0]) for key in ("fromEnteredTime", "toEnteredTime"))
        return code, {}, ([r for r in rows if not isinstance(r, dict) or "enteredTime" not in r
                          or start <= datetime.fromisoformat(r["enteredTime"]) <= stop]
                         if isinstance(rows, list) else rows)

    leaf._authorized_request_json = get
    return leaf


@pytest.mark.parametrize("rows,statuses,terminal", [
    ([], [], True),
    ([order()], ["working"], False),
    ([{**order(), "enteredTime": datetime.fromtimestamp((NOW - 120 * 86_400_000) / 1000, UTC).isoformat()}],
     ["working"], False),
    ([order(status="FILLED", children=[order(oid=2, status="AWAITING_PARENT_ORDER")])],
     ["working"], False),
    ([order(status="FILLED", children=[order(oid=2, status="PARTIAL_FILL")])],
     ["partially_filled"], False),
    ([order(side="SELL")], ["working"], True),
])
@pytest.mark.asyncio
async def test_unfiltered_slices_walk_terminal_parents(sessions, sdk, rows, statuses, terminal):
    calls = []
    leaf = adapter(200, rows, calls)
    book = await broker.acquire_complete_working_book(leaf, "schwab")
    assert book and book.source == "broker" and book.complete is True
    assert book.coverage == "all_working"
    assert [row.status for row in book.orders] == statuses
    assert broker.broker_binding(leaf, "schwab") == (leaf, "actual-hash")
    query = parse_qs(urlsplit(calls[0][1]).query)
    queries = [parse_qs(urlsplit(path).query) for _, path in calls]
    assert len(calls) == 26 and all(method == "GET" for method, _ in calls)
    assert all("status" not in q and q["maxResults"] == ["3000"] for q in queries)
    assert (datetime.fromisoformat(queries[-1]["toEnteredTime"][0])
            - datetime.fromisoformat(query["fromEnteredTime"][0])).days == 181
    assert all(a["toEnteredTime"] == b["fromEnteredTime"] for a, b in zip(queries, queries[1:]))
    assert all((datetime.fromisoformat(q["toEnteredTime"][0])
                - datetime.fromisoformat(q["fromEnteredTime"][0])).days <= 7 for q in queries)
    receipt = CancelReceipt(CancelScope("schwab", "actual-hash", "FLYE", "exact", "event"),
                            NOW - 1000, "rejected", "skipped_before_submit", "")
    evidence = await broker.acquire_broker_cancel_evidence(leaf, receipt)
    assert evaluate_cancel_terminal(receipt, evidence, now_ms=NOW).terminal is (not rows)
    from dataclasses import replace
    from project_mai_tai.cancel_terminal_proof import UnboundCancelFences, UnboundCancelRequest, evaluate_unbound_cancel_terminal
    request = UnboundCancelRequest("FLYE", "token", "token", str(NOW), "retry_exhausted", NOW - 1000,
        {"schwab": "actual-hash", "webull": "ACC1"}, {"schwab": "schwab", "webull": "webull"})
    books = {"schwab": book, "webull": replace(book, account_name="webull", account_id="ACC1", orders=())}
    assert evaluate_unbound_cancel_terminal(request, books,
        fences=UnboundCancelFences(request, True, True, True, True), now_ms=NOW).terminal is terminal
    with sessions() as session:
        assert session.execute(text("SELECT CAST(:epoch AS bigint)"), {"epoch": NOW}).scalar_one() == NOW


@pytest.mark.parametrize("code,rows", [
    (599, {"message": "timeout"}),
    (400, {"message": "invalid status"}),
    (200, {}),
    (200, [order()] * 3000),
    (200, [order(status="UNKNOWN")]),
    (200, [order(side="BUY_TO_COVER")]),
    (200, [order(children=[{}])]),
    (200, [order(), order(side="SELL")]),
])
@pytest.mark.asyncio
async def test_unreadable_capped_or_malformed_schwab_book_is_unknown(sessions, sdk, code, rows):
    calls = []
    assert await broker.acquire_complete_working_book(adapter(code, rows, calls), "schwab") is None
    assert 1 <= len(calls) <= 26
    with sessions() as session:
        assert session.execute(text("SELECT CAST(:epoch AS bigint)"), {"epoch": NOW}).scalar_one() == NOW


@pytest.mark.asyncio
async def test_schwab_acquisition_over_15_seconds_is_unknown(sessions, sdk, monkeypatch):
    times = iter([NOW, NOW + 15_001])
    monkeypatch.setattr(broker, "now_ms", lambda: next(times))
    assert await broker.acquire_complete_working_book(adapter(200, [], []), "schwab") is None


@pytest.mark.parametrize("symbol,webull_side,schwab_buy,terminal", [
    ("FLYE", None, False, True), ("DKI", None, False, True),
    ("FLYE", "BUY", False, False), ("FLYE", "SELL", False, True),
    ("FLYE", None, True, False),
])
@pytest.mark.asyncio
async def test_both_actual_adapter_paths_for_approved_unbound_replays(sessions, sdk, symbol, webull_side, schwab_buy, terminal):
    from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
    from project_mai_tai.cancel_terminal_proof import UnboundCancelFences, UnboundCancelRequest, evaluate_unbound_cancel_terminal

    client = runtime.Client(pages=[{"hasNext": False, "orders": ([] if webull_side is None else [
        {"order_id": "operator", "items": [{"symbol": symbol, "side": webull_side,
                                               "order_status": "SUBMITTED"}]}])}])
    wb = broker.broker_binding(runtime.adapter(client), "live:orb")[0]
    sc = adapter(200, [order()] if schwab_buy else [], [])
    routed = RoutingBrokerAdapter(default_provider="schwab",
        provider_by_account={"schwab": "schwab", "live:orb": "webull"},
        factories_by_provider={"schwab": lambda: sc, "webull": lambda: wb})
    books = await broker.acquire_request_working_books(routed, ["schwab", "live:orb"])
    request = UnboundCancelRequest(symbol, "token", "token", str(NOW), "retry_exhausted", NOW - 1000,
        {name: broker.broker_binding(routed, name)[1] for name in books},
        {"schwab": "schwab", "live:orb": "webull"})
    assert evaluate_unbound_cancel_terminal(request, books,
        fences=UnboundCancelFences(request, True, True, True, True), now_ms=NOW).terminal is terminal
    assert [kind for kind, _, _ in client.calls] == ["open"]
    with sessions() as session:
        assert session.execute(text("SELECT CAST(:epoch AS bigint)"), {"epoch": NOW}).scalar_one() == NOW


@pytest.mark.asyncio
async def test_failed_date_slice_invalidates_entire_book(sessions, sdk):
    calls = []
    leaf = adapter(200, [], calls)
    get = leaf._authorized_request_json

    async def fail_parent(method, path):
        if len(calls) == 5:
            calls.append((method, path))
            return 599, {}, {"message": "unreadable terminal roots"}
        return await get(method, path)

    leaf._authorized_request_json = fail_parent
    assert await broker.acquire_complete_working_book(leaf, "schwab") is None
    assert len(calls) > 1


@pytest.mark.asyncio
async def test_global_acquisition_timeout_does_not_return_partial_certificate(sessions, sdk, monkeypatch):
    import asyncio

    original_timeout = asyncio.timeout
    monkeypatch.setattr(broker.asyncio, "timeout", lambda seconds: original_timeout(0.001))
    leaf = adapter(200, [], [])
    calls = []

    async def slow(method, path):
        calls.append((method, path))
        await asyncio.sleep(0.05)
        return 200, {}, []

    leaf._authorized_request_json = slow
    assert await broker.acquire_request_working_books(leaf, ["schwab", "unavailable"]) == {
        "schwab": None, "unavailable": None}
    assert len(calls) == 1


@pytest.mark.parametrize("active_child", [False, True])
@pytest.mark.asyncio
async def test_terminal_option_multileg_history_does_not_hide_active_child(sessions, sdk, active_child):
    history = order(status="FILLED", children=[order(oid=2)] if active_child else [])
    history["orderLegCollection"] = [{"instrument": {"assetType": "OPTION"}}, {}]
    book = await broker.acquire_complete_working_book(adapter(200, [history], []), "schwab")
    assert book and book.complete
    assert [row.status for row in book.orders] == (["working"] if active_child else [])


@pytest.mark.parametrize("status,filled", [("FILLED", 1), ("CANCELED", 1), ("CANCELED", None)])
@pytest.mark.asyncio
async def test_exact_bound_target_fill_is_not_omitted_with_terminal_history(sessions, sdk, status, filled):
    target = {**order(status=status), "clientOrderId": "exact", "filledQuantity": filled}
    receipt = CancelReceipt(CancelScope("schwab", "actual-hash", "FLYE", "exact", "event"),
                            NOW - 1000, "rejected", "skipped_before_submit", "")
    evidence = await broker.acquire_broker_cancel_evidence(adapter(200, [target], []), receipt)
    assert not evaluate_cancel_terminal(receipt, evidence, now_ms=NOW).terminal


@pytest.mark.parametrize("when", ["before_read", "during_read"])
@pytest.mark.asyncio
async def test_journal_provider_mismatch_cannot_publish_evidence(sessions, sdk, when):
    from project_mai_tai.db.models import BrokerAccount, TradeIntent
    from project_mai_tai.oms import cancel_terminal as journal

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
    with sessions() as session:
        assert journal.JOURNAL_KEY not in session.get(TradeIntent, intent_id).payload
