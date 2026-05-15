from __future__ import annotations

import csv
from dataclasses import replace
from datetime import UTC, datetime
import pytest

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
    OHLCVBar,
    ReferenceData,
)
from project_mai_tai.strategy_core.momentum_alerts import MomentumAlertEngine
from project_mai_tai.strategy_core.momentum_confirmed import MomentumConfirmedScanner
from project_mai_tai.strategy_core.position_tracker import PositionTracker
from project_mai_tai.strategy_core.schwab_native_30s import (
    SchwabNativeBarBuilder,
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


def test_bar_builder_ignores_late_same_bucket_trade_after_flush() -> None:
    clock = {"ts": 1_700_000_030.0}
    builder = BarBuilder("UGRO", interval_secs=30, time_provider=lambda: clock["ts"])

    builder.on_trade(price=3.50, size=50)
    clock["ts"] = 1_700_000_060.0
    closed = builder.check_bar_close()

    assert closed is not None
    assert closed.timestamp == 1_700_000_010.0

    late_same_bucket = builder.on_trade(
        price=3.55,
        size=25,
        timestamp_ns=1_700_000_039_000_000_000,
    )

    assert late_same_bucket == []
    assert builder.get_bar_count() == 1
    assert builder._current_bar is None


def test_schwab_native_bar_builder_ignores_late_same_bucket_aggregate_after_flush() -> None:
    clock = {"ts": 1_700_000_070.0}
    builder = SchwabNativeBarBuilder("RDAC", interval_secs=30, time_provider=lambda: clock["ts"])

    builder.on_bar(
        OHLCVBar(
            open=1.20,
            high=1.21,
            low=1.19,
            close=1.20,
            volume=100,
            timestamp=1_700_000_050.0,
            trade_count=2,
        )
    )
    clock["ts"] = 1_700_000_090.0
    closed = builder.check_bar_closes()

    assert len(closed) == 1
    assert closed[0].timestamp == 1_700_000_040.0

    late_same_bucket = builder.on_bar(
        OHLCVBar(
            open=1.20,
            high=1.22,
            low=1.18,
            close=1.21,
            volume=150,
            timestamp=1_700_000_069.0,
            trade_count=3,
        )
    )

    assert late_same_bucket == []
    assert builder.get_bar_count() == 1
    assert builder._current_bar is None


def test_schwab_native_bar_builder_close_grace_keeps_same_bucket_trade_real() -> None:
    clock = {"ts": 1_700_000_059.5}
    builder = SchwabNativeBarBuilder(
        "RDAC",
        interval_secs=30,
        time_provider=lambda: clock["ts"],
        close_grace_seconds=2.0,
    )

    builder.on_trade(price=3.50, size=50)
    clock["ts"] = 1_700_000_070.5

    assert builder.check_bar_closes() == []

    clock["ts"] = 1_700_000_070.8
    late_trade = builder.on_trade(
        price=3.55,
        size=25,
        timestamp_ns=1_700_000_069_800_000_000,
    )

    assert late_trade == []

    clock["ts"] = 1_700_000_072.1
    closed = builder.check_bar_closes()

    assert len(closed) == 1
    assert closed[0].timestamp == 1_700_000_040.0
    assert closed[0].close == pytest.approx(3.55)
    assert closed[0].volume == 75
    assert closed[0].trade_count == 2


def test_schwab_native_bar_builder_late_trade_replaces_synthetic_flat_bar() -> None:
    clock = {"ts": 1_700_000_101.0}
    builder = SchwabNativeBarBuilder(
        "RDAC",
        interval_secs=30,
        time_provider=lambda: clock["ts"],
        fill_gap_bars=True,
    )

    builder.on_trade(
        price=3.50,
        size=50,
        timestamp_ns=1_700_000_045_000_000_000,
        cumulative_volume=1_000,
    )
    closed = builder.check_bar_closes()

    assert len(closed) == 2
    assert closed[0].timestamp == 1_700_000_040.0
    assert closed[1].timestamp == 1_700_000_070.0
    assert closed[1].volume == 0
    assert closed[1].trade_count == 0

    late_trade = builder.on_trade(
        price=3.62,
        size=25,
        timestamp_ns=1_700_000_073_500_000_000,
        cumulative_volume=1_125,
    )

    assert late_trade == []
    assert builder.get_bar_count() == 1
    assert builder.bars[-1].timestamp == 1_700_000_040.0
    assert builder._current_bar is not None
    assert builder._current_bar.timestamp == 1_700_000_070.0
    assert builder._current_bar.open == pytest.approx(3.62)
    assert builder._current_bar.close == pytest.approx(3.62)
    # The 2026-05-07 cum-volume baseline-preservation fix means the late trade's
    # volume contribution is computed as cum_vol delta against the prior trade's
    # cum_vol (1125 - 1000 = 125), not a fallback to the trade's `size` field.
    # See test_strategy_core_cum_vol_fix.py for the regression test of that fix.
    assert builder._current_bar.volume == 125
    assert builder._current_bar.trade_count == 1


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


def test_extended_vwap_default_covers_postmarket_session() -> None:
    config = IndicatorConfig()

    timestamps = [
        datetime(2026, 3, 31, 19, 59, tzinfo=UTC).timestamp(),  # 15:59 ET
        datetime(2026, 3, 31, 20, 0, tzinfo=UTC).timestamp(),   # 16:00 ET
        datetime(2026, 3, 31, 20, 1, tzinfo=UTC).timestamp(),   # 16:01 ET
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
        session_start_hour=config.extended_vwap_session_start_hour,
        session_start_minute=config.extended_vwap_session_start_minute,
        session_end_hour=config.extended_vwap_session_end_hour,
        session_end_minute=config.extended_vwap_session_end_minute,
    )

    assert config.extended_vwap_session_end_hour == 20
    assert values[0] == 10.0
    assert values[1] == 15.0
    assert values[2] == 20.0


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


def test_confirmed_scanner_path_c_uses_30pct_extreme_mover_threshold() -> None:
    confirmed_scanner = MomentumConfirmedScanner(
        MomentumConfirmedConfig(
            confirmed_min_volume=1_000,
            confirmed_max_float=50_000_000,
        )
    )
    ref = {"SNAP": ReferenceData(shares_outstanding=5_000_000, avg_daily_volume=390_000)}
    snap = snapshot(ticker="SNAP", price=2.60, volume=600_000, change_pct=35.0)
    snap.previous_close = 2.0
    alerts = [
        {
            "ticker": "SNAP",
            "type": "SQUEEZE_5MIN",
            "time": "08:00:00 AM ET",
            "price": 2.60,
            "volume": 600_000,
            "float": 5_000_000,
            "bid": 2.59,
            "ask": 2.60,
            "bid_size": 100,
            "ask_size": 100,
            "details": {"change_pct": 6.0},
        }
    ]

    confirmed = confirmed_scanner.process_alerts(alerts, ref, {"SNAP": snap})

    assert len(confirmed) == 1
    assert confirmed[0]["confirmation_path"] == "PATH_C_EXTREME_MOVER"


def test_confirmed_scanner_uses_tiered_float_turnover_thresholds() -> None:
    confirmed_scanner = MomentumConfirmedScanner(
        MomentumConfirmedConfig(
            confirmed_min_volume=500_000,
            confirmed_max_float=50_000_000,
        )
    )

    passed, reason = confirmed_scanner._check_common_filters({"volume": 900_000}, 9_000_000)
    assert passed is True
    assert reason == ""

    passed, reason = confirmed_scanner._check_common_filters({"volume": 1_800_000}, 18_000_000)
    assert passed is True
    assert reason == ""

    passed, reason = confirmed_scanner._check_common_filters({"volume": 3_600_000}, 30_000_000)
    assert passed is True
    assert reason == ""

    passed, reason = confirmed_scanner._check_common_filters({"volume": 3_300_000}, 33_000_000)
    assert passed is False
    assert "need >=12%" in reason


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


def test_schwab_native_indicator_engine_requires_real_warmup_bars() -> None:
    bars: list[OHLCVBar] = []
    for index in range(49):
        close = 2.0 + index * 0.01
        bars.append(
            OHLCVBar(
                open=close - 0.01,
                high=close + 0.02,
                low=close - 0.02,
                close=close,
                volume=3_000 + index * 25,
                timestamp=float(index * 30),
                trade_count=1,
            )
        )
    for offset in range(49, 52):
        bars.append(OHLCVBar.flat_fill(bars[-1].close, float(offset * 30)))

    indicator_config = IndicatorConfig()
    indicator_config.schwab_native_warmup_bars_required = 50  # type: ignore[attr-defined]
    engine = SchwabNativeIndicatorEngine(indicator_config)

    assert engine.calculate(bars) is None

    close = 2.0 + 49 * 0.01
    bars.append(
        OHLCVBar(
            open=close - 0.01,
            high=close + 0.02,
            low=close - 0.02,
            close=close,
            volume=3_000 + 49 * 25,
            timestamp=float(52 * 30),
            trade_count=1,
        )
    )

    assert engine.calculate(bars) is not None


def test_schwab_native_indicator_engine_skips_synthetic_bar_math_progression() -> None:
    real_bars: list[OHLCVBar] = []
    for index in range(55):
        close = 2.0 + index * 0.01
        real_bars.append(
            OHLCVBar(
                open=close - 0.01,
                high=close + 0.02,
                low=close - 0.02,
                close=close,
                volume=3_000 + index * 25,
                timestamp=float(index * 30),
                trade_count=1,
            )
        )

    synthetic_bars = [
        OHLCVBar.flat_fill(real_bars[-1].close, float(55 * 30)),
        OHLCVBar.flat_fill(real_bars[-1].close, float(56 * 30)),
    ]

    indicator_config = IndicatorConfig()
    indicator_config.schwab_native_warmup_bars_required = 50  # type: ignore[attr-defined]
    engine = SchwabNativeIndicatorEngine(indicator_config)

    baseline = engine.calculate(real_bars)
    with_synthetic = engine.calculate([*real_bars, *synthetic_bars])

    assert baseline is not None
    assert with_synthetic is not None
    for field in (
        "ema9",
        "ema20",
        "macd",
        "signal",
        "histogram",
        "vwap",
        "vol_avg20",
        "vol_avg5",
    ):
        assert with_synthetic[field] == pytest.approx(baseline[field])


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


def test_schwab_native_entry_engine_can_fire_p4_burst_from_previous_bar_setup() -> None:
    # `make_30s_schwab_native_variant` defaults `p4_prev_bar_entry_enabled` to
    # False (the feature is opt-in; see trading_config.py:389). This test
    # specifically validates the prev-bar path, so we explicitly enable the
    # feature it's exercising via dataclasses.replace.
    config = replace(
        TradingConfig().make_30s_schwab_native_variant(),
        p4_prev_bar_entry_enabled=True,
    )
    engine = SchwabNativeEntryEngine(
        config,
        now_provider=lambda: datetime(2026, 4, 17, 10, 0),
    )

    history = []
    for index in range(53):
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
    history.append(
        {
            "open": 2.14,
            "close": 2.18,
            "high": 2.19,
            "low": 2.13,
            "volume": 4_000.0,
            "ema9": 2.12,
            "ema20": 2.08,
            "vwap": 2.15,
            "vol_avg20": 3_000.0,
            "vol_avg5": 3_000.0,
        }
    )
    engine.seed_recent_bars("ELAB", history)

    signal = engine.check_entry(
        "ELAB",
        {
            "open": 2.17,
            "price": 2.205,
            "high": 2.215,
            "low": 2.165,
            "volume": 3_200.0,
            "ema9": 2.13,
            "ema20": 2.09,
            "vwap": 2.16,
            "vol_avg20": 3_100.0,
            "vol_avg5": 3_100.0,
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
            "vwap_dist_pct": 2.0,
            "ema9_trend_rising": True,
            "in_regular_session": True,
            "stoch_cross_below_exit": False,
        },
        bar_index=55,
        position_tracker=None,
    )

    assert signal is not None
    assert signal["path"] == "P4_BURST"


def test_schwab_native_entry_engine_blocks_p4_when_ema9_extension_is_too_large() -> None:
    config = TradingConfig().make_1m_schwab_native_variant()
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
            "ema9_dist_pct": 4.0,
            "vwap_dist_pct": 5.0,
            "ema9_trend_rising": True,
            "in_regular_session": True,
            "stoch_cross_below_exit": False,
        },
        bar_index=55,
        position_tracker=None,
    )

    assert signal is None


