from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


EASTERN_TZ = ZoneInfo("America/New_York")


def now_eastern() -> datetime:
    return datetime.now(EASTERN_TZ)


def now_eastern_str() -> str:
    return now_eastern().strftime("%I:%M:%S %p ET")


def today_eastern_str() -> str:
    return now_eastern().strftime("%Y-%m-%d")
