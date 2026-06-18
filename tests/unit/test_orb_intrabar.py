from __future__ import annotations

from datetime import datetime, timedelta, timezone

from project_mai_tai.strategy_core.orb_intrabar import (
    ExecutionMode,
    OrbBar,
    OrbConfig,
    TrailingStop,
    bar_confirms_breakout,
    build_opening_range,
    entry_fill_price,
    in_pre_open_universe,
)

CFG = OrbConfig()
OPEN = datetime(2026, 6, 18, 13, 30, tzinfo=timezone.utc)  # 09:30 ET


def _bar(i, o, h, l, c, v, vwap=None, ema9=None):
    return OrbBar(OPEN + timedelta(minutes=i), o, h, l, c, v, vwap, ema9)


# ---- opening range ----
def test_or_needs_full_coverage():
    # only 4 of the 5 OR bars present -> skip (in-time-coverage guard)
    bars = [_bar(i, 1, 1.05, 0.99, 1.0, 100) for i in range(4)]
    assert build_opening_range(bars, CFG) is None


def test_or_width_cap_skips_chop():
    bars = [_bar(i, 10, 12, 10, 11, 100) for i in range(5)]  # ~20% wide
    assert build_opening_range(bars, CFG) is None


def test_or_built_in_band():
    bars = [_bar(i, 5.0, 5.09, 4.95, 5.0, 100) for i in range(5)]
    orr = build_opening_range(bars, CFG)
    assert orr is not None
    assert orr.high == 5.09 and orr.low == 4.95


# ---- breakout filter ----
def test_breakout_requires_close_vol_vwap_ema():
    orr = build_opening_range([_bar(i, 5, 5.09, 4.9, 5.0, 100) for i in range(5)], CFG)
    good = _bar(6, 5.1, 5.4, 5.05, 5.33, 300, vwap=5.10, ema9=5.05)
    assert bar_confirms_breakout(orr, good, CFG)
    # wick (close <= OR_high) rejected
    assert not bar_confirms_breakout(orr, _bar(6, 5.0, 5.4, 5.0, 5.05, 300, vwap=5.0, ema9=5.0), CFG)
    # weak volume rejected
    assert not bar_confirms_breakout(orr, _bar(6, 5.1, 5.4, 5.05, 5.33, 120, vwap=5.1, ema9=5.05), CFG)
    # below VWAP rejected
    assert not bar_confirms_breakout(orr, _bar(6, 5.1, 5.4, 5.05, 5.33, 300, vwap=5.40, ema9=5.05), CFG)


def test_entry_fill_price_modes():
    orr = build_opening_range([_bar(i, 5, 5.09, 4.9, 5.0, 100) for i in range(5)], CFG)
    bar = _bar(6, 5.1, 5.4, 5.05, 5.33, 300, vwap=5.1, ema9=5.05)
    assert entry_fill_price(orr, bar, ExecutionMode.BAR_CLOSE) == 5.33
    assert entry_fill_price(orr, bar, ExecutionMode.INTRABAR) == 5.09  # the breakout level


# ---- pre-09:25 universe guard (the binding rule) ----
def test_universe_guard_arms_in_time_name():
    confirmed = OPEN - timedelta(minutes=30)  # 09:00, before 09:25
    assert in_pre_open_universe(confirmed, OPEN) is True


def test_universe_guard_skips_late_confirmer():
    confirmed = OPEN + timedelta(minutes=5)  # 09:35 (the breakout-confirm case)
    assert in_pre_open_universe(confirmed, OPEN) is False
    # exactly at the 09:25 boundary is in; one minute later is out
    assert in_pre_open_universe(OPEN - timedelta(minutes=5), OPEN) is True
    assert in_pre_open_universe(OPEN - timedelta(minutes=4), OPEN) is False


def test_universe_guard_skips_unknown():
    assert in_pre_open_universe(None, OPEN) is False


# ---- TRAIL-8% ----
def test_trailing_stop_arms_8pct_below_entry():
    ts = TrailingStop.arm(entry_price=5.0, trail_pct=8.0)
    assert ts.stop_price == 5.0 * 0.92


def test_trailing_stop_ratchets_up_never_down():
    ts = TrailingStop.arm(5.0, 8.0)
    ts.ratchet(6.0)            # new HWM -> stop to 5.52
    assert ts.stop_price == 6.0 * 0.92
    ts.ratchet(5.5)            # lower than HWM -> stop unchanged (never down)
    assert ts.stop_price == 6.0 * 0.92


def test_trailing_stop_breach_and_inert_default():
    ts = TrailingStop.arm(5.0, 8.0)
    assert ts.breached(4.59) is True
    assert ts.breached(4.61) is False
    inert = TrailingStop.arm(5.0, 0.0)   # default-off -> never ratchets
    inert.ratchet(10.0)
    assert inert.stop_price == 5.0  # entry * (1 - 0) == entry; no movement


def test_parity_full_position_trail_matches_canonical():
    """End-to-end: BAR_CLOSE entry + TRAIL-8% over a bar sequence reproduces the
    canonical backtest exit (stop off the bar low, ratchet from HWM)."""
    orr = build_opening_range([_bar(i, 5, 5.09, 4.95, 5.0, 100) for i in range(5)], CFG)
    entry_bar = _bar(6, 5.1, 5.4, 5.05, 5.33, 300, vwap=5.1, ema9=5.05)
    assert bar_confirms_breakout(orr, entry_bar, CFG)
    ep = entry_fill_price(orr, entry_bar, ExecutionMode.BAR_CLOSE)
    ts = TrailingStop.arm(ep, CFG.trail_pct)
    seq = [_bar(7, 5.4, 5.8, 5.35, 5.7, 200), _bar(8, 5.7, 6.0, 5.6, 5.9, 200),
           _bar(9, 5.9, 5.95, 5.40, 5.45, 200)]  # bar 9 low 5.40 hits the ratcheted stop
    exit_price = None
    for b in seq:
        if ts.breached(b.low):
            exit_price = ts.stop_price if b.open >= ts.stop_price else b.open
            break
        ts.ratchet(b.high)
    # HWM reached 6.00 on bar 8 -> stop ratcheted to 5.52; bar 9 low 5.40 breaches -> fill 5.52
    assert exit_price == 6.0 * 0.92
    assert round((exit_price - ep) / ep * 100, 2) == round((5.52 - 5.33) / 5.33 * 100, 2)
