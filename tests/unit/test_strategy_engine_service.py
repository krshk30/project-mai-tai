from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.events import (
    HistoricalBarPayload,
    HistoricalBarsEvent,
    HistoricalBarsPayload,
    MarketSnapshotPayload,
    OrderEventEvent,
    OrderEventPayload,
)
from project_mai_tai.services.strategy_engine_app import StrategyEngineService, StrategyEngineState, snapshot_from_payload
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import ReferenceData


def fixed_now() -> datetime:
    return datetime(2026, 3, 28, 10, 0)


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.entries.append((stream, fields["data"]))
        return "1-0"


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_snapshot_payload(*, symbol: str, price: float, volume: int) -> MarketSnapshotPayload:
    return MarketSnapshotPayload(
        symbol=symbol,
        day_close=Decimal("2.10"),
        day_volume=volume,
        day_high=Decimal(str(price)),
        day_vwap=Decimal("2.22"),
        minute_close=Decimal(str(price)),
        minute_accumulated_volume=volume,
        minute_high=Decimal(str(price)),
        minute_vwap=Decimal("2.22"),
        last_trade_price=Decimal(str(price)),
        todays_change_percent=Decimal("12.5"),
    )


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


def test_snapshot_batch_keeps_single_confirmed_name_in_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.4,
            "change_pct": 12.5,
            "volume": 900_000,
            "rvol": 6.2,
            "shares_outstanding": 50_000,
            "bid": 2.39,
            "ask": 2.40,
            "spread": 0.01,
            "spread_pct": 0.42,
            "hod": 2.45,
            "vwap": 2.31,
            "prev_close": 2.13,
            "avg_daily_volume": 390_000,
            "first_spike_time": "09:55:00 AM ET",
            "first_spike_price": 2.10,
            "squeeze_count": 2,
            "data_age_secs": 0,
            "confirmation_path": "PATH_B_2SQ",
        }
    ]
    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )

    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.4, volume=900_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert summary["watchlist"] == ["UGRO"]
    assert summary["top_confirmed"][0]["rank_score"] == 0.0
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"UGRO"}


def test_snapshot_batch_keeps_runner_aligned_to_visible_confirmed_names(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    visible_confirmed = [
        {
            "ticker": "ELAB",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 3.19,
            "price": 2.78,
            "change_pct": 66.5,
            "volume": 7_200_000,
            "rvol": 12.0,
            "shares_outstanding": 541_500,
            "bid": 2.76,
            "ask": 2.77,
            "spread": 0.01,
            "spread_pct": 0.36,
            "hod": 3.19,
            "vwap": 2.81,
            "prev_close": 1.67,
            "avg_daily_volume": 600_000,
            "first_spike_time": "09:45:00 AM ET",
            "first_spike_price": 2.20,
            "squeeze_count": 3,
            "data_age_secs": 0,
            "confirmation_path": "PATH_B_2SQ",
            "rank_score": 75.0,
        }
    ]
    hidden_confirmed = [
        *visible_confirmed,
        {
            "ticker": "ABCD",
            "rank_score": 20.0,
            "change_pct": 18.0,
            "confirmed_at": "09:40:00 AM ET",
            "confirmation_path": "PATH_B_2SQ",
        },
        {
            "ticker": "WXYZ",
            "rank_score": 15.0,
            "change_pct": 14.0,
            "confirmed_at": "09:41:00 AM ET",
            "confirmation_path": "PATH_B_2SQ",
        },
        {
            "ticker": "MNOP",
            "rank_score": 10.0,
            "change_pct": 11.0,
            "confirmed_at": "09:42:00 AM ET",
            "confirmation_path": "PATH_A_NEWS",
        },
    ]

    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_all_confirmed",
        lambda: list(hidden_confirmed),
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_top_n",
        lambda *args, **kwargs: list(visible_confirmed),
    )

    summary = state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.78, volume=7_200_000))],
        {"ELAB": ReferenceData(shares_outstanding=541_500, avg_daily_volume=600_000)},
    )

    assert summary["watchlist"] == ["ELAB"]
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"ELAB"}
    assert state.bots["runner"]._candidates == {"ELAB": visible_confirmed[0]}


