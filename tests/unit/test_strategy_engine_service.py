from __future__ import annotations

from datetime import UTC, datetime
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
    SnapshotBatchEvent,
)
from project_mai_tai.services.strategy_engine_app import (
    StrategyBotRuntime,
    StrategyDefinition,
    StrategyEngineService,
    StrategyEngineState,
    order_routing_metadata,
    snapshot_from_payload,
)
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import IndicatorConfig, ReferenceData, TradingConfig


def fixed_now() -> datetime:
    return datetime(2026, 3, 28, 10, 0)


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []
        self.stream_entries: dict[str, list[tuple[str, dict[str, str]]]] = {}

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.entries.append((stream, fields["data"]))
        self.stream_entries.setdefault(stream, []).insert(0, ("1-0", dict(fields)))
        return "1-0"

    async def xrevrange(self, stream: str, count: int | None = None, **kwargs):
        del kwargs
        entries = list(self.stream_entries.get(stream, []))
        if count is not None:
            entries = entries[:count]
        return entries


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


def test_order_routing_metadata_uses_extended_hours_limit_in_premarket() -> None:
    metadata = order_routing_metadata(
        price="2.55",
        side="buy",
        now=datetime(2026, 3, 31, 7, 0, tzinfo=UTC),
    )

    assert metadata == {
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": "true",
        "limit_price": "2.55",
        "reference_price": "2.55",
        "price_source": "ask",
    }


def test_order_routing_metadata_uses_market_in_regular_session() -> None:
    metadata = order_routing_metadata(
        price="2.55",
        side="buy",
        now=datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
    )

    assert metadata == {}


def test_macd_runtime_uses_quote_anchored_limit_prices_in_extended_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    runtime.update_market_snapshots(
        [
            snapshot_from_payload(
                MarketSnapshotPayload(
                    symbol="KIDZ",
                    last_trade_price=Decimal("3.10"),
                    bid_price=Decimal("3.11"),
                    ask_price=Decimal("3.12"),
                )
            )
        ]
    )
    runtime.positions.open_position("KIDZ", 3.10, quantity=100, path="P1")

    open_intent = runtime._emit_open_intent(
        {"ticker": "KIDZ", "price": 3.10, "path": "P1_MACD_CROSS", "score": 5, "score_details": "x"}
    )
    close_intent = runtime._emit_close_intent({"ticker": "KIDZ", "price": 3.10, "reason": "TEST"})

    assert open_intent.payload.metadata["limit_price"] == "3.12"
    assert open_intent.payload.metadata["price_source"] == "ask"
    assert close_intent.payload.metadata["limit_price"] == "3.11"
    assert close_intent.payload.metadata["price_source"] == "bid"


def test_runtime_blocks_close_retries_after_duplicate_exit_reject() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
    )

    runtime.pending_close_symbols.add("ELAB")
    runtime.apply_order_status(
        symbol="ELAB",
        intent_type="close",
        status="rejected",
        reason="duplicate_exit_in_flight",
    )

    assert "ELAB" not in runtime.pending_close_symbols
    assert runtime._is_exit_retry_blocked("ELAB") is True


