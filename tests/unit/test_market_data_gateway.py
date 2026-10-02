from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from massive.exceptions import BadResponse
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from project_mai_tai.events import MarketDataSubscriptionEvent, MarketDataSubscriptionPayload
from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider, MassiveTradeStream
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.market_data.models import HistoricalBarRecord, LiveBarRecord, SnapshotRecord, TradeTickRecord
from project_mai_tai.settings import Settings


def test_gateway_entrypoint_configures_logging_before_start(monkeypatch) -> None:
    from project_mai_tai.services import market_data_gateway as entrypoint

    calls = []
    monkeypatch.setattr(entrypoint, "get_settings", lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(
        entrypoint,
        "configure_logging",
        lambda name, level: calls.append(("logging", name, level))
        or SimpleNamespace(info=lambda message: calls.append(("startup", message))),
    )

    def close_main(coroutine):
        calls.append(("run",))
        coroutine.close()

    monkeypatch.setattr(entrypoint.asyncio, "run", close_main)
    entrypoint.run()

    assert calls == [
        ("logging", "market-data-gateway", "INFO"),
        ("startup", "[MARKET-DATA] process starting"),
        ("run",),
    ]


def test_reference_not_found_is_one_warning_without_traceback(caplog) -> None:
    provider = MassiveSnapshotProvider(api_key="test")

    def not_found(_ticker):
        raise BadResponse('{"status":"NOT_FOUND","message":"Ticker not found."}')

    provider._client = SimpleNamespace(get_ticker_details=not_found)
    with caplog.at_level("WARNING"):
        assert provider.get_ticker_details_batch(["ZEXIT"]) == {}

    assert len(caplog.records) == 1
    assert caplog.records[0].message == "Massive ticker details NOT_FOUND for ZEXIT"
    assert caplog.records[0].exc_info is None


@pytest.mark.parametrize(
    "error", [BadResponse('{"status":"ERROR"}'), RuntimeError("network failed")]
)
def test_reference_other_errors_keep_traceback(caplog, error) -> None:
    provider = MassiveSnapshotProvider(api_key="test")

    def broken(_ticker):
        raise error

    provider._client = SimpleNamespace(get_ticker_details=broken)
    with caplog.at_level("ERROR"):
        assert provider.get_ticker_details_batch(["ZEXIT"]) == {}

    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object], dict[str, object]]] = []
        self.hashes: dict[str, dict[str, str]] = {}

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        self.entries.append((stream, json.loads(fields["data"]), kwargs))
        return "1-0"

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def xrevrange(self, stream: str, count: int = 1):
        results = []
        for index in range(len(self.entries), 0, -1):
            saved_stream, payload, _kwargs = self.entries[index - 1]
            if saved_stream != stream:
                continue
            results.append((f"{index}-0", {"data": json.dumps(payload)}))
            if len(results) >= count:
                break
        return results

    async def xrange(self, stream: str, min: str = "-"):
        boundary = int(min.removeprefix("(").split("-")[0]) if min != "-" else 0
        return [
            (f"{index}-0", {"data": json.dumps(payload)})
            for index, (saved_stream, payload, _kwargs) in enumerate(self.entries, start=1)
            if saved_stream == stream and index > boundary
        ]

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def aclose(self) -> None:
        return None


class FlakyMarketDataRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        payload = json.loads(fields["data"])
        if (
            not self.failed_once
            and stream.endswith(":market-data")
            and payload.get("event_type") == "trade_tick"
        ):
            self.failed_once = True
            raise RuntimeError("synthetic xadd failure")
        self.entries.append((stream, payload, kwargs))
        return "1-0"


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
async def test_apply_subscription_event_replace_only_warms_new_symbols(caplog) -> None:
    caplog.set_level("INFO")
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
    assert "[MARKET-DATA-WARMUP-UNION-ADD] consumer=strategy-engine symbols=ANNA count=1" in caplog.text


