from __future__ import annotations

import csv
from datetime import UTC, datetime

from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.bar_builder import BarBuilder
from project_mai_tai.strategy_core.config import (
    IndicatorConfig,
    MomentumAlertConfig,
    MomentumConfirmedConfig,
)
from project_mai_tai.strategy_core.indicators import IndicatorEngine, vwap
from project_mai_tai.strategy_core.entry import EntryEngine
from project_mai_tai.strategy_core.models import (
    DaySnapshot,
    LastTrade,
    MarketSnapshot,
    MinuteSnapshot,
    ReferenceData,
)
from project_mai_tai.strategy_core.momentum_alerts import MomentumAlertEngine
from project_mai_tai.strategy_core.momentum_confirmed import MomentumConfirmedScanner
from project_mai_tai.strategy_core.position_tracker import PositionTracker
from project_mai_tai.strategy_core.schwab_native_30s import (
    SchwabNativeBarBuilder,
    SchwabNativeBarBuilderManager,
    SchwabNativeEntryEngine,
    SchwabNativeIndicatorEngine,
)
from project_mai_tai.strategy_core.top_gainers import TopGainersTracker
from project_mai_tai.strategy_core.trading_config import TradingConfig


def snapshot(
    *,
    ticker: str,
    price: float,
    volume: int,
    high: float | None = None,
    change_pct: float = 0,
) -> MarketSnapshot:
    return MarketSnapshot(
        ticker=ticker,
        day=DaySnapshot(close=price, volume=volume, high=high or price, vwap=price),
        minute=MinuteSnapshot(close=price, accumulated_volume=volume, high=high or price, vwap=price),
        last_trade=LastTrade(price=price),
        todays_change_percent=change_pct,
    )


def test_bar_builder_keeps_odd_lots_and_does_not_fill_gaps() -> None:
    builder = BarBuilder("UGRO", interval_secs=30, time_provider=lambda: 0)
    base_ns = 1_700_000_000_000_000_000

    first_completed = builder.on_trade(price=3.5, size=50, timestamp_ns=base_ns + 30_000_000_000)
    assert first_completed == []

    completed = builder.on_trade(price=3.7, size=100, timestamp_ns=base_ns + 120_000_000_000)

    assert len(completed) == 1
    assert completed[0].timestamp % 30 == 0
    assert completed[0].volume == 50
    assert builder.get_current_price() == 3.7


def test_schwab_native_bar_builder_close_grace_delays_periodic_close() -> None:
    """check_bar_closes() should wait close_grace_seconds past bucket end before finalizing.

    With grace=5.0 and a 30s bucket starting at t=0, the bar should remain open at
    wall-clock 32s (only 2s past bucket end) and only close at 35s (5s past bucket end).
    Without grace it would close at 30s. This is the live-vs-rebuild asymmetry that
    drops late LEVELONE trades into the just-closed bucket.
    """

    clock = {"now": 0.0}
    builder = SchwabNativeBarBuilder(
        "TST",
        interval_secs=30,
        time_provider=lambda: clock["now"],
        close_grace_seconds=5.0,
    )
    builder.on_trade(price=10.0, size=100, timestamp_ns=0)

    clock["now"] = 32.0
    assert builder.check_bar_closes() == [], "bar should still be open within close_grace window"
    assert builder._current_bar is not None

    clock["now"] = 35.0
    closed = builder.check_bar_closes()
    assert len(closed) == 1
    assert closed[0].timestamp == 0
    assert builder._current_bar is None


def test_schwab_native_bar_builder_default_grace_is_zero() -> None:
    """Behavior without explicit grace is unchanged: bar closes at exactly bucket_end."""

    clock = {"now": 0.0}
    builder = SchwabNativeBarBuilder(
        "TST",
        interval_secs=30,
        time_provider=lambda: clock["now"],
    )
    builder.on_trade(price=10.0, size=100, timestamp_ns=0)

    clock["now"] = 30.0
    closed = builder.check_bar_closes()
    assert len(closed) == 1


