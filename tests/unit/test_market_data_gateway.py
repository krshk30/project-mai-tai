from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from project_mai_tai.events import MarketDataSubscriptionEvent, MarketDataSubscriptionPayload
from project_mai_tai.market_data.massive_provider import MassiveTradeStream
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.market_data.models import HistoricalBarRecord, LiveBarRecord, SnapshotRecord, TradeTickRecord
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

    async def xrevrange(self, stream: str, count: int = 1):
        results = []
        for index, (saved_stream, payload, _kwargs) in enumerate(reversed(self.entries), start=1):
            if saved_stream != stream:
                continue
            results.append((f"{index}-0", {"data": json.dumps(payload)}))
            if len(results) >= count:
                break
        return results

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

    async def start(self, on_trade, on_quote=None, on_agg=None) -> None:
        del on_trade, on_quote, on_agg
        return None

    async def stop(self) -> None:
        return None

    async def sync_subscriptions(self, symbols) -> None:
        self.synced.append(sorted(symbols))


class FakeReferenceCache:
    def load_from_cache(self) -> bool:
        return True

    def build(self) -> None:
        return None

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
    assert redis.entries[0][2]["maxlen"] == 180


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
async def test_apply_subscription_event_replace_does_not_replay_warmup_when_symbols_unchanged() -> None:
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
    assert warmup_events == []


@pytest.mark.asyncio
async def test_apply_subscription_event_replace_only_warms_new_symbols() -> None:
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
                symbols=["UGRO", "ANNA"],
            ),
        )
    )

    assert symbols == {"UGRO", "ANNA"}
    assert trade_stream.synced[-1] == ["ANNA", "UGRO"]
    warmup_events = [
        payload
        for stream, payload, _kwargs in redis.entries
        if stream == "test:market-data" and payload["event_type"] == "historical_bars"
    ]
    assert len(warmup_events) == 2
    assert {event["payload"]["symbol"] for event in warmup_events} == {"ANNA"}
    assert {event["payload"]["interval_secs"] for event in warmup_events} == {30, 60}


def test_massive_trade_stream_accepts_and_normalizes_aggregate_callback() -> None:
    bars = []
    stream = MassiveTradeStream(api_key="test")
    stream._subscriptions = {"UGRO"}
    stream._on_agg = bars.append

    stream._handle_messages(
        [
            SimpleNamespace(
                ev="A",
                symbol="UGRO",
                o=2.0,
                h=2.1,
                l=1.9,
                c=2.05,
                v=1200,
                s=1_700_000_000_000,
                z=8,
            )
        ]
    )

    assert len(bars) == 1
    assert bars[0].symbol == "UGRO"
    assert bars[0].interval_secs == 1
    assert bars[0].open == 2.0
    assert bars[0].close == 2.05
    assert bars[0].volume == 1200
    assert bars[0].timestamp == 1_700_000_000.0
    assert bars[0].trade_count == 150


def test_massive_trade_stream_prefers_direct_aggregate_transactions() -> None:
    bars = []
    stream = MassiveTradeStream(api_key="test")
    stream._subscriptions = {"UGRO"}
    stream._on_agg = bars.append

    stream._handle_messages(
        [
            SimpleNamespace(
                ev="A",
                symbol="UGRO",
                o=2.0,
                h=2.1,
                l=1.9,
                c=2.05,
                v=1200,
                s=1_700_000_000_000,
                transactions=58,
                z=8,
            )
        ]
    )

    assert len(bars) == 1
    assert bars[0].trade_count == 58


def test_massive_trade_stream_derives_trade_count_from_average_size_field() -> None:
    bars = []
    stream = MassiveTradeStream(api_key="test")
    stream._subscriptions = {"UGRO"}
    stream._on_agg = bars.append

    stream._handle_messages(
        [
            SimpleNamespace(
                ev="A",
                symbol="UGRO",
                o=2.0,
                h=2.1,
                l=1.9,
                c=2.05,
                v=1517,
                s=1_700_000_000_000,
                average_size=303,
            )
        ]
    )

    assert len(bars) == 1
    assert bars[0].trade_count == 5


@pytest.mark.asyncio
async def test_massive_trade_stream_stop_awaits_async_websocket_close() -> None:
    closed = False

    class AsyncCloseWs:
        async def close(self):
            nonlocal closed
            await asyncio.sleep(0)
            closed = True

    stream = MassiveTradeStream(api_key="test")
    stream._ws = AsyncCloseWs()
    stream._connected = True

    await stream.stop()

    assert closed is True
    assert stream._ws is None
    assert stream._connected is False


@pytest.mark.asyncio
async def test_massive_trade_stream_run_loop_closes_websocket_on_unexpected_exit() -> None:
    closed = False
    warning_sleeps: list[float] = []

    class FakeWs:
        def subscribe(self, *args):
            return None

        def run(self, handler):
            del handler
            return None

        async def close(self):
            nonlocal closed
            closed = True

    stream = MassiveTradeStream(api_key="test")
    stream._running = True
    stream._subscriptions = {"UGRO"}

    async def fake_sleep(delay: float) -> None:
        warning_sleeps.append(delay)
        stream._running = False

    original_sleep = asyncio.sleep
    stream._build_client = lambda: FakeWs()
    asyncio.sleep = fake_sleep
    try:
        await stream._run_loop()
    finally:
        asyncio.sleep = original_sleep

    assert closed is True
    assert warning_sleeps == [1]
    assert stream._connected is False
    assert stream._ws is None