def test_historical_rest_audit_counts_only_actual_attempts(caplog) -> None:
    class Client:
        def list_aggs(self, *args, **kwargs):
            return []

    provider = MassiveSnapshotProvider(api_key="test")
    provider._client = Client()
    caplog.set_level("INFO")

    provider.fetch_historical_bars("ANNA", interval_secs=30, lookback_calendar_days=1, limit=20)
    assert caplog.text.count("[MARKET-DATA-HISTORICAL-REST-ATTEMPT] symbol=ANNA interval_s=30") == 1

    provider._historical_failures[("ANNA", 30)] = time.monotonic() + 60
    provider.fetch_historical_bars("ANNA", interval_secs=30, lookback_calendar_days=1, limit=20)
    assert caplog.text.count("[MARKET-DATA-HISTORICAL-REST-ATTEMPT] symbol=ANNA interval_s=30") == 1


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
async def test_massive_trade_stream_downgrades_aggregate_subscriptions_after_policy_violation() -> None:
    class FakeWebSocketClient:
        def __init__(self) -> None:
            self.subscriptions: list[str] = []
            self.closed = threading.Event()

        def subscribe(self, *subscriptions: str) -> None:
            self.subscriptions.extend(subscriptions)

        def unsubscribe(self, *subscriptions: str) -> None:
            for subscription in subscriptions:
                while subscription in self.subscriptions:
                    self.subscriptions.remove(subscription)

        async def connect(self, _processor, **_kwargs) -> None:
            if any(subscription.startswith("A.") for subscription in self.subscriptions):
                raise ConnectionClosedError(
                    Close(code=1008, reason=""),
                    Close(code=1008, reason=""),
                    True,
                )
            await asyncio.to_thread(self.closed.wait, 1.0)

        async def close(self) -> None:
            self.closed.set()

    clients: list[FakeWebSocketClient] = []
    stream = MassiveTradeStream(api_key="test", enable_aggregate_subscriptions=True)

    def build_client() -> FakeWebSocketClient:
        client = FakeWebSocketClient()
        clients.append(client)
        return client

    stream._build_client = build_client  # type: ignore[method-assign]
    await stream.start(
        on_trade=lambda _record: None,
        on_quote=lambda _record: None,
        on_agg=lambda _record: None,
    )
    await stream.sync_subscriptions(["UGRO"])

    try:
        async def aggregate_disabled() -> None:
            while stream._aggregate_subscriptions_allowed:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(aggregate_disabled(), timeout=1.0)
    finally:
        await stream.stop()

    assert len(clients) >= 2
    assert "A.UGRO" in clients[0].subscriptions
    assert all(not subscription.startswith("A.") for subscription in clients[1].subscriptions)
    assert stream._aggregate_subscriptions_allowed is False


@pytest.mark.asyncio
async def test_massive_trade_stream_defaults_to_trade_quote_only_even_with_live_bar_handler() -> None:
    class FakeWebSocketClient:
        def __init__(self) -> None:
            self.subscriptions: list[str] = []
            self.closed = threading.Event()

        def subscribe(self, *subscriptions: str) -> None:
            self.subscriptions.extend(subscriptions)

        def unsubscribe(self, *subscriptions: str) -> None:
            for subscription in subscriptions:
                while subscription in self.subscriptions:
                    self.subscriptions.remove(subscription)

        async def connect(self, _processor, **_kwargs) -> None:
            await asyncio.to_thread(self.closed.wait, 1.0)

        async def close(self) -> None:
            self.closed.set()

    clients: list[FakeWebSocketClient] = []
    stream = MassiveTradeStream(api_key="test")

    def build_client() -> FakeWebSocketClient:
        client = FakeWebSocketClient()
        clients.append(client)
        return client

    stream._build_client = build_client  # type: ignore[method-assign]
    await stream.start(
        on_trade=lambda _record: None,
        on_quote=lambda _record: None,
        on_agg=lambda _record: None,
    )
    await stream.sync_subscriptions(["UGRO"])

    try:
        async def connected() -> None:
            while not clients or not clients[0].subscriptions:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(connected(), timeout=1.0)
    finally:
        await stream.stop()

    assert any(subscription.startswith("T.") for subscription in clients[0].subscriptions)
    assert any(subscription.startswith("Q.") for subscription in clients[0].subscriptions)
    assert all(not subscription.startswith("A.") for subscription in clients[0].subscriptions)


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
async def test_stream_publish_loop_survives_single_market_data_publish_failure(caplog: pytest.LogCaptureFixture) -> None:
    redis = FlakyMarketDataRedis()
    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test"),
        redis_client=redis,
        snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(),
        reference_cache=FakeReferenceCache(),
    )
    stop_event = asyncio.Event()

    await service._trade_queue.put(
        TradeTickRecord(
            symbol="SAGT",
            price=2.55,
            size=100,
            timestamp_ns=1_700_000_030_000_000_000,
            cumulative_volume=12_500,
        )
    )
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
    assert "failed to publish trade tick for SAGT" in caplog.text


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


