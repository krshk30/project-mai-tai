from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.backtest.atr_flip_hold_study import FlipCandidate, NaturalPath
from project_mai_tai.backtest.atr_trend_exit_study import (
    _capture_pct,
    _downside_bucket,
    _five_number,
    path_timing,
    trend_outcomes,
)
from project_mai_tai.backtest.data import Quote


def _candidate(start: datetime) -> FlipCandidate:
    return FlipCandidate(
        session_day_et="2026-09-01",
        symbol="TEST",
        scanner_window_start=start - timedelta(minutes=10),
        scanner_window_end=None,
        buy_bar_ts=start - timedelta(minutes=1),
        buy_signal_ts=start,
        buy_close=10.0,
        buy_trail=9.5,
        decision_gap_minutes=1.0,
        entry_ts=start,
        entry_px=10.0,
        entry_bid=9.9,
        entry_quote_index=0,
        sell_signal_ts=None,
    )


def test_trend_outcomes_fill_first_signal_at_first_executable_bid() -> None:
    start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    quotes = [
        Quote(start, 9.9, 10.0),
        Quote(start + timedelta(minutes=2, seconds=1), 10.5, 10.6),
        Quote(start + timedelta(minutes=4, seconds=1), 10.2, 10.3),
        Quote(start + timedelta(minutes=10), 10.1, 10.2),
    ]
    outcomes = trend_outcomes(
        _candidate(start),
        quotes,
        start + timedelta(minutes=11),
        [start + timedelta(minutes=4)],
        [start + timedelta(minutes=2)],
    )
    assert outcomes["first"].trigger == "hist"
    assert outcomes["first"].exit_ts == quotes[1].ts
    assert outcomes["first"].return_pct == pytest.approx(5.0)
    assert outcomes["second"].trigger == "dot"
    assert outcomes["second"].exit_ts == quotes[2].ts


def test_second_signal_falls_back_to_close_when_one_indicator_never_fires() -> None:
    start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    quotes = [
        Quote(start, 9.9, 10.0),
        Quote(start + timedelta(minutes=2), 10.5, 10.6),
        Quote(start + timedelta(minutes=10), 10.1, 10.2),
    ]
    outcomes = trend_outcomes(
        _candidate(start),
        quotes,
        start + timedelta(minutes=11),
        [],
        [start + timedelta(minutes=2)],
    )
    assert outcomes["first"].exit_reason == "signal"
    assert outcomes["second"].exit_reason == "session_close"
    assert outcomes["second"].exit_ts == quotes[-1].ts


def test_capture_is_undefined_without_positive_max_up() -> None:
    assert _capture_pct(2.0, 10.0) == 20.0
    assert _capture_pct(-2.0, 10.0) == -20.0
    assert _capture_pct(-2.0, 0.0) is None


def test_path_timing_uses_first_low_and_includes_atr_sell_quote() -> None:
    start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    candidate = _candidate(start)
    quotes = [
        Quote(start, 9.9, 10.0),
        Quote(start + timedelta(minutes=2), 10.5, 10.6),
        Quote(start + timedelta(minutes=3), 9.5, 9.6),
        Quote(start + timedelta(minutes=4), 9.5, 9.6),
        Quote(start + timedelta(minutes=5), 9.0, 9.1),
        Quote(start + timedelta(minutes=6), 8.0, 8.1),
    ]
    path = NaturalPath(
        symbol="TEST",
        buy_signal_ts=start,
        entry_ts=start,
        entry_px=10.0,
        sell_signal_ts=start + timedelta(minutes=5),
        natural_exit_ts=start + timedelta(minutes=5),
        natural_exit_px=9.0,
        natural_exit_reason="atr_sell",
        natural_return_pct=-10.0,
        mfe_pct=5.0,
        mae_pct=-10.0,
        reached_5_ts=start + timedelta(minutes=2),
        reached_8_ts=None,
        reached_10_ts=None,
        quote_count=5,
    )

    timing = path_timing(candidate, path, quotes)

    assert timing.minutes_to_first_5 == 2.0
    assert timing.low_ts == start + timedelta(minutes=5)
    assert timing.minutes_to_low == 5.0
    assert timing.max_down_pct == pytest.approx(-10.0)


def test_timing_distribution_and_downside_boundaries_are_explicit() -> None:
    assert _five_number([1.0, 2.0, 3.0, 4.0, 9.0]) == {
        "min_minutes": 1.0,
        "q1_minutes": 2.0,
        "median_minutes": 3.0,
        "q3_minutes": 4.0,
        "max_minutes": 9.0,
    }
    assert _downside_bucket(-3.0) == "0 to -3%"
    assert _downside_bucket(-3.01) == "-3 to -5%"
    assert _downside_bucket(-5.01) == "-5 to -8%"
    assert _downside_bucket(-8.01) == "-8 to -12%"
    assert _downside_bucket(-12.01) == "Past -12%"
