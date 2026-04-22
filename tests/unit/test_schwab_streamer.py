from __future__ import annotations

import asyncio

import pytest

from project_mai_tai.market_data.schwab_streamer import (
    SchwabStreamerClient,
    SchwabStreamerCredentials,
)
from project_mai_tai.settings import Settings


class FakeAuthAdapter:
    def __init__(self, *, payload: object, access_token: str = "token-123") -> None:
        self.payload = payload
        self.access_token = access_token

    async def _authorized_request_json(self, method: str, path: str, *, body=None):
        del body
        assert method == "GET"
        assert path == "/trader/v1/userPreference"
        return 200, {}, self.payload

    async def _get_access_token(self) -> str:
        return self.access_token


class FakeWebSocket:
    def __init__(self, messages: list[object]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(3600)

    async def close(self) -> None:
        self.closed = True


def test_schwab_streamer_extracts_quote_and_trade_records() -> None:
    content = {
        "key": "MYSE",
        "1": "2.11",
        "2": "2.12",
        "3": "2.115",
        "4": "300",
        "5": "400",
        "8": "125000",
        "9": "500",
        "35": "1713361800123",
    }

    quote = SchwabStreamerClient._extract_quote_record(content)
    trade = SchwabStreamerClient._extract_trade_record(content)

    assert quote is not None
    assert quote.symbol == "MYSE"
    assert quote.bid_price == 2.11
    assert quote.ask_price == 2.12
    assert quote.bid_size == 300
    assert quote.ask_size == 400

    assert trade is not None
    assert trade.symbol == "MYSE"
    assert trade.price == 2.115
    assert trade.size == 500
    assert trade.cumulative_volume == 125000
    assert trade.timestamp_ns == 1713361800123 * 1_000_000


def test_schwab_streamer_uses_field_9_as_last_size_fallback() -> None:
    content = {
        "key": "UGRO",
        "3": "2.55",
        "8": "250000",
        "9": "700",
        "35": "1713361800456",
    }

    trade = SchwabStreamerClient._extract_trade_record(content)

    assert trade is not None
    assert trade.size == 700
    assert trade.timestamp_ns == 1713361800456 * 1_000_000


def test_schwab_streamer_ignores_field_38_for_last_size() -> None:
    content = {
        "key": "FRMM",
        "3": "3.98",
        "8": "40268162",
        "9": "1",
        "35": "1776466812502",
        "38": "1776466812336",
    }

    trade = SchwabStreamerClient._extract_trade_record(content)

    assert trade is not None
    assert trade.size == 1
    assert trade.timestamp_ns == 1776466812502 * 1_000_000


@pytest.mark.asyncio
async def test_schwab_streamer_fetches_credentials_from_list_payload() -> None:
    client = SchwabStreamerClient(
        Settings(),
        auth_adapter=FakeAuthAdapter(
            payload=[
                {
                    "streamerInfo": [
                        {
                            "streamerSocketUrl": "streamer.example/ws",
                            "schwabClientCustomerId": "cust-1",
                            "schwabClientCorrelId": "corr-1",
                            "schwabClientChannel": "chan-1",
                            "schwabClientFunctionId": "func-1",
                        }
                    ]
                }
            ]
        ),
    )

    credentials = await client._fetch_streamer_credentials()

    assert credentials == SchwabStreamerCredentials(
        socket_url="streamer.example/ws",
        customer_id="cust-1",
        correl_id="corr-1",
        channel="chan-1",
        function_id="func-1",
    )


@pytest.mark.asyncio
async def test_schwab_streamer_emits_callbacks_from_levelone_payload() -> None:
    trades = []
    quotes = []
    client = SchwabStreamerClient(Settings(), auth_adapter=FakeAuthAdapter(payload={}))
    client._on_trade = trades.append
    client._on_quote = quotes.append

    await client._handle_message(
        {
            "data": [
                {
                    "service": "LEVELONE_EQUITIES",
                    "content": [
                        {
                            "key": "ELAB",
                            "1": "3.11",
                            "2": "3.12",
                            "3": "3.115",
                            "4": "100",
                            "5": "200",
                            "8": "500000",
                            "9": "150",
                            "35": "1713361800789",
                        }
                    ],
                }
            ]
        }
    )

    assert len(quotes) == 1
    assert quotes[0].symbol == "ELAB"
    assert quotes[0].bid_size == 100
    assert quotes[0].ask_size == 200

    assert len(trades) == 1
    assert trades[0].symbol == "ELAB"
    assert trades[0].size == 150
    assert trades[0].cumulative_volume == 500000


def test_schwab_streamer_builds_login_and_subscribe_requests() -> None:
    client = SchwabStreamerClient(Settings(), auth_adapter=FakeAuthAdapter(payload={}))
    credentials = SchwabStreamerCredentials(
        socket_url="streamer.example/ws",
        customer_id="cust-1",
        correl_id="corr-1",
        channel="chan-1",
        function_id="func-1",
    )

    login_request = client._build_login_request(credentials=credentials, access_token="token-123")
    subscribe_request = client._build_subscription_request(
        credentials=credentials,
        command="ADD",
        symbols=["ELAB", "UGRO"],
    )

    assert login_request["requests"][0]["service"] == "ADMIN"
    assert login_request["requests"][0]["parameters"]["Authorization"] == "token-123"
    assert subscribe_request["requests"][0]["service"] == "LEVELONE_EQUITIES"
    assert subscribe_request["requests"][0]["parameters"]["keys"] == "ELAB,UGRO"
    assert subscribe_request["requests"][0]["parameters"]["fields"] == SchwabStreamerClient.LEVELONE_EQUITIES_FIELDS


@pytest.mark.asyncio
async def test_schwab_streamer_probe_collects_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    websocket = FakeWebSocket(
        messages=[
            {
                "response": [
                    {
                        "service": "ADMIN",
                        "command": "LOGIN",
                        "content": {"code": "0", "msg": "OK"},
                    }
                ]
            },
            {
                "data": [
                    {
                        "service": "LEVELONE_EQUITIES",
                        "content": [
                            {
                                "key": "ELAB",
                                "1": "3.11",
                                "2": "3.12",
                                "3": "3.115",
                                "8": "500000",
                                "9": "150",
                                "35": "1713361800789",
                            }
                        ],
                    }
                ]
            },
        ]
    )

    async def fake_connect(*args, **kwargs):
        del args, kwargs
        return websocket

    monkeypatch.setattr("project_mai_tai.market_data.schwab_streamer.websockets.connect", fake_connect)

    client = SchwabStreamerClient(
        Settings(),
        auth_adapter=FakeAuthAdapter(
            payload={
                "streamerInfo": {
                    "streamerSocketUrl": "streamer.example/ws",
                    "schwabClientCustomerId": "cust-1",
                    "schwabClientCorrelId": "corr-1",
                    "schwabClientChannel": "chan-1",
                    "schwabClientFunctionId": "func-1",
                }
            }
        ),
    )

    result = await client.probe(symbols=["ELAB"], duration_seconds=0.2, sample_limit=2)

    assert result.ok is True
    assert result.login_succeeded is True
    assert result.raw_messages_seen >= 1
    assert result.quote_count == 1
    assert result.trade_count == 1
    assert result.sampled_quotes[0]["symbol"] == "ELAB"
    assert result.sampled_trades[0]["symbol"] == "ELAB"
    assert websocket.closed is True
