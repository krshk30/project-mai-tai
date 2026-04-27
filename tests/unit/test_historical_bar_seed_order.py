from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider
from project_mai_tai.services.strategy_engine_app import StrategyBotRuntime, StrategyDefinition
from project_mai_tai.strategy_core import IndicatorConfig, TradingConfig


def fixed_now() -> datetime:
    return datetime(2026, 4, 27, 10, 0)


def test_massive_historical_bars_are_sorted_chronologically() -> None:
    provider = MassiveSnapshotProvider(api_key="test")
    provider._client = SimpleNamespace(
        list_aggs=lambda *_args, **_kwargs: [
            SimpleNamespace(open=3.0, high=3.1, low=2.9, close=3.05, volume=300, timestamp=1_700_000_060_000),
            SimpleNamespace(open=2.0, high=2.1, low=1.9, close=2.05, volume=200, timestamp=1_700_000_000_000),
            SimpleNamespace(open=4.0, high=4.1, low=3.9, close=4.05, volume=400, timestamp=1_700_000_120_000),
        ]
    )

    bars = provider.fetch_historical_bars(
        "TEST",
        interval_secs=30,
        lookback_calendar_days=14,
        limit=50_000,
    )

    assert [bar.timestamp for bar in bars] == sorted(bar.timestamp for bar in bars)
    assert [bar.close for bar in bars] == [2.05, 3.05, 4.05]


def test_strategy_runtime_seed_bars_sorts_out_of_order_history() -> None:
    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code="webull_30s",
            display_name="Webull 30 Sec Bot",
            account_name="live:webull_30s",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=fixed_now,
    )

    runtime.seed_bars(
        "TEST",
        [
            {"open": 3.0, "high": 3.1, "low": 2.9, "close": 3.05, "volume": 300, "timestamp": 1_700_000_060.0},
            {"open": 2.0, "high": 2.1, "low": 1.9, "close": 2.05, "volume": 200, "timestamp": 1_700_000_000.0},
            {"open": 4.0, "high": 4.1, "low": 3.9, "close": 4.05, "volume": 400, "timestamp": 1_700_000_120.0},
        ],
    )

    builder = runtime.builder_manager.get_builder("TEST")
    assert builder is not None
    assert [bar.timestamp for bar in builder.bars] == [1_700_000_000.0, 1_700_000_060.0, 1_700_000_120.0]
    assert builder.get_current_price() == 4.05
