from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.runtime_registry import (
    configured_broker_account_registrations,
    strategy_registration_map,
)
from project_mai_tai.services.control_plane import BOT_PAGE_META
from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.settings import Settings


class FakeRedis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"

    async def xread(self, *_args, **_kwargs):
        return []

    async def ping(self):
        return True

    async def aclose(self):
        return None


def fixed_now() -> datetime:
    return datetime(2026, 4, 23, 10, 0)


def build_test_session_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def build_recent_polygon_seed_bars(*, start: datetime, count: int = 55) -> list[dict[str, float | int]]:
    start_timestamp = start.timestamp()
    return [
        {
            "open": 1.00 + index * 0.01,
            "high": 1.02 + index * 0.01,
            "low": 0.99 + index * 0.01,
            "close": 1.01 + index * 0.01,
            "volume": 20_000 + index * 100,
            "timestamp": start_timestamp + index * 30,
            "trade_count": 10 + index,
        }
        for index in range(count)
    ]


def test_runtime_registry_registers_polygon_30s_as_live_polygon_strategy() -> None:
    settings = Settings(
        oms_adapter="schwab",
        strategy_polygon_30s_enabled=True,
        scanner_feed_retention_enabled=False,
    )

    registrations = strategy_registration_map(settings)
    broker_accounts = {item.name: item for item in configured_broker_account_registrations(settings)}

    assert registrations["macd_30s"].display_name == "Schwab 30 Sec Bot"
    assert registrations["polygon_30s"].display_name == "Polygon 30 Sec Bot"
    assert registrations["polygon_30s"].execution_mode == "live"
    assert registrations["polygon_30s"].metadata["provider"] == "webull"
    assert registrations["polygon_30s"].metadata["market_data_provider"] == "polygon"
    assert broker_accounts[settings.strategy_polygon_30s_account_name].provider == "webull"


def test_strategy_state_routes_polygon_30s_through_polygon_market_data_path() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    assert "polygon_30s" in state.bots


def test_polygon_30s_uses_tick_bar_close_grace_for_late_polygon_trades() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
            strategy_polygon_30s_tick_bar_close_grace_seconds=2.0,
        ),
        now_provider=fixed_now,
    )

    builder_manager = state.bots["polygon_30s"].builder_manager

    assert builder_manager.close_grace_seconds == 2.0
    assert state.bots["polygon_30s"].definition.display_name == "Polygon 30 Sec Bot"
    assert state.schwab_stream_strategy_codes() == ("macd_30s",)

    state._record_bot_handoff_symbols([{"ticker": "UGRO"}], strategy_codes=["polygon_30s"])
    state._resync_bot_watchlists_from_current_confirmed(strategy_codes=["polygon_30s"])

    assert "UGRO" in state.bots["polygon_30s"].watchlist
    assert "UGRO" in state.market_data_symbols()
    assert "UGRO" not in state.schwab_stream_symbols()


def test_polygon_30s_defaults_to_tick_built_runtime_when_live_aggregates_are_disabled() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    polygon_bot = state.bots["polygon_30s"]

    assert polygon_bot.use_live_aggregate_bars is False
    assert polygon_bot.live_aggregate_fallback_enabled is True
    assert polygon_bot.live_aggregate_bars_are_final is False
    assert polygon_bot.live_aggregate_stale_after_seconds == 3


def test_polygon_30s_can_force_live_bar_only_mode_for_diagnostics() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            strategy_polygon_30s_force_live_bar_only_mode=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    polygon_bot = state.bots["polygon_30s"]

    assert polygon_bot.use_live_aggregate_bars is True
    assert polygon_bot.live_aggregate_fallback_enabled is False


def test_polygon_30s_does_not_inherit_global_live_aggregate_stream_toggle() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
            market_data_live_aggregate_stream_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=False,
            strategy_polygon_30s_force_tick_built_mode=True,
        ),
        now_provider=fixed_now,
    )

    polygon_bot = state.bots["polygon_30s"]

    assert polygon_bot.use_live_aggregate_bars is False