@pytest.mark.asyncio
async def test_first_gateway_restart_bootstraps_all_retained_consumer_events() -> None:
    redis = FakeRedis()
    for consumer, symbols in (
        ("strategy-engine", ["SCAN"]),
        ("schwab-1m-v2", ["V2SY"]),
    ):
        event = MarketDataSubscriptionEvent(
            source_service=consumer,
            payload=MarketDataSubscriptionPayload(
                consumer_name=consumer, mode="replace", symbols=symbols,
            ),
        )
        await redis.xadd("test:market-data-subscriptions", {"data": event.model_dump_json()})
    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test", market_data_static_symbols="SPY"),
        redis_client=redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache(),
    )

    await service._restore_subscription_state()

    assert service.active_symbols() == {"SPY", "SCAN", "V2SY"}
    assert redis.hashes["test:market-data-subscription-owners"]["_migration_complete"] == "1"


@pytest.mark.asyncio
async def test_gateway_restart_replays_an_event_newer_than_durable_checkpoint() -> None:
    redis = FakeRedis()
    settings = Settings(redis_stream_prefix="test", market_data_static_symbols="SPY")
    first = MarketDataGatewayService(
        settings=settings, redis_client=redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache(),
    )
    await first._restore_subscription_state()
    assert redis.hashes["test:market-data-subscription-owners"]["_last_applied_id"] == "0-0"

    event = MarketDataSubscriptionEvent(
        source_service="strategy-engine",
        payload=MarketDataSubscriptionPayload(
            consumer_name="strategy-engine", mode="replace", symbols=["SCAN"],
        ),
    )
    await redis.xadd("test:market-data-subscriptions", {"data": event.model_dump_json()})
    restored = MarketDataGatewayService(
        settings=settings, redis_client=redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache(),
    )
    await restored._restore_subscription_state()

    assert restored.active_symbols() == {"SPY", "SCAN"}
    assert restored._subscription_offsets["test:market-data-subscriptions"] == "1-0"
    assert redis.hashes["test:market-data-subscription-owners"]["_last_applied_id"] == "1-0"


@pytest.mark.asyncio
async def test_deleted_owner_hash_is_fully_rebuilt_by_one_event_before_restart() -> None:
    redis = FakeRedis()
    settings = Settings(redis_stream_prefix="test", market_data_static_symbols="SPY")
    first = MarketDataGatewayService(
        settings=settings, redis_client=redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache(),
    )
    await first._restore_subscription_state()
    initial = (
        ("strategy-engine", ["SCAN"]),
        ("schwab-1m-v2", ["V2SY"]),
        ("orb", ["ORBSY"]),
        ("orb-schwab", ["ORBSC"]),
        ("momentum-paper", ["MOMO"]),
    )
    for index, (consumer, symbols) in enumerate(initial, start=1):
        event = MarketDataSubscriptionEvent(
            source_service=consumer,
            payload=MarketDataSubscriptionPayload(
                consumer_name=consumer, mode="replace", symbols=symbols,
            ),
        )
        message_id = await redis.xadd(
            "test:market-data-subscriptions", {"data": event.model_dump_json()},
        )
        await first.apply_subscription_event(event, message_id=message_id)
        saved = redis.hashes["test:market-data-subscription-owners"]
        assert saved["_migration_complete"] == "1"
        assert saved["_last_applied_id"] == message_id
        assert all(name in saved for name, _ in initial[:index])

    redis.hashes.pop("test:market-data-subscription-owners")
    release = MarketDataSubscriptionEvent(
        source_service="momentum-paper",
        payload=MarketDataSubscriptionPayload(
            consumer_name="momentum-paper", mode="replace", symbols=[],
        ),
    )
    message_id = await redis.xadd(
        "test:market-data-subscriptions", {"data": release.model_dump_json()},
    )
    await first.apply_subscription_event(release, message_id=message_id)

    saved = redis.hashes["test:market-data-subscription-owners"]
    assert saved == {
        "strategy-engine": '["SCAN"]',
        "schwab-1m-v2": '["V2SY"]',
        "orb": '["ORBSY"]',
        "orb-schwab": '["ORBSC"]',
        "momentum-paper": "[]",
        "_migration_complete": "1",
        "_last_applied_id": message_id,
    }

    # Retention can remove old events; the durable hash must carry all owners.
    redis.entries[:-1] = [
        ("retired-stream", payload, kwargs)
        for _stream, payload, kwargs in redis.entries[:-1]
    ]
    restored = MarketDataGatewayService(
        settings=settings, redis_client=redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache(),
    )
    await restored._restore_subscription_state()
    assert restored.active_symbols() == {"SPY", "SCAN", "V2SY", "ORBSY", "ORBSC"}
    assert restored._desired_symbols_by_consumer["momentum-paper"] == set()


