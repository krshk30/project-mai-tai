from __future__ import annotations

from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.alpaca import AlpacaPaperBrokerAdapter
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.settings import Settings


@pytest.mark.asyncio
async def test_alpaca_adapter_submits_and_polls_filled_market_order(monkeypatch) -> None:
    settings = Settings(
        oms_adapter="alpaca_paper",
        alpaca_macd_30s_api_key="key-30s",
        alpaca_macd_30s_secret_key="secret-30s",
    )
    adapter = AlpacaPaperBrokerAdapter(settings)
    responses = iter(
        [
            (
                200,
                {
                    "id": "ord-123",
                    "client_order_id": "macd_30s-UGRO-open-abc123",
                    "status": "accepted",
                    "symbol": "UGRO",
                    "side": "buy",
                    "qty": "10",
                    "filled_qty": "0",
                    "updated_at": "2026-03-28T14:00:00Z",
                },
            ),
            (
                200,
                {
                    "id": "ord-123",
                    "client_order_id": "macd_30s-UGRO-open-abc123",
                    "status": "filled",
                    "symbol": "UGRO",
                    "side": "buy",
                    "qty": "10",
                    "filled_qty": "10",
                    "filled_avg_price": "2.55",
                    "updated_at": "2026-03-28T14:00:01Z",
                },
            ),
        ]
    )

    async def fake_request_json(credentials, method, path, body=None):
        assert credentials.api_key == "key-30s"
        if method == "POST":
            assert path == "/v2/orders"
            assert body["client_order_id"] == "macd_30s-UGRO-open-abc123"
            assert body["symbol"] == "UGRO"
        return next(responses)

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(adapter, "_request_json", fake_request_json)
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
            metadata={"reference_price": "2.55"},
        )
    )

    assert [report.event_type for report in reports] == ["accepted", "filled"]
    assert reports[-1].fill_price == Decimal("2.55")
    assert reports[-1].filled_quantity == Decimal("10")


@pytest.mark.asyncio
async def test_alpaca_adapter_rejects_when_account_credentials_are_missing() -> None:
    adapter = AlpacaPaperBrokerAdapter(Settings(oms_adapter="alpaca_paper"))

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
            metadata={"reference_price": "2.55"},
        )
    )

    assert len(reports) == 1
    assert reports[0].event_type == "rejected"
    assert "missing Alpaca credentials" in reports[0].reason


@pytest.mark.asyncio
async def test_alpaca_adapter_cancels_order_by_client_order_id_lookup(monkeypatch) -> None:
    settings = Settings(
        oms_adapter="alpaca_paper",
        alpaca_macd_30s_api_key="key-30s",
        alpaca_macd_30s_secret_key="secret-30s",
    )
    adapter = AlpacaPaperBrokerAdapter(settings)
    responses = iter(
        [
            (
                200,
                {
                    "id": "ord-123",
                    "client_order_id": "macd_30s-UGRO-open-abc123",
                    "status": "accepted",
                    "symbol": "UGRO",
                    "side": "buy",
                    "qty": "10",
                    "filled_qty": "0",
                    "updated_at": "2026-03-28T14:00:00Z",
                },
            ),
            (204, {}),
            (
                200,
                {
                    "id": "ord-123",
                    "client_order_id": "macd_30s-UGRO-open-abc123",
                    "status": "canceled",
                    "symbol": "UGRO",
                    "side": "buy",
                    "qty": "10",
                    "filled_qty": "0",
                    "updated_at": "2026-03-28T14:00:02Z",
                },
            ),
        ]
    )

    async def fake_request_json(credentials, method, path, body=None):
        assert credentials.api_key == "key-30s"
        assert body is None
        if method == "GET" and path.startswith("/v2/orders:by_client_order_id"):
            assert "client_order_id=macd_30s-UGRO-open-abc123" in path
            return next(responses)
        if method == "DELETE":
            assert path == "/v2/orders/ord-123"
            return next(responses)
        assert method == "GET"
        assert path == "/v2/orders/ord-123"
        return next(responses)

    monkeypatch.setattr(adapter, "_request_json", fake_request_json)

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
            metadata={},
        )
    )

    assert len(reports) == 1
    assert reports[0].event_type == "cancelled"
    assert reports[0].broker_order_id == "ord-123"
    assert reports[0].client_order_id == "macd_30s-UGRO-open-abc123"


