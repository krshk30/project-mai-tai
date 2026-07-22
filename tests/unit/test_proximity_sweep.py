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


def _b(o, h, lo, c, i=0):
    return Bar(ts=i * 60000, open=o, high=h, low=lo, close=c, volume=1000)


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


class _Q:
    def __init__(self, ts_ms, bid, ask):
        from datetime import datetime, timezone
        self.ts = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
        self.bid = bid
        self.ask = ask


def test_resting_buy_stop_arms_below_and_fills_on_the_way_up() -> None:
    """BUY STOP semantics, not buy limit.

    The trail sits ABOVE price while short, so trail*(1-X%) is normally ABOVE the ask. A buy
    LIMIT there is marketable and fills instantly at a worse price -- that bug produced a 2.9%
    win rate and -9.3% stop fills. A buy STOP must ARM while the ask is below the level and
    trigger only when the ask RISES to it.
    """
    from project_mai_tai.backtest.proximity_sweep import simulate_resting_entry
    base = 1784988000000          # 10:00 ET, inside the live window
    bars = [Bar(ts=base + i * 60000, open=10, high=10, low=10, close=10, volume=100)
            for i in range(3)]
    rows = [{"state": "short", "trail": 10.0, "close": 10.0, "flip": None} for _ in bars]

    # offset 1% -> level 9.90. Ask stays below it: armed, never triggers, NO trade.
    below = [_Q(base + i * 1000, 9.79, 9.80) for i in range(150)]
    trades, acct = simulate_resting_entry(bars, rows, below, symbol="X", day="d", offset_pct=1.0)
    assert trades == []
    assert acct["filled"] == 0

    # Ask starts below (arms) then rises through 9.90 -> fills at the OBSERVED ask, >= level.
    rising = [_Q(base + i * 1000, 9.79, 9.80 if i < 5 else 9.93) for i in range(150)]
    trades2, acct2 = simulate_resting_entry(bars, rows, rising, symbol="X", day="d", offset_pct=1.0)
    assert len(trades2) == 1
    assert round(trades2[0].entry_price, 4) == 9.93     # paid the ask, not the level
    assert acct2["filled"] == 1


def test_resting_buy_stop_does_not_place_when_already_through_the_level() -> None:
    """If the ask is already above the level there is no valid stop placement -- skip, never
    fill at a worse price (the marketable-limit bug)."""
    from project_mai_tai.backtest.proximity_sweep import simulate_resting_entry
    base = 1784988000000
    bars = [Bar(ts=base + i * 60000, open=10, high=10, low=10, close=10, volume=100)
            for i in range(3)]
    rows = [{"state": "short", "trail": 10.0, "close": 10.0, "flip": None} for _ in bars]
    through = [_Q(base + i * 1000, 9.98, 9.99) for i in range(150)]   # already above 9.90
    trades, acct = simulate_resting_entry(bars, rows, through, symbol="X", day="d", offset_pct=1.0)
    assert trades == []
    assert acct["filled"] == 0


def test_resting_entry_counts_a_missed_cross_as_a_miss_not_a_free_skip() -> None:
    """An unfilled segment that then CROSSES is a missed winner and must be counted."""
    from project_mai_tai.backtest.proximity_sweep import simulate_resting_entry
    base = 1784988000000
    bars = [Bar(ts=base + i * 60000, open=10, high=10, low=10, close=10, volume=100)
            for i in range(3)]
    rows = [
        {"state": "short", "trail": 10.0, "close": 10.0, "flip": None},
        {"state": "short", "trail": 10.0, "close": 10.0, "flip": None},
        {"state": "long", "trail": 9.0, "close": 10.5, "flip": "BUY"},   # crossed without us
    ]
    quotes = [_Q(base + i * 1000, 9.95, 9.99) for i in range(200)]   # never reaches 9.90
    trades, acct = simulate_resting_entry(bars, rows, quotes, symbol="X", day="d", offset_pct=1.0)
    assert trades == []
    assert acct["missed_cross"] == 1
    assert acct["avoided_no_cross"] == 0