@pytest.mark.asyncio
async def test_unmarked_owner_hash_rebuilds_from_all_retained_replace_events() -> None:
    redis = FakeRedis()
    settings = Settings(redis_stream_prefix="test", market_data_static_symbols="SPY")
    for consumer, symbols in (("strategy-engine", ["SCAN"]), ("orb", ["ORBSY"])):
        event = MarketDataSubscriptionEvent(
            source_service=consumer,
            payload=MarketDataSubscriptionPayload(
                consumer_name=consumer, mode="replace", symbols=symbols,
            ),
        )
        await redis.xadd("test:market-data-subscriptions", {"data": event.model_dump_json()})
    redis.hashes["test:market-data-subscription-owners"] = {
        "orb": '["STALE"]', "_last_applied_id": "2-0",
    }
    restored = MarketDataGatewayService(
        settings=settings, redis_client=redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache(),
    )
    await restored._restore_subscription_state()
    assert restored.active_symbols() == {"SPY", "SCAN", "ORBSY"}
    assert redis.hashes["test:market-data-subscription-owners"]["_migration_complete"] == "1"


@pytest.mark.asyncio
async def test_unmarked_owner_history_with_first_add_refuses_instead_of_guessing() -> None:
    redis = FakeRedis()
    event = MarketDataSubscriptionEvent(
        source_service="orb",
        payload=MarketDataSubscriptionPayload(
            consumer_name="orb", mode="add", symbols=["ORBSY"],
        ),
    )
    await redis.xadd("test:market-data-subscriptions", {"data": event.model_dump_json()})
    restored = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test"), redis_client=redis,
        snapshot_provider=FakeSnapshotProvider(), trade_stream=FakeTradeStream(),
        reference_cache=FakeReferenceCache(),
    )

    with pytest.raises(RuntimeError, match="does not start with replace"):
        await restored._restore_subscription_state()
    assert "_migration_complete" not in redis.hashes.get(
        "test:market-data-subscription-owners", {},
    )


@pytest.mark.asyncio
async def test_failed_subscription_state_write_does_not_advance_owner_or_stream_offset() -> None:
    class FailingHashRedis(FakeRedis):
        async def hset(self, key: str, mapping: dict[str, str]) -> None:
            raise RuntimeError("Redis hash unavailable")

    redis = FailingHashRedis()
    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test"), redis_client=redis,
        snapshot_provider=FakeSnapshotProvider(), trade_stream=FakeTradeStream(),
        reference_cache=FakeReferenceCache(),
    )
    event = MarketDataSubscriptionEvent(
        source_service="momentum-paper",
        payload=MarketDataSubscriptionPayload(
            consumer_name="momentum-paper", mode="replace", symbols=["MOMO"],
        ),
    )

    with pytest.raises(RuntimeError, match="Redis hash unavailable"):
        await service.apply_subscription_event(event, message_id="1-0")

    assert service.active_symbols() == set()
    assert service._subscription_offsets["test:market-data-subscriptions"] == "$"


