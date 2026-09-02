from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.backtest.atr_no_reclaim_trade_list import (
    GROUP_NEVER_PLUS_1,
    GROUP_PARTIAL,
    GROUP_REACHED_PLUS_5,
    excursion_times,
    select_rows,
)
from project_mai_tai.backtest.data import Quote


def _trend(symbol: str, max_up: float, reached_5: bool) -> dict[str, str]:
    return {
        "symbol": symbol,
        "entry_ts": f"2026-08-24T12:00:{int(symbol[1:]):02d}+00:00",
        "reached_5": str(reached_5),
        "atr_segment_max_up_pct": str(max_up),
    }


def _profile(symbol: str, population: str) -> dict[str, str]:
    return {
        "symbol": symbol,
        "entry_ts": f"2026-08-24T12:00:{int(symbol[1:]):02d}+00:00",
        "population": population,
        "first_five_shape": "never_reclaimed_entry",
    }


def test_select_rows_preserves_corrected_three_groups() -> None:
    trend = []
    profiles = []
    specs = (
        [(GROUP_NEVER_PLUS_1, 0.5, False)] * 14
        + [(GROUP_REACHED_PLUS_5, 6.0, True)] * 5
        + [(GROUP_PARTIAL, 2.0, False)] * 3
    )
    for index, (group, max_up, reached_5) in enumerate(specs):
        symbol = f"S{index}"
        population = "never_touched_plus_1" if group == GROUP_NEVER_PLUS_1 else "other_73"
        trend.append(_trend(symbol, max_up, reached_5))
        profiles.append(_profile(symbol, population))

    selected = select_rows(trend, profiles)

    assert [row["group"] for row in selected].count(GROUP_NEVER_PLUS_1) == 14
    assert [row["group"] for row in selected].count(GROUP_REACHED_PLUS_5) == 5
    assert [row["group"] for row in selected].count(GROUP_PARTIAL) == 3


def test_excursion_times_reconcile_bid_path() -> None:
    start = datetime(2026, 8, 24, 12, tzinfo=UTC)
    row = {
        "symbol": "TEST",
        "entry_ts": start.isoformat(),
        "entry_px": "10",
        "atr_sell_exit_ts": (start + timedelta(minutes=3)).isoformat(),
        "atr_segment_max_up_pct": "5",
        "atr_segment_max_down_pct": "-3",
    }
    quotes = [
        Quote(start, 9.9, 10.0),
        Quote(start + timedelta(minutes=1), 10.5, 10.6),
        Quote(start + timedelta(minutes=2), 9.7, 9.8),
    ]

    peak_ts, max_up, low_ts, max_down = excursion_times(row, quotes)

    assert peak_ts == start + timedelta(minutes=1)
    assert max_up == pytest.approx(5.0)
    assert low_ts == start + timedelta(minutes=2)
    assert max_down == pytest.approx(-3.0)
