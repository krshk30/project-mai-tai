from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from compare_tos_modified_atr import (  # noqa: E402
    BAR_SQL,
    LIVE_GAP_BOUND_MS,
    TOS_WILDERS_PREFETCH_BARS,
    AtrStage,
    _modified_true_range,
    calculate_live_strategy,
    calculate_modified_atr,
    first_divergence,
    refuse_regular_market_hours,
    session_anchor_ms,
)


def _bar(at: datetime, close: float, *, high: float | None = None, low: float | None = None) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=int(at.timestamp() * 1000),
        open=close - 0.02,
        high=high if high is not None else close + 0.05,
        low=low if low is not None else close - 0.05,
        close=close,
        volume=10_000,
    )


def _series(start: datetime, count: int, *, step: timedelta = timedelta(minutes=1)) -> list[OHLCVBar]:
    return [_bar(start + index * step, 5.0 + index * 0.01) for index in range(count)]


def test_modified_true_range_uses_the_gap_minimizing_references() -> None:
    previous = _bar(datetime(2026, 9, 10, 14, 0, tzinfo=UTC), 10.0, high=10.2, low=9.8)
    current = _bar(datetime(2026, 9, 10, 14, 1, tzinfo=UTC), 12.0, high=12.2, low=11.8)

    result = _modified_true_range(
        current=current,
        previous=previous,
        average_high_low=0.4,
    )

    assert result == pytest.approx(1.4)


def test_wilders_is_seeded_from_the_first_five_valid_modified_ranges() -> None:
    bars = _series(datetime(2026, 9, 10, 8, 0, tzinfo=UTC), 10)
    rows = calculate_modified_atr(bars, session_sliced=True, guard_bar_gaps=True)
    valid = [row.modified_tr for row in rows if row.modified_tr is not None]
    first_wilder = next(row.wilders for row in rows if row.wilders is not None)

    assert len(valid) >= 5
    assert first_wilder == pytest.approx(sum(valid[:5]) / 5)


def test_session_sliced_model_resets_at_0400_et_while_continuous_does_not() -> None:
    before = _series(datetime(2026, 9, 10, 23, 50, tzinfo=UTC), 10)
    after = _series(datetime(2026, 9, 11, 8, 0, tzinfo=UTC), 10)
    bars = before + after

    sliced = calculate_modified_atr(bars, session_sliced=True, guard_bar_gaps=False)
    continuous = calculate_modified_atr(bars, session_sliced=False, guard_bar_gaps=False)

    boundary = len(before)
    assert sliced[boundary].reset is True
    assert sliced[boundary].modified_tr is None
    assert continuous[boundary].reset is False
    assert continuous[boundary].modified_tr is not None


def test_gap_guard_changes_true_range_at_the_gap_not_at_a_later_stage() -> None:
    bars = _series(datetime(2026, 9, 10, 14, 0, tzinfo=UTC), 10)
    bars.append(_bar(datetime(2026, 9, 10, 15, 0, tzinfo=UTC), 7.0, high=7.1, low=6.9))

    guarded = calculate_modified_atr(bars, session_sliced=True, guard_bar_gaps=True)
    unguarded = calculate_modified_atr(bars, session_sliced=True, guard_bar_gaps=False)

    assert guarded[-1].gap_ms > LIVE_GAP_BOUND_MS
    assert first_divergence(guarded, unguarded) == (len(bars) - 1, "modified_true_range")


def test_real_live_method_matches_independent_live_model() -> None:
    first = _series(datetime(2026, 9, 10, 13, 0, tzinfo=UTC), 18)
    gap = _series(datetime(2026, 9, 10, 14, 0, tzinfo=UTC), 12)
    next_session = _series(datetime(2026, 9, 11, 8, 0, tzinfo=UTC), 12)
    bars = first + gap + next_session

    actual = calculate_live_strategy(bars)
    independent = calculate_modified_atr(
        bars,
        session_sliced=True,
        guard_bar_gaps=True,
    )

    assert first_divergence(actual, independent) is None


def test_first_divergence_names_wilders_and_trail_stages() -> None:
    bars = _series(datetime(2026, 9, 10, 14, 0, tzinfo=UTC), 10)
    original = calculate_modified_atr(bars, session_sliced=True, guard_bar_gaps=True)
    index = next(i for i, row in enumerate(original) if row.wilders is not None)
    row = original[index]
    altered_wilder = list(original)
    altered_wilder[index] = AtrStage(**{**row.__dict__, "wilders": float(row.wilders) + 0.1})
    altered_trail = list(original)
    altered_trail[index] = AtrStage(**{**row.__dict__, "trail": float(row.trail) + 0.1})

    assert first_divergence(original, altered_wilder) == (index, "wilders_seed_or_update")
    assert first_divergence(original, altered_trail) == (index, "trail_state_or_flip")


def test_market_hours_guard_is_non_optional() -> None:
    with pytest.raises(SystemExit, match="refusing historical ATR query"):
        refuse_regular_market_hours(datetime(2026, 9, 11, 14, 0, tzinfo=UTC))
    refuse_regular_market_hours(datetime(2026, 9, 11, 20, 1, tzinfo=UTC))


def test_session_anchor_uses_0400_et() -> None:
    before = datetime(2026, 9, 11, 7, 59, tzinfo=UTC)
    after = datetime(2026, 9, 11, 8, 0, tzinfo=UTC)
    assert session_anchor_ms(int(after.timestamp() * 1000)) > session_anchor_ms(
        int(before.timestamp() * 1000)
    )


def test_query_reads_only_complete_live_strategy_history() -> None:
    lowered = BAR_SQL.lower()
    assert "source = 'live'" in lowered
    assert "strategy_code = 'schwab_1m_v2'" in lowered
    assert "bar_time >= %(history_start)s" in lowered
    assert not any(word in lowered for word in ("insert ", "update ", "delete ", "truncate "))


def test_tos_wilders_prefetch_requirement_is_the_documented_seven_lengths() -> None:
    assert TOS_WILDERS_PREFETCH_BARS == 7 * 5 == 35