def test_polygon_30s_keeps_polygon_market_data_when_execution_routes_to_schwab() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_broker_provider="schwab",
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    assert state.schwab_stream_strategy_codes() == ("macd_30s",)
    state._record_bot_handoff_symbols([{"ticker": "UGRO"}], strategy_codes=["polygon_30s"])
    state._resync_bot_watchlists_from_current_confirmed(strategy_codes=["polygon_30s"])
    assert "UGRO" in state.market_data_symbols()
    assert "UGRO" not in state.schwab_stream_symbols()


def test_polygon_30s_runtime_uses_polygon_validation_entry_tuning() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    trading = state.bots["polygon_30s"].definition.trading_config

    assert trading.entry_logic_mode == "polygon_30s"
    assert trading.schwab_native_use_chop_regime is False
    assert trading.p3_allow_momentum_override is True
    assert trading.p3_entry_stoch_k_cap is None


def test_gap_recovery_only_tracks_real_risk_cases_for_flat_symbols() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    schwab_bot = state.bots["macd_30s"]
    schwab_bot.set_watchlist(["UGRO"])

    assert schwab_bot._should_track_gap_recovery("UGRO") is False

    schwab_bot.apply_data_warning("UGRO", reason="quiet tape")
    assert schwab_bot._should_track_gap_recovery("UGRO") is True


