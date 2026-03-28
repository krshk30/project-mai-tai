from __future__ import annotations

from datetime import datetime

from project_mai_tai.strategy_core.bar_builder import BarBuilder
from project_mai_tai.strategy_core.config import (
    IndicatorConfig,
    MomentumAlertConfig,
    MomentumConfirmedConfig,
)
from project_mai_tai.strategy_core.indicators import IndicatorEngine
from project_mai_tai.strategy_core.models import (
    DaySnapshot,
    LastTrade,
    MarketSnapshot,
    MinuteSnapshot,
    ReferenceData,
)
from project_mai_tai.strategy_core.momentum_alerts import MomentumAlertEngine
from project_mai_tai.strategy_core.momentum_confirmed import MomentumConfirmedScanner


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
