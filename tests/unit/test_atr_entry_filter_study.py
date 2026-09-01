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
        future_mae_pct=-2.0,
        future_reached_plus_1=True,
        future_reached_plus_2=True,
        future_reached_plus_5=True,
        mae_before_plus_1_pct=-0.5,
        mae_before_plus_2_pct=-1.0,
        mae_before_plus_5_pct=-2.0,
        seconds_to_plus_1=10.0,
        seconds_to_plus_2=20.0,
        seconds_to_plus_5=30.0,
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

    audit = audit_future_quotes(quotes, entry, 10.0)

    assert audit.first_bid_ret_pct == pytest.approx(-0.5)
    assert audit.mfe_pct == pytest.approx(6.0)
    assert audit.mae_before_plus_5_pct == pytest.approx(-0.5)
    assert audit.seconds_to_plus_5 == pytest.approx(120.0)
    assert audit.reached_plus_1 and audit.reached_plus_2 and audit.reached_plus_5


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