def test_schwab_native_bar_builder_manager_propagates_close_grace() -> None:
    manager = SchwabNativeBarBuilderManager(
        interval_secs=30,
        time_provider=lambda: 0.0,
        close_grace_seconds=5.0,
    )
    builder = manager.get_or_create("TST")
    assert builder.close_grace_seconds == 5.0


def test_vwap_resets_on_regular_session_anchor() -> None:
    timestamps = [
        datetime(2026, 3, 31, 13, 29, tzinfo=UTC).timestamp(),  # 09:29 ET
        datetime(2026, 3, 31, 13, 30, tzinfo=UTC).timestamp(),  # 09:30 ET
        datetime(2026, 3, 31, 13, 31, tzinfo=UTC).timestamp(),  # 09:31 ET
    ]
    highs = [10.0, 20.0, 30.0]
    lows = [10.0, 20.0, 30.0]
    closes = [10.0, 20.0, 30.0]
    volumes = [1.0, 1.0, 1.0]

    values = vwap(
        highs,
        lows,
        closes,
        volumes,
        timestamps,
        session_start_hour=9,
        session_start_minute=30,
        session_end_hour=16,
        session_end_minute=0,
    )

    assert values[0] == 10.0
    assert values[1] == 20.0
    assert values[2] == 25.0


def test_indicator_engine_generates_expected_flags() -> None:
    bars = []
    for index in range(40):
        close = 2.0 + index * 0.05
        bars.append(
            {
                "open": close - 0.02,
                "high": close + 0.03,
                "low": close - 0.04,
                "close": close,
                "volume": 1_000 + index * 10,
                "timestamp": float(index * 30),
            }
        )

    engine = IndicatorEngine(IndicatorConfig())
    result = engine.calculate(bars)

    assert result is not None
    assert result["price"] == bars[-1]["close"]
    assert result["price_above_ema20"] is True
    assert result["price_above_vwap"] is True
    assert isinstance(result["macd_cross_above"], bool)


def test_trading_config_defaults_to_4am_through_8pm_window() -> None:
    config = TradingConfig()

    assert config.trading_start_hour == 4
    assert config.trading_end_hour == 20


def test_strategy_bots_use_eastern_clock_for_trading_hours(monkeypatch) -> None:
    fixed_et = datetime(2026, 4, 2, 16, 30)
    monkeypatch.setattr(
        "project_mai_tai.services.strategy_engine_app.now_eastern",
        lambda: fixed_et,
    )

    state = StrategyEngineState(settings=Settings(strategy_tos_enabled=True))
    tos = state.bots["tos"]

    gate = tos.entry_engine._check_hard_gates(
        "TMDE",
        {
            "price": 1.75,
            "price_above_ema20": True,
            "stoch_k": 30.0,
        },
        bar_index=1,
        position_tracker=None,
    )

    assert gate["passed"] is True


