from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.settings import Settings


@pytest.mark.asyncio
async def test_schwab_adapter_submits_and_polls_filled_market_order(monkeypatch) -> None:
    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_access_token="token-123",
            schwab_account_hash="hash-123",
        )
    )
    responses = iter(
        [
            (
                201,
                {
                    "Location": "https://api.schwabapi.com/trader/v1/accounts/hash-123/orders/987654321"
                },
                {},
            ),
            (
                200,
                {},
                {
                    "orderId": 987654321,
                    "status": "FILLED",
                    "quantity": "10",
                    "filledQuantity": "10",
                    "enteredTime": "2026-03-28T14:00:00Z",
                    "closeTime": "2026-03-28T14:00:02Z",
                    "orderActivityCollection": [
                        {
                            "executionLegs": [
                                {
                                    "price": "2.55",
                                    "quantity": "10",
                                    "time": "2026-03-28T14:00:02Z",
                                }
                            ]
                        }
                    ],
                },
            ),
        ]
    )

    async def fake_authorized_request_json(method: str, path: str, *, body=None):
        if method == "POST":
            assert path == "/trader/v1/accounts/hash-123/orders"
            assert body == {
                "session": "NORMAL",
                "duration": "DAY",
                "orderType": "MARKET",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [
                    {
                        "instruction": "BUY",
                        "quantity": 10.0,
                        "instrument": {"symbol": "UGRO", "assetType": "EQUITY"},
                    }
                ],
            }
        else:
            assert method == "GET"
            assert path == "/trader/v1/accounts/hash-123/orders/987654321"
        return next(responses)

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(adapter, "_authorized_request_json", fake_authorized_request_json)
    monkeypatch.setattr(adapter, "_sleep", fake_sleep)

    reports = await adapter.submit_order(
        OrderRequest(
            client_order_id="macd_30s-UGRO-open-abc123",
            broker_account_name="paper:macd_30s",
            strategy_code="macd_30s",
            symbol="UGRO",
            side="buy",
            intent_type="open",
            quantity=Decimal("10"),
            reason="ENTRY_P1_MACD_CROSS",
            metadata={},
        )
    )

    assert [report.event_type for report in reports] == ["accepted", "filled"]
    assert reports[-1].broker_order_id == "987654321"
    assert reports[-1].fill_price == Decimal("2.55")
    assert reports[-1].filled_quantity == Decimal("10")


@pytest.mark.asyncio
async def test_schwab_adapter_cancels_order_by_broker_order_id(monkeypatch) -> None:
    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_access_token="token-123",
            schwab_account_hash="hash-123",
        )
    )
    responses = iter(
        [
            (200, {}, {}),
            (
                200,
                {},
                {
                    "orderId": 987654321,
                    "status": "CANCELED",
                    "quantity": "10",
                    "filledQuantity": "0",
                    "enteredTime": "2026-03-28T14:00:00Z",
                    "closeTime": "2026-03-28T14:00:03Z",
                },
            ),
        ]
    )

    async def fake_authorized_request_json(method: str, path: str, *, body=None):
        del body
        if method == "DELETE":
            assert path == "/trader/v1/accounts/hash-123/orders/987654321"
        else:
            assert method == "GET"
            assert path == "/trader/v1/accounts/hash-123/orders/987654321"
        return next(responses)

    monkeypatch.setattr(adapter, "_authorized_request_json", fake_authorized_request_json)

    reports = await adapter.submit_order(
        OrderRequest(
            client_order_id="macd_30s-UGRO-open-abc123",
            broker_account_name="paper:macd_30s",
            strategy_code="macd_30s",
            symbol="UGRO",
            side="buy",
            intent_type="cancel",
            quantity=Decimal("10"),
            reason="USER_CANCEL",
            metadata={"broker_order_id": "987654321"},
        )
    )

    assert len(reports) == 1
    assert reports[0].event_type == "cancelled"
    assert reports[0].broker_order_id == "987654321"


@pytest.mark.asyncio
async def test_schwab_adapter_lists_account_positions(monkeypatch) -> None:
    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_access_token="token-123",
            schwab_account_hash="hash-123",
        )
    )

    async def fake_authorized_request_json(method: str, path: str, *, body=None):
        del body
        assert method == "GET"
        assert path == "/trader/v1/accounts/hash-123?fields=positions"
        return (
            200,
            {},
            {
                "securitiesAccount": {
                    "positions": [
                        {
                            "instrument": {"symbol": "UGRO", "assetType": "EQUITY"},
                            "longQuantity": "10",
                            "shortQuantity": "0",
                            "averagePrice": "2.55",
                            "marketValue": "25.50",
                            "tradeDate": "2026-03-28T14:00:00Z",
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr(adapter, "_authorized_request_json", fake_authorized_request_json)

    positions = await adapter.list_account_positions("paper:macd_30s")

    assert len(positions) == 1
    assert positions[0].symbol == "UGRO"
    assert positions[0].quantity == Decimal("10")
    assert positions[0].average_price == Decimal("2.55")


@pytest.mark.asyncio
async def test_schwab_adapter_refreshes_and_persists_token_store(
    monkeypatch,
    tmp_path: Path,
) -> None:
    token_store_path = tmp_path / "test-schwab-token-store.json"
    token_store_path.write_text(
        json.dumps(
            {
                "refresh_token": "refresh-old",
                "expires_at": "2026-03-28T13:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_client_id="client-id",
            schwab_client_secret="client-secret",
            schwab_token_store_path=str(token_store_path),
            schwab_account_hash="hash-123",
        )
    )

    async def fake_token_request_json(*, form_data):
        assert form_data == {
            "grant_type": "refresh_token",
            "refresh_token": "refresh-old",
        }
        return (
            200,
            {},
            {
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "expires_in": 1800,
                "token_type": "Bearer",
                "scope": "readonly",
            },
        )

    monkeypatch.setattr(adapter, "_token_request_json", fake_token_request_json)

    access_token = await adapter._get_access_token()

    assert access_token == "access-new"
    persisted = json.loads(token_store_path.read_text(encoding="utf-8"))
    assert persisted["access_token"] == "access-new"
    assert persisted["refresh_token"] == "refresh-new"
    assert persisted["token_type"] == "Bearer"