def test_schwab_native_entry_engine_respects_disabled_p4_burst_path() -> None:
    config = TradingConfig().make_1m_schwab_native_variant()
    config.p4_enabled = False
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

    assert signal is None


def _make_schwab_native_base_indicators() -> dict[str, float | bool]:
    return {
        "open": 2.00,
        "price": 2.05,
        "high": 2.06,
        "low": 1.99,
        "volume": 20_000.0,
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
                "volume": 20_000.0,
                "vol_avg20": 3_000.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None
    decision = engine.pop_last_decision("ELAB")
    assert decision is not None
    assert decision["status"] == "blocked"
    assert decision["path"] == "P3_SURGE"
    assert "P3 entry stoch_k cap (90.0 >= 80.0)" == decision["reason"]


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
    assert "P3 entry stoch_k cap (88.0 >= 80.0)" == decision["reason"]


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
            "stoch_k": 79.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is not None
    assert signal["path"] == "P3_SURGE"


def test_schwab_native_entry_engine_blocks_p3_when_absolute_volume_is_too_low() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "stoch_k": 70.0,
            "volume": 9_500.0,
            "vol_avg20": 5_000.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None
    decision = engine.pop_last_decision("ELAB")
    assert decision is not None
    assert "p3_vol" in decision["score_details"]


def test_schwab_native_entry_engine_blocks_p3_when_macd_cross_is_too_old() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    history: list[dict[str, float | bool]] = []
    for index in range(52, 60):
        snapshot = _make_schwab_native_base_indicators()
        snapshot.update(
            {
                "open": 2.00,
                "price": 2.02,
                "high": 2.03,
                "low": 1.99,
                "volume": 12_500.0,
                "vol_avg20": 5_000.0,
                "macd_cross_above": index == 55,
                "price_cross_above_vwap": False,
            }
        )
        history.append(snapshot)
    engine.seed_recent_bars("ELAB", history)
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "stoch_k": 70.0,
            "volume": 12_500.0,
            "vol_avg20": 5_000.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None
    decision = engine.pop_last_decision("ELAB")
    assert decision is not None
    assert "cross_age" in decision["score_details"]


def test_schwab_native_entry_engine_blocks_p3_when_recent_runup_is_too_large() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    engine._recent_bars["ELAB"] = [
        {
            "bar_index": float(index),
            "open": 2.00 + (index - 52) * 0.02,
            "close": 2.02 + (index - 52) * 0.02,
            "high": 2.04 + (index - 52) * 0.03,
            "low": 1.98 + (index - 52) * 0.01,
            "volume": 12_500.0,
            "ema9": 2.00,
            "ema20": 1.95,
            "vwap": 1.99,
            "vol_avg20": 5_000.0,
            "vol_avg5": 5_000.0,
            "ema9_prev": 1.99,
            "hist_value": 0.02,
            "macd_cross_above": index == 58,
        }
        for index in range(52, 60)
    ]
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "open": 2.30,
            "stoch_k": 70.0,
            "high": 2.28,
            "low": 2.08,
            "price": 2.25,
            "volume": 20_000.0,
            "vol_avg20": 5_000.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None
    decision = engine.pop_last_decision("ELAB")
    assert decision is not None
    assert "runup" in decision["score_details"]


def test_schwab_native_entry_engine_p3_hard_stop_pause_blocks_only_p3() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    engine.record_path_exit("ELAB", path="P3_SURGE", reason="HARD_STOP_NATIVE_BACKUP")

    p3_indicators = _make_schwab_native_base_indicators()
    p3_indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "stoch_k": 70.0,
            "volume": 20_000.0,
            "vol_avg20": 5_000.0,
        }
    )

    p3_signal = engine.check_entry("ELAB", p3_indicators, bar_index=60, position_tracker=None)

    assert p3_signal is None
    decision = engine.pop_last_decision("ELAB")
    assert decision is not None
    assert decision["path"] == "P3_SURGE"
    assert "P3 hard-stop pause active" in decision["reason"]

    p1_indicators = _make_schwab_native_base_indicators()
    p1_indicators.update(
        {
            "volume": 12_500.0,
            "vol_avg20": 5_000.0,
        }
    )

    p1_signal = engine.check_entry("ELAB", p1_indicators, bar_index=61, position_tracker=None)

    assert p1_signal is not None
    assert p1_signal["path"] == "P1_CROSS"