def test_snapshot_batch_releases_removed_symbols_from_all_bot_watchlists(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    first_confirmed = [
        {"ticker": "ELAB", "rank_score": 80.0, "change_pct": 40.0, "confirmed_at": "09:45:00 AM ET"},
        {"ticker": "UGRO", "rank_score": 70.0, "change_pct": 32.0, "confirmed_at": "09:50:00 AM ET"},
    ]
    second_confirmed = [
        {"ticker": "ELAB", "rank_score": 82.0, "change_pct": 42.0, "confirmed_at": "09:45:00 AM ET"}
    ]
    current_all = {"value": list(first_confirmed)}
    current_top = {"value": list(first_confirmed)}

    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_all_confirmed",
        lambda: list(current_all["value"]),
    )
    monkeypatch.setattr(
        state.confirmed_scanner,
        "get_top_n",
        lambda *args, **kwargs: list(current_top["value"]),
    )

    state.process_snapshot_batch(
        [
            snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.78, volume=7_200_000)),
            snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.40, volume=900_000)),
        ],
        {
            "ELAB": ReferenceData(shares_outstanding=541_500, avg_daily_volume=600_000),
            "UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000),
        },
    )

    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"ELAB", "UGRO"}

    current_all["value"] = list(second_confirmed)
    current_top["value"] = list(second_confirmed)

    state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.82, volume=7_400_000))],
        {"ELAB": ReferenceData(shares_outstanding=541_500, avg_daily_volume=600_000)},
    )

    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == {"ELAB"}
    assert state.bots["runner"]._candidates == {"ELAB": second_confirmed[0]}


def test_trade_tick_generates_open_intent_for_confirmed_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )
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

    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_001_500_000_000_000,
    )

    assert intents
    open_intents = [intent for intent in intents if intent.payload.intent_type == "open"]
    assert open_intents
    assert open_intents[0].payload.symbol == "UGRO"
    assert open_intents[0].payload.strategy_code == "macd_30s"
    assert "UGRO" in bot.pending_open_symbols


def test_strategy_summary_includes_indicator_snapshots_for_1m_parity(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_1m"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_1m",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=60),
    )
    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 2.8,
            "ema9": 2.7,
            "ema20": 2.55,
            "macd": 0.08231,
            "signal": 0.07411,
            "histogram": 0.0082,
            "vwap": 2.61,
            "macd_above_signal": True,
            "price_above_vwap": True,
            "price_above_ema9": True,
            "price_above_ema20": True,
        },
    )
    monkeypatch.setattr(bot.entry_engine, "check_entry", lambda *args, **kwargs: None)

    state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_003_000_000_000_000,
    )

    summary = state.summary()
    indicator_snapshots = summary["bots"]["macd_1m"]["indicator_snapshots"]
    assert indicator_snapshots
    assert indicator_snapshots[0]["symbol"] == "UGRO"
    assert indicator_snapshots[0]["interval_secs"] == 60
    assert indicator_snapshots[0]["macd_above_signal"] is True


@pytest.mark.asyncio
async def test_order_event_fill_opens_position_and_clears_pending_state() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
        redis_client=redis,
    )
    bot = service.state.bots["macd_30s"]
    bot.pending_open_symbols.add("UGRO")

    order_event = OrderEventEvent(
        source_service="oms-risk",
        payload=OrderEventPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            client_order_id="macd_30s-UGRO-open-abc123",
            symbol="UGRO",
            side="buy",
            intent_type="open",
            status="filled",
            quantity=Decimal("10"),
            filled_quantity=Decimal("10"),
            fill_price=Decimal("2.55"),
            reason="ENTRY_P1_MACD_CROSS",
            metadata={"path": "P1_MACD_CROSS", "reference_price": "2.55"},
        ),
    )

    await service._handle_stream_message(
        "test:order-events",
        {"data": order_event.model_dump_json()},
    )

    position = bot.positions.get_position("UGRO")
    assert position is not None
    assert position.quantity == 10
    assert position.entry_price == 2.55
    assert "UGRO" not in bot.pending_open_symbols
    assert any(stream == "test:strategy-state" for stream, _payload in redis.entries)