def test_alert_engine_and_confirmed_scanner_path_b() -> None:
    times = iter(
        [
            datetime(2026, 3, 28, 9, 35),
            datetime(2026, 3, 28, 9, 37, 30),
            datetime(2026, 3, 28, 9, 40),
            datetime(2026, 3, 28, 9, 42, 30),
        ]
    )
    alert_engine = MomentumAlertEngine(
        MomentumAlertConfig(
            min_price=1.0,
            max_price=10.0,
            min_momentum_volume=1_000,
            squeeze_5min_pct=5.0,
            squeeze_10min_pct=10.0,
            volume_spike_mult=2.0,
            alert_cooldown_mins=0,
        ),
        scan_interval_secs=150,
        now_provider=lambda: next(times),
    )
    confirmed_scanner = MomentumConfirmedScanner(
        MomentumConfirmedConfig(
            confirmed_min_volume=1_000,
            confirmed_max_float=1_000_000,
        )
    )
    ref = {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)}

    cycle1 = [snapshot(ticker="UGRO", price=2.0, volume=1_000)]
    alert_engine.record_snapshot(cycle1)
    alerts = alert_engine.check_alerts(cycle1, ref)
    assert alerts == []

    cycle2 = [snapshot(ticker="UGRO", price=2.0, volume=12_000)]
    alert_engine.record_snapshot(cycle2)
    alerts = alert_engine.check_alerts(cycle2, ref)
    assert [alert["type"] for alert in alerts] == ["VOLUME_SPIKE"]
    assert confirmed_scanner.process_alerts(alerts, ref, {"UGRO": cycle2[0]}) == []

    cycle3 = [snapshot(ticker="UGRO", price=2.2, volume=14_000)]
    alert_engine.record_snapshot(cycle3)
    alerts = alert_engine.check_alerts(cycle3, ref)
    assert [alert["type"] for alert in alerts] == ["SQUEEZE_5MIN"]
    assert confirmed_scanner.process_alerts(alerts, ref, {"UGRO": cycle3[0]}) == []

    cycle4 = [snapshot(ticker="UGRO", price=2.4, volume=18_000)]
    alert_engine.record_snapshot(cycle4)
    alerts = alert_engine.check_alerts(cycle4, ref)
    assert [alert["type"] for alert in alerts] == ["SQUEEZE_5MIN", "SQUEEZE_10MIN"]

    newly_confirmed = confirmed_scanner.process_alerts(alerts, ref, {"UGRO": cycle4[0]})
    assert len(newly_confirmed) == 1
    assert newly_confirmed[0]["ticker"] == "UGRO"
    assert newly_confirmed[0]["confirmation_path"] == "PATH_B_2SQ"


def test_alert_engine_backfills_missed_spike_when_late_squeeze_is_obvious() -> None:
    times = iter(
        [
            datetime(2026, 4, 17, 9, 15),
            datetime(2026, 4, 17, 9, 17, 30),
            datetime(2026, 4, 17, 9, 20),
        ]
    )
    alert_engine = MomentumAlertEngine(
        MomentumAlertConfig(
            min_price=1.0,
            max_price=10.0,
            min_momentum_volume=1_000,
            squeeze_5min_pct=5.0,
            squeeze_10min_pct=10.0,
            volume_spike_mult=2.0,
            alert_cooldown_mins=0,
        ),
        scan_interval_secs=150,
        now_provider=lambda: next(times),
    )
    ref = {"EFOI": ReferenceData(shares_outstanding=6_000_000, avg_daily_volume=3_900_000)}

    cycle1 = [snapshot(ticker="EFOI", price=4.10, volume=100_000)]
    alert_engine.record_snapshot(cycle1)
    assert alert_engine.check_alerts(cycle1, ref) == []

    cycle2 = [snapshot(ticker="EFOI", price=4.18, volume=130_000)]
    alert_engine.record_snapshot(cycle2)
    assert alert_engine.check_alerts(cycle2, ref) == []

    # Simulate the internal spike flag being missed earlier. The next explosive
    # move should still backfill a seed spike even though the remembered last
    # spike volume would normally suppress a duplicate alert.
    alert_engine._last_spike_volume["EFOI"] = 2_000_000
    cycle3 = [snapshot(ticker="EFOI", price=5.05, volume=1_500_000)]
    alert_engine.record_snapshot(cycle3)
    alerts = alert_engine.check_alerts(cycle3, ref)

    assert [alert["type"] for alert in alerts] == ["VOLUME_SPIKE", "SQUEEZE_5MIN"]
    assert alerts[0]["details"]["catchup_seed"] is True
    assert alerts[1]["ticker"] == "EFOI"


