"""Pin the TOS dot-plot port. These rows ARE the operator's rule -- if they are wrong, the
backtest measures a filter he never described, and the error is invisible in the output.

The off-by-one in `lowest(x,n)[1]` is the dangerous one: including the current bar turns
"turned up off its recent low" into "is its own low", which can never be true, silently
producing zero entries (or, inverted, entries everywhere).
"""

from __future__ import annotations

import math

from project_mai_tai.backtest.dot_entry import (
    DotRows,
    _green_band,
    _green_macd,
    fast_stoch_k,
    lowest_prior,
    rsi_wilders,
)


def test_lowest_prior_excludes_the_current_bar() -> None:
    s = [5.0, 4.0, 3.0, 1.0, 9.0]
    # at i=4: min over bars 1..3 -> min(4,3,1) = 1. The current bar (9.0) is NOT considered.
    assert lowest_prior(s, 3, 4) == 1.0
    # at i=3 the current bar is the low (1.0) but the window is bars 0..2 -> 3.0
    assert lowest_prior(s, 3, 3) == 3.0


def test_lowest_prior_needs_a_full_warm_window() -> None:
    s = [5.0, 4.0, 3.0]
    assert math.isnan(lowest_prior(s, 3, 2))  # only 2 prior bars available
    assert math.isnan(lowest_prior([1.0, float("nan"), 3.0, 4.0], 3, 3))  # NaN in window


def test_macd_row_green_only_when_above_the_three_bar_prior_low() -> None:
    # bars 1..3 lows = min(2,1,0) = 0 ; current 0.5 > 0 -> turned up
    assert _green_macd([9.0, 2.0, 1.0, 0.0, 0.5], 4) is True
    # current equals that low -> NOT green (must exceed)
    assert _green_macd([9.0, 2.0, 1.0, 0.0, 0.0], 4) is False
    # still falling
    assert _green_macd([9.0, 2.0, 1.0, 0.0, -0.5], 4) is False


def test_band_row_overbought_shortcut_is_green_even_while_falling() -> None:
    """`or stochasticfast() > 70` fires regardless of the low test -- already strong."""
    falling = [95.0, 90.0, 85.0, 80.0, 75.0, 71.0]
    assert _green_band(falling, 5) is True


def test_band_row_requires_above_thirty_when_using_the_low_test() -> None:
    # turning up off the 5-bar low but still in the basement (<=30) -> NOT green
    assert _green_band([40.0, 30.0, 25.0, 20.0, 10.0, 25.0], 5) is False
    # same shape, above 30 -> green
    assert _green_band([40.0, 30.0, 25.0, 20.0, 10.0, 35.0], 5) is True


def test_band_row_not_green_when_below_its_prior_low() -> None:
    assert _green_band([80.0, 60.0, 55.0, 50.0, 45.0, 40.0], 5) is False


def test_all_green_requires_all_three_rows() -> None:
    up = [9.0, 2.0, 1.0, 0.0, 0.5]          # macd row green
    strong = [95.0, 90.0, 85.0, 80.0, 75.0]  # band row green (>70)
    weak = [80.0, 60.0, 55.0, 50.0, 45.0]    # band row NOT green
    assert DotRows(macd=up, stoch=strong, rsi=strong).all_green(4) is True
    assert DotRows(macd=up, stoch=strong, rsi=weak).all_green(4) is False
    assert DotRows(macd=up, stoch=strong, rsi=weak).consensus(4) == 2


def test_fast_stoch_k_endpoints() -> None:
    highs = [10.0] * 10
    lows = [0.0] * 10
    closes = [0.0] * 9 + [10.0]
    assert fast_stoch_k(highs, lows, closes, 10)[9] == 100.0  # close at the high
    closes2 = [5.0] * 9 + [0.0]
    assert fast_stoch_k(highs, lows, closes2, 10)[9] == 0.0   # close at the low


def test_rsi_all_gains_is_hundred_and_warmup_is_nan() -> None:
    rising = [float(i) for i in range(30)]
    r = rsi_wilders(rising, 14)
    assert math.isnan(r[13])       # not warm until bar 14
    assert r[14] == 100.0          # no losses at all
    assert r[-1] == 100.0


def test_volume_hold_rejects_a_one_bar_spike() -> None:
    """The whole point of persistence: a spike that evaporates must NOT pass."""
    from project_mai_tai.backtest.dot_entry import _vol_non_declining
    # 100, 900 (spike), 200, 210 -> declines after the spike
    assert _vol_non_declining([100.0, 900.0, 200.0, 210.0], 3) is False
    # a genuine staircase
    assert _vol_non_declining([100.0, 150.0, 200.0, 260.0], 3) is True
    # flat counts as holding (>=), it is not declining
    assert _vol_non_declining([200.0, 200.0, 200.0, 200.0], 3) is True


def test_volume_hold_rejects_any_decline_in_the_window() -> None:
    from project_mai_tai.backtest.dot_entry import _vol_non_declining
    assert _vol_non_declining([100.0, 200.0, 190.0, 300.0], 3) is False


def test_volume_sustained_tolerates_a_dip_but_needs_the_window_elevated() -> None:
    from project_mai_tai.backtest.dot_entry import _vol_sustained
    base = [100.0] * 11          # median baseline = 100
    assert _vol_sustained(base + [300.0, 250.0, 280.0], 13, 3, 10) is True
    # one of the last three falls back to baseline -> not sustained
    assert _vol_sustained(base + [300.0, 90.0, 280.0], 13, 3, 10) is False