def test_market_data_gateway_enables_live_aggregate_stream_for_polygon_30s() -> None:
    service = MarketDataGatewayService(
        settings=Settings(
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        redis_client=Mock(),
        snapshot_provider=Mock(),
        trade_stream=Mock(),
        reference_cache=Mock(),
    )

    assert service._live_aggregate_stream_enabled is True


def test_market_data_gateway_keeps_polygon_trade_quote_only_by_default() -> None:
    service = MarketDataGatewayService(
        settings=Settings(
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        redis_client=Mock(),
        snapshot_provider=Mock(),
        trade_stream=Mock(),
        reference_cache=Mock(),
    )

    assert service._live_aggregate_stream_enabled is False


def test_polygon_30s_does_not_use_live_aggregate_bars_when_disabled() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=False,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    assert state.bots["polygon_30s"].use_live_aggregate_bars is False


def test_oms_service_builds_webull_provider_inside_mixed_router() -> None:
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="schwab",
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
    )

    assert isinstance(service.broker_adapter, RoutingBrokerAdapter)
    assert isinstance(service._build_provider_adapter("webull"), WebullBrokerAdapter)


def test_control_plane_meta_includes_polygon_and_renamed_schwab_bot() -> None:
    assert BOT_PAGE_META["macd_30s"]["title"] == "Schwab 30 Sec Bot"
    assert BOT_PAGE_META["polygon_30s"]["title"] == "Polygon 30 Sec Bot"
    assert BOT_PAGE_META["polygon_30s"]["path"] == "/bot/30s-polygon"


def test_restore_confirmed_runtime_view_seeds_new_polygon_bot_from_confirmed_state() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    confirmed = [{"ticker": "UGRO", "score": 7}]
    state.restore_confirmed_runtime_view(
        confirmed,
        all_confirmed=confirmed,
        bot_handoff_symbols_by_strategy={"macd_30s": ["UGRO"]},
        bot_handoff_history_by_strategy={"macd_30s": ["UGRO"]},
    )

    assert "UGRO" in state.bots["macd_30s"].watchlist
    assert "UGRO" in state.bots["polygon_30s"].watchlist


def test_restore_confirmed_runtime_view_seeds_new_polygon_bot_from_existing_handoff_history() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    state.restore_confirmed_runtime_view(
        [],
        all_confirmed=[],
        bot_handoff_symbols_by_strategy={"macd_30s": ["UGRO"]},
        bot_handoff_history_by_strategy={"macd_30s": ["UGRO"]},
    )

    assert "UGRO" in state.bots["macd_30s"].watchlist
    assert "UGRO" in state.bots["polygon_30s"].watchlist


def test_active_bot_handoff_replaces_current_confirmed_set_without_losing_history() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    state._record_bot_handoff_symbols(
        [{"ticker": "UGRO"}, {"ticker": "CAST"}],
        replace_active=True,
    )
    state._resync_bot_watchlists_from_current_confirmed()

    assert state.bots["macd_30s"].watchlist == {"UGRO", "CAST"}
    assert state.bots["polygon_30s"].watchlist == {"UGRO", "CAST"}

    state._record_bot_handoff_symbols(
        [{"ticker": "UGRO"}],
        replace_active=True,
    )
    state._resync_bot_watchlists_from_current_confirmed()

    assert state.bots["macd_30s"].watchlist == {"UGRO"}
    assert state.bots["polygon_30s"].watchlist == {"UGRO"}
    assert state.bot_handoff_history_by_strategy["macd_30s"] == {"UGRO", "CAST"}
    assert state.bot_handoff_history_by_strategy["polygon_30s"] == {"UGRO", "CAST"}


def test_polygon_30s_does_not_re_evaluate_same_bar_after_late_same_bucket_live_bar() -> None:
    clock = {"now": datetime(2026, 4, 23, 15, 26, 45, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            strategy_polygon_30s_tick_bar_close_grace_seconds=0.0,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=lambda: clock["now"],
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["RDAC"])
    bot.seed_bars("RDAC", build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 14, 59, 0, tzinfo=UTC)))
    baseline_decisions = len(bot.recent_decisions)

    observed_prices: list[float] = []

    def fake_calculate(bars):
        last_bar = bars[-1]
        return {
            "price": float(last_bar["close"]),
            "bar_timestamp": float(last_bar["timestamp"]),
        }

    def fake_check_entry(_symbol, indicators, _bar_index, _runtime):
        observed_prices.append(float(indicators["price"]))
        return None

    def fake_pop_last_decision(_symbol):
        if observed_prices[-1] < 1.205:
            return {
                "status": "blocked",
                "reason": "chop lock active (current 1/4): NO_CLEAN_SIDE; P1/P2/P3 gated",
            }
        return {
            "status": "idle",
            "reason": "no entry path matched",
        }

    bot.indicator_engine.calculate = fake_calculate
    bot.entry_engine.check_entry = fake_check_entry
    bot.entry_engine.pop_last_decision = fake_pop_last_decision

    bot.handle_live_bar(
        symbol="RDAC",
        open_price=1.20,
        high_price=1.20,
        low_price=1.20,
        close_price=1.20,
        volume=100,
        timestamp=datetime(2026, 4, 23, 15, 26, 30, tzinfo=UTC).timestamp(),
        trade_count=1,
    )

    clock["now"] = datetime(2026, 4, 23, 15, 27, 1, tzinfo=UTC)
    _intents, completed_count = bot.flush_completed_bars()

    assert completed_count == 1
    assert len(bot.recent_decisions) == baseline_decisions + 1
    assert observed_prices == [1.2]

    bot.handle_live_bar(
        symbol="RDAC",
        open_price=1.20,
        high_price=1.22,
        low_price=1.19,
        close_price=1.21,
        volume=150,
        timestamp=datetime(2026, 4, 23, 15, 26, 59, tzinfo=UTC).timestamp(),
        trade_count=2,
    )

    assert len(bot.recent_decisions) == baseline_decisions + 1
    assert observed_prices == [1.2]

    bot.handle_live_bar(
        symbol="RDAC",
        open_price=1.21,
        high_price=1.23,
        low_price=1.20,
        close_price=1.22,
        volume=125,
        timestamp=datetime(2026, 4, 23, 15, 27, 0, tzinfo=UTC).timestamp(),
        trade_count=1,
    )

    assert len(bot.recent_decisions) == baseline_decisions + 1
    assert observed_prices == [1.2]


