from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import OHLCVBar

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
        ),
        now_provider=lambda: datetime(2026, 4, 27, 10, 1, 0, tzinfo=EASTERN_TZ),
    )
    runtime = state.bots["macd_30s"]
    start = _seed_bars(runtime, "TEST", interval_secs=30)
    runtime.data_warning_symbols["TEST"] = "warning"
    runtime._arm_gap_recovery("TEST", synthetic_gap_count=3)
    runtime._finalize_gap_recovery_completed_bar("TEST")

    assert runtime._gap_recovery_bars_remaining["TEST"] == 3
    assert runtime._gap_recovery_synthetic_bars["TEST"] == 3
    assert "skipped 3 synthetic 30s bar(s)" in runtime.recent_decisions[0]["reason"]
    assert start > 0

    runtime._advance_gap_recovery(
        "TEST",
        OHLCVBar(
            open=5.25,
            high=5.26,
            low=5.24,
            close=5.25,
            volume=100,
            timestamp=start + (63 * 30),
            trade_count=1,
        ),
    )
    assert runtime._gap_recovery_bars_remaining["TEST"] == 2
    assert "waiting for 3 real completed bar(s)" in runtime.recent_decisions[0]["reason"]

    runtime._advance_gap_recovery(
        "TEST",
        OHLCVBar(
            open=5.26,
            high=5.27,
            low=5.25,
            close=5.26,
            volume=100,
            timestamp=start + (64 * 30),
            trade_count=1,
        ),
    )
    assert runtime._gap_recovery_bars_remaining["TEST"] == 1

    runtime._advance_gap_recovery(
        "TEST",
        OHLCVBar(
            open=5.27,
            high=5.28,
            low=5.26,
            close=5.27,
            volume=100,
            timestamp=start + (65 * 30),
            trade_count=1,
        ),
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
            strategy_macd_30s_tick_bar_close_grace_seconds=0.0,
        ),
        now_provider=lambda: now_ref["dt"],
    )
    runtime = state.bots["macd_30s"]
    start = _seed_bars(runtime, "TEST", interval_secs=30)
    runtime.data_warning_symbols["TEST"] = "warning"
    runtime._arm_gap_recovery("TEST", synthetic_gap_count=3)
    runtime._finalize_gap_recovery_completed_bar("TEST")
    runtime.builder_manager.get_or_create("TEST").time_provider = lambda: start + (61 * 30) + 1

    runtime.handle_trade_tick(
        "TEST",
        price=5.25,
        size=100,
        timestamp_ns=int((start + (60 * 30) + 5) * 1_000_000_000),
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
