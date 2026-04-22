from __future__ import annotations

from project_mai_tai.strategy_core.bar_builder import BarBuilder
from project_mai_tai.strategy_core.models import OHLCVBar
from project_mai_tai.strategy_core.schwab_native_30s import SchwabNativeBarBuilder


def test_bar_builder_builds_30s_bar_from_live_second_bars() -> None:
    builder = BarBuilder(ticker="SKYQ", interval_secs=30)
    start = 1_700_000_010.0

    for offset in range(30):
        price = 5.0 + offset * 0.01
        completed = builder.on_bar(
            OHLCVBar(
                open=price,
                high=price + 0.02,
                low=price - 0.01,
                close=price + 0.01,
                volume=100 + offset,
                timestamp=start + offset,
                trade_count=1,
            )
        )
        assert completed == []

    completed = builder.on_bar(
        OHLCVBar(
            open=5.50,
            high=5.52,
            low=5.48,
            close=5.51,
            volume=150,
            timestamp=start + 30,
            trade_count=1,
        )
    )

    assert len(completed) == 1
    closed = completed[0]
    assert closed.timestamp == start
    assert closed.open == 5.0
    assert closed.high == 5.31
    assert closed.low == 4.99
    assert closed.close == 5.30
    assert closed.volume == sum(100 + offset for offset in range(30))
    assert closed.trade_count == 30


def test_bar_builder_ignores_stale_live_updates_after_advance() -> None:
    builder = BarBuilder(ticker="SKYQ", interval_secs=30)
    start = 1_700_000_010.0

    builder.on_bar(
        OHLCVBar(
            open=5.0,
            high=5.1,
            low=4.9,
            close=5.05,
            volume=100,
            timestamp=start,
            trade_count=1,
        )
    )
    builder.on_bar(
        OHLCVBar(
            open=5.2,
            high=5.3,
            low=5.15,
            close=5.25,
            volume=120,
            timestamp=start + 30,
            trade_count=1,
        )
    )

    completed = builder.on_bar(
        OHLCVBar(
            open=4.0,
            high=6.0,
            low=3.5,
            close=5.9,
            volume=999,
            timestamp=start + 10,
            trade_count=1,
        )
    )

    assert completed == []
    assert builder.get_bars_as_dicts()[-1]["close"] == 5.05
    assert builder.get_current_price() == 5.25


def test_schwab_native_bar_builder_uses_cumulative_volume_and_gap_fill() -> None:
    builder = SchwabNativeBarBuilder(ticker="ELAB", interval_secs=30, time_provider=lambda: 1_700_000_120.0)
    base_ns = 1_700_000_010_000_000_000

    assert builder.on_trade(price=2.00, size=25, timestamp_ns=base_ns, cumulative_volume=1_000) == []
    assert builder.on_trade(price=2.05, size=40, timestamp_ns=base_ns + 15_000_000_000, cumulative_volume=1_050) == []

    completed = builder.on_trade(
        price=2.20,
        size=20,
        timestamp_ns=base_ns + 60_000_000_000,
        cumulative_volume=1_100,
    )

    assert len(completed) == 2
    assert completed[0].volume == 75
    assert completed[1].volume == 0
    assert completed[1].open == completed[0].close


def test_bar_builder_exposes_current_bar_with_closed_history() -> None:
    builder = BarBuilder(ticker="SKYQ", interval_secs=30)
    start = 1_700_000_010.0

    builder.on_bar(
        OHLCVBar(
            open=5.0,
            high=5.1,
            low=4.9,
            close=5.05,
            volume=100,
            timestamp=start,
            trade_count=1,
        )
    )
    builder.on_bar(
        OHLCVBar(
            open=5.2,
            high=5.3,
            low=5.1,
            close=5.25,
            volume=120,
            timestamp=start + 30,
            trade_count=1,
        )
    )

    bars = builder.get_bars_with_current_as_dicts()

    assert len(bars) == 2
    assert bars[0]["timestamp"] == start
    assert bars[1]["timestamp"] == start + 30
    assert bars[1]["close"] == 5.25


def test_schwab_native_bar_builder_exposes_current_bar_with_closed_history() -> None:
    builder = SchwabNativeBarBuilder(ticker="ELAB", interval_secs=30, time_provider=lambda: 1_700_000_020.0)
    base_ns = 1_700_000_010_000_000_000

    builder.on_trade(price=2.00, size=25, timestamp_ns=base_ns, cumulative_volume=1_000)
    builder.on_trade(price=2.05, size=40, timestamp_ns=base_ns + 30_000_000_000, cumulative_volume=1_050)

    bars = builder.get_bars_with_current_as_dicts()

    assert len(bars) == 2
    assert bars[0]["timestamp"] == 1_700_000_010.0
    assert bars[1]["timestamp"] == 1_700_000_040.0
    assert bars[1]["close"] == 2.05