@pytest.mark.asyncio
async def test_failed_subscription_sync_retries_without_losing_owner_update() -> None:
    class FailOnceTradeStream(FakeTradeStream):
        async def sync_subscriptions(self, symbols) -> None:
            if not self.synced:
                self.synced.append(["failed"])
                raise RuntimeError("stream reconnecting")
            await super().sync_subscriptions(symbols)

    stream = FailOnceTradeStream()
    service = MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test"), redis_client=FakeRedis(),
        snapshot_provider=FakeSnapshotProvider(), trade_stream=stream,
        reference_cache=FakeReferenceCache(),
    )
    event = MarketDataSubscriptionEvent(
        source_service="momentum-paper",
        payload=MarketDataSubscriptionPayload(
            consumer_name="momentum-paper", mode="replace", symbols=["MOMO"],
        ),
    )

    with pytest.raises(RuntimeError, match="stream reconnecting"):
        await service.apply_subscription_event(event, message_id="1-0")
    assert service.active_symbols() == set()

    assert await service.apply_subscription_event(event, message_id="1-0") == {"MOMO"}
    assert stream.synced[-1] == ["MOMO"]


@pytest.mark.asyncio
async def test_momentum_replace_and_restart_never_remove_other_consumer_symbols(caplog) -> None:
    caplog.set_level("INFO")
    redis = FakeRedis()
    stream = FakeTradeStream()
    settings = Settings(redis_stream_prefix="test", market_data_static_symbols="SPY")
    service = MarketDataGatewayService(
        settings=settings, redis_client=redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=stream, reference_cache=FakeReferenceCache(),
    )
    await service._restore_subscription_state()

    for consumer, symbols in (
        ("strategy-engine", ["SCAN"]),
        ("schwab-1m-v2", ["V2SY"]),
        ("momentum-paper", ["MOMO"]),
    ):
        event = MarketDataSubscriptionEvent(
            source_service=consumer,
            payload=MarketDataSubscriptionPayload(
                consumer_name=consumer, mode="replace", symbols=symbols,
            ),
        )
        await redis.xadd("test:market-data-subscriptions", {"data": event.model_dump_json()})
        await service.apply_subscription_event(event)

    assert service.active_symbols() == {"SPY", "SCAN", "V2SY", "MOMO"}
    replacement = MarketDataSubscriptionEvent(
        source_service="momentum-paper",
        payload=MarketDataSubscriptionPayload(
            consumer_name="momentum-paper", mode="replace", symbols=[],
        ),
    )
    await redis.xadd("test:market-data-subscriptions", {"data": replacement.model_dump_json()})
    await service.apply_subscription_event(replacement)
    assert service.active_symbols() == {"SPY", "SCAN", "V2SY"}
    assert (
        "[MARKET-DATA-SUBSCRIPTION-UNION] consumer=momentum-paper "
        "added=- removed=MOMO count=3"
    ) in caplog.text
    redis.entries = redis.entries[-1:]

    restored = MarketDataGatewayService(
        settings=settings, redis_client=redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache(),
    )
    await restored._restore_subscription_state()
    assert restored.active_symbols() == {"SPY", "SCAN", "V2SY"}


def test_massive_trade_stream_forwards_conditions_for_paper_eligibility() -> None:
    trades: list[TradeTickRecord] = []
    stream = MassiveTradeStream(api_key="test")
    stream._subscriptions = {"MOMO"}
    stream._on_trade = trades.append

    stream._handle_messages([
        SimpleNamespace(
            ev="T", symbol="MOMO", price=2.5, size=100,
            sip_timestamp=1_790_000_000_000, conditions=[12, 37],
        )
    ])

    assert len(trades) == 1
    assert trades[0].conditions == ("12", "37")
    assert trades[0].conditions_present is True


def test_massive_trade_stream_does_not_invent_missing_condition_provenance() -> None:
    trades: list[TradeTickRecord] = []
    stream = MassiveTradeStream(api_key="test")
    stream._subscriptions = {"MOMO"}
    stream._on_trade = trades.append

    stream._handle_messages([
        SimpleNamespace(
            ev="T", symbol="MOMO", price=2.5, size=100,
            sip_timestamp=1_790_000_000_000,
        )
    ])

    assert len(trades) == 1
    assert trades[0].conditions == ()
    assert trades[0].conditions_present is False
    assert trades[0].to_payload().conditions_present is False


# ------------------------------------------------- periodic reference refresh (DFNS/LGHL incident)
# 2026-07-27: `_ensure_reference_data()` was called ONCE in run(), so a gateway with long uptime
# served an ever-staler cache. Ours reached 19 DAYS. five_pillars drops any symbol with no reference
# entry (`if ref is None: continue`) — silently — so DFNS (+64%) and LGHL (+120%) were invisible to
# the scanner all session. A manual rebuild had LGHL confirmed within 90s.