@pytest.mark.asyncio
async def test_stream_publish_loop_publishes_live_bar_events() -> None:
    redis = FakeRedis()
    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test"),
        redis_client=redis,
        snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(),
        reference_cache=FakeReferenceCache(),
    )
    stop_event = asyncio.Event()

    await service._bar_queue.put(
        LiveBarRecord(
            symbol="SAGT",
            interval_secs=30,
            open=2.4,
            high=2.6,
            low=2.35,
            close=2.55,
            volume=12_500,
            timestamp=1_700_000_030.0,
            trade_count=18,
        )
    )
    await service._trade_queue.put(
        TradeTickRecord(
            symbol="SAGT",
            price=2.55,
            size=100,
            timestamp_ns=1_700_000_030_000_000_000,
            cumulative_volume=12_500,
        )
    )

    task = asyncio.create_task(service._stream_publish_loop(stop_event))
    try:
        await asyncio.wait_for(_wait_for_event(redis, "test:market-data", "live_bar"), timeout=2.0)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    live_bar_events = [
        payload
        for stream, payload, _kwargs in redis.entries
        if stream == "test:market-data" and payload["event_type"] == "live_bar"
    ]
    assert len(live_bar_events) == 1
    assert live_bar_events[0]["payload"]["symbol"] == "SAGT"
    assert live_bar_events[0]["payload"]["interval_secs"] == 30
    assert live_bar_events[0]["payload"]["close"] == "2.55"


@pytest.mark.asyncio
async def test_live_bars_can_publish_while_historical_warmup_is_inflight() -> None:
    redis = FakeRedis()

    class SlowWarmupSnapshotProvider(FakeSnapshotProvider):
        def fetch_historical_bars(
            self,
            symbol: str,
            *,
            interval_secs: int,
            lookback_calendar_days: int,
            limit: int,
        ):
            del symbol, interval_secs, lookback_calendar_days, limit
            time.sleep(0.5)
            return []

    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test", market_data_static_symbols="SAGT"),
        redis_client=redis,
        snapshot_provider=SlowWarmupSnapshotProvider(),
        trade_stream=FakeTradeStream(),
        reference_cache=FakeReferenceCache(),
    )
    stop_event = asyncio.Event()

    await service._bar_queue.put(
        LiveBarRecord(
            symbol="SAGT",
            interval_secs=30,
            open=2.4,
            high=2.6,
            low=2.35,
            close=2.55,
            volume=12_500,
            timestamp=1_700_000_030.0,
            trade_count=18,
        )
    )
    await service._trade_queue.put(
        TradeTickRecord(
            symbol="SAGT",
            price=2.55,
            size=100,
            timestamp_ns=1_700_000_030_000_000_000,
            cumulative_volume=12_500,
        )
    )

    warmup_task = asyncio.create_task(service._publish_historical_warmup({"SAGT"}))
    stream_task = asyncio.create_task(service._stream_publish_loop(stop_event))
    try:
        await asyncio.wait_for(_wait_for_event(redis, "test:market-data", "live_bar"), timeout=0.25)
        assert warmup_task.done() is False
    finally:
        stop_event.set()
        await asyncio.wait_for(stream_task, timeout=2.0)
        await asyncio.wait_for(warmup_task, timeout=2.0)

    live_bar_events = [
        payload
        for stream, payload, _kwargs in redis.entries
        if stream == "test:market-data" and payload["event_type"] == "live_bar"
    ]
    assert len(live_bar_events) == 1
    assert live_bar_events[0]["payload"]["symbol"] == "SAGT"


async def _wait_for_event(redis: FakeRedis, stream: str, event_type: str) -> None:
    while True:
        if any(saved_stream == stream and payload["event_type"] == event_type for saved_stream, payload, _kwargs in redis.entries):
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_restore_subscription_state_rehydrates_latest_replace_event() -> None:
    redis = FakeRedis()
    redis.entries.append(
        (
            "test:market-data-subscriptions",
            MarketDataSubscriptionEvent(
                source_service="strategy-engine",
                payload=MarketDataSubscriptionPayload(
                    consumer_name="strategy-engine",
                    mode="replace",
                    symbols=["SAGT", "XTLB"],
                ),
            ).model_dump(mode="json"),
            {},
        )
    )
    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test", market_data_static_symbols="SPY"),
        redis_client=redis,
        snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(),
        reference_cache=FakeReferenceCache(),
    )

    await service._restore_subscription_state()

    assert service.active_symbols() == {"SPY", "SAGT", "XTLB"}
    assert service._desired_symbols_by_consumer["strategy-engine"] == {"SAGT", "XTLB"}
