"""Regression test: check_bar_closes() must not lose the cumulative-volume baseline.

Without this fix, the first trade of every periodically-closed bucket falls back
to `size` (LEVELONE last_size) instead of computing a real cum_vol delta, which
under-counts persisted bar volumes by 20-50% on Schwab LEVELONE feeds.
"""
from __future__ import annotations

from project_mai_tai.strategy_core.schwab_native_30s import SchwabNativeBarBuilder


def test_cum_volume_baseline_survives_periodic_close() -> None:
    clock = {"now": 0.0}
    builder = SchwabNativeBarBuilder(
        "TST",
        interval_secs=30,
        time_provider=lambda: clock["now"],
        close_grace_seconds=2.0,
    )

    # Bar 1: trades arrive with cumulative volume rising 1000 -> 1100 -> 1200.
    builder.on_trade(price=10.0, size=100, timestamp_ns=0, cumulative_volume=1000)
    clock["now"] = 5.0
    builder.on_trade(price=10.1, size=100, timestamp_ns=0, cumulative_volume=1100)
    clock["now"] = 10.0
    builder.on_trade(price=10.2, size=100, timestamp_ns=0, cumulative_volume=1200)

    # Periodic close fires after grace.
    clock["now"] = 33.0
    closed = builder.check_bar_closes()
    assert len(closed) == 1
    # First trade falls back to last_size because there's no prior cum-vol baseline,
    # so bar = 100 (first size) + 100 (delta) + 100 (delta) = 300.
    assert closed[0].volume == 300

    # First trade of the next bucket arrives. Cum volume jumped to 1500 (300 more
    # shares traded, all of which belong to bucket 2). Without the fix this
    # collapses to size=50 because the baseline was reset to None.
    clock["now"] = 35.0
    builder.on_trade(price=10.3, size=50, timestamp_ns=0, cumulative_volume=1500)

    assert builder._current_bar is not None
    assert builder._current_bar.volume == 300, (
        "first trade of new bucket must compute cum_vol delta from previous bar's "
        "last cum_vol, not fall back to last_size"
    )