def test_alert_engine_records_recent_rejection_reasons_for_near_candidates() -> None:
    times = iter(
        [
            datetime(2026, 4, 24, 8, 30),
            datetime(2026, 4, 24, 8, 32, 30),
            datetime(2026, 4, 24, 8, 32, 35),
        ]
    )
    alert_engine = MomentumAlertEngine(
        MomentumAlertConfig(
            min_price=1.0,
            max_price=10.0,
            min_momentum_volume=1_000,
            squeeze_5min_pct=5.0,
            squeeze_10min_pct=10.0,
            volume_spike_mult=2.0,
            alert_cooldown_mins=0,
        ),
        scan_interval_secs=150,
        now_provider=lambda: next(times),
    )
    ref = {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)}

    cycle1 = [snapshot(ticker="UGRO", price=2.00, volume=1_000)]
    alert_engine.record_snapshot(cycle1)
    assert alert_engine.check_alerts(cycle1, ref) == []

    cycle2 = [snapshot(ticker="UGRO", price=2.08, volume=2_000)]
    alert_engine.record_snapshot(cycle2)
    assert alert_engine.check_alerts(cycle2, ref) == []

    exported = alert_engine.export_state()
    rejection = exported["recent_rejections"][-1]
    assert rejection["ticker"] == "UGRO"
    assert "volume_spike_gate_not_met" in rejection["reasons"]
    assert "volume_gate_closed" in rejection["reasons"]
    assert "waiting_for_10min_history" in rejection["reasons"]


def test_alert_engine_history_is_compact_and_backwards_compatible() -> None:
    alert_engine = MomentumAlertEngine(
        MomentumAlertConfig(
            min_price=1.0,
            max_price=10.0,
            min_momentum_volume=1_000,
        ),
        scan_interval_secs=5,
    )

    alert_engine.record_snapshot(
        [
            snapshot(ticker="KEEP", price=2.5, volume=12_000),
            snapshot(ticker="DROP", price=12.5, volume=15_000),
        ]
    )
    exported = alert_engine.export_state()

    history = exported["history"]
    assert isinstance(history, list)
    assert len(history) == 1
    assert history[0]["KEEP"] == (2.5, 12_000)
    assert "DROP" not in history[0]

    restored = MomentumAlertEngine(
        MomentumAlertConfig(
            min_price=1.0,
            max_price=10.0,
            min_momentum_volume=1_000,
        ),
        scan_interval_secs=5,
    )
    assert restored.restore_state(
        {
            "history": [
                {
                    "LEGACY": {
                        "price": 3.1,
                        "volume": 9_000,
                        "hod": 3.2,
                    }
                }
            ]
        }
    )
    assert restored.export_state()["history"][0]["LEGACY"] == (3.1, 9_000)


def test_confirmed_scanner_allows_same_cycle_5m_and_10m_squeeze_burst() -> None:
    confirmed_scanner = MomentumConfirmedScanner(
        MomentumConfirmedConfig(
            confirmed_min_volume=1_000,
            confirmed_max_float=1_000_000,
        )
    )
    ref = {"RENX": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)}

    volume_spike = [
        {
            "ticker": "RENX",
            "type": "VOLUME_SPIKE",
            "time": "07:31:05 AM ET",
            "price": 2.05,
            "volume": 237_057,
            "float": 50_000,
            "bid": 2.04,
            "ask": 2.05,
            "bid_size": 100,
            "ask_size": 500,
        }
    ]
    assert confirmed_scanner.process_alerts(volume_spike, ref, {"RENX": snapshot(ticker="RENX", price=2.05, volume=237_057)}) == []

    burst_alerts = [
        {
            "ticker": "RENX",
            "type": "SQUEEZE_5MIN",
            "time": "07:31:05 AM ET",
            "price": 2.05,
            "volume": 237_057,
            "float": 50_000,
            "bid": 2.04,
            "ask": 2.05,
            "bid_size": 100,
            "ask_size": 500,
            "details": {"change_pct": 12.1},
        },
        {
            "ticker": "RENX",
            "type": "SQUEEZE_10MIN",
            "time": "07:31:05 AM ET",
            "price": 2.05,
            "volume": 237_057,
            "float": 50_000,
            "bid": 2.04,
            "ask": 2.05,
            "bid_size": 100,
            "ask_size": 500,
            "details": {"change_pct": 12.0},
        },
    ]

    newly_confirmed = confirmed_scanner.process_alerts(
        burst_alerts,
        ref,
        {"RENX": snapshot(ticker="RENX", price=2.05, volume=237_057)},
    )

    assert [item["ticker"] for item in newly_confirmed] == ["RENX"]
    assert newly_confirmed[0]["confirmation_path"] == "PATH_B_2SQ"


