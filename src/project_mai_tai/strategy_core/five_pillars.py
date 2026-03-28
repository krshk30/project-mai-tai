from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from project_mai_tai.strategy_core.models import MarketSnapshot, ReferenceData
from project_mai_tai.strategy_core.snapshot_utils import (
    compute_rvol,
    get_bid_ask,
    get_current_hod,
    get_current_price,
    get_current_volume,
    get_current_vwap,
    get_data_age_secs,
    get_minutes_since_4am,
)


@dataclass(frozen=True)
class FivePillarsConfig:
    min_price: float = 1.0
    max_price: float = 10.0
    max_float: int = 100_000_000
    min_change_pct: float = 10.0
    min_rvol_5pillars: float = 2.0
    min_today_volume: int = 500_000


def apply_five_pillars(
    snapshots: Sequence[MarketSnapshot],
    reference_data: Mapping[str, ReferenceData],
    config: FivePillarsConfig | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    active_config = config or FivePillarsConfig()
    minutes = get_minutes_since_4am(now)
    if minutes <= 0:
        return []

    results: list[dict[str, object]] = []
    for snapshot in snapshots:
        ticker = snapshot.ticker.upper()
        if not ticker or snapshot.day is None:
            continue

        price = get_current_price(snapshot)
        volume = get_current_volume(snapshot)
        if price is None:
            continue
        if not (active_config.min_price <= price <= active_config.max_price):
            continue

        ref = reference_data.get(ticker)
        if ref is None:
            continue
        if ref.shares_outstanding > active_config.max_float:
            continue
        if volume < active_config.min_today_volume:
            continue

        change_pct = snapshot.todays_change_percent
        if change_pct is None or change_pct < active_config.min_change_pct:
            continue

        rvol = compute_rvol(volume, ref.avg_daily_volume, minutes)
        if rvol < active_config.min_rvol_5pillars:
            continue

        bid_ask = get_bid_ask(snapshot)
        results.append(
            {
                "ticker": ticker,
                "price": round(price, 4),
                "change_pct": round(change_pct, 2),
                "volume": int(volume),
                "rvol": round(rvol, 2),
                "shares_outstanding": int(ref.shares_outstanding),
                "hod": round(get_current_hod(snapshot), 4),
                "vwap": round(get_current_vwap(snapshot), 4),
                "prev_close": snapshot.previous_close,
                "avg_daily_volume": round(ref.avg_daily_volume, 2),
                "bid": bid_ask["bid"],
                "ask": bid_ask["ask"],
                "bid_size": bid_ask["bid_size"],
                "ask_size": bid_ask["ask_size"],
                "spread": bid_ask["spread"],
                "spread_pct": bid_ask["spread_pct"],
                "data_age_secs": get_data_age_secs(snapshot),
            }
        )

    return sorted(results, key=lambda item: float(item["change_pct"]), reverse=True)
