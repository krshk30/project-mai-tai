from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from project_mai_tai.market_data.models import (
    HistoricalBarRecord,
    LiveBarRecord,
    QuoteTickRecord,
    SnapshotRecord,
    TradeTickRecord,
)


class SnapshotProvider(Protocol):
    def fetch_all_snapshots(self) -> list[SnapshotRecord]:
        """Fetch the latest full-market snapshot batch."""

    def get_grouped_daily_multi(self, days: int = 20) -> dict[str, list[float]]:
        """Fetch daily grouped aggregates for average-volume computation."""

    def get_ticker_details_batch(
        self,
        tickers: list[str],
        batch_size: int = 10,
        delay_between_batches: float = 0.2,
    ) -> dict[str, int]:
        """Fetch shares outstanding for a set of tickers."""

    def fetch_historical_bars(
        self,
        symbol: str,
        *,
        interval_secs: int,
        lookback_calendar_days: int,
        limit: int,
    ) -> list[HistoricalBarRecord]:
        """Fetch historical OHLCV bars for a symbol and interval."""


class TradeStreamProvider(Protocol):
    async def start(
        self,
        on_trade: Callable[[TradeTickRecord], None],
        on_quote: Callable[[QuoteTickRecord], None] | None = None,
        on_agg: Callable[[LiveBarRecord], None] | None = None,
    ) -> None:
        """Start the underlying real-time stream."""

    async def stop(self) -> None:
        """Stop the underlying real-time stream."""

    async def sync_subscriptions(self, symbols: Iterable[str]) -> None:
        """Replace the current live subscription set."""