def test_top_gainer_changes_use_eastern_time_labels() -> None:
    tracker = TopGainersTracker()
    ref = {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=175_000)}

    gainers, changes = tracker.update(
        [snapshot(ticker="UGRO", price=2.5, volume=900_000, change_pct=12.5)],
        ref,
        now=datetime(2026, 3, 28, 10, 0),
    )

    assert gainers
    assert changes
    assert str(changes[0]["time"]).endswith("ET")


def test_confirmed_scanner_retains_faded_candidates_for_session_continuity() -> None:
    confirmed_scanner = MomentumConfirmedScanner(
        MomentumConfirmedConfig(
            confirmed_min_volume=1_000,
            confirmed_max_float=1_000_000,
            live_min_change_pct=20.0,
        )
    )
    confirmed_scanner.seed_confirmed_candidates(
        [
            {
                "ticker": "POLA",
                "confirmed_at": "08:00:00 AM ET",
                "entry_price": 2.30,
                "price": 2.32,
                "change_pct": 19.4,
                "volume": 900_000,
                "rvol": 8.0,
                "shares_outstanding": 1_000_000,
                "confirmation_path": "PATH_B_2SQ",
            }
        ]
    )
    confirmed_scanner._tracking["POLA"] = {
        "has_volume_spike": True,
        "first_spike_time": "07:45:00 AM ET",
        "first_spike_price": 2.1,
        "first_spike_volume": 500_000,
        "squeezes": [{"time": "08:00:00 AM ET", "price": 2.32, "volume": 900_000}],
        "confirmed": True,
        "confirmed_at": "08:00:00 AM ET",
        "confirmed_price": 2.32,
    }

    dropped = confirmed_scanner.prune_faded_candidates()

    assert dropped == []
    assert [item["ticker"] for item in confirmed_scanner.get_all_confirmed()] == ["POLA"]
    assert confirmed_scanner._tracking["POLA"]["confirmed"] is True
    assert confirmed_scanner._tracking["POLA"]["has_volume_spike"] is True
    assert confirmed_scanner._tracking["POLA"]["squeezes"] == [{"time": "08:00:00 AM ET", "price": 2.32, "volume": 900_000}]


def test_confirmed_scanner_single_candidate_gets_full_rank_score() -> None:
    confirmed_scanner = MomentumConfirmedScanner(MomentumConfirmedConfig(rank_min_score=50.0))
    confirmed_scanner.seed_confirmed_candidates(
        [
            {
                "ticker": "CYCN",
                "change_pct": 45.0,
                "volume": 5_000_000,
                "rvol": 20.0,
                "shares_outstanding": 4_000_000,
                "bid": 3.8,
                "ask": 3.81,
            }
        ]
    )

    top = confirmed_scanner.get_top_n(min_change_pct=20.0)

    assert len(top) == 1
    assert top[0]["ticker"] == "CYCN"
    assert top[0]["rank_score"] == 100.0


def test_entry_engine_allows_default_window_until_8pm_et() -> None:
    engine = EntryEngine(
        TradingConfig(),
        now_provider=lambda: datetime(2026, 3, 30, 19, 0),
    )

    gate = engine._check_hard_gates(
        "ELAB",
        {
            "price_above_ema20": True,
        },
        bar_index=1,
    )

    assert gate["passed"] is True


def test_entry_engine_no_longer_blocks_midday_dead_zone() -> None:
    engine = EntryEngine(
        TradingConfig(),
        now_provider=lambda: datetime(2026, 3, 30, 13, 30),
    )

    gate = engine._check_hard_gates(
        "ELAB",
        {
            "price_above_ema20": True,
        },
        bar_index=1,
    )

    assert gate["passed"] is True


