from __future__ import annotations

import json

import pytest

from project_mai_tai.market_data.models import QuoteTickRecord, TradeTickRecord
from project_mai_tai.services.strategy_engine_app import StrategyEngineService
from project_mai_tai.settings import Settings


class _DummyRedis:
    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_strategy_service_archives_schwab_ticks_and_uses_configured_quantity(tmp_path) -> None:
    settings = Settings(
        redis_stream_prefix="test",
        schwab_tick_archive_enabled=True,
        schwab_tick_archive_root=str(tmp_path),
        strategy_macd_30s_default_quantity=10,
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
    )
    service = StrategyEngineService(settings=settings, redis_client=_DummyRedis())

    assert service.state.bots["macd_30s"].definition.trading_config.default_quantity == 10

    await service._schwab_quote_queue.put(
        QuoteTickRecord(symbol="EFOI", bid_price=6.10, ask_price=6.12, bid_size=100, ask_size=200)
    )
    await service._schwab_trade_queue.put(
        TradeTickRecord(
            symbol="EFOI",
            price=6.1005,
            size=3,
            timestamp_ns=1_776_470_399_366_000_000,
            cumulative_volume=167_170_015,
        )
    )

    await service._drain_schwab_stream_queues()
    service._schwab_tick_archive.close()

    files = list(tmp_path.rglob("*.jsonl"))
    assert files
    rows = []
    for path in files:
        if path.name == "EFOI.jsonl":
            rows.extend(
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
    assert [row["event_type"] for row in rows] == ["quote", "trade"]
