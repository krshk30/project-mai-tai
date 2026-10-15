import importlib.util
from datetime import date, datetime, time
from pathlib import Path

from project_mai_tai.momwarn_validation_window import MomentumValidationWindow

SPEC = importlib.util.spec_from_file_location("validation_day_guard", Path(__file__).with_name("daily_guard.py"))
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


def test_active_validation_day_is_not_rolled_at_default_close():
    day = date(2026, 10, 13)
    value = MomentumValidationWindow(day, time(18), time(18), time(18, 10))
    assert GUARD.session_day(datetime(2026, 10, 13, 12), set(), value) == day
    assert GUARD.session_day(datetime(2026, 10, 13, 18, 9, 59), set(), value) == day
    assert GUARD.session_day(datetime(2026, 10, 13, 18, 10), set(), value) == date(2026, 10, 14)


def test_still_configured_old_date_does_not_extend_tomorrows_guard():
    value = MomentumValidationWindow(date(2026, 10, 13), time(18), time(18), time(18, 10))
    assert GUARD.session_day(datetime(2026, 10, 14, 9, 39), set(), value) == date(2026, 10, 14)
    assert GUARD.session_day(datetime(2026, 10, 14, 9, 40), set(), value) == date(2026, 10, 15)


def test_default_weekend_and_holiday_skips_remain():
    value = MomentumValidationWindow()
    assert GUARD.closed_timer_day(datetime(2026, 10, 17, 8), set(), value)
    assert GUARD.closed_timer_day(datetime(2026, 10, 13, 8), {date(2026, 10, 13)}, value)
    assert not GUARD.closed_timer_day(datetime(2026, 10, 13, 8), set(), value)