def test_snapshot_batch_keeps_single_confirmed_name_in_watchlist(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.confirmed_scanner._confirmed = [
        {
            "ticker": "UGRO",
            "confirmed_at": "10:00:00 AM ET",
            "entry_price": 2.25,
            "price": 2.4,
            "change_pct": 24.5,
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
        [snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.7, volume=900_000))],
        {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert summary["watchlist"] == []
    assert summary["top_confirmed"] == []
    assert state.confirmed_scanner.get_all_confirmed()[0]["rank_score"] == 0.0
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == set()


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


def test_snapshot_batch_prunes_faded_confirmed_symbols_from_all_bot_watchlists() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    state.confirmed_scanner.seed_confirmed_candidates(
        [
            {
                "ticker": "POLA",
                "confirmed_at": "08:00:00 AM ET",
                "entry_price": 2.30,
                "price": 2.32,
                "change_pct": 24.0,
                "volume": 900_000,
                "rvol": 8.0,
                "shares_outstanding": 1_000_000,
                "bid": 2.31,
                "ask": 2.32,
                "spread": 0.01,
                "spread_pct": 0.43,
                "first_spike_time": "07:45:00 AM ET",
                "squeeze_count": 2,
                "confirmation_path": "PATH_B_2SQ",
                "rank_score": 72.0,
                "prev_close": 2.0,
            }
        ]
    )
    state.confirmed_scanner._tracking["POLA"] = {
        "has_volume_spike": True,
        "first_spike_time": "07:45:00 AM ET",
        "first_spike_price": 2.1,
        "first_spike_volume": 500_000,
        "squeezes": [{"time": "08:00:00 AM ET", "price": 2.32, "volume": 900_000}],
        "confirmed": True,
        "confirmed_at": "08:00:00 AM ET",
        "confirmed_price": 2.32,
    }

    state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="POLA", price=2.10, volume=950_000))],
        {"POLA": ReferenceData(shares_outstanding=1_000_000, avg_daily_volume=200_000)},
    )

    assert state.confirmed_scanner.get_all_confirmed() == []
    for code in ("macd_30s", "macd_1m", "tos", "runner"):
        assert state.bots[code].watchlist == set()
    assert state.bots["runner"]._candidates == {}


def test_bot_runtime_clears_ghost_position_on_no_position_reject() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.positions.open_position("ASTC", 5.31, quantity=10, path="P1_MACD_CROSS")
    bot.pending_close_symbols.add("ASTC")

    bot.apply_order_status(
        symbol="ASTC",
        intent_type="close",
        status="rejected",
        reason='asset "ASTC" cannot be sold short',
    )

    assert bot.positions.get_position("ASTC") is None
    assert "ASTC" not in bot.pending_close_symbols


def test_bot_runtime_clears_position_on_final_close_fill_even_if_qty_differs() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.positions.open_position("BFRG", 1.28, quantity=10, path="P1_MACD_CROSS")
    bot.pending_close_symbols.add("BFRG")

    bot.apply_execution_fill(
        client_order_id="macd_30s-BFRG-close-1",
        symbol="BFRG",
        intent_type="close",
        status="filled",
        side="sell",
        quantity=Decimal("9"),
        price=Decimal("1.28"),
    )

    assert bot.positions.get_position("BFRG") is None
    assert "BFRG" not in bot.pending_close_symbols


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