def test_schwab_native_entry_engine_blocks_p1_when_chop_lock_hits_threshold() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    _seed_schwab_native_chop_history(engine, "ELAB")
    engine._recent_bars["ELAB"][-2]["macd_cross_above"] = True
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


def test_polygon_variant_does_not_inherit_schwab_chop_lock_default() -> None:
    config = TradingConfig().make_30s_polygon_variant()
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

    assert signal is not None
    assert signal["path"] == "P1_CROSS"


def test_schwab_native_confirm_bars_one_requires_one_confirmation_bar() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    assert config.schwab_native_use_confirmation is True
    assert config.confirm_bars == 1
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
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
    assert decision["status"] == "pending"
    assert decision["reason"] == "P1_CROSS waiting confirmation"
    assert decision["path"] == "P1_CROSS"

    signal = engine.check_entry("ELAB", indicators, bar_index=61, position_tracker=None)

    assert signal is not None
    assert signal["path"] == "P1_CROSS"
    decision = engine.pop_last_decision("ELAB")
    assert decision is not None
    assert decision["status"] == "signal"
    assert decision["reason"] == "P1_CROSS"
    assert decision["path"] == "P1_CROSS"
    assert decision["score"] == "6"


def test_schwab_native_p1_cross_blocks_when_relative_volume_is_too_weak() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "price": 2.02,
            "high": 2.06,
            "low": 1.98,
            "volume": 3_000.0,
            "ema9": 2.015,
            "ema20": 2.00,
            "vwap": 2.01,
            "vol_avg20": 4_000.0,
            "vol_avg5": 3_500.0,
            "ema9_dist_pct": 0.25,
            "vwap_dist_pct": 0.5,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)
    _, _, details, _ = engine._evaluate_paths("ELAB", indicators, 60)

    assert signal is None
    assert "P1:vol20" in details