def test_schwab_native_indicator_engine_emits_distance_fields() -> None:
    bars = []
    for index in range(55):
        close = 2.0 + index * 0.01
        bars.append(
            {
                "open": close - 0.01,
                "high": close + 0.02,
                "low": close - 0.02,
                "close": close,
                "volume": 3_000 + index * 25,
                "timestamp": float(index * 30),
            }
        )

    indicator_config = IndicatorConfig()
    indicator_config.schwab_native_warmup_bars_required = 50  # type: ignore[attr-defined]
    engine = SchwabNativeIndicatorEngine(indicator_config)
    result = engine.calculate(bars)

    assert result is not None
    assert "ema9_dist_pct" in result
    assert "vwap_dist_pct" in result
    assert "bars_below_signal_prev" in result


def test_schwab_native_entry_engine_can_fire_p4_burst() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    engine = SchwabNativeEntryEngine(
        config,
        now_provider=lambda: datetime(2026, 4, 17, 10, 0),
    )

    history = []
    for index in range(54):
        price = 2.00 + index * 0.002
        history.append(
            {
                "open": price - 0.01,
                "price": price,
                "high": price + 0.01,
                "low": price - 0.015,
                "volume": 3_000.0,
                "ema9": price - 0.005,
                "ema20": price - 0.015,
                "vwap": price - 0.02,
                "vol_avg20": 3_000.0,
                "vol_avg5": 3_000.0,
            }
        )
    engine.seed_recent_bars("ELAB", history)

    signal = engine.check_entry(
        "ELAB",
        {
            "open": 2.10,
            "price": 2.20,
            "high": 2.22,
            "low": 2.09,
            "volume": 12_000.0,
            "ema9": 2.12,
            "ema20": 2.05,
            "vwap": 2.08,
            "vol_avg20": 3_500.0,
            "vol_avg5": 3_500.0,
            "macd": 0.01,
            "signal": 0.02,
            "histogram": 0.03,
            "stoch_k": 70.0,
            "macd_cross_above": False,
            "bars_below_signal_prev": 0,
            "price_cross_above_vwap": False,
            "macd_above_signal": False,
            "macd_increasing": False,
            "macd_delta": 0.0,
            "macd_delta_prev": 0.01,
            "hist_value": 0.03,
            "price_above_ema9": True,
            "price_above_ema20": True,
            "price_above_vwap": True,
            "hist_growing": True,
            "stoch_k_rising": True,
            "ema9_dist_pct": 1.5,
            "vwap_dist_pct": 5.0,
            "ema9_trend_rising": True,
            "in_regular_session": True,
            "stoch_cross_below_exit": False,
        },
        bar_index=55,
        position_tracker=None,
    )

    assert signal is not None
    assert signal["path"] == "P4_BURST"


def _make_schwab_native_base_indicators() -> dict[str, float | bool]:
    return {
        "open": 2.00,
        "price": 2.05,
        "high": 2.06,
        "low": 1.99,
        "volume": 6_000.0,
        "ema9": 2.01,
        "ema20": 1.98,
        "vwap": 1.99,
        "vol_avg20": 2_500.0,
        "vol_avg5": 2_500.0,
        "macd": 0.03,
        "signal": 0.02,
        "histogram": 0.01,
        "stoch_k": 70.0,
        "macd_cross_above": True,
        "bars_below_signal_prev": 4,
        "price_cross_above_vwap": False,
        "macd_above_signal": True,
        "macd_increasing": True,
        "macd_delta": 0.02,
        "macd_delta_prev": 0.01,
        "hist_value": 0.02,
        "price_above_ema9": True,
        "price_above_ema20": True,
        "price_above_vwap": True,
        "hist_growing": True,
        "stoch_k_rising": True,
        "ema9_dist_pct": 1.5,
        "vwap_dist_pct": 5.0,
        "ema9_trend_rising": True,
        "in_regular_session": True,
        "stoch_cross_below_exit": False,
        "macd_cross_below": False,
    }


def _seed_schwab_native_chop_history(engine: SchwabNativeEntryEngine, ticker: str) -> None:
    history: list[dict[str, float | bool]] = []
    for index in range(40, 60):
        close = 2.03 if index % 2 == 0 else 1.98
        history.append(
            {
                "open": 2.00,
                "price": close,
                "high": close + 0.04,
                "low": close - 0.04,
                "volume": 4_000.0,
                "ema9": 2.00,
                "ema20": 2.00 + ((index % 3) - 1) * 0.002,
                "vwap": 2.01,
                "vol_avg20": 3_000.0,
                "vol_avg5": 3_000.0,
                "ema9_prev": 2.00,
                "hist_value": 0.02 if index % 2 == 0 else 0.015,
            }
        )
    engine.seed_recent_bars(ticker, history)


