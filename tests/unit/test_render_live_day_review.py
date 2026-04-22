from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from render_live_day_review import BarRow, _find_bar_index  # noqa: E402


def _bar(ts: datetime) -> BarRow:
    return BarRow(
        bar_time=ts,
        open_price=1.0,
        high_price=1.1,
        low_price=0.9,
        close_price=1.0,
        volume=100,
        decision_status="",
        decision_reason="",
        decision_path="",
        decision_score="",
        indicators={},
    )


def test_find_bar_index_prefers_exact_timestamp_match_for_completed_bar_mode() -> None:
    bars = [
        _bar(datetime(2026, 4, 1, 18, 52, 0, tzinfo=UTC)),
        _bar(datetime(2026, 4, 1, 18, 52, 30, tzinfo=UTC)),
        _bar(datetime(2026, 4, 1, 18, 53, 0, tzinfo=UTC)),
    ]

    index = _find_bar_index(
        bars,
        datetime(2026, 4, 1, 18, 52, 30, tzinfo=UTC),
        prefer_completed_bar=True,
    )

    assert index == 1


def test_find_bar_index_falls_back_to_previous_completed_bar_when_no_exact_match() -> None:
    bars = [
        _bar(datetime(2026, 4, 1, 18, 52, 0, tzinfo=UTC)),
        _bar(datetime(2026, 4, 1, 18, 52, 30, tzinfo=UTC)),
        _bar(datetime(2026, 4, 1, 18, 53, 0, tzinfo=UTC)),
    ]

    index = _find_bar_index(
        bars,
        datetime(2026, 4, 1, 18, 52, 45, tzinfo=UTC),
        prefer_completed_bar=True,
    )

    assert index == 1
