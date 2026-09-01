from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.backtest.atr_entry_filter_study import (
    EntryCandidate,
    IndicatorBar,
    _latest_completed_indicator,
    audit_future_quotes,
    leave_one_day_out,
)
from project_mai_tai.backtest.data import Quote


def _candidate(day: str, index: int, *, won: bool) -> EntryCandidate:
    value = float(index)
    return EntryCandidate(
        session_day_et=day,
        symbol=f"S{index}",
        entry_ts=f"{day}T14:00:00+00:00",
        entry_slot="first",
        entry_mode="resting",
        entry_px=10.0,
        won=won,
        ret_pct=1.0 if won else -2.0,
        features={"signal": value},
        first_bid_ret_pct=-0.1,
        future_mfe_pct=5.0,
        future_reached_plus_1=True,
        future_reached_plus_2=True,
        future_reached_plus_5=True,
    )


def test_latest_indicator_requires_the_bar_to_be_fully_closed() -> None:
    entry = datetime(2026, 8, 24, 14, 0, 20, tzinfo=UTC)
    bars = [
        IndicatorBar(entry - timedelta(seconds=50), 10, {}),
        IndicatorBar(entry - timedelta(seconds=20), 20, {}),
    ]

    bar, index = _latest_completed_indicator(bars, entry)

    assert bar == bars[0]
    assert index == 0


def test_zero_floor_can_exit_before_a_later_five_percent_run() -> None:
    entry = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
    quotes = [
        Quote(entry + timedelta(milliseconds=1), bid=9.95, ask=10.0),
        Quote(entry + timedelta(minutes=2), bid=10.6, ask=10.61),
    ]

    first, mfe, hit_1, hit_2, hit_5 = audit_future_quotes(quotes, entry, 10.0)

    assert first == pytest.approx(-0.5)
    assert mfe == pytest.approx(6.0)
    assert hit_1 and hit_2 and hit_5


def test_leave_one_day_out_caps_each_day_at_six() -> None:
    candidates = []
    for day_index, day in enumerate(("2026-08-24", "2026-08-25", "2026-08-26")):
        candidates.extend(
            _candidate(day, day_index * 10 + index, won=index >= 5) for index in range(10)
        )

    selected = leave_one_day_out(candidates, ("signal",))

    assert len(selected) == 18
    assert all(
        sum(row["session_day_et"] == day for row in selected) == 6
        for day in ("2026-08-24", "2026-08-25", "2026-08-26")
    )
