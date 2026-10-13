from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import asdict
import json
import re
import time

from redis.asyncio import Redis
from redis.exceptions import ResponseError

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
    MomentumWarningAggregateRecord,
    QuoteTickRecord,
    SnapshotRecord,
    TradeTickRecord,
)


class WarningBatchWriteError(RuntimeError):
    def __init__(self, acknowledged: int, unknown: int = 0):
        super().__init__('warning batch has incomplete or failed acknowledgements')
        self.acknowledged = acknowledged
        self.unknown = unknown


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
        momentum_warning_stream_maxlen: int = 20_000,
    ):
        self.redis = redis
        self.stream_prefix = stream_prefix
        self.service_name = service_name
        self.snapshot_batch_stream_maxlen = max(1, snapshot_batch_stream_maxlen)
        self.market_data_stream_maxlen = max(100, market_data_stream_maxlen)
        self.heartbeat_stream_maxlen = max(10, heartbeat_stream_maxlen)
        self.momentum_warning_stream_maxlen = max(1, momentum_warning_stream_maxlen)

    async def publish_momentum_warning_aggregate(
        self, record: MomentumWarningAggregateRecord, *, sequence: int, dropped_total: int,
        gateway_instance_id: str, generation: int,
    ) -> str:
        payload = {
            **asdict(record), "version": 1, "event_type": "aggregate",
            "sequence": sequence, "dropped_total": dropped_total,
            "gateway_instance_id": gateway_instance_id, "generation": generation,
            "published_utc_ns": time.time_ns(),
        }
        return await self.redis.xadd(
            stream_name(self.stream_prefix, "momentum-warning-bars"),
            {"data": json.dumps(payload, separators=(",", ":"), allow_nan=False)},
            maxlen=self.momentum_warning_stream_maxlen,
            approximate=False,
        )

    async def publish_momentum_warning_metric(self, payload: dict) -> str:
        return await self.redis.xadd(
            stream_name(self.stream_prefix, "momentum-warning-metrics"),
            {"data": json.dumps(payload, separators=(",", ":"), allow_nan=False)},
            maxlen=1000, approximate=False,
        )

    async def publish_momentum_warning_batch(
        self, records: tuple[tuple[int, MomentumWarningAggregateRecord], ...], *, dropped_total: int,
        gateway_instance_id: str, generation: int,
    ) -> int:
        if not 1 <= len(records) <= 32:
            raise ValueError('warning batch bound')
        async with self.redis.pipeline(transaction=False) as pipeline:
            for sequence, record in records:
                payload = {**asdict(record), 'version': 1, 'event_type': 'aggregate',
                           'sequence': sequence, 'dropped_total': dropped_total,
                           'gateway_instance_id': gateway_instance_id, 'generation': generation,
                           'published_utc_ns': time.time_ns()}
                pipeline.xadd(stream_name(self.stream_prefix, 'momentum-warning-bars'),
                              {'data': json.dumps(payload, separators=(',', ':'), allow_nan=False)},
                              maxlen=self.momentum_warning_stream_maxlen, approximate=False)
            replies = await pipeline.execute(raise_on_error=False)
        if not isinstance(replies, list) or len(replies) != len(records):
            raise WarningBatchWriteError(0, len(records))
        acknowledged = sum(isinstance(reply, (str, bytes)) and re.fullmatch(
            rb'\d+-\d+' if isinstance(reply, bytes) else r'\d+-\d+', reply) is not None
            for reply in replies)
        if acknowledged != len(records):
            known_errors = sum(isinstance(reply, ResponseError) for reply in replies)
            raise WarningBatchWriteError(acknowledged, len(records)-acknowledged-known_errors)
        return acknowledged

    async def publish_snapshot_batch(
        self,
        snapshots: Iterable[SnapshotRecord],
        reference_data: Iterable[ReferenceDataPayload],
    ) -> str:
        # Whole-market model construction and JSON serialization must not block
        # watched-name T/Q or the independent A publisher on the gateway loop.
        encoded = await asyncio.to_thread(
            self._encode_snapshot_batch, tuple(snapshots), tuple(reference_data),
        )
        return await self.redis.xadd(
            stream_name(self.stream_prefix, "snapshot-batches"),
            {"data": encoded},
            maxlen=self.snapshot_batch_stream_maxlen,
            approximate=True,
        )

    def _encode_snapshot_batch(
        self, snapshots: Iterable[SnapshotRecord], reference_data: Iterable[ReferenceDataPayload],
    ) -> str:
        event = SnapshotBatchEvent(
            source_service=self.service_name,
            payload=SnapshotBatchPayload(
                snapshots=[snapshot.to_payload() for snapshot in snapshots],
                reference_data=list(reference_data),
            ),
        )
        return event.model_dump_json()

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
