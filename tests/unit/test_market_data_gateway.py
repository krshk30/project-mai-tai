from __future__ import annotations

import json

import pytest

from project_mai_tai.events import MarketDataSubscriptionEvent, MarketDataSubscriptionPayload
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.market_data.models import HistoricalBarRecord, SnapshotRecord
from project_mai_tai.settings import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object], dict[str, object]]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        self.entries.append((stream, json.loads(fields["data"]), kwargs))
        return "1-0"

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def aclose(self) -> None:
        return None


class FakeSnapshotProvider:
    def fetch_all_snapshots(self):
        return []

    def get_grouped_daily_multi(self, days: int = 20):
        del days
        return {}

    def get_ticker_details_batch(self, tickers, batch_size: int = 10, delay_between_batches: float = 0.2):
        del tickers, batch_size, delay_between_batches
        return {}

    def fetch_historical_bars(
        self,
        symbol: str,
        *,
        interval_secs: int,
        lookback_calendar_days: int,
        limit: int,
    ):
        del lookback_calendar_days, limit
        return [
            HistoricalBarRecord(
                open=2.0,
                high=2.1,
                low=1.9,
                close=2.05 + interval_secs / 10_000,
                volume=10_000,
                timestamp=1_700_000_000.0,
            )
            for _ in range(2)
            if symbol
        ]


class FakeTradeStream:
    def __init__(self) -> None:
        self.synced: list[list[str]] = []

    async def start(self, on_trade, on_quote=None) -> None:
        del on_trade, on_quote
        return None

    async def stop(self) -> None:
        return None

    async def sync_subscriptions(self, symbols) -> None:
        self.synced.append(sorted(symbols))


class FakeReferenceCache:
    def as_payloads(self, symbols) -> list[dict[str, object]]:
        symbol_list = sorted(set(symbols))
        if "UGRO" not in symbol_list:
            return []
        return [
            {
                "symbol": "UGRO",
                "shares_outstanding": 50_000,
                "avg_daily_volume": 390_000,
            }
        ]

    def ticker_count(self) -> int:
        return 1


@pytest.mark.asyncio
async def test_publish_snapshot_batch_once_writes_snapshot_batch_event() -> None:
    redis = FakeRedis()
    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test"),
        redis_client=redis,
        snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(),
        reference_cache=FakeReferenceCache(),
    )

    count = await service.publish_snapshot_batch_once(
        [
            SnapshotRecord(
                symbol="UGRO",
                previous_close=2.10,
                day_close=2.35,
                day_volume=900_000,
                last_trade_price=2.36,
            )
        ]
    )

    assert count == 1
    assert redis.entries[0][0] == "test:snapshot-batches"
    payload = redis.entries[0][1]
    assert payload["event_type"] == "snapshot_batch"
    assert payload["payload"]["snapshots"][0]["symbol"] == "UGRO"
    assert payload["payload"]["snapshots"][0]["previous_close"] == "2.1"
    assert payload["payload"]["reference_data"][0]["shares_outstanding"] == 50000
    assert redis.entries[0][2]["maxlen"] == 12


@pytest.mark.asyncio
async def test_apply_subscription_event_unions_static_and_consumer_symbols() -> None:
    redis = FakeRedis()
    trade_stream = FakeTradeStream()
    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test", market_data_static_symbols="SPY"),
        redis_client=redis,
        snapshot_provider=FakeSnapshotProvider(),
        trade_stream=trade_stream,
        reference_cache=FakeReferenceCache(),
    )

    symbols = await service.apply_subscription_event(
        MarketDataSubscriptionEvent(
            source_service="strategy-engine",
            payload=MarketDataSubscriptionPayload(
                consumer_name="strategy-engine",
                mode="replace",
                symbols=["UGRO", "ANNA"],
            ),
        )
    )

    assert symbols == {"SPY", "UGRO", "ANNA"}
    assert trade_stream.synced[-1] == ["ANNA", "SPY", "UGRO"]
    warmup_events = [
        payload
        for stream, payload, _kwargs in redis.entries
        if stream == "test:market-data" and payload["event_type"] == "historical_bars"
    ]
    assert len(warmup_events) == 4
    assert {event["payload"]["interval_secs"] for event in warmup_events} == {30, 60}


@pytest.mark.asyncio
async def test_apply_subscription_event_replace_replays_warmup_when_symbols_unchanged() -> None:
    redis = FakeRedis()
    trade_stream = FakeTradeStream()
    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test"),
        redis_client=redis,
        snapshot_provider=FakeSnapshotProvider(),
        trade_stream=trade_stream,
        reference_cache=FakeReferenceCache(),
    )
    service._desired_symbols_by_consumer["strategy-engine"] = {"UGRO"}
    service._active_symbols = {"UGRO"}

    symbols = await service.apply_subscription_event(
        MarketDataSubscriptionEvent(
            source_service="strategy-engine",
            payload=MarketDataSubscriptionPayload(
                consumer_name="strategy-engine",
                mode="replace",
                symbols=["UGRO"],
            ),
        )
    )

    assert symbols == {"UGRO"}
    assert trade_stream.synced == []
    warmup_events = [
        payload
        for stream, payload, _kwargs in redis.entries
        if stream == "test:market-data" and payload["event_type"] == "historical_bars"
    ]
    assert len(warmup_events) == 2
    assert {event["payload"]["interval_secs"] for event in warmup_events} == {30, 60}
