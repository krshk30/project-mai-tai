"""Recorded zero/full responses; partial/error variants are labelled synthetic."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters.atr_buy_readback import (
    schwab_buy_readback,
    webull_buy_readback,
)
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from tests.unit.test_webull_adapter import fake_sdk  # noqa: F401


FIXTURES = Path(__file__).parents[1] / "fixtures"


def recorded(broker, *, filled=False, amod_1356=False):
    if broker == "schwab":
        if amod_1356:
            record = json.loads((FIXTURES / "rpg1_amod_1356_recorded.json").read_text())["schwab"]
            body = record["body"]
            request = OrderRequest(
                client_order_id=record["client_order_id"], broker_account_name=record["broker_account_name"],
                strategy_code="schwab_1m_v2", symbol="AMOD", side="buy", intent_type="cancel",
                quantity=Decimal(str(body["quantity"])), reason="reprice", order_type="stop_limit",
                metadata={**record["original_metadata"], "broker_order_id": str(body["orderId"]),
                          "resting_entry_cancel": "true"})
            return request, body, schwab_buy_readback
        body = json.loads((FIXTURES / "rpg1_schwab_cancelled_buy.json").read_text())["body"]
        request = OrderRequest(
            client_order_id="recorded-amod-parent", broker_account_name="live:schwab_1m_v2",
            strategy_code="schwab_1m_v2", symbol="AMOD", side="buy", intent_type="cancel",
            quantity=Decimal(str(body["quantity"])), reason="reprice", order_type="stop_limit",
            metadata={"broker_order_id": str(body["orderId"]), "resting_entry_cancel": "true"},
        )
        return request, body, schwab_buy_readback
    record = json.loads((FIXTURES / "rpg1_webull_buy_readbacks.json").read_text())["records"][int(filled)]
    row, body = record["db_order"], record["body"]
    request = OrderRequest(
        client_order_id=row["client_order_id"], broker_account_name="live:orb",
        strategy_code="schwab_1m_v2", symbol=row["symbol"], side="buy", intent_type="cancel",
        quantity=Decimal(row["quantity"]), reason="reprice", order_type="stop_limit",
        metadata={"broker_order_id": row["broker_order_id"], "resting_entry_cancel": "true"},
    )
    return request, body, webull_buy_readback


@pytest.mark.parametrize("broker", ["schwab", "webull"])
def test_recorded_terminal_cancel_explicit_zero_allows_replacement(broker):
    request, body, decode = recorded(broker)
    result = decode(request, body)
    assert result.outcome == "cancelled_empty"
    assert result.cumulative_filled == Decimal(0)
    assert result.can_replace
    assert result.terminal_cancel


def test_schwab_canceled_activity_is_not_a_fill_and_generic_decoder_is_not_used(monkeypatch):
    request, body, decode = recorded("schwab")
    assert body["filledQuantity"] == 0.0
    activity = body["orderActivityCollection"][0]
    assert activity["executionType"] == "CANCELED"
    assert activity["executionLegs"][0]["quantity"] == 2
    monkeypatch.setattr(SchwabBrokerAdapter, "_extract_filled_quantity", lambda *a: pytest.fail("shared decoder"))
    assert decode(request, body).cumulative_filled == 0


def test_recorded_webull_filled_buy_never_replaces():
    request, body, decode = recorded("webull", filled=True)
    result = decode(request, body)
    assert result.outcome == "fills"
    assert result.cumulative_filled == 1
    assert result.fill_price == Decimal("3.41")
    assert not result.can_replace


def test_exact_amod_1356_recorded_parent_not_the_earlier_1350_cancel():
    request, body, decode = recorded("schwab", amod_1356=True)
    assert request.client_order_id == "schwab_1m_v2-AMOD-open-0fa77151f873"
    assert body["orderId"] == 1008159036751
    assert body["closeTime"] == "2026-10-02T17:56:03+0000"
    assert decode(request, body).can_replace


@pytest.mark.parametrize("change", [None, "client", "missing_broker", "side", "quantity", "fills"])
def test_webull_explicit_client_identity_scope_still_requires_exact_complete_parent(change):
    request, body, decode = recorded("webull")
    request = replace(request, metadata={"resting_entry_cancel": "true",
        "atr_reprice_identity": "webull_client_order_id"})
    if change == "client":
        body["client_order_id"] = "different-order"
    elif change == "missing_broker":
        body.pop("order_id")
    elif change == "side":
        body["items"][0]["side"] = "SELL"
    elif change == "quantity":
        body["items"][0]["qty"] = "1000"
    elif change == "fills":
        body["items"][0].pop("filled_qty")
    result = decode(request, body)
    assert result.can_replace is (change is None)
    if change is None:
        assert result.broker_order_id == body["order_id"]


def test_webull_client_identity_scope_does_not_relax_schwab_identity():
    request, body, decode = recorded("schwab")
    request = replace(request, metadata={"resting_entry_cancel": "true",
        "atr_reprice_identity": "webull_client_order_id"})
    assert decode(request, body).outcome == "unknown"


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", "-1", "bad", True])
def test_synthetic_missing_or_invalid_cumulative_quantity_is_unknown(broker, value):
    request, body, decode = recorded(broker)
    parent = body if broker == "schwab" else body["items"][0]
    parent["filledQuantity" if broker == "schwab" else "filled_qty"] = value
    result = decode(request, body)
    assert result.outcome == "unknown"
    assert not result.can_replace


@pytest.mark.parametrize("broker", ["schwab", "webull"])
def test_synthetic_absent_filled_field_never_defaults_to_zero(broker):
    request, body, decode = recorded(broker)
    parent = body if broker == "schwab" else body["items"][0]
    del parent["filledQuantity" if broker == "schwab" else "filled_qty"]
    assert decode(request, body).outcome == "unknown"


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("body", [None, {}, [], {"message": "accepted"}, {"code": "ORDER_NOT_FOUND"}])
def test_synthetic_absence_ack_and_not_found_are_unknown_not_cancelled(broker, body):
    request, _, decode = recorded(broker)
    assert decode(request, body).outcome == "unknown"
    assert not decode(request, body).can_replace


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("status", ["WORKING", "PENDING_CANCEL", "ACCEPTED", "FILLED", "NEW_UNKNOWN"])
def test_synthetic_non_cancel_status_with_zero_never_releases(broker, status):
    request, body, decode = recorded(broker)
    parent = body if broker == "schwab" else body["items"][0]
    parent["status" if broker == "schwab" else "order_status"] = status
    assert not decode(request, body).can_replace


@pytest.mark.parametrize("broker,quantity,filled", [("schwab", 197, 31), ("webull", 98, 17)])
@pytest.mark.parametrize("terminal", [False, True])
def test_synthetic_dollar_size_partial_is_position_not_rebuy_permission(broker, quantity, filled, terminal):
    request, body, decode = recorded(broker)
    request = replace(request, quantity=Decimal(quantity))
    if broker == "schwab":
        body.update(quantity=quantity, filledQuantity=filled, status="CANCELED" if terminal else "WORKING")
        body["orderLegCollection"][0]["quantity"] = quantity
        body["orderActivityCollection"] = [{
            "activityType": "EXECUTION", "executionType": "FILL",
            "executionLegs": [{"quantity": filled, "price": 3.05}],
        }]
    else:
        body["items"][0].update(qty=str(quantity), filled_qty=str(filled), filled_price="3.05",
                                order_status="CANCELLED" if terminal else "PARTIAL_FILLED")
    result = decode(request, body)
    assert result.outcome == "fills"
    assert result.cumulative_filled == filled
    assert result.fill_price == Decimal("3.05")
    assert result.terminal_cancel is terminal
    assert not result.can_replace  # Neither the original full amount nor the remainder is re-bought.


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("change", ["order_id", "symbol", "side", "quantity", "missing_id"])
def test_synthetic_wrong_or_unbound_order_never_releases(broker, change):
    request, body, decode = recorded(broker)
    if change == "missing_id":
        request = replace(request, metadata={"resting_entry_cancel": "true"})
    elif change == "order_id":
        body["orderId" if broker == "schwab" else "order_id"] = "another-order"
    elif change == "quantity":
        parent = body if broker == "schwab" else body["items"][0]
        parent["quantity" if broker == "schwab" else "qty"] = 1000
    elif broker == "schwab":
        leg = body["orderLegCollection"][0]
        if change == "symbol":
            leg["instrument"]["symbol"] = "ANOTHER"
        else:
            leg["instruction"] = "SELL"
    else:
        body["items"][0][change] = "ANOTHER" if change == "symbol" else "SELL"
    assert decode(request, body).outcome == "unknown"


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("override", [{"side": "sell"}, {"strategy_code": "orb_schwab"}, {"intent_type": "open"}])
def test_scoped_to_atr_buy_cancel(broker, override):
    request, body, decode = recorded(broker)
    assert decode(replace(request, **override), body).outcome == "unknown"


def test_synthetic_webull_multiple_items_not_collapsed_to_first():
    request, body, decode = recorded("webull")
    body["items"].append(copy.deepcopy(body["items"][0]))
    assert decode(request, body).outcome == "unknown"


def test_synthetic_schwab_positive_execution_contradicts_zero_and_blocks():
    request, body, decode = recorded("schwab")
    body["orderActivityCollection"][0]["executionType"] = "FILL"
    body["orderActivityCollection"][0]["executionLegs"][0]["price"] = 3.3
    assert decode(request, body).outcome == "unknown"


@pytest.mark.asyncio
async def test_schwab_dedicated_read_gets_exact_parent_and_ignores_shared_decoder(monkeypatch):
    request, body, _ = recorded("schwab")
    adapter = object.__new__(SchwabBrokerAdapter)
    adapter.accounts_by_name = {request.broker_account_name: SchwabAccountConfig("configured-hash")}
    calls = []

    async def get(method, path, **kwargs):
        calls.append((method, path))
        return 200, {}, body

    monkeypatch.setattr(adapter, "_authorized_request_json", get)
    monkeypatch.setattr(adapter, "_execution_report_from_order", lambda **kw: pytest.fail("shared decoder"))
    result = await adapter.read_atr_resting_buy_after_cancel(request)
    assert result.can_replace
    assert calls == [("GET", "/trader/v1/accounts/configured-hash/orders/1008159036414")]


@pytest.mark.asyncio
async def test_webull_dedicated_read_no_exit_helper_no_today_cache(monkeypatch):
    request, body, _ = recorded("webull")
    adapter = object.__new__(WebullBrokerAdapter)
    adapter.accounts_by_name = {request.broker_account_name: WebullAccountConfig("configured-id")}
    calls = []

    def read(account, client_order_id):
        calls.append((account.account_id, client_order_id))
        return 200, body

    monkeypatch.setattr(adapter, "_atr_buy_order_detail", read)
    monkeypatch.setattr(adapter, "_confirm_cancel_blocking", lambda *a: pytest.fail("exit helper"))
    monkeypatch.setattr(adapter, "_order_detail_body_with_fallback", lambda *a: pytest.fail("cached fallback"))
    result = await adapter.read_atr_resting_buy_after_cancel(request)
    assert result.can_replace
    assert calls == [("configured-id", request.client_order_id)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_sdk")
@pytest.mark.parametrize("client_only", [False, True])
async def test_webull_recorded_body_through_sdk_exact_order_get(monkeypatch, client_only):
    request, body, _ = recorded("webull")
    if client_only:
        request = replace(request, metadata={"resting_entry_cancel": "true",
            "atr_reprice_identity": "webull_client_order_id"})
    adapter = object.__new__(WebullBrokerAdapter)
    adapter.accounts_by_name = {request.broker_account_name: WebullAccountConfig("configured-id")}
    calls = []

    class Client:
        def get_response(self, detail):
            calls.append(detail)
            return body

    monkeypatch.setattr(adapter, "_get_client", lambda: Client())
    monkeypatch.setattr(adapter, "_response_status", lambda response: 200)
    monkeypatch.setattr(adapter, "_body", lambda response: response)
    result = await adapter.read_atr_resting_buy_after_cancel(request)
    assert result.can_replace
    assert len(calls) == 1
    assert calls[0]._kind == "detail"
    assert calls[0].values["account_id"] == "configured-id"
    assert calls[0].values["client_order_id"] == request.client_order_id


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("failure", ["raise", "404", "429", "account"])
async def test_dedicated_read_fails_closed_without_sending_cancel_or_buy(broker, failure, monkeypatch):
    request, body, _ = recorded(broker)
    cls = SchwabBrokerAdapter if broker == "schwab" else WebullBrokerAdapter
    adapter = object.__new__(cls)
    config = SchwabAccountConfig("hash") if broker == "schwab" else WebullAccountConfig("id")
    adapter.accounts_by_name = {} if failure == "account" else {request.broker_account_name: config}
    monkeypatch.setattr(adapter, "submit_order", lambda *a: pytest.fail("write"))

    def read(*args, **kwargs):
        if failure == "raise":
            raise RuntimeError("transport failed")
        status = int(failure) if failure != "account" else 200
        return (status, {}, body) if broker == "schwab" else (status, body)

    async def async_read(*args, **kwargs):
        return read(*args, **kwargs)

    monkeypatch.setattr(adapter, "_authorized_request_json" if broker == "schwab" else "_atr_buy_order_detail",
                        async_read if broker == "schwab" else read)
    result = await adapter.read_atr_resting_buy_after_cancel(request)
    assert result.outcome == "unknown"
    assert not result.can_replace
