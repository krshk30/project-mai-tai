"""Option A contracts: snapshot candidates, capped ownership, trade-only outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.events import (
    MarketSnapshotPayload, SnapshotBatchEvent, SnapshotBatchPayload,
    TradeTickEvent, TradeTickPayload,
)
from project_mai_tai.momentum_paper.conditions import ConditionSnapshot
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-16T{clock}").replace(
        tzinfo=ZoneInfo("America/New_York")
    ).astimezone(UTC)


def _snapshot(symbol: str, price: str, clock: str) -> MarketSnapshotPayload:
    return MarketSnapshotPayload(
        symbol=symbol,
        last_trade_price=Decimal(price),
        last_trade_timestamp_ns=int(_at(clock).timestamp() * 1_000_000_000),
    )


def test_snapshot_batches_detect_but_cannot_fill_without_a_trade_tick() -> None:
    engine = MomentumPaperEngine(
        prior_closes={"MOMO": Decimal("1.00")}, condition_version="fixture",
        coverage_started_ms=int(_at("04:00").timestamp() * 1000),
    )
    first, _ = engine.detect_from_snapshots(
        [_snapshot("MOMO", "1.00", "04:11")],
        completed_at=_at("04:11"), max_symbols=16,
    )
    detected, capped = engine.detect_from_snapshots(
        [_snapshot("MOMO", "1.25", "04:11:25")],
        completed_at=_at("04:11:25"), max_symbols=16,
    )

    assert not first
    assert capped == 0
    assert {row.strategy_code for row in detected} == {"momentum_30s", "momentum_60s"}
    assert {row.event_type for row in detected} == {"DETECTED"}
    assert engine.active_symbols == {"MOMO"}
    assert all(not row.get("fill") for row in engine.active_events)

    later_snapshot, _ = engine.detect_from_snapshots(
        [_snapshot("MOMO", "1.30", "04:11:30")],
        completed_at=_at("04:11:30"), max_symbols=16,
    )
    assert not later_snapshot
    assert all(not row.get("fill") for row in engine.active_events)

    tick_ms = int(_at("04:11:31").timestamp() * 1000)
    fills = engine.ingest(
        TradePrint("MOMO", tick_ms, Decimal("1.30"), 100), allow_detection=False
    )
    assert sum(row.event_type == "FILLED" for row in fills) == 2


def test_snapshot_candidate_cap_and_release_on_no_fill_expiry() -> None:
    symbols = [f"M{i:03d}" for i in range(17)]
    engine = MomentumPaperEngine(
        prior_closes={symbol: Decimal("1.00") for symbol in symbols},
        condition_version="fixture",
        coverage_started_ms=int(_at("04:00").timestamp() * 1000),
    )
    engine.detect_from_snapshots(
        [_snapshot(symbol, "1.00", "04:11") for symbol in symbols],
        completed_at=_at("04:11"), max_symbols=16,
    )
    detected, capped = engine.detect_from_snapshots(
        [_snapshot(symbol, "1.25", "04:11:25") for symbol in symbols],
        completed_at=_at("04:11:25"), max_symbols=16,
    )

    assert len(engine.active_symbols) == 16
    assert len(detected) == 32
    assert capped == 2
    expired = engine.advance_clock(int(_at("04:11:36").timestamp() * 1000))
    assert sum(row.event_type == "NO_FILL" for row in expired) == 32
    assert not engine.active_symbols


def test_gateway_trade_ticks_cannot_reintroduce_global_trade_detection() -> None:
    engine = MomentumPaperEngine(
        prior_closes={"MOMO": Decimal("1.00")}, condition_version="fixture",
        coverage_started_ms=int(_at("04:00").timestamp() * 1000),
    )
    engine.ingest(
        TradePrint("MOMO", int(_at("04:11").timestamp() * 1000), Decimal("1.00"), 10),
        allow_detection=False,
    )
    records = engine.ingest(
        TradePrint("MOMO", int(_at("04:11:25").timestamp() * 1000), Decimal("1.25"), 10),
        allow_detection=False,
    )
    assert all(row.event_type != "DETECTED" for row in records)


@pytest.mark.asyncio
async def test_service_consumes_snapshot_then_only_subscribed_trades_and_releases() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.rows: list[tuple[str, dict[str, object]]] = []

        async def xadd(self, stream: str, fields: dict[str, str], **_kwargs) -> None:
            import json

            self.rows.append((stream, json.loads(fields["data"])))

    class FakeStore:
        def __init__(self) -> None:
            self.records = []

        def append_many(self, records) -> None:
            self.records.extend(records)

    now = [_at("04:11")]
    redis = FakeRedis()
    store = FakeStore()
    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True, redis_stream_prefix="test"),
        redis_client=redis, store=store, clock=lambda: now[0],  # type: ignore[arg-type]
    )
    service._engine = MomentumPaperEngine(
        prior_closes={"MOMO": Decimal("1.00")}, condition_version="fixture",
        coverage_started_ms=int(_at("04:00").timestamp() * 1000),
    )
    service._condition_snapshot = ConditionSnapshot(retrieved_at=now[0], rules={})

    for clock, price in (("04:11", "1.00"), ("04:11:25", "1.25")):
        now[0] = _at(clock)
        event = SnapshotBatchEvent(
            source_service="market-data-gateway",
            payload=SnapshotBatchPayload(
                snapshots=[_snapshot("MOMO", price, clock)], completed_at=now[0],
            ),
        )
        await service._handle_gateway_event(event.model_dump_json())

    assert service._subscribed_symbols == {"MOMO"}
    subscriptions = [row for stream, row in redis.rows if stream.endswith("subscriptions")]
    assert len(subscriptions) == 1
    assert subscriptions[0]["payload"] == {
        "consumer_name": "momentum-paper", "mode": "replace", "symbols": ["MOMO"]
    }
    assert sum(row.event_type == "DETECTED" for row in store.records) == 2

    now[0] = _at("04:11:26")
    old_gateway_tick = TradeTickEvent(
        source_service="market-data-gateway",
        payload=TradeTickPayload(
            symbol="MOMO", price=Decimal("1.30"), size=100,
            timestamp_ns=int(now[0].timestamp() * 1_000_000_000),
        ),
    )
    await service._handle_gateway_event(old_gateway_tick.model_dump_json())
    assert sum(row.event_type == "FILLED" for row in store.records) == 0
    assert service._condition_feed_verified is False

    now[0] = _at("04:11:27")
    valid_tick = TradeTickEvent(
        source_service="market-data-gateway",
        payload=TradeTickPayload(
            symbol="MOMO", price=Decimal("1.30"), size=100,
            timestamp_ns=int(now[0].timestamp() * 1_000_000_000),
            conditions_present=True,
        ),
    )
    await service._handle_gateway_event(valid_tick.model_dump_json())
    assert sum(row.event_type == "FILLED" for row in store.records) == 2
    assert service._condition_feed_verified is True

    await service._stop_gateway()
    subscriptions = [row for stream, row in redis.rows if stream.endswith("subscriptions")]
    assert subscriptions[-1]["payload"]["symbols"] == []


@pytest.mark.asyncio
async def test_no_fill_expiry_publishes_replace_empty_for_momentum_owner() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.rows: list[dict[str, object]] = []

        async def xadd(self, _stream: str, fields: dict[str, str], **_kwargs) -> None:
            import json

            self.rows.append(json.loads(fields["data"]))

    now = [_at("04:11:25")]
    redis = FakeRedis()
    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True, redis_stream_prefix="test"),
        redis_client=redis, clock=lambda: now[0],  # type: ignore[arg-type]
    )
    engine = MomentumPaperEngine(
        prior_closes={"MOMO": Decimal("1.00")}, condition_version="fixture",
        coverage_started_ms=int(_at("04:00").timestamp() * 1000),
    )
    engine.detect_from_snapshots(
        [_snapshot("MOMO", "1.00", "04:11")],
        completed_at=_at("04:11"), max_symbols=16,
    )
    engine.detect_from_snapshots(
        [_snapshot("MOMO", "1.25", "04:11:25")],
        completed_at=now[0], max_symbols=16,
    )
    service._engine = engine
    await service._sync_gateway_subscriptions()
    assert redis.rows[-1]["payload"]["symbols"] == ["MOMO"]

    now[0] = _at("04:11:36")
    engine.advance_clock(int(now[0].timestamp() * 1000))
    await service._sync_gateway_subscriptions()

    assert redis.rows[-1]["payload"]["consumer_name"] == "momentum-paper"
    assert redis.rows[-1]["payload"]["symbols"] == []