def test_polygon_30s_revises_last_closed_bar_when_late_second_arrives() -> None:
    clock = {"now": datetime(2026, 4, 23, 15, 27, 0, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            strategy_polygon_30s_tick_bar_close_grace_seconds=0.0,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=lambda: clock["now"],
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["CTNT"])
    recent_bars = build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 14, 59, 0, tzinfo=UTC))
    for index, bar in enumerate(recent_bars):
        bar["open"] = 3.00 + index * 0.01
        bar["high"] = 3.02 + index * 0.01
        bar["low"] = 2.99 + index * 0.01
        bar["close"] = 3.01 + index * 0.01
    bot.seed_bars("CTNT", recent_bars)

    completed_evaluations: list[str] = []

    def fake_evaluate_completed_bar(symbol: str, *, completed_bar=None):
        assert completed_bar is not None
        completed_evaluations.append(symbol)
        return []

    bot._evaluate_completed_bar = fake_evaluate_completed_bar  # type: ignore[method-assign]

    bot.handle_live_bar(
        symbol="CTNT",
        open_price=3.21,
        high_price=3.25,
        low_price=3.2032,
        close_price=3.23,
        volume=14_122,
        timestamp=datetime(2026, 4, 23, 15, 26, 30, tzinfo=UTC).timestamp(),
        trade_count=171,
    )
    bot.handle_live_bar(
        symbol="CTNT",
        open_price=3.23,
        high_price=3.24,
        low_price=3.22,
        close_price=3.23,
        volume=500,
        timestamp=datetime(2026, 4, 23, 15, 27, 0, tzinfo=UTC).timestamp(),
        trade_count=5,
    )

    builder = bot.builder_manager.get_builder("CTNT")
    assert builder is not None
    assert completed_evaluations == ["CTNT"]
    assert builder.bars[-1].timestamp == datetime(2026, 4, 23, 15, 26, 30, tzinfo=UTC).timestamp()
    assert builder.bars[-1].volume == 14_122
    assert builder.bars[-1].trade_count == 171

    bot.handle_live_bar(
        symbol="CTNT",
        open_price=3.23,
        high_price=3.24,
        low_price=3.21,
        close_price=3.23,
        volume=1_538,
        timestamp=datetime(2026, 4, 23, 15, 26, 59, tzinfo=UTC).timestamp(),
        trade_count=30,
    )

    assert completed_evaluations == ["CTNT"]
    assert builder.bars[-1].volume == 15_660
    assert builder.bars[-1].trade_count == 201