def test_trade_tick_records_blocked_decision_reason(monkeypatch) -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    bot = state.bots["macd_30s"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "macd_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=1_700_000_000.0, interval_secs=30),
    )
    monkeypatch.setattr(
        bot.indicator_engine,
        "calculate",
        lambda bars: {
            "price": 2.8,
            "price_above_ema20": True,
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "macd_above_signal": True,
            "macd_increasing": True,
            "macd_delta": 0.0,
            "macd_delta_accelerating": False,
            "histogram": 0.0,
            "price_above_ema9": True,
            "volume": 20_000,
            "histogram_growing": False,
            "stoch_k_rising": False,
            "price_above_vwap": True,
            "price_above_both_emas": True,
            "macd": 0.1,
            "signal": 0.05,
            "stoch_k": 40.0,
            "ema9": 2.7,
            "ema20": 2.6,
        },
    )

    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.8,
        size=200,
        timestamp_ns=1_700_001_500_000_000_000,
    )

    assert intents == []
    recent_decision = bot.summary()["recent_decisions"][0]
    assert recent_decision["status"] == "idle"
    assert recent_decision["reason"] == "no entry path matched"


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
async def test_order_event_fill_uses_incremental_quantity_for_cumulative_reports() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=False),
        redis_client=redis,
    )
    bot = service.state.bots["macd_30s"]
    bot.pending_open_symbols.add("ELAB")

    partial_fill = OrderEventEvent(
        source_service="oms-risk",
        payload=OrderEventPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            client_order_id="macd_30s-ELAB-open-cumulative",
            broker_order_id="broker-order-1",
            broker_fill_id="fill-1",
            symbol="ELAB",
            side="buy",
            intent_type="open",
            status="partially_filled",
            quantity=Decimal("100"),
            filled_quantity=Decimal("19"),
            fill_price=Decimal("3.95"),
            reason="ENTRY_P1_MACD_CROSS",
            metadata={"path": "P1_MACD_CROSS", "reference_price": "3.95"},
        ),
    )
    final_fill = OrderEventEvent(
        source_service="oms-risk",
        payload=OrderEventPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            client_order_id="macd_30s-ELAB-open-cumulative",
            broker_order_id="broker-order-1",
            broker_fill_id="fill-2",
            symbol="ELAB",
            side="buy",
            intent_type="open",
            status="filled",
            quantity=Decimal("100"),
            filled_quantity=Decimal("100"),
            fill_price=Decimal("3.95"),
            reason="ENTRY_P1_MACD_CROSS",
            metadata={"path": "P1_MACD_CROSS", "reference_price": "3.95"},
        ),
    )

    await service._handle_stream_message("test:order-events", {"data": partial_fill.model_dump_json()})
    await service._handle_stream_message("test:order-events", {"data": final_fill.model_dump_json()})

    position = bot.positions.get_position("ELAB")
    assert position is not None
    assert position.quantity == 100
    assert position.original_quantity == 100


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
    historical_runner = HistoricalBarsEvent(
        source_service="market-data-gateway",
        payload=HistoricalBarsPayload(
            symbol="UGRO",
            interval_secs=60,
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
                    timestamp=1_700_000_060.0,
                ),
            ],
        ),
    )

    await service._handle_stream_message("test:market-data", {"data": historical_30s.model_dump_json()})
    await service._handle_stream_message("test:market-data", {"data": historical_runner.model_dump_json()})

    assert len(service.state.bots["macd_30s"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["macd_1m"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["tos"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["runner"].builder_manager.get_bars("UGRO")) == 2


@pytest.mark.asyncio
async def test_snapshot_batch_history_prefill_restores_alert_warmup() -> None:
    redis = FakeRedis()
    service = StrategyEngineService(
        settings=Settings(
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            market_data_snapshot_interval_seconds=30,
        ),
        redis_client=redis,
    )

    snapshot_stream = "test:snapshot-batches"
    for index in range(20):
        event = SnapshotBatchEvent(
            source_service="market-data-gateway",
            payload={
                "snapshots": [
                    make_snapshot_payload(
                        symbol="UGRO",
                        price=2.40 + index * 0.01,
                        volume=900_000 + index * 10_000,
                    )
                ],
                "reference_data": [
                    {
                        "symbol": "UGRO",
                        "shares_outstanding": 50_000,
                        "avg_daily_volume": "390000",
                    }
                ],
            },
        )
        redis.stream_entries.setdefault(snapshot_stream, []).insert(
            0,
            (f"{index + 1}-0", {"data": event.model_dump_json()}),
        )

    await service._prefill_alert_history_from_snapshot_batches()

    warmup = service.state.alert_warmup
    assert warmup["history_cycles"] == 20
    assert warmup["squeeze_5min_ready"] is True
    assert warmup["squeeze_10min_ready"] is True
    assert warmup["fully_ready"] is True


@pytest.mark.asyncio
async def test_subscription_sync_replays_recent_historical_bars_for_active_symbols() -> None:
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
    historical_60s = HistoricalBarsEvent(
        source_service="market-data-gateway",
        payload=HistoricalBarsPayload(
            symbol="UGRO",
            interval_secs=60,
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
                    timestamp=1_700_000_060.0,
                ),
            ],
        ),
    )
    redis.stream_entries.setdefault("test:market-data", []).extend(
        [
            ("2-0", {"data": historical_30s.model_dump_json()}),
            ("3-0", {"data": historical_60s.model_dump_json()}),
        ]
    )

    await service._sync_market_data_subscriptions(["UGRO"])

    assert len(service.state.bots["macd_30s"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["macd_1m"].builder_manager.get_bars("UGRO")) == 1
    assert len(service.state.bots["tos"].builder_manager.get_bars("UGRO")) == 1
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
    service.state.confirmed_scanner.seed_confirmed_candidates(
        [
            {
                "ticker": "UGRO",
                "rank_score": 72.0,
                "confirmed_at": "10:00:00 AM ET",
                "entry_price": 2.25,
                "price": 2.40,
                "change_pct": 12.5,
                "volume": 900_000,
                "rvol": 6.2,
                "shares_outstanding": 50_000,
                "bid": 2.39,
                "ask": 2.40,
                "spread": 0.01,
                "spread_pct": 0.42,
                "squeeze_count": 2,
                "confirmation_path": "PATH_B_2SQ",
            },
            {
                "ticker": "ELAB",
                "rank_score": 82.0,
                "confirmed_at": "10:05:00 AM ET",
                "entry_price": 3.05,
                "price": 3.82,
                "change_pct": 128.7,
                "volume": 26_400_000,
                "rvol": 13.0,
                "shares_outstanding": 541_461,
                "bid": 3.81,
                "ask": 3.82,
                "spread": 0.01,
                "spread_pct": 0.26,
                "squeeze_count": 2,
                "confirmation_path": "PATH_B_2SQ",
            },
        ]
    )

    await service._publish_strategy_state_snapshot()

    with session_factory() as session:
        snapshot = session.scalar(
            select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "scanner_confirmed_last_nonempty"
            )
        )

    assert snapshot is not None
    assert snapshot.payload["top_confirmed"][0]["ticker"] == "UGRO"
    assert len(snapshot.payload["all_confirmed_candidates"]) == 2
    assert snapshot.payload["top_confirmed"][0]["headline"] == "Quantum Biopharma Wins Hospital Supply Agreement"
    assert snapshot.payload["top_confirmed"][0]["path_a_eligible"] is True


def test_seeded_confirmed_candidates_are_revalidated_into_fresh_top_confirmed(monkeypatch) -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_confirmed_last_nonempty",
                payload={
                    "persisted_at": datetime(2026, 3, 30, 10, 5, tzinfo=UTC).isoformat(),
                    "all_confirmed_candidates": [
                        {
                            "ticker": "UGRO",
                            "rank_score": 72.0,
                            "confirmed_at": "10:00:00 AM ET",
                            "entry_price": 2.25,
                            "price": 2.40,
                            "change_pct": 24.5,
                            "volume": 900_000,
                            "rvol": 6.2,
                            "shares_outstanding": 50_000,
                            "bid": 2.39,
                            "ask": 2.40,
                            "spread": 0.01,
                            "spread_pct": 0.42,
                            "squeeze_count": 2,
                            "confirmation_path": "PATH_B_2SQ",
                        },
                        {
                            "ticker": "ELAB",
                            "rank_score": 82.0,
                            "confirmed_at": "10:05:00 AM ET",
                            "entry_price": 3.05,
                            "price": 3.82,
                            "change_pct": 128.7,
                            "volume": 26_400_000,
                            "rvol": 13.0,
                            "shares_outstanding": 541_461,
                            "bid": 3.81,
                            "ask": 3.82,
                            "spread": 0.01,
                            "spread_pct": 0.26,
                            "squeeze_count": 2,
                            "confirmation_path": "PATH_B_2SQ",
                        },
                    ],
                    "top_confirmed": [
                            {
                                "ticker": "UGRO",
                                "rank_score": 72.0,
                                "confirmed_at": "10:00:00 AM ET",
                                "entry_price": 2.25,
                                "price": 2.40,
                                "change_pct": 24.5,
                                "volume": 900_000,
                                "rvol": 6.2,
                                "shares_outstanding": 50_000,
                            "bid": 2.39,
                            "ask": 2.40,
                            "spread": 0.01,
                            "spread_pct": 0.42,
                            "squeeze_count": 2,
                            "confirmation_path": "PATH_B_2SQ",
                        }
                    ]
                },
            )
        )
        session.commit()

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 3, 30, 14, 0, tzinfo=UTC),
    )

    service = StrategyEngineService(
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._seed_confirmed_candidates_from_dashboard_snapshot()

    summary = service.state.process_snapshot_batch(
        [
            snapshot_from_payload(make_snapshot_payload(symbol="UGRO", price=2.62, volume=1_100_000)),
            snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=3.90, volume=28_000_000)),
        ],
        {
            "UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000),
            "ELAB": ReferenceData(shares_outstanding=541_461, avg_daily_volume=1_941_514.84),
        },
    )

    assert service.state._seeded_confirmed_pending_revalidation is False
    assert [item["ticker"] for item in service.state.confirmed_scanner.get_all_confirmed()] == ["UGRO", "ELAB"]
    assert summary["watchlist"] == ["ELAB"]
    assert [item["ticker"] for item in summary["top_confirmed"]] == ["ELAB"]
    assert summary["top_confirmed"][0]["price"] == 3.90
    assert service.state.confirmed_scanner.get_all_confirmed()[0]["volume"] == 1_100_000


