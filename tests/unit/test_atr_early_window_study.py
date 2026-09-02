from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.backtest.atr_early_window_study import (
    EarlySnapshot,
    LockedEntry,
    early_snapshot,
    missing_first_ten_bars,
    threshold_rows,
)
from project_mai_tai.backtest.data import Quote, SchwabBar


def _entry(start: datetime, *, reached_5: bool = True) -> LockedEntry:
    return LockedEntry(
        session_day_et="2026-09-01",
        symbol="TEST",
        buy_signal_ts=start,
        entry_ts=start + timedelta(seconds=10),
        entry_px=10.0,
        reached_5=reached_5,
    )


def _bar(open_ts: datetime, open_px: float, close_px: float) -> SchwabBar:
    return SchwabBar(
        ts=int(open_ts.timestamp() * 1000),
        open=open_px,
        high=max(open_px, close_px),
        low=min(open_px, close_px),
        close=close_px,
        volume=100,
    )


def test_missing_first_ten_bars_names_exact_missing_closes() -> None:
    start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    bars = [_bar(start + timedelta(minutes=index), 10.0, 10.1) for index in range(10)]
    bars.pop(4)

    missing = missing_first_ten_bars(_entry(start), bars)

    assert missing == (start + timedelta(minutes=5),)


def test_snapshot_uses_executable_bids_and_elapsed_minute_low_progression() -> None:
    start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    entry = _entry(start)
    quotes = [
        Quote(entry.entry_ts, 9.9, 10.0),
        Quote(entry.entry_ts + timedelta(seconds=50), 10.2, 10.3),
        Quote(entry.entry_ts + timedelta(minutes=1, seconds=50), 9.6, 9.7),
        Quote(entry.entry_ts + timedelta(minutes=2, seconds=50), 9.5, 9.6),
    ]
    bars = [
        _bar(start, 10.0, 10.1),
        _bar(start + timedelta(minutes=1), 10.1, 10.0),
        _bar(start + timedelta(minutes=2), 10.0, 9.8),
    ]

    snapshot = early_snapshot(entry, quotes, bars, 3)

    assert snapshot.current_return_pct == pytest.approx(-5.0)
    assert snapshot.max_up_so_far_pct == pytest.approx(2.0)
    assert snapshot.max_down_so_far_pct == pytest.approx(-5.0)
    assert snapshot.touched_plus_2 is True
    assert snapshot.touched_minus_3 is True
    assert snapshot.last_bar_direction == "down"
    assert snapshot.latest_minute_new_low is True
    assert snapshot.every_minute_new_low is True
    assert snapshot.new_low_streak_minutes == 2


def _snapshot(*, reached_5: bool, current_return_pct: float) -> EarlySnapshot:
    start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    return EarlySnapshot(
        session_day_et="2026-09-01",
        symbol="TEST",
        buy_signal_ts=start,
        entry_ts=start,
        entry_px=10.0,
        reached_5=reached_5,
        checkpoint_minutes=3,
        checkpoint_ts=start + timedelta(minutes=3),
        quote_ts=start + timedelta(minutes=3),
        current_return_pct=current_return_pct,
        max_up_so_far_pct=1.0,
        max_down_so_far_pct=-1.0,
        touched_plus_2=False,
        touched_minus_3=False,
        last_bar_ts=start + timedelta(minutes=3),
        last_bar_direction="up",
        latest_minute_new_low=False,
        every_minute_new_low=False,
        new_low_streak_minutes=0,
    )


def test_threshold_rows_report_both_sides_and_denominators() -> None:
    snapshots = [
        *[_snapshot(reached_5=True, current_return_pct=1.0) for _ in range(35)],
        *[_snapshot(reached_5=True, current_return_pct=-3.0) for _ in range(7)],
        *[_snapshot(reached_5=False, current_return_pct=-3.0) for _ in range(25)],
        *[_snapshot(reached_5=False, current_return_pct=1.0) for _ in range(30)],
    ]

    row = next(
        row
        for row in threshold_rows(snapshots)
        if row["checkpoint_minutes"] == 3
        and row["measure"] == "Price vs entry"
        and row["cut"] == "<= -3"
    )

    assert row["winner_on_cut_side"] == 7
    assert row["winner_other_side"] == 35
    assert row["loser_on_cut_side"] == 25
    assert row["loser_other_side"] == 30
    assert row["winner_denominator"] == 42
    assert row["loser_denominator"] == 55
    assert row["separates"] is True