def test_polygon_30s_skips_first_mid_bucket_live_aggregate_bar() -> None:
    clock = {"now": datetime(2026, 4, 23, 15, 26, 35, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=lambda: clock["now"],
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["CANF"])
    recent_bars = build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 14, 59, 0, tzinfo=UTC))
    for index, bar in enumerate(recent_bars):
        bar["open"] = 3.00 + index * 0.01
        bar["high"] = 3.02 + index * 0.01
        bar["low"] = 2.99 + index * 0.01
        bar["close"] = 3.01 + index * 0.01
    bot.seed_bars("CANF", recent_bars)

    bot.handle_live_bar(
        symbol="CANF",
        open_price=4.00,
        high_price=4.04,
        low_price=3.99,
        close_price=4.02,
        volume=900,
        timestamp=datetime(2026, 4, 23, 15, 26, 35, tzinfo=UTC).timestamp(),
        trade_count=6,
    )
    bot.handle_live_bar(
        symbol="CANF",
        open_price=4.02,
        high_price=4.05,
        low_price=4.00,
        close_price=4.03,
        volume=1_100,
        timestamp=datetime(2026, 4, 23, 15, 26, 59, tzinfo=UTC).timestamp(),
        trade_count=7,
    )

    clock["now"] = datetime(2026, 4, 23, 15, 27, 29, tzinfo=UTC)
    skipped_intents, skipped_completed = bot.flush_completed_bars()

    assert skipped_intents == []
    assert skipped_completed == 1

    bot.handle_live_bar(
        symbol="CANF",
        open_price=4.10,
        high_price=4.12,
        low_price=4.08,
        close_price=4.11,
        volume=1_200,
        timestamp=datetime(2026, 4, 23, 15, 27, 0, tzinfo=UTC).timestamp(),
        trade_count=8,
    )
    bot.handle_live_bar(
        symbol="CANF",
        open_price=4.11,
        high_price=4.14,
        low_price=4.09,
        close_price=4.13,
        volume=1_500,
        timestamp=datetime(2026, 4, 23, 15, 27, 29, tzinfo=UTC).timestamp(),
        trade_count=9,
    )

    clock["now"] = datetime(2026, 4, 23, 15, 27, 32, tzinfo=UTC)
    _completed_intents, completed_count = bot.flush_completed_bars()
    assert completed_count == 1
    builder = bot.builder_manager.get_builder("CANF")

    assert builder is not None
    assert builder.bars[-1].timestamp == datetime(2026, 4, 23, 15, 27, 0, tzinfo=UTC).timestamp()


def test_polygon_30s_keeps_sparse_bucket_when_provider_coverage_predates_bucket() -> None:
    clock = {"now": datetime(2026, 4, 23, 15, 26, 35, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            strategy_polygon_30s_tick_bar_close_grace_seconds=0.0,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=lambda: clock["now"],
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["IONZ"])
    recent_bars = build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 14, 59, 0, tzinfo=UTC))
    for index, bar in enumerate(recent_bars):
        bar["open"] = 5.00 + index * 0.01
        bar["high"] = 5.02 + index * 0.01
        bar["low"] = 4.99 + index * 0.01
        bar["close"] = 5.01 + index * 0.01
    bot.seed_bars("IONZ", recent_bars)

    bucket_start = datetime(2026, 4, 23, 15, 26, 30, tzinfo=UTC).timestamp()
    coverage_started_at = datetime(2026, 4, 23, 15, 26, 0, tzinfo=UTC).timestamp()

    bot.handle_live_bar(
        symbol="IONZ",
        open_price=5.10,
        high_price=5.12,
        low_price=5.09,
        close_price=5.11,
        volume=1_800,
        timestamp=datetime(2026, 4, 23, 15, 26, 35, tzinfo=UTC).timestamp(),
        trade_count=3,
        coverage_started_at=coverage_started_at,
    )
    bot.handle_live_bar(
        symbol="IONZ",
        open_price=5.11,
        high_price=5.14,
        low_price=5.10,
        close_price=5.13,
        volume=2_400,
        timestamp=datetime(2026, 4, 23, 15, 26, 59, tzinfo=UTC).timestamp(),
        trade_count=5,
        coverage_started_at=coverage_started_at,
    )

    clock["now"] = datetime(2026, 4, 23, 15, 27, 29, tzinfo=UTC)
    _completed_intents, completed_count = bot.flush_completed_bars()
    assert completed_count == 1
    builder = bot.builder_manager.get_builder("IONZ")

    assert builder is not None
    assert builder.bars[-1].timestamp == bucket_start


