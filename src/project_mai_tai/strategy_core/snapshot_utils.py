from __future__ import annotations

from datetime import datetime
import time
from zoneinfo import ZoneInfo

from project_mai_tai.strategy_core.models import MarketSnapshot


EASTERN_TZ = ZoneInfo("America/New_York")


def now_eastern() -> datetime:
    return datetime.now(EASTERN_TZ)


def get_current_price(snapshot: MarketSnapshot) -> float | None:
    last_trade = snapshot.last_trade
    if last_trade and last_trade.price and last_trade.price > 0:
        return last_trade.price

    minute = snapshot.minute
    if minute and minute.close is not None and minute.close > 0:
        return minute.close

    day = snapshot.day
    if day and day.close is not None and day.close > 0:
        return day.close

    return None


def get_current_volume(snapshot: MarketSnapshot) -> int:
    day = snapshot.day
    if day and day.volume and day.volume > 0:
        return int(day.volume)

    minute = snapshot.minute
    if minute and minute.accumulated_volume:
        return int(minute.accumulated_volume)

    return 0


def get_current_hod(snapshot: MarketSnapshot) -> float:
    day = snapshot.day
    if day and day.high and day.high > 0:
        return day.high

    minute = snapshot.minute
    if minute and minute.high and minute.high > 0:
        return minute.high

    price = get_current_price(snapshot)
    return price if price else 0.0


def get_current_vwap(snapshot: MarketSnapshot) -> float:
    day = snapshot.day
    if day and day.vwap and day.vwap > 0:
        return day.vwap

    minute = snapshot.minute
    if minute and minute.vwap and minute.vwap > 0:
        return minute.vwap

    return 0.0


def get_bid_ask(snapshot: MarketSnapshot) -> dict[str, float | int]:
    result: dict[str, float | int] = {
        "bid": 0.0,
        "ask": 0.0,
        "bid_size": 0,
        "ask_size": 0,
        "spread": 0.0,
        "spread_pct": 0.0,
    }
    quote = snapshot.last_quote
    if quote is None:
        return result

    bid = quote.bid_price or 0.0
    ask = quote.ask_price or 0.0
    bid_size = int(quote.bid_size or 0)
    ask_size = int(quote.ask_size or 0)

    spread = round(ask - bid, 4) if bid > 0 and ask > 0 else 0.0
    mid = (ask + bid) / 2 if bid > 0 and ask > 0 else 0.0
    spread_pct = round((spread / mid) * 100, 2) if mid > 0 else 0.0

    return {
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread": spread,
        "spread_pct": spread_pct,
    }


def get_data_age_secs(snapshot: MarketSnapshot) -> int:
    if not snapshot.updated_ns:
        return -1
    now_ns = int(time.time() * 1e9)
    age_secs = (now_ns - snapshot.updated_ns) / 1e9
    return max(0, int(age_secs))


def compute_rvol(day_volume: float, avg_daily_volume: float, minutes_since_4am: int) -> float:
    if avg_daily_volume <= 0 or minutes_since_4am <= 0:
        return 0.0

    expected = avg_daily_volume * (minutes_since_4am / 390)
    if expected <= 0:
        return 0.0

    return day_volume / expected


def get_minutes_since_4am(now: datetime | None = None) -> int:
    current = now or now_eastern()
    four_am = current.replace(hour=4, minute=0, second=0, microsecond=0)
    if current < four_am:
        return 0
    return int((current - four_am).total_seconds() / 60)
