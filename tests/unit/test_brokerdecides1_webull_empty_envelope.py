"""One ACTUAL empty open envelope; positions and failures are labeled controls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from project_mai_tai.brokerdecides1 import AdmissionSnapshot, NativeAccountBook, assess
from project_mai_tai.brokerdecides1_collect import webull_pages
from project_mai_tai.brokerdecides1_native import terminal_empty_open_orders
from test_brokerdecides1_native import webull

PACKET = Path(__file__).parents[1] / "fixtures" / "l12_webull_open_empty_20261022.json"
SHA = "c9547b055b88814d95a41d3cfcf31dc18ec061e50f123c064c2873e192e5268f"


def actual():
    raw = PACKET.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA
    packet = json.loads(raw)
    assert packet["read_at_utc"] == "2026-10-22T20:11:31.461470"
    assert len(packet["pages"]) == 1 and packet["pages"][0]["status"] == 200
    return packet["pages"][0]["body"]


def pages(fetch):
    return webull_pages(fetch, account_id="wb123", field="orders", page_size=100,
        cursor_keys=("client_order_id", "clientOrderId"), order_request_account="wb123")


def test_actual_empty_envelope_is_terminal_under_exact_request_binding_once():
    body, calls = actual(), []
    original = deepcopy(body)

    def fetch(cursor):
        calls.append(cursor)
        return 200, body

    assert pages(fetch) == [original] and calls == [""]
    book = webull(order_pages=[body], order_request_account="wb123")
    assert book.complete and not book.working and not book.positions
    assert body == original and "account_id" not in body  # No invented response echo.


@pytest.mark.parametrize("request_account", [None, "foreign", False])
def test_missing_or_foreign_request_binding_is_unknown(request_account):
    with pytest.raises(ValueError, match="request_binding_UNKNOWN"):
        terminal_empty_open_orders(actual(), native_account="wb123", request_account=request_account)


@pytest.mark.parametrize("change", [
    {"hasNext": True}, {"hasNext": "false"}, {"has_next": True},
    {"pageSize": False}, {"pageSize": 1}, {"pageSize": -1}, {"pageSize": "0"},
    {"orders": None}, {"orders": {}}, {"drop": "hasNext"}, {"drop": "pageSize"},
    {"drop": "orders"}, {"error_code": "server_error"}, {"unexpected": "unproved"},
])
def test_changed_empty_envelope_never_becomes_complete(change):
    body = actual()
    if "drop" in change:
        body.pop(change["drop"])
    else:
        body.update(change)
    with pytest.raises(ValueError, match="UNKNOWN"):
        pages(lambda cursor: (200, body))


@pytest.mark.parametrize("rows", [[{}], [{"status": "FILLED"}]])
def test_nonempty_model_stays_unknown_even_when_status_looks_terminal(rows):
    body = actual()
    body["orders"] = rows
    with pytest.raises(ValueError, match="unlocated_symbol_UNKNOWN"):
        pages(lambda cursor: (200, body))


def test_readable_nonempty_model_holds_symbol_instead_of_proving_empty():
    body = actual()
    body["orders"] = [{"order_id": "private-manual", "client_order_id": "private-cursor",
        "status": "WORKING", "items": [{"symbol": "STAK"}]}]
    observed = pages(lambda cursor: (200, body))
    assert observed == [body]  # Request binding proves collection, not row semantics.
    book = webull(order_pages=observed, order_request_account="wb123")
    assert book.complete and book.reason == "Webull_symbol_scoped_UNKNOWN"
    assert book.working == frozenset({"STAK"})
    primary = NativeAccountBook("live:schwab", "schwab", 1000, 1100,
        frozenset(), frozenset(), True, "CONTROLLED-flat")
    snapshot = AdmissionSnapshot("CONTROLLED", 1, 1000, 1200, (primary, book),
        frozenset(), frozenset(), True, "CONTROLLED-current-book")
    decision = assess(snapshot, accounts=("live:schwab", "live:webull"), symbol="STAK",
        segment=900, now_ms=1300, max_age_ms=20_000)
    assert not decision.allowed and decision.reason == "native_working_order"


@pytest.mark.parametrize("status", [None, 503, True])
def test_observed_body_does_not_waive_http_proof(status):
    with pytest.raises(ValueError, match="http_UNKNOWN"):
        pages(lambda cursor: (status, actual()))


def test_duplicate_terminal_pages_are_not_a_proven_empty_read():
    with pytest.raises(ValueError, match="continuation_UNKNOWN"):
        webull(order_pages=[actual(), actual()], order_request_account="wb123")


def test_legacy_empty_echo_without_fresh_request_binding_is_not_complete():
    book = webull(order_pages=[{"account_id": "wb123", "has_next": False, "orders": []}],
                  order_request_account=None)
    assert not book.complete and book.reason == "Webull_open_request_binding_UNKNOWN"


def test_empty_orders_cannot_clear_a_manual_position_control():
    # Controlled position shape/quantity, NOT a retained native positions response.
    # Operator identified STAK 2000 as a POSITION, never an open-order item.
    book = webull(order_pages=[actual()], order_request_account="wb123", position_pages=[
        {"account_id": "wb123", "has_next": False,
         "holdings": [{"symbol": "STAK", "quantity": 2000}]}])
    assert not book.working and book.positions == frozenset({"STAK"})
    primary = NativeAccountBook("live:schwab", "schwab", 1000, 1100,
        frozenset(), frozenset(), True, "private controlled book")
    snapshot = AdmissionSnapshot("private", 1, 1000, 1200, (primary, book),
        frozenset(), frozenset(), True, "private controlled positions")
    decision = assess(snapshot, accounts=("live:schwab", "live:webull"), symbol="STAK",
        segment=900, now_ms=1300, max_age_ms=20_000)
    assert not decision.allowed and decision.reason == "native_position"
