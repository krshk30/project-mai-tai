"""Pin the proximity-entry semantics before any number is believed."""

from __future__ import annotations

from project_mai_tai.backtest.atr_oracle import Bar
from project_mai_tai.backtest.proximity_sweep import (
    find_proximity_signals,
    simulate_cell,
)


def _row(state, trail, close, flip=None):
    return {"state": state, "trail": trail, "close": close, "flip": flip}


def test_fires_only_when_close_is_within_the_threshold_below_the_trail() -> None:
    rows = [
        _row("short", 10.0, 9.00),   # 11.1% away -> no
        _row("short", 10.0, 9.95),   # 0.50% away -> yes at 1.0%
    ]
    assert find_proximity_signals(rows, 1.0) == [1]
    assert find_proximity_signals(rows, 0.4) == []  # tighter than the gap


def test_never_fires_when_the_trail_is_below_price() -> None:
    """Above the trail means the cross ALREADY happened -- that is the CW rule, not this one.
    proximity would be negative; the rule requires 0 <= prox <= X."""
    rows = [_row("short", 10.0, 10.5)]
    assert find_proximity_signals(rows, 5.0) == []


def test_only_long_state_bars_are_skipped() -> None:
    rows = [_row("long", 10.0, 9.95), _row("short", 10.0, 9.95)]
    assert find_proximity_signals(rows, 1.0) == [1]


def test_one_entry_per_short_segment() -> None:
    """Three qualifying bars in ONE segment -> ONE signal (operator: one per segment)."""
    rows = [_row("short", 10.0, 9.95)] * 3
    assert find_proximity_signals(rows, 1.0) == [0]


def test_a_new_short_segment_re_arms() -> None:
    rows = [
        _row("short", 10.0, 9.95),          # signal, claims segment
        _row("short", 10.0, 9.96),          # same segment -> suppressed
        _row("long", 9.0, 9.50),            # flip long ends the segment
        _row("short", 10.0, 9.95),          # NEW segment -> fires again
    ]
    assert find_proximity_signals(rows, 1.0) == [0, 3]


def _bars(prices):
    return [Bar(ts=i * 60000, open=p, high=p, low=p, close=p, volume=1000)
            for i, p in enumerate(prices)]


def test_stop_wins_when_one_bar_breaches_both() -> None:
    """Pessimistic precedence: a bar spanning stop AND target books the STOP."""
    bars = _bars([10.0, 10.0])
    bars[1] = Bar(ts=60000, open=10.0, high=10.5, low=9.0, close=10.0, volume=1000)  # both
    rows = [_row("short", 10.05, 10.0), _row("short", 10.05, 10.0)]
    trades = simulate_cell(bars, rows, symbol="X", day="d", threshold_pct=1.0,
                           fill_mode="same_bar")
    assert len(trades) == 1
    assert trades[0].reason == "STOP"
    assert round(trades[0].pnl_pct, 6) == -5.0


def test_next_open_fill_uses_the_following_bar_open() -> None:
    bars = [
        Bar(ts=0, open=10.0, high=10.0, low=10.0, close=10.0, volume=1000),
        Bar(ts=60000, open=9.8, high=9.8, low=9.8, close=9.8, volume=1000),   # the fill
        Bar(ts=120000, open=10.2, high=10.2, low=10.2, close=10.2, volume=1000),
    ]
    rows = [_row("short", 10.05, 10.0)] * 3
    same = simulate_cell(bars, rows, symbol="X", day="d", threshold_pct=1.0, fill_mode="same_bar")
    nxt = simulate_cell(bars, rows, symbol="X", day="d", threshold_pct=1.0, fill_mode="next_open")
    assert same[0].entry_price == 10.0
    assert nxt[0].entry_price == 9.8   # waiting a bar was a DISCOUNT here


def _b(o, h, l, c, i=0):
    return Bar(ts=i * 60000, open=o, high=h, low=l, close=c, volume=1000)