def test_schwab_native_p1_cross_blocks_when_absolute_p1_volume_floor_not_met() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.p1_min_volume_abs = 7_500
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "price": 2.02,
            "high": 2.06,
            "low": 1.98,
            "volume": 6_000.0,
            "ema9": 2.015,
            "ema20": 2.00,
            "vwap": 2.01,
            "vol_avg20": 4_000.0,
            "vol_avg5": 3_500.0,
            "ema9_dist_pct": 0.25,
            "vwap_dist_pct": 0.5,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)
    _, _, details, _ = engine._evaluate_paths("ELAB", indicators, 60)

    assert signal is None
    assert "P1:p1_vol" in details


def test_schwab_native_p1_cross_blocks_when_dollar_volume_floor_not_met() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.p1_min_dollar_volume_abs = 25_000
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "price": 2.02,
            "high": 2.06,
            "low": 1.98,
            "volume": 8_000.0,
            "ema9": 2.015,
            "ema20": 2.00,
            "vwap": 2.01,
            "vol_avg20": 4_000.0,
            "vol_avg5": 3_500.0,
            "ema9_dist_pct": 0.25,
            "vwap_dist_pct": 0.5,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)
    _, _, details, _ = engine._evaluate_paths("ELAB", indicators, 60)

    assert signal is None
    assert "P1:p1_dollar" in details


