from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.strategy_core.time_utils import session_day_eastern_str


EASTERN = ZoneInfo("America/New_York")


def test_session_day_eastern_str_uses_same_calendar_day_after_four_am() -> None:
    current = datetime(2026, 4, 22, 6, 30, tzinfo=EASTERN)

    assert session_day_eastern_str(current) == "2026-04-22"


def test_session_day_eastern_str_uses_previous_calendar_day_before_four_am() -> None:
    current = datetime(2026, 4, 22, 3, 59, tzinfo=EASTERN)

    assert session_day_eastern_str(current) == "2026-04-21"