def test_polygon_30s_live_bar_resume_backfills_missing_gap_bars() -> None:
    clock = {"now": datetime(2026, 4, 23, 15, 30, 5, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            strategy_polygon_30s_tick_bar_close_grace_seconds=0.0,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=lambda: clock["now"],
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["IONZ"])
    last_closed_at = datetime(2026, 4, 23, 15, 26, 30, tzinfo=UTC).timestamp()
    seed_start = last_closed_at - (54 * 30)
    bot.seed_bars(
        "IONZ",
        [
            {
                "open": 5.00 + index * 0.01,
                "high": 5.02 + index * 0.01,
                "low": 4.99 + index * 0.01,
                "close": 5.01 + index * 0.01,
                "volume": 20_000 + index * 100,
                "timestamp": seed_start + index * 30,
                "trade_count": 10 + index,
            }
            for index in range(55)
        ],
    )

    bot.handle_live_bar(
        symbol="IONZ",
        open_price=5.10,
        high_price=5.12,
        low_price=5.09,
        close_price=5.11,
        volume=1_800,
        timestamp=datetime(2026, 4, 23, 15, 30, 5, tzinfo=UTC).timestamp(),
        trade_count=3,
        coverage_started_at=datetime(2026, 4, 23, 15, 0, 0, tzinfo=UTC).timestamp(),
    )

    builder = bot.builder_manager.get_builder("IONZ")
    assert builder is not None
    assert builder.bars[-1].timestamp == datetime(2026, 4, 23, 15, 29, 30, tzinfo=UTC).timestamp()
    assert builder._current_bar_start == datetime(2026, 4, 23, 15, 30, 0, tzinfo=UTC).timestamp()
    assert bot.recent_decisions[0]["last_bar_at"] == "2026-04-23T11:29:30-04:00"


def test_polygon_30s_open_current_live_bar_resume_backfills_intermediate_gap_bars() -> None:
    clock = {"now": datetime(2026, 4, 23, 15, 27, 5, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            strategy_polygon_30s_tick_bar_close_grace_seconds=0.0,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=lambda: clock["now"],
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["IONZ"])
    last_closed_at = datetime(2026, 4, 23, 15, 26, 30, tzinfo=UTC).timestamp()
    seed_start = last_closed_at - (54 * 30)
    bot.seed_bars(
        "IONZ",
        [
            {
                "open": 5.00 + index * 0.01,
                "high": 5.02 + index * 0.01,
                "low": 4.99 + index * 0.01,
                "close": 5.01 + index * 0.01,
                "volume": 20_000 + index * 100,
                "timestamp": seed_start + index * 30,
                "trade_count": 10 + index,
            }
            for index in range(55)
        ],
    )

    bot.handle_live_bar(
        symbol="IONZ",
        open_price=5.10,
        high_price=5.12,
        low_price=5.09,
        close_price=5.11,
        volume=1_800,
        timestamp=datetime(2026, 4, 23, 15, 27, 5, tzinfo=UTC).timestamp(),
        trade_count=3,
        coverage_started_at=datetime(2026, 4, 23, 15, 0, 0, tzinfo=UTC).timestamp(),
    )

    clock["now"] = datetime(2026, 4, 23, 15, 30, 5, tzinfo=UTC)
    bot.handle_live_bar(
        symbol="IONZ",
        open_price=5.20,
        high_price=5.24,
        low_price=5.18,
        close_price=5.23,
        volume=1_500,
        timestamp=datetime(2026, 4, 23, 15, 30, 5, tzinfo=UTC).timestamp(),
        trade_count=2,
        coverage_started_at=datetime(2026, 4, 23, 15, 0, 0, tzinfo=UTC).timestamp(),
    )

    builder = bot.builder_manager.get_builder("IONZ")
    assert builder is not None
    assert builder.bars[-4].timestamp == datetime(2026, 4, 23, 15, 28, 0, tzinfo=UTC).timestamp()
    assert builder.bars[-3].timestamp == datetime(2026, 4, 23, 15, 28, 30, tzinfo=UTC).timestamp()
    assert builder.bars[-2].timestamp == datetime(2026, 4, 23, 15, 29, 0, tzinfo=UTC).timestamp()
    assert builder.bars[-1].timestamp == datetime(2026, 4, 23, 15, 29, 30, tzinfo=UTC).timestamp()
    assert builder._current_bar_start == datetime(2026, 4, 23, 15, 30, 0, tzinfo=UTC).timestamp()


def test_polygon_30s_uses_real_live_bar_fallback_when_tick_builder_lags() -> None:
    clock = {"now": datetime(2026, 4, 23, 15, 11, 0, tzinfo=UTC)}
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=lambda: clock["now"],
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["CANF"])
    recent_bars = build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 14, 43, 0, tzinfo=UTC))
    for index, bar in enumerate(recent_bars):
        bar["open"] = 3.00 + index * 0.01
        bar["high"] = 3.02 + index * 0.01
        bar["low"] = 2.99 + index * 0.01
        bar["close"] = 3.01 + index * 0.01
    bot.seed_bars("CANF", recent_bars)
    baseline_decisions = len(bot.recent_decisions)

    observed_timestamps: list[float] = []

    def fake_calculate(bars):
        last_bar = bars[-1]
        return {
            "price": float(last_bar["close"]),
            "bar_timestamp": float(last_bar["timestamp"]),
        }

    def fake_check_entry(_symbol, indicators, _bar_index, _runtime):
        observed_timestamps.append(float(indicators["bar_timestamp"]))
        return None

    def fake_pop_last_decision(_symbol):
        return {
            "status": "idle",
            "reason": "no entry path matched",
        }

    bot.indicator_engine.calculate = fake_calculate
    bot.entry_engine.check_entry = fake_check_entry
    bot.entry_engine.pop_last_decision = fake_pop_last_decision

    first_live_bar_ts = datetime(2026, 4, 23, 15, 10, 30, tzinfo=UTC).timestamp()
    second_live_bar_ts = datetime(2026, 4, 23, 15, 11, 0, tzinfo=UTC).timestamp()

    bot.handle_live_bar(
        symbol="CANF",
        open_price=4.00,
        high_price=4.05,
        low_price=3.98,
        close_price=4.02,
        volume=1_000,
        timestamp=first_live_bar_ts,
        trade_count=8,
    )

    assert len(bot.recent_decisions) == baseline_decisions

    bot.handle_live_bar(
        symbol="CANF",
        open_price=4.02,
        high_price=4.08,
        low_price=4.01,
        close_price=4.06,
        volume=1_200,
        timestamp=second_live_bar_ts,
        trade_count=10,
    )

    assert len(bot.recent_decisions) == baseline_decisions + 1
    assert bot.recent_decisions[0]["last_bar_at"] == "2026-04-23T11:10:30-04:00"
    assert observed_timestamps[-1] == first_live_bar_ts