def test_schwab_native_entry_engine_allows_p3_extreme_override_during_chop_lock() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    config.p3_max_bars_since_macd_cross = None
    config.p3_max_recent_runup_pct = None
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    _seed_schwab_native_chop_history(engine, "ELAB")
    engine._recent_bars["ELAB"][-2]["macd_cross_above"] = True
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "open": 2.03,
            "price": 2.18,
            "high": 2.23,
            "low": 2.03,
            "volume": 20_000.0,
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


def test_polygon_variant_restores_polygon_momentum_override_that_schwab_blocks() -> None:
    schwab_config = TradingConfig().make_30s_schwab_native_variant()
    schwab_config.schwab_native_use_confirmation = False
    schwab_config.p3_allow_momentum_override = True
    schwab_engine = SchwabNativeEntryEngine(schwab_config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))

    polygon_config = TradingConfig().make_30s_polygon_variant()
    polygon_config.schwab_native_use_confirmation = False
    polygon_engine = SchwabNativeEntryEngine(polygon_config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))

    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "vwap_dist_pct": 25.0,
            "ema9_dist_pct": 5.0,
            "stoch_k": 90.0,
            "price_above_vwap": False,
            "volume": 20_000.0,
            "vol_avg20": 3_000.0,
        }
    )

    schwab_signal = schwab_engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)
    assert schwab_signal is None
    schwab_decision = schwab_engine.pop_last_decision("ELAB")
    assert schwab_decision is not None
    assert schwab_decision["status"] == "blocked"
    assert schwab_decision["path"] == "P3_SURGE"
    assert schwab_decision["reason"] == "P3 entry stoch_k cap (90.0 >= 80.0)"

    polygon_signal = polygon_engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)
    assert polygon_signal is not None
    assert polygon_signal["path"] == "P3_SURGE"