def test_resting_entry_is_gated_to_the_live_window() -> None:
    """03:00 ET is outside 07:00-16:30: no fill even though the ask is at the level."""
    from project_mai_tai.backtest.proximity_sweep import simulate_resting_entry
    base = 1784962800000   # 2026-07-20 03:00 ET
    bars = [Bar(ts=base + i * 60000, open=10, high=10, low=10, close=10, volume=100)
            for i in range(3)]
    rows = [{"state": "short", "trail": 10.0, "close": 10.0, "flip": None} for _ in bars]
    quotes = [_Q(base + i * 1000, 9.88, 9.90) for i in range(150)]
    trades, acct = simulate_resting_entry(bars, rows, quotes, symbol="X", day="d", offset_pct=1.0)
    assert trades == []
    assert acct["out_of_window"] > 0


def test_limit_pullback_fills_at_our_price_when_the_market_comes_down() -> None:
    from project_mai_tai.backtest.proximity_sweep import simulate_limit_pullback_entry
    base = 1784988000000                      # 10:00 ET, in-window
    bars = [Bar(ts=base + i * 60000, open=10, high=10, low=10, close=10, volume=100)
            for i in range(6)]
    # signal bar 0: close 10.00, trail 10.15 -> proximity 1.5% (inside a 2% threshold)
    rows = [{"state": "short", "trail": 10.15, "close": 10.0, "flip": None} for _ in bars]
    # pullback 1% -> level 9.90. Ask dips to 9.90 in a later bar.
    q = [_Q(base + 60000 + i * 1000, 9.85, 9.90 if i == 30 else 9.99) for i in range(300)]
    trades, acct = simulate_limit_pullback_entry(
        bars, rows, q, symbol="X", day="d", proximity_pct=2.0, pullback_pct=1.0)
    assert acct["filled"] == 1
    assert round(trades[0].entry_price, 4) == 9.90     # OUR price, not the market print


def test_limit_pullback_records_a_missed_cross_when_price_never_comes_back() -> None:
    """The cost side: if it runs straight up, we never fill AND we lose the winner."""
    from project_mai_tai.backtest.proximity_sweep import simulate_limit_pullback_entry
    base = 1784988000000
    bars = [Bar(ts=base + i * 60000, open=10, high=10, low=10, close=10, volume=100)
            for i in range(4)]
    rows = [
        {"state": "short", "trail": 10.15, "close": 10.0, "flip": None},
        {"state": "short", "trail": 10.15, "close": 10.1, "flip": None},
        {"state": "long", "trail": 9.5, "close": 10.3, "flip": "BUY"},    # crossed without us
        {"state": "long", "trail": 9.5, "close": 10.4, "flip": None},
    ]
    q = [_Q(base + 60000 + i * 1000, 10.05, 10.08) for i in range(200)]   # never hits 9.90
    trades, acct = simulate_limit_pullback_entry(
        bars, rows, q, symbol="X", day="d", proximity_pct=2.0, pullback_pct=1.0)
    assert trades == []
    assert acct["missed_cross"] == 1
    assert acct["no_fill_no_cross"] == 0


def test_quote_sanity_filter_rejects_bad_prints() -> None:
    """CPHI 2026-07-21 10:28 showed a 1,989,900% spread. Real spreads on these names are
    0.20-0.89%, so anything past 50% is a bad print, not a market."""
    from datetime import datetime, timezone
    from project_mai_tai.backtest.data import Quote, _quote_is_sane
    ts = datetime(2026, 7, 21, tzinfo=timezone.utc)
    assert _quote_is_sane(Quote(ts=ts, bid=1.50, ask=1.51)) is True      # normal
    assert _quote_is_sane(Quote(ts=ts, bid=1.50, ask=2.20)) is True      # wide but real (46%)
    assert _quote_is_sane(Quote(ts=ts, bid=0.0001, ask=1.99)) is False   # the CPHI shape
    assert _quote_is_sane(Quote(ts=ts, bid=0.0, ask=1.50)) is False      # no bid
    assert _quote_is_sane(Quote(ts=ts, bid=1.60, ask=1.50)) is False     # crossed