def test_seeded_confirmed_candidates_drop_when_missing_from_fresh_snapshots() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_confirmed_last_nonempty",
                payload={
                    "persisted_at": datetime(2026, 3, 30, 10, 5, tzinfo=UTC).isoformat(),
                    "top_confirmed": [
                        {
                            "ticker": "UGRO",
                            "rank_score": 72.0,
                            "confirmed_at": "10:00:00 AM ET",
                            "entry_price": 2.25,
                            "price": 2.40,
                            "change_pct": 12.5,
                            "volume": 900_000,
                            "rvol": 6.2,
                            "shares_outstanding": 50_000,
                            "bid": 2.39,
                            "ask": 2.40,
                            "spread": 0.01,
                            "spread_pct": 0.42,
                            "squeeze_count": 2,
                            "confirmation_path": "PATH_B_2SQ",
                        }
                    ]
                },
            )
        )
        session.commit()

    service = StrategyEngineService(
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._seed_confirmed_candidates_from_dashboard_snapshot()

    summary = service.state.process_snapshot_batch(
        [snapshot_from_payload(make_snapshot_payload(symbol="ELAB", price=2.62, volume=1_100_000))],
        {"ELAB": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)},
    )

    assert service.state._seeded_confirmed_pending_revalidation is False
    assert summary["watchlist"] == []
    assert summary["top_confirmed"] == []


