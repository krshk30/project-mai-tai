"""Late-universe snapshot volume reaches the unchanged live admission gates."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.events import MarketSnapshotPayload, SnapshotBatchEvent, SnapshotBatchPayload
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_paper_service import _condition_snapshot
from tests.unit.test_momlive1_handoff import row


NOW = datetime(2026, 10, 15, 14, 17, 46, tzinfo=UTC)


def service(now):
    result = MomentumPaperService(
        Settings(_env_file=None, momentum_live_enabled=True,
                 market_data_subscription_startup_enabled=False),
        redis_client=AsyncMock(), clock=lambda: now[0],
    )
    result._engine = MomentumPaperEngine(prior_closes={}, condition_version="controlled",
                                         coverage_started_ms=0)
    result._condition_snapshot = _condition_snapshot()
    result._persist = AsyncMock()
    result._live_handoff.ledger.ready = True
    return result


async def snapshot(target, now, *, count=4097, day=109055, accumulated=108897):
    symbols = [MarketSnapshotPayload(symbol=f"S{i}", day_volume=1) for i in range(count - 1)]
    symbols.append(MarketSnapshotPayload(symbol="RYOJ", day_volume=day,
                                          minute_accumulated_volume=accumulated))
    event = SnapshotBatchEvent(source_service="market-data-gateway",
                               payload=SnapshotBatchPayload(completed_at=now, snapshots=symbols))
    await target._handle_gateway_event(event.model_dump_json())


def offer(target, now):
    observed = int(now.timestamp() * 1_000_000_000)
    detected = row(symbol="RYOJ", session_date=now.date(), payload={
        "observed_decision_ns": observed, "reference": {"end_ms": 1},
        "detect": {"end_ms": 3, "price": "1.7341"},
    })
    target._offer_live_signals([detected], observed)


@pytest.mark.asyncio
async def test_full_16486_batch_caches_late_warned_symbol_and_admits():
    now = [NOW]
    target = service(now)
    await snapshot(target, now[0], count=16486)
    assert len(target._live_day_volumes) == 16486
    assert target._live_day_volumes["RYOJ"] == (109055, now[0])
    offer(target, now[0])
    assert target._live_handoff.counts["offered"] == 1
    assert target._live_handoff.queue._queue[0][1].day_volume == 109055
    target.redis.xadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_empty_cache_recovers_late_warned_symbol_on_next_batch():
    now = [NOW]
    before = service(now)
    await snapshot(before, now[0], count=16486)
    restarted = service(now)
    assert not restarted._live_day_volumes
    await snapshot(restarted, now[0], count=16486)
    assert restarted._live_day_volumes["RYOJ"] == before._live_day_volumes["RYOJ"]
    offer(restarted, now[0])
    assert restarted._live_handoff.counts["offered"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("day,accumulated,expected,reason", [
    (0, 483557, 483557, None),
    (900000, 483557, 900000, None),
    (109055, 108897, 109055, None),
    (0, 0, 0, "day_volume_below_50000"),
    (None, None, None, "day_volume_unknown"),
    (49999, 40000, 49999, "day_volume_below_50000"),
])
async def test_late_symbol_keeps_max_volume_unknown_and_floor(day, accumulated, expected, reason):
    now = [NOW]
    target = service(now)
    await snapshot(target, now[0], day=day, accumulated=accumulated)
    assert target._live_day_volumes["RYOJ"] == (expected, now[0])
    offer(target, now[0])
    if reason:
        assert target._live_handoff.counts[reason] == 1
        assert target._live_handoff.queue.empty()
        assert not target._live_handoff.ledger.episodes
    else:
        assert target._live_handoff.counts["offered"] == 1
        assert target._live_handoff.queue._queue[0][1].day_volume == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [16, -1])
async def test_stale_or_future_snapshot_is_not_cached(age):
    now = [NOW]
    target = service(now)
    await snapshot(target, now[0] - timedelta(seconds=age))
    assert not target._live_day_volumes
    offer(target, now[0])
    assert target._live_handoff.counts["day_volume_unknown"] == 1
    assert target._live_handoff.queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [timedelta(seconds=16), timedelta(days=1)])
async def test_expired_or_prior_date_cached_volume_still_refuses(age):
    now = [NOW]
    target = service(now)
    await snapshot(target, now[0])
    assert target._live_day_volumes["RYOJ"][0] == 109055
    now[0] += age
    offer(target, now[0])
    assert target._live_handoff.counts["day_volume_unknown"] == 1
    assert target._live_handoff.queue.empty()
    assert not target._live_handoff.ledger.episodes
