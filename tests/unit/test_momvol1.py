"""Controlled actual snapshot -> paper cache -> handoff -> admission path."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.events import MarketSnapshotPayload, SnapshotBatchEvent, SnapshotBatchPayload
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_paper_service import _condition_snapshot
from tests.unit.test_momlive1_handoff import row


@pytest.mark.asyncio
@pytest.mark.parametrize("day,accumulated,age,expected,reason", [
    (0, 483557, 0, 483557, None),
    (0, 0, 0, None, "day_volume_below_50000"),
    (900000, 483557, 0, 900000, None),
    (0, 483557, 16, None, "day_volume_unknown"),
], ids=["VBIO-premarket", "both-zero-refused", "RTH-day-greater", "stale-refused"])
async def test_snapshot_volume_reaches_actual_live_admission(day, accumulated, age, expected, reason):
    now = [datetime(2026, 10, 15, 12, 2, 14, tzinfo=UTC)]
    service = MomentumPaperService(
        Settings(_env_file=None, momentum_live_enabled=True,
                 market_data_subscription_startup_enabled=False),
        redis_client=AsyncMock(), clock=lambda: now[0],
    )
    service._engine = MomentumPaperEngine(
        prior_closes={}, condition_version="controlled", coverage_started_ms=0,
    )
    service._condition_snapshot = _condition_snapshot()
    service._persist = AsyncMock()
    service._live_handoff.ledger.ready = True
    event = SnapshotBatchEvent(source_service="market-data-gateway", payload=SnapshotBatchPayload(
        completed_at=now[0], snapshots=[MarketSnapshotPayload(
            symbol="VBIO", day_volume=day, minute_accumulated_volume=accumulated,
        )],
    ))
    await service._handle_gateway_event(event.model_dump_json())
    assert service._live_day_volumes["VBIO"] == (max(day, accumulated), now[0])
    now[0] += timedelta(seconds=age)
    observed = int(now[0].timestamp() * 1_000_000_000)
    detected = row(symbol="VBIO", session_date=now[0].date(), payload={
        "prior_close": "1.50",  # CONTROLLED volume-path input, not historical VBIO proof.
        "observed_decision_ns": observed,
        "reference": {"end_ms": 1}, "detect": {"end_ms": 3, "price": "1.7341"},
    })
    service._offer_live_signals([detected], observed)
    target = service._live_handoff
    if reason is None:
        assert target.counts["offered"] == 1
        assert target.queue.qsize() == 1
        assert target.queue._queue[0][1].day_volume == expected
        assert len(target.ledger.episodes) == 1
    else:
        assert target.counts[reason] == 1
        assert target.queue.empty()
        assert not target.ledger.episodes
    service.redis.xadd.assert_not_awaited()