def test_polygon_30s_trade_ticks_keep_bot_alive_when_live_bars_starve() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["UGRO"])
    recent_bars = build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 9, 32, 0, tzinfo=UTC))
    for index, bar in enumerate(recent_bars):
        bar["open"] = 2.50 + index * 0.01
        bar["high"] = 2.52 + index * 0.01
        bar["low"] = 2.49 + index * 0.01
        bar["close"] = 2.51 + index * 0.01
    state.seed_bars("polygon_30s", "UGRO", recent_bars)
    bot.latest_quotes["UGRO"] = {"bid": 2.79, "ask": 2.80}

    assert bot._should_fallback_to_trade_ticks("UGRO") is True

    # Tick timestamp lands in the bucket immediately after the last seed bar
    # (last seed at 2026-04-23 9:59:00 UTC = 1_776_938_340; next bucket starts
    # at 1_776_938_370; tick at 1_776_938_375 = 5s into that new bucket).
    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=3.11,
        size=200,
        timestamp_ns=1_776_938_375_000_000_000,
        strategy_codes=["polygon_30s"],
    )

    builder = bot.builder_manager.get_builder("UGRO")

    assert intents == []
    assert builder is not None
    latest_bar = builder.get_bars_with_current_as_dicts()[-1]
    assert float(latest_bar["timestamp"]) >= float(builder.bars[-1].timestamp)
    assert latest_bar["close"] == 3.11


