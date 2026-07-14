from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo


EASTERN_TZ = ZoneInfo("America/New_York")


# US equity FULL-closure holidays (no regular OR extended session). Shared by the
# v2 bot (entry-window gate) and the OMS (exit fillable-session gate) so the list
# lives in ONE place. **Roll this forward** — add the next year before ~December,
# or window checks silently treat an un-listed holiday as a normal trading day.
US_MARKET_HOLIDAYS: frozenset[date] = frozenset(
    {
        # --- 2026 ---
        date(2026, 1, 1),    # New Year's Day
        date(2026, 1, 19),   # MLK Jr. Day
        date(2026, 2, 16),   # Presidents' Day
        date(2026, 4, 3),    # Good Friday
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day (observed; Jul 4 is a Saturday)
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        # --- 2027 ---
        date(2027, 1, 1),    # New Year's Day
        date(2027, 1, 18),   # MLK Jr. Day
        date(2027, 2, 15),   # Presidents' Day
        date(2027, 3, 26),   # Good Friday
        date(2027, 5, 31),   # Memorial Day
        date(2027, 6, 18),   # Juneteenth (observed; Jun 19 is a Saturday)
        date(2027, 7, 5),    # Independence Day (observed; Jul 4 is a Sunday)
        date(2027, 9, 6),    # Labor Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 12, 24),  # Christmas (observed; Dec 25 is a Saturday)
    }
)


def is_fillable_et_session(
    now: datetime, start_hour: int, end_hour: int
) -> bool:
    """True iff `now` is a weekday, non-holiday ET day with hour in
    [start_hour, end_hour). Whole-hour granularity: end_hour=20 blocks at
    20:00:00 sharp, start_hour=7 allows from 07:00:00. Used as both the v2 entry
    window (7–18) and the OMS exit fillable-session gate (7–20 = Schwab pre-market
    fills open ~7 AM ET, after-hours fills end ~8 PM ET). Outside this window an
    order cannot fill, so placing/refreshing one is pure churn."""
    et = now.astimezone(EASTERN_TZ)
    if et.weekday() >= 5:
        return False
    if et.date() in US_MARKET_HOLIDAYS:
        return False
    return start_hour <= et.hour < end_hour


def now_eastern() -> datetime:
    return datetime.now(EASTERN_TZ)


def now_eastern_str() -> str:
    return now_eastern().strftime("%I:%M:%S %p ET")


def today_eastern_str() -> str:
    return now_eastern().strftime("%Y-%m-%d")


def session_day_eastern_str(
    now: datetime | None = None,
    *,
    reset_hour: int = 4,
    reset_minute: int = 0,
) -> str:
    current = now.astimezone(EASTERN_TZ) if now is not None else now_eastern()
    session_roll = current.replace(hour=reset_hour, minute=reset_minute, second=0, microsecond=0)
    if current < session_roll:
        current = current - timedelta(days=1)
    return current.strftime("%Y-%m-%d")
