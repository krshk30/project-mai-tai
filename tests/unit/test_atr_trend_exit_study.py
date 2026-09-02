from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.backtest.atr_flip_hold_study import FlipCandidate
from project_mai_tai.backtest.atr_trend_exit_study import (
    _capture_pct,
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