def test_polygon_30s_trade_tick_fallback_accepts_epoch_millisecond_timestamps() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["UGRO"])
    recent_bars = build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 9, 32, 0, tzinfo=UTC))
    for index, bar in enumerate(recent_bars):
        bar["open"] = 2.50 + index * 0.01
        bar["high"] = 2.52 + index * 0.01
        bar["low"] = 2.49 + index * 0.01
        bar["close"] = 2.51 + index * 0.01
    state.seed_bars("polygon_30s", "UGRO", recent_bars)
    bot.latest_quotes["UGRO"] = {"bid": 2.79, "ask": 2.80}

    assert bot._should_fallback_to_trade_ticks("UGRO") is True

    # Same tick-timing as the ns-scale variant above, but expressed in
    # milliseconds to exercise _resolve_timestamp's ms-scale branch.
    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=3.11,
        size=200,
        timestamp_ns=1_776_938_375_000,
        strategy_codes=["polygon_30s"],
    )

    builder = bot.builder_manager.get_builder("UGRO")

    assert intents == []
    assert builder is not None
    latest_bar = builder.get_bars_with_current_as_dicts()[-1]
    assert float(latest_bar["timestamp"]) >= float(builder.bars[-1].timestamp)
    assert latest_bar["close"] == 3.11


def test_polygon_open_rejection_blocks_same_symbol_for_20_bars() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    polygon_bot = state.bots["polygon_30s"]
    polygon_bot.seed_bars(
        "UGRO",
        build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 9, 32, 0, tzinfo=UTC)),
    )

    bar_count = polygon_bot.builder_manager.get_or_create("UGRO").get_bar_count()

    state.apply_order_status(
        strategy_code="polygon_30s",
        symbol="UGRO",
        intent_type="open",
        status="rejected",
        reason="temporary broker reject",
    )

    blocked_gate = polygon_bot.entry_engine._check_hard_gates("UGRO", bar_count + 19)
    allowed_gate = polygon_bot.entry_engine._check_hard_gates("UGRO", bar_count + 20)

    assert blocked_gate == {
        "passed": False,
        "reason": "open rejection cooldown (1 bars remaining)",
    }
    assert allowed_gate == {"passed": True, "reason": ""}


def test_polygon_webull_auth_rejection_does_not_add_open_cooldown() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    polygon_bot = state.bots["polygon_30s"]
    polygon_bot.seed_bars(
        "UGRO",
        build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 9, 32, 0, tzinfo=UTC)),
    )

    bar_count = polygon_bot.builder_manager.get_or_create("UGRO").get_bar_count()

    state.apply_order_status(
        strategy_code="polygon_30s",
        symbol="UGRO",
        intent_type="open",
        status="rejected",
        reason=(
            "Webull order rejected: missing Webull App Key/App Secret; "
            "listening is active but broker auth is not configured yet"
        ),
    )

    assert polygon_bot.entry_engine._check_hard_gates("UGRO", bar_count + 1) == {
        "passed": True,
        "reason": "",
    }


def test_schwab_open_rejection_does_not_add_polygon_rejection_cooldown() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    schwab_bot = state.bots["macd_30s"]
    schwab_bot.seed_bars(
        "UGRO",
        build_recent_polygon_seed_bars(start=datetime(2026, 4, 23, 9, 32, 0, tzinfo=UTC)),
    )

    bar_count = schwab_bot.builder_manager.get_or_create("UGRO").get_bar_count()

    state.apply_order_status(
        strategy_code="macd_30s",
        symbol="UGRO",
        intent_type="open",
        status="rejected",
        reason="temporary broker reject",
    )

    assert schwab_bot.entry_engine._check_hard_gates("UGRO", bar_count + 1) == {"passed": True, "reason": ""}
