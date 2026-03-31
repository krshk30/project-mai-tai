from __future__ import annotations

import csv
from datetime import datetime

from project_mai_tai.strategy_core.bar_builder import BarBuilder
from project_mai_tai.strategy_core.config import (
    IndicatorConfig,
    MomentumAlertConfig,
    MomentumConfirmedConfig,
)
from project_mai_tai.strategy_core.indicators import IndicatorEngine
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


def test_bar_builder_ignores_odd_lots_and_fills_gap() -> None:
    builder = BarBuilder("UGRO", interval_secs=30, time_provider=lambda: 0)
    base_ns = 1_700_000_000_000_000_000

    assert builder.on_trade(price=3.5, size=50, timestamp_ns=base_ns + 30_000_000_000) == []

    first_completed = builder.on_trade(price=3.5, size=100, timestamp_ns=base_ns + 30_000_000_000)
    assert first_completed == []

    completed = builder.on_trade(price=3.7, size=100, timestamp_ns=base_ns + 120_000_000_000)

    assert len(completed) == 3
    assert completed[0].timestamp % 30 == 0
    assert completed[1].volume == 0
    assert completed[1].timestamp - completed[0].timestamp == 30
    assert completed[2].volume == 0
    assert completed[2].timestamp - completed[1].timestamp == 30
    assert builder.get_current_price() == 3.7


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


def test_confirmed_scanner_prunes_faded_candidates_and_allows_reconfirmation() -> None:
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

    assert dropped == ["POLA"]
    assert confirmed_scanner.get_all_confirmed() == []
    assert confirmed_scanner._tracking["POLA"]["confirmed"] is False
    assert confirmed_scanner._tracking["POLA"]["has_volume_spike"] is False
    assert confirmed_scanner._tracking["POLA"]["squeezes"] == []


def test_entry_engine_allows_default_window_until_6pm_et() -> None:
    engine = EntryEngine(
        TradingConfig(),
        now_provider=lambda: datetime(2026, 3, 30, 17, 0),
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
