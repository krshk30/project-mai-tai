from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Iterable

from redis.asyncio import Redis

from project_mai_tai.events import MarketDataSubscriptionEvent, stream_name
from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider, MassiveTradeStream
from project_mai_tai.market_data.models import (
    HistoricalBarsRecord,
    LiveBarRecord,
    QuoteTickRecord,
    SnapshotRecord,
    TradeTickRecord,
)
from project_mai_tai.market_data.protocols import SnapshotProvider, TradeStreamProvider
from project_mai_tai.market_data.publisher import MarketDataPublisher
from project_mai_tai.market_data.reference_cache import ReferenceDataCache
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.settings import Settings, get_settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "market-data-gateway"
WARMUP_INTERVALS = (30, 60)


class MarketDataGatewayService:
    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        *,
        snapshot_provider: SnapshotProvider | None = None,
        trade_stream: TradeStreamProvider | None = None,
        reference_cache: ReferenceDataCache | None = None,
    ):
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.publisher = MarketDataPublisher(
            self.redis,
            self.settings.redis_stream_prefix,
            SERVICE_NAME,
            snapshot_batch_stream_maxlen=self.settings.redis_snapshot_batch_stream_maxlen,
            market_data_stream_maxlen=self.settings.redis_market_data_stream_maxlen,
            heartbeat_stream_maxlen=self.settings.redis_heartbeat_stream_maxlen,
        )
        self.snapshot_provider = snapshot_provider or self._build_snapshot_provider()
        self.trade_stream = trade_stream or self._build_trade_stream()
        self.reference_cache = reference_cache or ReferenceDataCache(
            self.snapshot_provider,
            cache_path=self.settings.market_data_reference_cache_path,
            max_age_hours=self.settings.market_data_reference_cache_max_age_hours,
            min_price=self.settings.market_data_scan_min_price,
            max_price=self.settings.market_data_scan_max_price,
            lookback_days=self.settings.market_data_reference_lookback_days,
        )
        self.logger = logging.getLogger(SERVICE_NAME)
        self.instance_name = socket.gethostname()
        self._trade_queue: asyncio.Queue[TradeTickRecord] = asyncio.Queue()
        self._quote_queue: asyncio.Queue[QuoteTickRecord] = asyncio.Queue()
        self._bar_queue: asyncio.Queue[LiveBarRecord] = asyncio.Queue()
        self._live_aggregate_stream_enabled = (
            self.settings.market_data_live_aggregate_stream_enabled
            or self.settings.strategy_macd_30s_live_aggregate_bars_enabled
            or self.settings.strategy_polygon_30s_runtime_uses_live_aggregate_bars
        )
        self._desired_symbols_by_consumer: dict[str, set[str]] = {
            "static": set(self.settings.market_data_static_symbol_list),
        }
        self._active_symbols: set[str] = set(self.settings.market_data_static_symbol_list)
        self._subscription_offsets = {
            stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions"): "$",
        }

    async def run(self) -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)

        await self._ensure_reference_data()
        await self.publisher.publish_heartbeat(
            instance_name=self.instance_name,
            status="starting",
            details={"reference_tickers": str(self.reference_cache.ticker_count())},
        )
        await self._restore_subscription_state()

        loop = asyncio.get_running_loop()
        await self.trade_stream.start(
            on_trade=lambda record: loop.call_soon_threadsafe(self._trade_queue.put_nowait, record),
            on_quote=lambda record: loop.call_soon_threadsafe(self._quote_queue.put_nowait, record),
            on_agg=(
                (lambda record: loop.call_soon_threadsafe(self._bar_queue.put_nowait, record))
                if self._live_aggregate_stream_enabled
                else None
            ),
        )
        await self.trade_stream.sync_subscriptions(self._active_symbols)

        tasks = [
            asyncio.create_task(self._snapshot_loop(stop_event)),
            asyncio.create_task(self._subscription_loop(stop_event)),
            asyncio.create_task(self._stream_publish_loop(stop_event)),
            asyncio.create_task(self._heartbeat_loop(stop_event)),
        ]
        if self._active_symbols and self.settings.market_data_warmup_enabled:
            tasks.append(
                asyncio.create_task(
                    self._publish_historical_warmup(self._active_symbols),
                )
            )

        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.trade_stream.stop()
            await self.publisher.publish_heartbeat(
                instance_name=self.instance_name,
                status="stopping",
                details={"active_symbols": str(len(self._active_symbols))},
            )
            await self.redis.aclose()

    async def publish_snapshot_batch_once(self, snapshots: Iterable[SnapshotRecord]) -> int:
        snapshot_list = list(snapshots)
        reference_payloads = self.reference_cache.as_payloads(snapshot.symbol for snapshot in snapshot_list)
        await self.publisher.publish_snapshot_batch(snapshot_list, reference_payloads)
        return len(snapshot_list)

    async def apply_subscription_event(self, event: MarketDataSubscriptionEvent) -> set[str]:
        symbols = {symbol.upper() for symbol in event.payload.symbols if symbol}
        consumer = event.payload.consumer_name
        mode = event.payload.mode

        current = self._desired_symbols_by_consumer.get(consumer, set())
        if mode == "replace":
            updated = symbols
        elif mode == "add":
            updated = current | symbols
        else:
            updated = current - symbols

        self._desired_symbols_by_consumer[consumer] = updated
        next_symbols = set().union(*self._desired_symbols_by_consumer.values())
        added_symbols = next_symbols - self._active_symbols
        if next_symbols != self._active_symbols:
            self._active_symbols = next_symbols
            await self.trade_stream.sync_subscriptions(sorted(self._active_symbols))
            await self._publish_historical_warmup(added_symbols)
        return set(self._active_symbols)

    def active_symbols(self) -> set[str]:
        return set(self._active_symbols)

    def _build_snapshot_provider(self) -> SnapshotProvider:
        if not self.settings.massive_api_key:
            raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is required for market-data polling.")
        return MassiveSnapshotProvider(self.settings.massive_api_key)

    def _build_trade_stream(self) -> TradeStreamProvider:
        if not self.settings.massive_api_key:
            raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is required for market-data streaming.")
        return MassiveTradeStream(self.settings.massive_api_key)

    async def _ensure_reference_data(self) -> None:
        loaded = await asyncio.to_thread(self.reference_cache.load_from_cache)
        if loaded:
            return
        await asyncio.to_thread(self.reference_cache.build)

    async def _snapshot_loop(self, stop_event: asyncio.Event) -> None:
        interval = max(1, self.settings.market_data_snapshot_interval_seconds)
        while not stop_event.is_set():
            try:
                snapshots = await asyncio.to_thread(self.snapshot_provider.fetch_all_snapshots)
                count = await self.publish_snapshot_batch_once(snapshots)
                self.logger.info("published snapshot batch with %s records", count)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("snapshot polling failed")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _subscription_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                messages = await self.redis.xread(self._subscription_offsets, block=1000, count=50)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("subscription stream read failed")
                await asyncio.sleep(1)
                continue

            for stream, entries in messages:
                for message_id, fields in entries:
                    self._subscription_offsets[stream] = message_id
                    data = fields.get("data")
                    if not data:
                        continue
                    event = MarketDataSubscriptionEvent.model_validate(json.loads(data))
                    symbols = await self.apply_subscription_event(event)
                    self.logger.info(
                        "market-data subscriptions updated by %s -> %s symbols",
                        event.payload.consumer_name,
                        len(symbols),
                    )

    async def _restore_subscription_state(self) -> None:
        stream = stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions")
        try:
            entries = await self.redis.xrevrange(stream, count=1)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("failed to restore latest market-data subscription state")
            return

        if not entries:
            return

        message_id, fields = entries[0]
        self._subscription_offsets[stream] = message_id
        data = fields.get("data")
        if not data:
            return

        event = MarketDataSubscriptionEvent.model_validate(json.loads(data))
        symbols = {symbol.upper() for symbol in event.payload.symbols if symbol}
        current = self._desired_symbols_by_consumer.get(event.payload.consumer_name, set())
        if event.payload.mode == "replace":
            updated = symbols
        elif event.payload.mode == "add":
            updated = current | symbols
        else:
            updated = current - symbols

        self._desired_symbols_by_consumer[event.payload.consumer_name] = updated
        self._active_symbols = set().union(*self._desired_symbols_by_consumer.values())
        self.logger.info(
            "restored market-data subscriptions from stream -> %s symbols",
            len(self._active_symbols),
        )

    async def _stream_publish_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                trade = await asyncio.wait_for(self._trade_queue.get(), timeout=1.0)
                await self.publisher.publish_trade_tick(trade)
            except TimeoutError:
                pass

            while not self._bar_queue.empty():
                bar = await self._bar_queue.get()
                await self.publisher.publish_live_bar(bar)

            while not self._quote_queue.empty():
                quote = await self._quote_queue.get()
                await self.publisher.publish_quote_tick(quote)

    async def _heartbeat_loop(self, stop_event: asyncio.Event) -> None:
        interval = max(1, self.settings.service_heartbeat_interval_seconds)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                await self.publisher.publish_heartbeat(
                    instance_name=self.instance_name,
                    status="healthy",
                    details={
                        "reference_tickers": str(self.reference_cache.ticker_count()),
                        "active_symbols": str(len(self._active_symbols)),
                    },
                )

    async def _publish_historical_warmup(self, symbols: Iterable[str]) -> None:
        normalized = sorted({symbol.upper() for symbol in symbols if symbol})
        if not normalized or not self.settings.market_data_warmup_enabled:
            return

        for symbol in normalized:
            for interval_secs in WARMUP_INTERVALS:
                try:
                    bars = await asyncio.to_thread(
                        self.snapshot_provider.fetch_historical_bars,
                        symbol,
                        interval_secs=interval_secs,
                        lookback_calendar_days=self.settings.market_data_warmup_lookback_days,
                        limit=self.settings.market_data_warmup_bar_limit,
                    )
                except Exception:
                    self.logger.exception(
                        "historical warmup fetch failed for %s @ %ss",
                        symbol,
                        interval_secs,
                    )
                    continue

                if not bars:
                    continue

                await self.publisher.publish_historical_bars(
                    HistoricalBarsRecord(
                        symbol=symbol,
                        interval_secs=interval_secs,
                        bars=tuple(bars),
                    )
                )
