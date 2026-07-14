from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from project_mai_tai.strategy_core.time_utils import (
    is_fillable_et_session,
    session_day_eastern_str,
)


EASTERN = ZoneInfo("America/New_York")


def test_session_day_eastern_str_uses_same_calendar_day_after_four_am() -> None:
    current = datetime(2026, 4, 22, 6, 30, tzinfo=EASTERN)

    assert session_day_eastern_str(current) == "2026-04-22"


def test_session_day_eastern_str_uses_previous_calendar_day_before_four_am() -> None:
    current = datetime(2026, 4, 22, 3, 59, tzinfo=EASTERN)

    assert session_day_eastern_str(current) == "2026-04-21"


# --- is_fillable_et_session (v2 entry window 7–18, OMS exit window 7–20) ---
# 2026-07-14 is a Tuesday; 2026-07-11 a Saturday; 2026-07-03 a full-closure
# holiday (Independence Day observed, a Friday).


def test_fillable_true_midday_weekday() -> None:
    now = datetime(2026, 7, 14, 10, 0, tzinfo=EASTERN)
    assert is_fillable_et_session(now, 7, 20) is True
    assert is_fillable_et_session(now, 7, 18) is True


def test_fillable_false_before_start_hour() -> None:
    now = datetime(2026, 7, 14, 6, 59, tzinfo=EASTERN)  # 6:59 AM ET, pre-7AM
    assert is_fillable_et_session(now, 7, 20) is False


def test_fillable_end_hour_is_exclusive() -> None:
    # 6 PM sharp is OUTSIDE the entry window (end=18); 5:59 PM is inside.
    assert is_fillable_et_session(datetime(2026, 7, 14, 18, 0, tzinfo=EASTERN), 7, 18) is False
    assert is_fillable_et_session(datetime(2026, 7, 14, 17, 59, tzinfo=EASTERN), 7, 18) is True
    # 8 PM sharp is outside the exit window (end=20); 7:59 PM is inside.
    assert is_fillable_et_session(datetime(2026, 7, 14, 20, 0, tzinfo=EASTERN), 7, 20) is False
    assert is_fillable_et_session(datetime(2026, 7, 14, 19, 59, tzinfo=EASTERN), 7, 20) is True


def test_fillable_false_on_weekend() -> None:
    now = datetime(2026, 7, 11, 10, 0, tzinfo=EASTERN)  # Saturday midday
    assert is_fillable_et_session(now, 7, 20) is False


def test_fillable_false_on_holiday() -> None:
    now = datetime(2026, 7, 3, 10, 0, tzinfo=EASTERN)  # Independence Day (observed)
    assert is_fillable_et_session(now, 7, 20) is False


def test_fillable_converts_utc_to_eastern() -> None:
    # 2026-07-14 23:00 UTC = 19:00 ET (7 PM) -> inside the 7–20 exit window,
    # but OUTSIDE the 7–18 entry window. Confirms tz conversion, not naive hour.
    now = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
    assert is_fillable_et_session(now, 7, 20) is True
    assert is_fillable_et_session(now, 7, 18) is False
