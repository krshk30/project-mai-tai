"""ORB-owned raw Webull read controls: exact coid, explicit fills, no master fallback."""

from copy import deepcopy
from decimal import Decimal

import pytest

from tests.unit.test_webull_adapter import _FakeClient, _ServerException, _adapter, _order
from tests.unit.test_webull_adapter import fake_sdk as fake_sdk


def receipt():
    return {"client_order_id": "orb-child-T", "order_id": "child-native", "account_id": "ACC1",
        "items": [{"symbol": "AAPL", "side": "SELL", "quantity": "10", "order_status": "CANCELLED",
                   "filled_qty": "0"}]}


def request():
    return _order(client_order_id="orb-child-T", strategy_code="orb_schwab", side="sell",
        intent_type="close", quantity=Decimal(10), metadata={"orbroute1_child": "true",
        "orbroute1_fresh_read": "true"})


def test_fresh_raw_terminal_zero_child_keeps_exact_native_identity(fake_sdk):
    client = _FakeClient({"detail": receipt()})
    adapter = _adapter(client)
    report = adapter._fetch_order_blocking(adapter.accounts_by_name["live:orb"], request())
    assert report.event_type == "cancelled"
    assert report.filled_quantity == 0
    assert report.client_order_id == "orb-child-T"
    assert report.broker_order_id == "child-native"
    assert report.origin == "broker"
    assert client.calls == {"detail": 1}


@pytest.mark.parametrize("mutation", ["coid", "account", "symbol", "side", "quantity", "missing_fill",
                                    "nan_fill", "unknown_status", "missing_native", "duplicate_items"])
def test_bad_raw_terminal_receipt_stays_unknown(fake_sdk, mutation):
    body = deepcopy(receipt())
    if mutation == "coid":
        body["client_order_id"] = "another-owner"
    elif mutation == "account":
        body["account_id"] = "another-account"
    elif mutation == "symbol":
        body["items"][0]["symbol"] = "MSFT"
    elif mutation == "side":
        body["items"][0]["side"] = "BUY"
    elif mutation == "quantity":
        body["items"][0]["quantity"] = "11"
    elif mutation == "missing_fill":
        body["items"][0].pop("filled_qty")
    elif mutation == "nan_fill":
        body["items"][0]["filled_qty"] = "NaN"
    elif mutation == "unknown_status":
        body["items"][0]["order_status"] = "PENDING_MYSTERY"
    elif mutation == "missing_native":
        body.pop("order_id")
    elif mutation == "duplicate_items":
        body["items"].append(deepcopy(body["items"][0]))
    client = _FakeClient({"detail": body})
    adapter = _adapter(client)
    assert adapter._fetch_order_blocking(adapter.accounts_by_name["live:orb"], request()) is None
    assert client.calls == {"detail": 1}


def test_unknown_child_never_looks_up_an_unrelated_master(fake_sdk):
    client = _FakeClient({})
    client.raises["detail"] = _ServerException("ORDER_NOT_FOUND", "not found", 417)
    adapter = _adapter(client)
    import asyncio
    assert asyncio.run(adapter.fetch_order_update(request())) is None
    assert client.calls == {"detail": 1}