def test_floor_ladder_locks_the_whole_percent_reached_and_ratchets() -> None:
    """Reaches +3.5% then falls back: floor sits at +3%, not +2%, and not the -5% stop."""
    from project_mai_tai.backtest.proximity_sweep import _walk_exit
    bars = [
        _b(10, 10, 10, 10, 0),
        _b(10, 10.35, 10, 10.3, 1),   # high = +3.5% -> floor ratchets to +3%
        _b(10.3, 10.3, 10.0, 10.0, 2),  # falls back through +3%
    ]
    rows = [{"flip": None}] * 3
    px, reason = _walk_exit(bars, rows, 0, 10.0, exit_mode="floor_ladder",
                            target_pct=2.0, stop_pct=-5.0, trail_pct=2.0)
    assert reason == "FLOOR"
    assert round(px, 4) == 10.30      # +3%, the ratcheted floor


def test_floor_ladder_does_not_cap_a_runner_at_two_percent() -> None:
    """The whole point: no hard target, so a runner keeps running."""
    from project_mai_tai.backtest.proximity_sweep import _walk_exit
    bars = [_b(10, 10, 10, 10, 0)] + [
        _b(10 + i, 10 + i + 0.5, 10 + i - 0.1, 10 + i, i) for i in range(1, 6)
    ]
    rows = [{"flip": None}] * len(bars)
    px, _ = _walk_exit(bars, rows, 0, 10.0, exit_mode="floor_ladder",
                       target_pct=2.0, stop_pct=-5.0, trail_pct=2.0)
    assert px > 10.2   # far beyond the +2% the incumbent geometry would have taken


def test_trail2_exits_two_percent_below_the_high_water_mark() -> None:
    from project_mai_tai.backtest.proximity_sweep import _walk_exit
    bars = [
        _b(10, 10, 10, 10, 0),
        _b(10, 11.0, 10, 11.0, 1),      # hwm = 11.00 -> trail = 10.78
        _b(11, 11, 10.5, 10.6, 2),      # low 10.50 breaches 10.78
    ]
    rows = [{"flip": None}] * 3
    px, reason = _walk_exit(bars, rows, 0, 10.0, exit_mode="trail2",
                            target_pct=2.0, stop_pct=-5.0, trail_pct=2.0)
    assert reason == "FLOOR"
    assert round(px, 4) == 10.78


def test_hard_stop_still_governs_before_any_floor_exists() -> None:
    from project_mai_tai.backtest.proximity_sweep import _walk_exit
    bars = [_b(10, 10, 10, 10, 0), _b(10, 10.1, 9.4, 9.5, 1)]
    rows = [{"flip": None}] * 2
    px, reason = _walk_exit(bars, rows, 0, 10.0, exit_mode="floor_ladder",
                            target_pct=2.0, stop_pct=-5.0, trail_pct=2.0)
    assert reason == "STOP"
    assert round(px, 4) == 9.50


def test_floor_start_delays_the_first_floor() -> None:
    """With floor_start=4%, a run to +3% must NOT set a floor -- it stays on the hard stop.
    Pins that raising floor_start genuinely trades give-back protection for room to run."""
    from project_mai_tai.backtest.proximity_sweep import _walk_exit
    bars = [
        _b(10, 10, 10, 10, 0),
        _b(10, 10.30, 10, 10.3, 1),      # +3% high
        _b(10.3, 10.3, 9.4, 9.5, 2),     # collapses through the -5% stop
    ]
    rows = [{"flip": None}] * 3
    px2, r2 = _walk_exit(bars, rows, 0, 10.0, exit_mode="floor_ladder", target_pct=2.0,
                         stop_pct=-5.0, trail_pct=2.0, floor_start_pct=2.0)
    px4, r4 = _walk_exit(bars, rows, 0, 10.0, exit_mode="floor_ladder", target_pct=2.0,
                         stop_pct=-5.0, trail_pct=2.0, floor_start_pct=4.0)
    assert (r2, round(px2, 4)) == ("FLOOR", 10.30)   # floor armed at +3%
    assert (r4, round(px4, 4)) == ("STOP", 9.50)     # never armed -> took the stop
