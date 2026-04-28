from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.settings import Settings

EASTERN_TZ = ZoneInfo("America/New_York")


def _seed_bars(runtime, symbol: str, *, interval_secs: int, count: int = 60) -> float:
    start = datetime(2026, 4, 27, 13, 30, tzinfo=UTC).timestamp()
    bars = []
    for index in range(count):
        timestamp = start + (index * interval_secs)
        price = 5.0 + (index * 0.001)
        bars.append(
            {
                "open": price,
                "high": price + 0.01,
                "low": price - 0.01,
                "close": price,
                "volume": 100,
                "timestamp": timestamp,
                "trade_count": 1,
            }
        )
    runtime.set_watchlist([symbol])
    runtime.seed_bars(symbol, bars)
    return start


def test_schwab_30s_gap_recovery_blocks_entries_until_real_bars_rebuild() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_webull_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=False,
        )
    )
    runtime = state.bots["macd_30s"]
    runtime.now_provider = lambda: datetime(2026, 4, 27, 10, 1, 0, tzinfo=EASTERN_TZ)
    start = _seed_bars(runtime, "TEST", interval_secs=30)

    runtime.handle_trade_tick(
        "TEST",
        price=5.25,
        size=100,
        timestamp_ns=int((start + (63 * 30) + 5) * 1_000_000_000),
    )

    assert runtime._gap_recovery_bars_remaining["TEST"] == 3
    assert runtime._gap_recovery_synthetic_bars["TEST"] == 3
    assert "skipped 3 synthetic 30s bar(s)" in runtime.recent_decisions[0]["reason"]

    runtime.handle_trade_tick(
        "TEST",
        price=5.26,
        size=100,
        timestamp_ns=int((start + (64 * 30) + 5) * 1_000_000_000),
    )
    assert runtime._gap_recovery_bars_remaining["TEST"] == 2
    assert "waiting for 3 real completed bar(s)" in runtime.recent_decisions[0]["reason"]

    runtime.handle_trade_tick(
        "TEST",
        price=5.27,
        size=100,
        timestamp_ns=int((start + (65 * 30) + 5) * 1_000_000_000),
    )
    assert runtime._gap_recovery_bars_remaining["TEST"] == 1

    runtime.handle_trade_tick(
        "TEST",
        price=5.28,
        size=100,
        timestamp_ns=int((start + (66 * 30) + 5) * 1_000_000_000),
    )
    assert "TEST" not in runtime._gap_recovery_bars_remaining
    assert "TEST" not in runtime._gap_recovery_synthetic_bars


def test_gap_recovery_window_scales_by_interval() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_webull_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        )
    )

    assert state.bots["macd_30s"]._gap_recovery_bars_required() == 3
    assert state.bots["schwab_1m"]._gap_recovery_bars_required() == 2


def test_flush_completed_bars_advances_gap_recovery() -> None:
    now_ref = {"dt": datetime(2026, 4, 27, 10, 2, 0, tzinfo=EASTERN_TZ)}
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_webull_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=False,
        )
    )
    runtime = state.bots["macd_30s"]
    runtime.now_provider = lambda: now_ref["dt"]
    start = _seed_bars(runtime, "TEST", interval_secs=30)
    runtime.builder_manager.get_or_create("TEST").time_provider = lambda: start + (64 * 30) + 1

    runtime.handle_trade_tick(
        "TEST",
        price=5.25,
        size=100,
        timestamp_ns=int((start + (63 * 30) + 5) * 1_000_000_000),
    )
    assert runtime._gap_recovery_bars_remaining["TEST"] == 3

    runtime.handle_trade_tick(
        "TEST",
        price=5.26,
        size=100,
        timestamp_ns=int((start + (63 * 30) + 10) * 1_000_000_000),
    )
    assert runtime._gap_recovery_bars_remaining["TEST"] == 3

    now_ref["dt"] = datetime(2026, 4, 27, 10, 2, 1, tzinfo=EASTERN_TZ)
    _intents, completed_count = runtime.flush_completed_bars()

    assert completed_count >= 1
    assert runtime._gap_recovery_bars_remaining["TEST"] == 2


def test_after_hours_synthetic_gap_does_not_arm_recovery_for_flat_symbol() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=False,
            strategy_webull_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_schwab_1m_enabled=True,
        )
    )
    runtime = state.bots["schwab_1m"]
    runtime.now_provider = lambda: datetime(2026, 4, 27, 20, 45, 0, tzinfo=EASTERN_TZ)
    start = _seed_bars(runtime, "TEST", interval_secs=60)

    runtime.handle_trade_tick(
        "TEST",
        price=5.25,
        size=100,
        timestamp_ns=int((start + (66 * 60) + 5) * 1_000_000_000),
    )

    assert "TEST" not in runtime._gap_recovery_bars_remaining
    assert "TEST" not in runtime._gap_recovery_synthetic_bars
