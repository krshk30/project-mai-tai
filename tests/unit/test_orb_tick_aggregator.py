from __future__ import annotations

from datetime import datetime, timedelta, timezone

from project_mai_tai.strategy_core.orb_tick_aggregator import OrbTickAggregator

OPEN = datetime(2026, 6, 18, 13, 30, tzinfo=timezone.utc)  # 09:30 ET


def _ts(minute, second=0):
    return OPEN + timedelta(minutes=minute, seconds=second)


def test_no_bar_until_minute_rolls():
    agg = OrbTickAggregator(session_open=OPEN)
    assert agg.add_tick(_ts(0, 1), 5.00, 100) is None
    assert agg.add_tick(_ts(0, 30), 5.10, 100) is None  # same minute -> still building


def test_ohlcv_and_roll():
    agg = OrbTickAggregator(session_open=OPEN)
    agg.add_tick(_ts(0, 1), 5.00, 100)   # open
    agg.add_tick(_ts(0, 10), 5.20, 50)   # high
    agg.add_tick(_ts(0, 20), 4.90, 50)   # low
    agg.add_tick(_ts(0, 50), 5.10, 100)  # close
    bar = agg.add_tick(_ts(1, 1), 5.15, 10)  # next minute -> emits the 09:30 bar
    assert bar is not None
    assert bar.open == 5.00 and bar.high == 5.20 and bar.low == 4.90 and bar.close == 5.10
    assert bar.volume == 300
    assert bar.timestamp == OPEN


def test_vwap_and_ema_present_and_sane():
    agg = OrbTickAggregator(session_open=OPEN)
    agg.add_tick(_ts(0, 1), 5.00, 100)
    agg.add_tick(_ts(0, 50), 5.00, 100)
    b1 = agg.add_tick(_ts(1, 1), 6.00, 100)
    # first bar: flat at 5.00 -> vwap ~5.00, ema9 seeds to close 5.00
    assert round(b1.vwap, 4) == 5.0
    assert b1.ema9 == 5.0
    b2 = agg.add_tick(_ts(2, 1), 6.0, 1)  # second bar closed at 6.00
    assert b2 is not None
    assert b2.ema9 > 5.0  # EMA pulled toward the higher close
    assert 5.0 < b2.vwap < 6.0  # session VWAP between the two bars' typical prices


def test_premarket_ticks_excluded_from_session_vwap():
    agg = OrbTickAggregator(session_open=OPEN)
    pre = OPEN - timedelta(minutes=5)
    agg.add_tick(pre.replace(second=1), 10.0, 1000)          # pre-open (should NOT feed VWAP)
    bar_pre = agg.add_tick(OPEN.replace(second=1), 5.0, 100)  # rolls the pre-open bar
    assert bar_pre is not None
    assert bar_pre.vwap == bar_pre.close  # cum_v=0 for pre-open -> vwap falls back to close


def test_out_of_order_tick_ignored():
    agg = OrbTickAggregator(session_open=OPEN)
    agg.add_tick(_ts(2, 1), 5.0, 100)
    # a late tick stamped to an earlier minute is dropped (returns None, no crash)
    assert agg.add_tick(_ts(1, 1), 4.0, 100) is None


def test_flush_finalizes_open_bucket():
    agg = OrbTickAggregator(session_open=OPEN)
    agg.add_tick(_ts(0, 1), 5.0, 100)
    bar = agg.flush()
    assert bar is not None and bar.close == 5.0
    assert agg.flush() is None  # nothing left
