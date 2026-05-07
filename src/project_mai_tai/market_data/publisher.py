from __future__ import annotations

from collections.abc import Iterable

from redis.asyncio import Redis

from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    HistoricalBarsEvent,
    LiveBarEvent,
    QuoteTickEvent,
    ReferenceDataPayload,
    SnapshotBatchEvent,
    SnapshotBatchPayload,
    TradeTickEvent,
    stream_name,
)
from project_mai_tai.market_data.models import (
    HistoricalBarsRecord,
    LiveBarRecord,
    QuoteTickRecord,
    SnapshotRecord,
    TradeTickRecord,
)


class MarketDataPublisher:
    def __init__(
        self,
        redis: Redis,
        stream_prefix: str,
        service_name: str,
        *,
        snapshot_batch_stream_maxlen: int = 4,
        market_data_stream_maxlen: int = 10_000,
        heartbeat_stream_maxlen: int = 1_000,
    ):
        self.redis = redis
        self.stream_prefix = stream_prefix
        self.service_name = service_name
        self.snapshot_batch_stream_maxlen = max(1, snapshot_batch_stream_maxlen)
        self.market_data_stream_maxlen = max(100, market_data_stream_maxlen)
        self.heartbeat_stream_maxlen = max(10, heartbeat_stream_maxlen)

    async def publish_snapshot_batch(
        self,
        snapshots: Iterable[SnapshotRecord],
        reference_data: Iterable[ReferenceDataPayload],
    ) -> str:
        event = SnapshotBatchEvent(
            source_service=self.service_name,
            payload=SnapshotBatchPayload(
                snapshots=[snapshot.to_payload() for snapshot in snapshots],
                reference_data=list(reference_data),
            ),
        )
        return await self.redis.xadd(
            stream_name(self.stream_prefix, "snapshot-batches"),
            {"data": event.model_dump_json()},
            maxlen=self.snapshot_batch_stream_maxlen,
            approximate=True,
        )

    async def publish_trade_tick(self, record: TradeTickRecord) -> str:
        event = TradeTickEvent(
            source_service=self.service_name,
            payload=record.to_payload(),
        )
        return await self.redis.xadd(
            stream_name(self.stream_prefix, "market-data"),
            {"data": event.model_dump_json()},
            maxlen=self.market_data_stream_maxlen,
            approximate=True,
        )

    async def publish_quote_tick(self, record: QuoteTickRecord) -> str:
        event = QuoteTickEvent(
            source_service=self.service_name,
            payload=record.to_payload(),
        )
        return await self.redis.xadd(
            stream_name(self.stream_prefix, "market-data"),
            {"data": event.model_dump_json()},
            maxlen=self.market_data_stream_maxlen,
            approximate=True,
        )

    async def publish_live_bar(self, record: LiveBarRecord) -> str:
        event = LiveBarEvent(
            source_service=self.service_name,
            payload=record.to_payload(),
        )
        return await self.redis.xadd(
            stream_name(self.stream_prefix, "market-data"),
            {"data": event.model_dump_json()},
            maxlen=self.market_data_stream_maxlen,
            approximate=True,
        )

    async def publish_historical_bars(self, record: HistoricalBarsRecord) -> str:
        event = HistoricalBarsEvent(
            source_service=self.service_name,
            payload=record.to_payload(),
        )
        return await self.redis.xadd(
            stream_name(self.stream_prefix, "market-data"),
            {"data": event.model_dump_json()},
            maxlen=self.market_data_stream_maxlen,
            approximate=True,
        )

    async def publish_heartbeat(
        self,
        *,
        instance_name: str,
        status: str,
        details: dict[str, str] | None = None,
    ) -> str:
        event = HeartbeatEvent(
            source_service=self.service_name,
            payload=HeartbeatPayload(
                service_name=self.service_name,
                instance_name=instance_name,
                status=status,
                details=details or {},
            ),
        )
        return await self.redis.xadd(
            stream_name(self.stream_prefix, "heartbeats"),
            {"data": event.model_dump_json()},
            maxlen=self.heartbeat_stream_maxlen,
            approximate=True,
        )
