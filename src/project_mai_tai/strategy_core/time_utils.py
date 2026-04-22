from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo


EASTERN_TZ = ZoneInfo("America/New_York")


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