@pytest.mark.asyncio
async def test_alpaca_adapter_lists_account_positions(monkeypatch) -> None:
    settings = Settings(
        oms_adapter="alpaca_paper",
        alpaca_macd_30s_api_key="key-30s",
        alpaca_macd_30s_secret_key="secret-30s",
    )
    adapter = AlpacaPaperBrokerAdapter(settings)

    async def fake_request_json(credentials, method, path, body=None):
        del credentials, body
        assert method == "GET"
        assert path == "/v2/positions"
        return (
            200,
            [
                {
                    "symbol": "UGRO",
                    "qty": "10",
                    "avg_entry_price": "2.55",
                    "market_value": "25.50",
                    "updated_at": "2026-03-28T14:00:00Z",
                },
                {
                    "symbol": "SBET",
                    "qty": "0",
                    "avg_entry_price": "3.10",
                    "market_value": "0",
                    "updated_at": "2026-03-28T14:00:00Z",
                },
            ],
        )

    monkeypatch.setattr(adapter, "_request_json", fake_request_json)

    positions = await adapter.list_account_positions("paper:macd_30s")

    assert len(positions) == 1
    assert positions[0].symbol == "UGRO"
    assert positions[0].quantity == Decimal("10")
    assert positions[0].average_price == Decimal("2.55")


@pytest.mark.asyncio
async def test_alpaca_adapter_marks_timeout_cancelled_when_cancel_reflection_lags(monkeypatch) -> None:
    settings = Settings(
        oms_adapter="alpaca_paper",
        alpaca_macd_1m_api_key="key-1m",
        alpaca_macd_1m_secret_key="secret-1m",
        alpaca_cancel_confirm_timeout_seconds=0,
    )
    adapter = AlpacaPaperBrokerAdapter(settings)
    credentials = adapter.credentials_by_account["paper:macd_1m"]

    async def fake_request_json(credentials, method, path, body=None):
        assert credentials.api_key == "key-1m"
        assert method == "GET"
        assert path == "/v2/orders/ord-123"
        assert body is None
        return (
            200,
            {
                "id": "ord-123",
                "client_order_id": "macd_1m-BFRG-open-abc123",
                "status": "accepted",
                "symbol": "BFRG",
                "side": "buy",
                "qty": "100",
                "filled_qty": "0",
                "updated_at": "2026-03-30T13:35:10Z",
            },
        )

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(adapter, "_request_json", fake_request_json)
    monkeypatch.setattr(adapter, "_sleep", fake_sleep)

    order = await adapter._wait_for_cancel_confirmation(
        credentials=credentials,
        request=OrderRequest(
            client_order_id="macd_1m-BFRG-open-abc123",
            broker_account_name="paper:macd_1m",
            strategy_code="macd_1m",
            symbol="BFRG",
            side="buy",
            intent_type="open",
            quantity=Decimal("100"),
            reason="ENTRY_P3_MACD_SURGE",
            metadata={"reference_price": "1.15"},
        ),
        order_id="ord-123",
    )

    assert order is not None
    assert order["status"] == "canceled"
    assert order["id"] == "ord-123"


def test_alpaca_paper_adapter_uses_paper_timeout_defaults() -> None:
    settings = Settings(
        oms_adapter="alpaca_paper",
        alpaca_macd_30s_api_key="key-30s",
        alpaca_macd_30s_secret_key="secret-30s",
    )

    adapter = AlpacaPaperBrokerAdapter(settings)

    assert adapter.fill_timeout_seconds == 10
    assert adapter.cancel_unfilled_after_timeout is True


def test_alpaca_paper_adapter_reuses_30s_credentials_for_30s_variants() -> None:
    settings = Settings(
        oms_adapter="alpaca_paper",
        alpaca_macd_30s_api_key="key-30s",
        alpaca_macd_30s_secret_key="secret-30s",
    )

    adapter = AlpacaPaperBrokerAdapter(settings)

    assert adapter.credentials_by_account["paper:macd_30s"].api_key == "key-30s"
    assert adapter.credentials_by_account["paper:macd_30s_probe"].api_key == "key-30s"
    assert adapter.credentials_by_account["paper:macd_30s_reclaim"].api_key == "key-30s"
    assert adapter.credentials_by_account["paper:macd_30s_retest"].api_key == "key-30s"