@pytest.mark.asyncio
async def test_historical_bars_hydrate_matching_strategy_intervals() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
        redis_client=redis,
    )

    historical_30s = HistoricalBarsEvent(
        source_service="market-data-gateway",
        payload=HistoricalBarsPayload(
            symbol="UGRO",
            interval_secs=30,
            bars=[
                HistoricalBarPayload(
                    open=Decimal("2.00"),
                    high=Decimal("2.10"),
                    low=Decimal("1.99"),
                    close=Decimal("2.05"),
                    volume=20_000,
                    timestamp=1_700_000_000.0,
                ),
                HistoricalBarPayload(
                    open=Decimal("2.05"),
                    high=Decimal("2.15"),
                    low=Decimal("2.04"),
                    close=Decimal("2.12"),
                    volume=22_000,
                    timestamp=1_700_000_030.0,
                ),
            ],
        ),
    )
    historical_5m = HistoricalBarsEvent(
        source_service="market-data-gateway",
        payload=HistoricalBarsPayload(
            symbol="UGRO",
            interval_secs=300,
            bars=[
                HistoricalBarPayload(
                    open=Decimal("2.00"),
                    high=Decimal("2.20"),
                    low=Decimal("1.95"),
                    close=Decimal("2.15"),
                    volume=80_000,
                    timestamp=1_700_000_000.0,
                ),
                HistoricalBarPayload(
                    open=Decimal("2.15"),
                    high=Decimal("2.25"),
                    low=Decimal("2.10"),
                    close=Decimal("2.22"),
                    volume=85_000,
                    timestamp=1_700_000_300.0,
                ),
            ],
        ),
    )

    await service._handle_stream_message("test:market-data", {"data": historical_30s.model_dump_json()})
    await service._handle_stream_message("test:market-data", {"data": historical_5m.model_dump_json()})

    assert len(service.state.bots["macd_30s"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["macd_1m"].builder_manager.get_bars("UGRO")) == 0
    assert len(service.state.bots["tos"].builder_manager.get_bars("UGRO")) == 0
    assert len(service.state.bots["runner"].builder_manager.get_bars("UGRO")) == 2


@pytest.mark.asyncio
async def test_strategy_state_snapshot_persists_last_nonempty_confirmed_snapshot() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = StrategyEngineService(
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=redis,
        session_factory=session_factory,
    )
    service.state.current_confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.4,
            "change_pct": 12.5,
            "volume": 900_000,
            "rvol": 6.2,
            "shares_outstanding": 50_000,
            "bid": 2.39,
            "ask": 2.40,
            "spread": 0.01,
            "spread_pct": 0.42,
            "first_spike_time": "09:55:00 AM ET",
            "squeeze_count": 2,
            "confirmation_path": "PATH_B_2SQ",
            "headline": "Quantum Biopharma Wins Hospital Supply Agreement",
            "catalyst": "DEAL/CONTRACT",
            "catalyst_type": "DEAL/CONTRACT",
            "sentiment": "bullish",
            "direction": "bullish",
            "news_url": "https://example.com/ugro-news",
            "news_date": "03/27 05:05PM ET",
            "news_window_start": "03/27 04:00PM ET",
            "catalyst_reason": "Bullish DEAL/CONTRACT catalyst across 2 article(s), latest 55m old.",
            "catalyst_confidence": 0.91,
            "article_count": 3,
            "real_catalyst_article_count": 2,
            "freshness_minutes": 55,
            "is_generic_roundup": False,
            "has_real_catalyst": True,
            "path_a_eligible": True,
        }
    ]

    await service._publish_strategy_state_snapshot()

    with session_factory() as session:
        snapshot = session.scalar(
            select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "scanner_confirmed_last_nonempty"
            )
        )

    assert snapshot is not None
    assert snapshot.payload["top_confirmed"][0]["ticker"] == "UGRO"
    assert snapshot.payload["top_confirmed"][0]["headline"] == "Quantum Biopharma Wins Hospital Supply Agreement"
    assert snapshot.payload["top_confirmed"][0]["path_a_eligible"] is True