class CountingReferenceCache(FakeReferenceCache):
    """Tracks ensure/build calls and can report itself stale, like the real cache does past max-age."""

    def __init__(self, stale: bool = False) -> None:
        self.load_calls = 0
        self.build_calls = 0
        self._stale = stale

    def load_from_cache(self) -> bool:
        self.load_calls += 1
        return not self._stale

    def build(self) -> None:
        self.build_calls += 1
        self._stale = False


def _svc(cache, **settings_kwargs):
    return MarketDataGatewayService(
        settings=Settings(redis_stream_prefix="test", **settings_kwargs),
        redis_client=FakeRedis(),
        snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(),
        reference_cache=cache,
    )


@pytest.mark.asyncio
async def test_reference_refresh_rebuilds_a_stale_cache() -> None:
    """The fix: a STALE cache is rebuilt by the periodic loop, not left to rot until a restart."""
    cache = CountingReferenceCache(stale=True)
    service = _svc(cache, market_data_reference_refresh_interval_seconds=1)
    stop = asyncio.Event()
    task = asyncio.create_task(service._reference_refresh_loop(stop))
    await asyncio.sleep(1.4)
    stop.set()
    await asyncio.gather(task, return_exceptions=True)
    assert cache.load_calls >= 1
    assert cache.build_calls >= 1  # WITHOUT the fix this loop does not exist -> 0


@pytest.mark.asyncio
async def test_reference_refresh_leaves_a_fresh_cache_alone() -> None:
    """A fresh cache costs one file read per tick and is never rebuilt."""
    cache = CountingReferenceCache(stale=False)
    service = _svc(cache, market_data_reference_refresh_interval_seconds=1)
    stop = asyncio.Event()
    task = asyncio.create_task(service._reference_refresh_loop(stop))
    await asyncio.sleep(1.4)
    stop.set()
    await asyncio.gather(task, return_exceptions=True)
    assert cache.load_calls >= 1
    assert cache.build_calls == 0


@pytest.mark.asyncio
async def test_reference_refresh_disabled_by_zero_interval() -> None:
    """interval <= 0 restores the previous boot-only behaviour exactly (the rollback lever)."""
    cache = CountingReferenceCache(stale=True)
    service = _svc(cache, market_data_reference_refresh_interval_seconds=0)
    stop = asyncio.Event()
    await asyncio.wait_for(service._reference_refresh_loop(stop), timeout=2)
    assert cache.load_calls == 0 and cache.build_calls == 0


@pytest.mark.asyncio
async def test_refresh_failure_keeps_serving_the_old_cache() -> None:
    """A refresh blowing up must never kill the gateway — the stale cache keeps serving."""
    class Exploding(CountingReferenceCache):
        def load_from_cache(self) -> bool:
            self.load_calls += 1
            raise RuntimeError("provider down")

    cache = Exploding(stale=True)
    service = _svc(cache, market_data_reference_refresh_interval_seconds=1)
    stop = asyncio.Event()
    task = asyncio.create_task(service._reference_refresh_loop(stop))
    await asyncio.sleep(1.4)
    stop.set()
    await asyncio.gather(task, return_exceptions=True)
    assert cache.load_calls >= 1
    assert not task.cancelled()  # loop survived the exception


@pytest.mark.asyncio
async def test_counts_snapshots_with_no_reference_entry() -> None:
    """The observability half: the silent drop is now countable. UGRO has a reference entry, DFNS
    does not — exactly the shape that hid this incident for 19 days."""
    service = _svc(FakeReferenceCache())
    await service.publish_snapshot_batch_once(
        [
            SnapshotRecord(symbol="UGRO", previous_close=2.10, day_close=2.35,
                           day_volume=900_000, last_trade_price=2.36),
            SnapshotRecord(symbol="DFNS", previous_close=4.35, day_close=0.0,
                           day_volume=0, last_trade_price=7.13),
        ]
    )
    assert service._last_snapshot_symbol_count == 2
    assert service._snapshots_without_reference == 1  # DFNS — the scanner would silently drop it