def test_schwab_native_entry_engine_1m_p3_requires_average_volume() -> None:
    config = TradingConfig().make_1m_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "vwap_dist_pct": 5.0,
            "ema9_dist_pct": 1.0,
            "stoch_k": 60.0,
            "volume": 900.0,
            "vol_avg20": 1000.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None


def test_schwab_native_entry_engine_1m_p3_blocks_when_ema9_distance_is_too_large() -> None:
    config = TradingConfig().make_1m_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 17, 10, 0))
    indicators = _make_schwab_native_base_indicators()
    indicators.update(
        {
            "macd_cross_above": False,
            "price_cross_above_vwap": False,
            "vwap_dist_pct": 5.0,
            "ema9_dist_pct": 2.5,
            "stoch_k": 60.0,
            "volume": 1500.0,
            "vol_avg20": 1000.0,
        }
    )

    signal = engine.check_entry("ELAB", indicators, bar_index=60, position_tracker=None)

    assert signal is None


def test_schwab_native_entry_engine_records_path_diagnostics_on_no_match() -> None:
    config = TradingConfig().make_30s_polygon_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 29, 15, 30))

    signal = engine.check_entry(
        "XTLB",
        {
            "open": 3.42,
            "price": 3.45,
            "high": 3.46,
            "low": 3.41,
            "volume": 6_000.0,
            "ema9": 3.43,
            "ema20": 3.36,
            "vwap": 3.40,
            "vol_avg20": 5_000.0,
            "vol_avg5": 5_500.0,
            "ema9_prev": 3.42,
            "macd": 0.012,
            "signal": 0.010,
            "histogram": 0.002,
            "hist_value": 0.002,
            "stoch_k": 68.0,
            "macd_cross_above": False,
            "bars_below_signal_prev": 0,
            "price_cross_above_vwap": False,
            "macd_above_signal": True,
            "macd_increasing": True,
            "macd_delta": 0.0006,
            "macd_delta_prev": 0.0008,
            "price_above_ema9": True,
            "price_above_ema20": True,
            "price_above_vwap": True,
            "hist_growing": True,
            "stoch_k_rising": True,
            "ema9_dist_pct": 0.6,
            "vwap_dist_pct": 1.4,
            "ema9_trend_rising": True,
            "in_regular_session": True,
            "stoch_cross_below_exit": False,
            "macd_cross_below": False,
        },
        bar_index=60,
        position_tracker=None,
    )

    assert signal is None
    decision = engine.pop_last_decision("XTLB")
    assert decision is not None
    assert decision["status"] == "idle"
    assert decision["reason"] == "no entry path matched"
    assert decision["score_details"].startswith("diag: g[")
    assert "best=P2:vwap_cross" in decision["score_details"]
    assert "P3:delta|delta_prev|hist" in decision["score_details"]
    assert "P5:pullback" in decision["score_details"]


