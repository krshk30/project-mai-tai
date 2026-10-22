"""Independent REST snapshot process; never constructs a websocket or gateway.

The existing snapshot methods are reused verbatim to preserve enrichment,
serialization, cadence and logging. Only this process schedules those jobs.
"""

from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis

from project_mai_tai.events import stream_name
from project_mai_tai.market_data.gateway import MarketDataGatewayService, SERVICE_NAME
from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider
from project_mai_tai.market_data.publisher import MarketDataPublisher
from project_mai_tai.market_data.reference_cache import ReferenceDataCache
from project_mai_tai.market_data.stage_diagnostics import StageDiagnostics
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.settings import get_settings


SNAPSHOT_HEALTH_TOPIC = "snapshot-worker-health"


class SnapshotWorkerService:
    # Compatibility algorithm reuse, NOT a constructed gateway/service subclass.
    # Importing its class does not construct MassiveTradeStream or any client.
    _timings = MarketDataGatewayService._timings
    _snapshot_loop = MarketDataGatewayService._snapshot_loop
    _reference_refresh_loop = MarketDataGatewayService._reference_refresh_loop

    def __init__(self, settings=None, redis_client=None, *, snapshot_provider=None, reference_cache=None):
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        if snapshot_provider is None:
            if not self.settings.massive_api_key:
                raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is required for snapshot polling.")
            snapshot_provider = MassiveSnapshotProvider(self.settings.massive_api_key)
        self.snapshot_provider = snapshot_provider
        self.reference_cache = reference_cache or ReferenceDataCache(
            self.snapshot_provider,
            cache_path=self.settings.market_data_reference_cache_path,
            max_age_hours=self.settings.market_data_reference_cache_max_age_hours,
            min_price=self.settings.market_data_scan_min_price,
            max_price=self.settings.market_data_scan_max_price,
            lookback_days=self.settings.market_data_reference_lookback_days,
        )
        # Keep the original envelope source_service, stream, schema and maxlen.
        self.publisher = MarketDataPublisher(
            self.redis, self.settings.redis_stream_prefix, SERVICE_NAME,
            snapshot_batch_stream_maxlen=self.settings.redis_snapshot_batch_stream_maxlen,
        )
        self.logger = logging.getLogger(SERVICE_NAME)
        self._stage_diagnostics = StageDiagnostics()
        self.publisher.stage_diagnostics = self._stage_diagnostics
        if isinstance(self.snapshot_provider, MassiveSnapshotProvider):
            self.snapshot_provider.stage_diagnostics = self._stage_diagnostics
        self._snapshots_without_reference = 0
        self._last_snapshot_symbol_count = 0

    async def _publish_snapshot_health(self):
        # Small counters only. Never send whole-market records through the gateway.
        try:
            await asyncio.wait_for(self.redis.hset(
                stream_name(self.settings.redis_stream_prefix, SNAPSHOT_HEALTH_TOPIC), mapping={
                    "reference_tickers": str(self.reference_cache.ticker_count()),
                    "snapshots_without_reference": str(self._snapshots_without_reference),
                    "last_snapshot_symbols": str(self._last_snapshot_symbol_count),
                }), timeout=1.)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.warning("snapshot worker counters unavailable; batch stream remains authoritative")

    async def _ensure_reference_data(self):
        await MarketDataGatewayService._ensure_reference_data(self)
        await self._publish_snapshot_health()

    async def publish_snapshot_batch_once(self, snapshots):
        count = await MarketDataGatewayService.publish_snapshot_batch_once(self, snapshots)
        await self._publish_snapshot_health()
        return count

    async def run(self, stop_event=None):
        owned_stop = stop_event is None
        stop_event = stop_event if stop_event is not None else asyncio.Event()
        if owned_stop:
            _install_signal_handlers(stop_event)
        tasks = []
        waiter = None
        try:
            await self._ensure_reference_data()
            tasks = [
                asyncio.create_task(self._snapshot_loop(stop_event), name="snapshot-worker-poll"),
                asyncio.create_task(self._reference_refresh_loop(stop_event), name="snapshot-worker-reference"),
            ]
            waiter = asyncio.create_task(stop_event.wait())
            # A disabled periodic reference loop may return normally. Polling must
            # not disappear silently; process failure belongs to systemd restart.
            done, _ = await asyncio.wait((tasks[0], waiter), return_when=asyncio.FIRST_COMPLETED)
            if tasks[0] in done and not stop_event.is_set():
                if tasks[0].cancelled():
                    raise RuntimeError("snapshot polling cancelled unexpectedly")
                error = tasks[0].exception()
                if error is not None:
                    raise error
                raise RuntimeError("snapshot polling returned unexpectedly")
        finally:
            for task in [*tasks, *([waiter] if waiter is not None else [])]:
                task.cancel()
            await asyncio.gather(*tasks, *([waiter] if waiter is not None else []), return_exceptions=True)
            await self.redis.aclose()
