from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AccountPosition, VirtualPosition
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.services.strategy_engine_app import StrategyEngineService
from project_mai_tai.settings import Settings


def fixed_now() -> datetime:
    return datetime(2026, 3, 28, 10, 0)


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def aclose(self) -> None:
        return None


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed_trending_bars(
    start_price: float = 2.0,
    count: int = 50,
    *,
    start_timestamp: float = 1_700_000_000.0,
    interval_secs: int = 30,
) -> list[dict[str, float | int]]:
    bars = []
    for index in range(count):
        close = start_price + index * 0.01
        bars.append(
            {
                "open": close - 0.01,
                "high": close + 0.02,
                "low": close - 0.02,
                "close": close,
                "volume": 20_000 + index * 50,
                "timestamp": start_timestamp + index * interval_secs,
            }
        )
    return bars


@pytest.mark.asyncio
async def test_strategy_and_oms_roundtrip_opens_positions_across_services(monkeypatch) -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    strategy_service = StrategyEngineService(
        settings=Settings(redis_stream_prefix="itest", dashboard_snapshot_persistence_enabled=False),
        redis_client=redis,
    )
    oms_service = OmsRiskService(
        settings=Settings(redis_stream_prefix="itest", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    bot = strategy_service.state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    strategy_service.state.seed_bars("macd_30s", "UGRO", seed_trending_bars())
    monkeypatch.setattr(bot.indicator_engine, "calculate", lambda bars: {"price": 2.8})
    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda symbol, indicators, bar_index, position_tracker: {
            "action": "BUY",
            "ticker": symbol,
            "path": "P1_MACD_CROSS",
            "price": 2.8,
            "score": 5,
            "score_details": "hist+ stK+ vwap+ vol+ macd+ emas-",
        },
    )

    intents = strategy_service.state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_001_500_000_000_000,
    )
    assert len(intents) == 1

    order_events = await oms_service.process_trade_intent(intents[0])
    for order_event in order_events:
        await strategy_service._handle_stream_message(
            "itest:order-events",
            {"data": order_event.model_dump_json()},
        )

    position = strategy_service.state.bots["macd_30s"].positions.get_position("UGRO")
    assert position is not None
    assert position.quantity == 10
    assert position.entry_price == 2.8

    with session_factory() as session:
        virtual_position = session.scalar(select(VirtualPosition))
        account_position = session.scalar(select(AccountPosition))

        assert virtual_position is not None
        assert virtual_position.quantity == Decimal("10")
        assert account_position is not None
        assert account_position.quantity == Decimal("10")