def test_schwab_native_entry_engine_records_path_diagnostics_on_chop_block() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 29, 10, 0))
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
    assert "chop lock active" in decision["reason"]
    assert decision["score_details"].startswith("diag: g[")
    assert "chop=4/4:" in decision["score_details"]
    assert "P1:chop" in decision["score_details"]


def test_schwab_native_entry_engine_allows_p4_when_higher_paths_are_only_chop_blocked() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    config.schwab_native_use_confirmation = False
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 30, 16, 5))
    _seed_schwab_native_chop_history(engine, "SKLZ")

    signal = engine.check_entry(
        "SKLZ",
        {
            "open": 8.47,
            "price": 8.7698,
            "high": 8.8692,
            "low": 8.43,
            "volume": 98_910.0,
            "ema9": 8.3072,
            "ema20": 8.1924,
            "vwap": 7.7858,
            "vol_avg20": 20_000.0,
            "vol_avg5": 25_000.0,
            "ema9_prev": 8.20,
            "macd": 0.1057,
            "signal": 0.0543,
            "histogram": 0.0514,
            "stoch_k": 89.7,
            "macd_cross_above": False,
            "bars_below_signal_prev": 0,
            "price_cross_above_vwap": True,
            "macd_above_signal": True,
            "macd_increasing": True,
            "macd_delta": 0.0010,
            "macd_delta_prev": 0.0015,
            "hist_value": 0.0514,
            "price_above_ema9": True,
            "price_above_ema20": True,
            "price_above_vwap": True,
            "hist_growing": True,
            "stoch_k_rising": True,
            "ema9_dist_pct": 5.0,
            "vwap_dist_pct": 12.0,
            "ema9_trend_rising": True,
            "in_regular_session": True,
            "stoch_cross_below_exit": False,
            "macd_cross_below": False,
        },
        bar_index=60,
        position_tracker=None,
    )

    assert signal is not None
    assert signal["path"] == "P4_BURST"


def test_schwab_native_entry_engine_replaces_same_bar_probe_snapshot() -> None:
    config = TradingConfig().make_30s_schwab_native_variant()
    engine = SchwabNativeEntryEngine(config, now_provider=lambda: datetime(2026, 4, 28, 10, 0))

    first_snapshot = engine._snapshot_from_indicators(
        {
            "open": 3.40,
            "price": 3.44,
            "high": 3.45,
            "low": 3.39,
            "volume": 10_000.0,
            "ema9": 3.35,
            "ema20": 3.30,
            "vwap": 3.33,
            "vol_avg20": 8_000.0,
            "vol_avg5": 9_000.0,
            "ema9_prev": 3.34,
            "hist_value": 0.01,
        },
        bar_index=56,
    )
    second_snapshot = engine._snapshot_from_indicators(
        {
            "open": 3.40,
            "price": 3.51,
            "high": 3.52,
            "low": 3.39,
            "volume": 18_000.0,
            "ema9": 3.36,
            "ema20": 3.30,
            "vwap": 3.35,
            "vol_avg20": 8_500.0,
            "vol_avg5": 10_000.0,
            "ema9_prev": 3.35,
            "hist_value": 0.02,
        },
        bar_index=56,
    )

    assert first_snapshot is not None
    assert second_snapshot is not None

    engine._remember_bar("SBLX", first_snapshot)
    engine._remember_bar("SBLX", second_snapshot)

    remembered = engine._recent_bars["SBLX"]
    assert len(remembered) == 1
    assert remembered[0]["close"] == pytest.approx(3.51)
    assert remembered[0]["high"] == pytest.approx(3.52)
    assert remembered[0]["volume"] == pytest.approx(18_000.0)


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