def test_schwab_native_entry_engine_blocks_p1_on_stoch_k_cap() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators["stoch_k"] = 95.0

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None


def test_schwab_native_entry_engine_blocks_p1_on_ema9_distance_cap() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators["ema9_dist_pct"] = 12.0

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None


def test_schwab_native_entry_engine_blocks_p1_on_vwap_distance_cap() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators["vwap_dist_pct"] = 15.0

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None


def test_schwab_native_entry_engine_can_fire_p3_with_high_vwap_override() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "vwap_dist_pct": 20.0,
            "ema9_dist_pct": 1.5,
            "price_above_vwap": False,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is not None
    assert signal["path"] == "P3_SURGE"


def test_schwab_native_entry_engine_blocks_p3_when_momentum_override_would_have_fired() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    config.p3_allow_momentum_override = True
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "vwap_dist_pct": 25.0,
            "ema9_dist_pct": 5.0,
            "stoch_k": 90.0,
            "price_above_vwap": False,
            "volume": 7_000.0,
            "vol_avg20": 3_000.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None
    decision = engine.pop_last_decision("ELAB")
    assert decision is not None
    assert decision["status"] == "blocked"
    assert decision["path"] == "P3_SURGE"
    assert "P3 entry stoch_k cap (90.0 >= 85.0)" == decision["reason"]


def test_schwab_native_entry_engine_blocks_p3_when_entry_stoch_k_hits_cap() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "vwap_dist_pct": 20.0,
            "ema9_dist_pct": 1.5,
            "price_above_vwap": False,
            "stoch_k": 88.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None
    decision = engine.pop_last_decision("ELAB")
    assert decision is not None
    assert decision["status"] == "blocked"
    assert decision["path"] == "P3_SURGE"
    assert "P3 entry stoch_k cap (88.0 >= 85.0)" == decision["reason"]


def test_schwab_native_entry_engine_allows_p3_when_entry_stoch_k_is_below_cap() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "vwap_dist_pct": 20.0,
            "ema9_dist_pct": 1.5,
            "price_above_vwap": False,
            "stoch_k": 84.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is not None
    assert signal["path"] == "P3_SURGE"


def test_schwab_native_entry_engine_blocks_p1_when_chop_lock_hits_threshold() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    _seed_schwab_native_chop_history(engine, "ELAB")
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "price": 2.02,
            "high": 2.06,
            "low": 1.98,
            "ema9": 2.015,
            "ema20": 2.00,
            "vwap": 2.01,
            "vol_avg20": 3_000.0,
            "vol_avg5": 3_000.0,
            "ema9_dist_pct": 0.25,
            "vwap_dist_pct": 0.5,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None
    decision = engine.pop_last_decision("ELAB")
    assert decision is not None
    assert decision["status"] == "blocked"
    assert "chop lock active (current 4/4)" in decision["reason"]
    assert "COMPRESS" in decision["reason"]
    assert "EMA20_FLAT" in decision["reason"]
    assert "WHIPSAW" in decision["reason"]
    assert "NO_CLEAN_SIDE" in decision["reason"]


def test_schwab_native_entry_engine_allows_p3_extreme_override_during_chop_lock() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    _seed_schwab_native_chop_history(engine, "ELAB")
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "open": 2.03,
            "price": 2.18,
            "high": 2.23,
            "low": 2.03,
            "volume": 7_000.0,
            "ema9": 2.07,
            "ema20": 2.04,
            "vwap": 2.05,
            "vol_avg20": 3_000.0,
            "vol_avg5": 3_500.0,
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "macd_delta": 0.003,
            "macd_delta_prev": 0.001,
            "hist_value": 0.04,
            "histogram": 0.04,
            "hist_growing": True,
            "price_above_vwap": True,
            "price_above_ema9": True,
            "price_above_ema20": True,
            "ema9_dist_pct": 3.0,
            "vwap_dist_pct": 6.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is not None
    assert signal["path"] == "P3_SURGE"


