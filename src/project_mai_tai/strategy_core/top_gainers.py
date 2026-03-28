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
class TopGainersConfig:
    min_price: float = 1.0
    max_price: float = 10.0
    min_rvol_top_gainers: float = 2.0
    top_gainers_count: int = 20


class TopGainersTracker:
    def __init__(self, config: TopGainersConfig | None = None) -> None:
        self.config = config or TopGainersConfig()
        self._previous_tickers: list[str] = []
        self._previous_ranks: dict[str, int] = {}

    def update(
        self,
        snapshots: Sequence[MarketSnapshot],
        reference_data: Mapping[str, ReferenceData],
        *,
        now: datetime | None = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        minutes = get_minutes_since_4am(now)
        if minutes <= 0:
            return [], []

        candidates: list[dict[str, object]] = []
        for snapshot in snapshots:
            ticker = snapshot.ticker.upper()
            if not ticker or snapshot.day is None:
                continue

            price = get_current_price(snapshot)
            volume = get_current_volume(snapshot)
            change_pct = snapshot.todays_change_percent
            if price is None or change_pct is None:
                continue
            if not (self.config.min_price <= price <= self.config.max_price):
                continue

            ref = reference_data.get(ticker)
            if ref is None:
                continue

            rvol = compute_rvol(volume, ref.avg_daily_volume, minutes)
            if rvol < self.config.min_rvol_top_gainers:
                continue

            bid_ask = get_bid_ask(snapshot)
            candidates.append(
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

        candidates.sort(key=lambda item: float(item["change_pct"]), reverse=True)
        top_gainers = candidates[: self.config.top_gainers_count]

        current_tickers = [str(item["ticker"]) for item in top_gainers]
        current_ranks = {ticker: index + 1 for index, ticker in enumerate(current_tickers)}
        change_events = self._detect_changes(current_tickers, current_ranks, top_gainers)
        self._previous_tickers = current_tickers
        self._previous_ranks = current_ranks
        return top_gainers, change_events

    def reset(self) -> None:
        self._previous_tickers = []
        self._previous_ranks = {}

    def _detect_changes(
        self,
        current_tickers: list[str],
        current_ranks: dict[str, int],
        top_gainers: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        now_str = datetime.now().strftime("%H:%M:%S")
        previous_set = set(self._previous_tickers)
        current_set = set(current_tickers)
        events: list[dict[str, object]] = []

        for ticker in current_tickers:
            if ticker not in previous_set:
                rank = current_ranks[ticker]
                stock = next((item for item in top_gainers if item["ticker"] == ticker), None)
                events.append(
                    {
                        "type": "NEW",
                        "ticker": ticker,
                        "rank": rank,
                        "time": now_str,
                        "change_pct": stock["change_pct"] if stock else 0,
                        "price": stock["price"] if stock else 0,
                        "rvol": stock["rvol"] if stock else 0,
                    }
                )

        for ticker in current_tickers:
            if ticker not in previous_set:
                continue
            old_rank = self._previous_ranks[ticker]
            new_rank = current_ranks[ticker]
            if old_rank == new_rank:
                continue
            events.append(
                {
                    "type": "RANK",
                    "ticker": ticker,
                    "old_rank": old_rank,
                    "new_rank": new_rank,
                    "direction": "UP" if new_rank < old_rank else "DOWN",
                    "time": now_str,
                }
            )

        for ticker in self._previous_tickers:
            if ticker in current_set:
                continue
            events.append(
                {
                    "type": "DROP",
                    "ticker": ticker,
                    "time": now_str,
                }
            )

        return events
