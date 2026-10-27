"""G1 P2 byte parity. Synthetic input only; no native connections or execution receipt."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from project_mai_tai.market_data import publisher as publisher_module
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.market_data.models import SnapshotRecord
from project_mai_tai.market_data.publisher import MarketDataPublisher
from project_mai_tai.market_data.snapshot_worker import SnapshotWorkerService
from project_mai_tai.settings import Settings
from tests.unit.test_market_data_gateway import FakeReferenceCache, FakeSnapshotProvider


class BytesRedis:
    def __init__(self):
        self.entries = []
        self.hashes = {}

    async def xadd(self, stream, fields, **kwargs):
        self.entries.append((stream, dict(fields), kwargs))
        return "1-0"

    async def hset(self, key, *, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def aclose(self):
        pass


def freeze_envelope(monkeypatch, now):
    original_event = publisher_module.SnapshotBatchEvent
    original_payload = publisher_module.SnapshotBatchPayload
    monkeypatch.setattr(publisher_module, "SnapshotBatchPayload",
                        lambda **kwargs: original_payload(completed_at=now, **kwargs))
    monkeypatch.setattr(publisher_module, "SnapshotBatchEvent", lambda **kwargs: original_event(
        event_id=UUID("12345678-1234-4234-8234-123456789abc"), produced_at=now, **kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [11, 15, 23])
@pytest.mark.parametrize("size", [0, 2, 13_000])
async def test_p2_old_gateway_and_worker_publish_identical_bytes(monkeypatch, hour, size):
    freeze_envelope(monkeypatch, datetime(2026, 10, 22, hour, tzinfo=UTC))
    settings = Settings(redis_stream_prefix="l13-test", market_data_warmup_enabled=False)
    rows = [SnapshotRecord(symbol="UGRO" if index == 0 else f"T{index}",
                           previous_close=2.1, day_close=2.35, day_volume=900_000,
                           minute_close=2.34, last_trade_price=2.36, bid_price=2.35,
                           ask_price=2.37, updated_ns=1_792_000_000_000_000_000)
            for index in range(size)]
    legacy_redis, worker_redis = BytesRedis(), BytesRedis()
    # Original690 method path, with no gateway/client constructor and a fixed envelope.
    legacy = MarketDataGatewayService.__new__(MarketDataGatewayService)
    legacy.reference_cache = FakeReferenceCache()
    legacy.publisher = MarketDataPublisher(legacy_redis, settings.redis_stream_prefix,
        "market-data-gateway", snapshot_batch_stream_maxlen=settings.redis_snapshot_batch_stream_maxlen)
    legacy_count = await legacy.publish_snapshot_batch_once(iter(rows))
    worker = SnapshotWorkerService(settings, worker_redis,
        snapshot_provider=FakeSnapshotProvider(), reference_cache=FakeReferenceCache())
    worker_count = await worker.publish_snapshot_batch_once(iter(rows))
    assert legacy_count == worker_count == size
    assert worker_redis.entries == legacy_redis.entries
    assert len(worker_redis.entries) == 1
    assert worker_redis.entries[0][0] == "l13-test:snapshot-batches"
    assert worker._snapshots_without_reference == legacy._snapshots_without_reference
    assert worker._last_snapshot_symbol_count == legacy._last_snapshot_symbol_count == size
    assert worker_redis.hashes["l13-test:snapshot-worker-health"] == {
        "reference_tickers": "1", "snapshots_without_reference": str(max(0, size - 1)),
        "last_snapshot_symbols": str(size)}


@pytest.mark.asyncio
async def test_health_failure_does_not_turn_a_published_batch_into_a_poll_failure(monkeypatch):
    class BrokenHealth(BytesRedis):
        async def hset(self, *args, **kwargs):
            raise RuntimeError("synthetic health unavailable")

    redis = BrokenHealth()
    worker = SnapshotWorkerService(Settings(), redis,
        snapshot_provider=FakeSnapshotProvider(), reference_cache=FakeReferenceCache())
    assert await worker.publish_snapshot_batch_once([SnapshotRecord("UGRO")]) == 1
    assert len(redis.entries) == 1