def test_schwab_native_entry_engine_can_fire_p5_pullback() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    engine._recent_bars["ELAB"] = [
        {
            "bar_index": float(index),
            "open": 2.08 + (index - 43) * 0.01,
            "close": 2.09 + (index - 43) * 0.01,
            "high": 2.10 + (index - 43) * 0.01,
            "low": 2.06 + (index - 43) * 0.01,
            "volume": 3_000.0,
            "ema9": 2.05 + (index - 43) * 0.009,
            "ema20": 2.01 + (index - 43) * 0.008,
            "vwap": 2.04 + (index - 43) * 0.008,
            "vol_avg20": 3_000.0,
            "vol_avg5": 3_000.0,
            "ema9_prev": 2.04 + (index - 43) * 0.009,
        }
        for index in range(43, 55)
    ]
    engine._spike_anchor_bar["ELAB"] = 50
    engine._spike_anchor_high["ELAB"] = 2.25
    engine._session_highs["ELAB"] = 2.28

    signal = engine.check_entry(
        "ELAB",
        {
            "open": 2.17,
            "price": 2.245,
            "high": 2.25,
            "low": 2.16,
            "volume": 5_000.0,
            "ema9": 2.16,
            "ema20": 2.08,
            "vwap": 2.12,
            "vol_avg20": 3_000.0,
            "vol_avg5": 4_000.0,
            "ema9_prev": 2.15,
            "macd": 0.01,
            "signal": 0.02,
            "histogram": 0.01,
            "stoch_k": 68.0,
            "macd_cross_above": False,
            "bars_below_signal_prev": 0,
            "price_cross_above_vwap": False,
            "macd_above_signal": False,
            "macd_increasing": False,
            "macd_delta": 0.0,
            "macd_delta_prev": 0.0,
            "hist_value": 0.0,
            "price_above_ema9": True,
            "price_above_ema20": True,
            "price_above_vwap": True,
            "hist_growing": True,
            "stoch_k_rising": True,
            "ema9_dist_pct": 4.5,
            "vwap_dist_pct": 6.0,
            "ema9_trend_rising": True,
            "in_regular_session": True,
            "stoch_cross_below_exit": False,
            "macd_cross_below": False,
        },
        bar_index=55,
        position_tracker=None,
    )

    assert signal is not None
    assert signal["path"] == "P5_PULLBACK"


def test_position_tracker_loads_closed_trades_from_sibling_data_dir(tmp_path, monkeypatch) -> None:
    repo_dir = tmp_path / "project-mai-tai"
    repo_dir.mkdir()
    history_dir = tmp_path / "project-mai-tai-data" / "history"
    history_dir.mkdir(parents=True)
    filepath = history_dir / "macdbot_closed_2026-03-30.csv"

    with filepath.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "ticker",
                "entry_price",
                "exit_price",
                "quantity",
                "pnl",
                "pnl_pct",
                "reason",
                "entry_time",
                "exit_time",
                "peak_profit_pct",
                "tier",
                "scales_done",
                "path",
            ]
        )
        writer.writerow(
            [
                "ELAB",
                "3.10",
                "3.40",
                "100",
                "30.0",
                "9.68",
                "OMS_FILL",
                "2026-03-30 03:00:00 PM ET",
                "2026-03-30 03:10:00 PM ET",
                "9.7",
                "2",
                "",
                "P1_MACD_CROSS",
            ]
    )

    monkeypatch.chdir(repo_dir)
    monkeypatch.setattr(
        "project_mai_tai.strategy_core.position_tracker.today_eastern_str",
        lambda: "2026-03-30",
    )
    tracker = PositionTracker(TradingConfig())

    tracker.load_closed_trades()

    assert tracker.get_daily_pnl() == 30.0
    assert tracker.get_closed_today()[0]["ticker"] == "ELAB"