def test_seeded_confirmed_candidates_skip_prior_session_snapshot(monkeypatch) -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="scanner_confirmed_last_nonempty",
                payload={
                    "persisted_at": datetime(2026, 3, 30, 1, 0, tzinfo=UTC).isoformat(),
                    "top_confirmed": [
                        {
                            "ticker": "ELAB",
                            "rank_score": 72.0,
                            "confirmed_at": "06:03:59 PM ET",
                            "entry_price": 3.73,
                            "price": 3.32,
                            "change_pct": 98.8,
                            "volume": 249_300,
                        }
                    ],
                },
            )
        )
        session.commit()

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.utcnow",
        lambda: datetime(2026, 3, 31, 9, 0, tzinfo=UTC),
    )

    service = StrategyEngineService(
        settings=Settings(redis_stream_prefix="test", dashboard_snapshot_persistence_enabled=True),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    service._seed_confirmed_candidates_from_dashboard_snapshot()

    assert service.state.confirmed_scanner.get_all_confirmed() == []
    assert service.state._seeded_confirmed_pending_revalidation is False


def test_strategy_bot_runtime_loads_closed_trades_for_daily_pnl(monkeypatch) -> None:
    calls: list[str] = []

    def fake_load_closed_trades(self) -> None:
        calls.append(self.config.__class__.__name__)
        self._daily_pnl = 42.5

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.PositionTracker.load_closed_trades",
        fake_load_closed_trades,
    )

    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="MACD Bot",
            account_name="paper:macd_30s",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=fixed_now,
    )

    assert calls == ["TradingConfig"]
    assert runtime.positions.get_daily_pnl() == 42.5


def test_strategy_bot_runtime_uses_strategy_specific_trade_history(tmp_path, monkeypatch) -> None:
    repo_dir = tmp_path / "project-mai-tai"
    data_dir = tmp_path / "project-mai-tai-data" / "history"
    repo_dir.mkdir()
    data_dir.mkdir(parents=True)
    monkeypatch.chdir(repo_dir)

    (data_dir / "macdbot_closed_2026-03-30.csv").write_text(
        "ticker,entry_price,exit_price,quantity,pnl,pnl_pct,reason,entry_time,exit_time,peak_profit_pct,tier,scales_done,path\n"
        "ELAB,3.00,3.10,100,10.0,3.33,OMS_FILL,09:30:00 AM ET,09:31:00 AM ET,4.0,1,,P1_MACD_CROSS\n",
        encoding="utf-8",
    )
    (data_dir / "macd_1m_closed_2026-03-30.csv").write_text(
        "ticker,entry_price,exit_price,quantity,pnl,pnl_pct,reason,entry_time,exit_time,peak_profit_pct,tier,scales_done,path\n"
        "ASTC,4.00,4.50,100,50.0,12.50,OMS_FILL,09:35:00 AM ET,09:36:00 AM ET,10.0,2,,P3_MACD_SURGE\n",
        encoding="utf-8",
    )
    (data_dir / "tos_closed_2026-03-30.csv").write_text(
        "ticker,entry_price,exit_price,quantity,pnl,pnl_pct,reason,entry_time,exit_time,peak_profit_pct,tier,scales_done,path\n"
        "BFRG,1.00,0.95,100,-5.0,-5.00,OMS_FILL,09:40:00 AM ET,09:41:00 AM ET,2.0,1,,P1_MACD_CROSS\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "project_mai_tai.strategy_core.position_tracker.today_eastern_str",
        lambda: "2026-03-30",
    )

    def make_runtime(strategy_code: str) -> StrategyBotRuntime:
        return StrategyBotRuntime(
            StrategyDefinition(
                code=strategy_code,
                display_name=strategy_code,
                account_name=f"paper:{strategy_code}",
                interval_secs=30 if strategy_code == "macd_30s" else 60,
                trading_config=TradingConfig(),
                indicator_config=IndicatorConfig(),
            )
        )

    assert make_runtime("macd_30s").summary()["daily_pnl"] == 10.0
    assert make_runtime("macd_1m").summary()["daily_pnl"] == 50.0
    assert make_runtime("tos").summary()["daily_pnl"] == -5.0


def test_strategy_bot_runtime_rolls_daily_pnl_and_closed_trades_at_new_et_day(monkeypatch) -> None:
    active_day = {"value": "2026-03-30"}

    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.today_eastern_str",
        lambda: active_day["value"],
    )
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.PositionTracker.load_closed_trades",
        lambda self: None,
    )

    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="macd_30s",
            account_name="paper:macd_30s",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        )
    )
    runtime.positions._daily_pnl = 12.5
    runtime.positions._closed_today = [{"ticker": "ELAB"}]

    active_day["value"] = "2026-03-31"

    summary = runtime.summary()

    assert summary["daily_pnl"] == 0.0
    assert summary["closed_today"] == []
