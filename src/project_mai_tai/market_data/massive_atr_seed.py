"""Massive 1-minute aggregates used only to seed v2's session ATR state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms


@dataclass(frozen=True)
class MassiveAtrSeedBar:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int

    def as_chart_bar(self, symbol: str) -> ChartBar:
        return ChartBar(
            symbol=symbol,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            timestamp_ms=self.timestamp_ms,
        )


def select_massive_atr_seed_bars(
    bars: Iterable[MassiveAtrSeedBar],
    *,
    first_schwab_bar_ts_ms: int,
) -> list[MassiveAtrSeedBar]:
    """Return the exact Massive side of the time splice.

    The first Schwab minute determines both the 04:00-ET session and the exclusive
    upper bound. Deduplication is timestamp-based, so a revised REST row cannot make
    the ATR walk the same minute twice. At a shared minute Schwab wins because the
    Massive side is strictly earlier than ``first_schwab_bar_ts_ms``.
    """

    first_schwab = int(first_schwab_bar_ts_ms)
    session_start = session_start_ts_ms(first_schwab)
    selected = {
        int(bar.timestamp_ms): bar
        for bar in bars
        if session_start <= int(bar.timestamp_ms) < first_schwab
    }
    return [selected[timestamp] for timestamp in sorted(selected)]


class MassiveAtrSeedClient:
    """Small synchronous REST client; the service owns the bounded async timeout."""

    def __init__(self, api_key: str, *, rest_client=None) -> None:
        self.api_key = str(api_key or "").strip()
        self._rest_client = rest_client

    def fetch(
        self,
        symbol: str,
        session_start_ms: int,
        first_schwab_bar_ts_ms: int,
    ) -> list[MassiveAtrSeedBar]:
        if not self.api_key:
            raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is missing")
        start = datetime.fromtimestamp(int(session_start_ms) / 1000.0, UTC)
        end = datetime.fromtimestamp(int(first_schwab_bar_ts_ms) / 1000.0, UTC)
        client = self._client()
        aggregates = client.list_aggs(
            str(symbol).upper(),
            1,
            "minute",
            from_=start.date().isoformat(),
            to=end.date().isoformat(),
            adjusted=True,
            sort="asc",
            limit=50_000,
        )
        bars: list[MassiveAtrSeedBar] = []
        for aggregate in aggregates:
            timestamp_ms = self._timestamp_ms(getattr(aggregate, "timestamp", None))
            values = tuple(
                self._float(getattr(aggregate, field, None))
                for field in ("open", "high", "low", "close")
            )
            if timestamp_ms is None or any(value is None for value in values):
                continue
            open_price, high, low, close = values
            bars.append(
                MassiveAtrSeedBar(
                    timestamp_ms=timestamp_ms,
                    open=float(open_price),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=self._int(getattr(aggregate, "volume", None)) or 0,
                )
            )
        return select_massive_atr_seed_bars(
            bars,
            first_schwab_bar_ts_ms=first_schwab_bar_ts_ms,
        )

    def _client(self):
        if self._rest_client is None:
            from massive import RESTClient

            self._rest_client = RESTClient(api_key=self.api_key)
        return self._rest_client

    @staticmethod
    def _timestamp_ms(value: object) -> int | None:
        try:
            timestamp = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if timestamp <= 0:
            return None
        if timestamp >= 1_000_000_000_000_000_000:
            return timestamp // 1_000_000
        if timestamp >= 1_000_000_000_000_000:
            return timestamp // 1_000
        if timestamp >= 1_000_000_000_000:
            return timestamp
        if timestamp >= 1_000_000_000:
            return timestamp * 1_000
        return None

    @staticmethod
    def _float(value: object) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _int(value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError, OverflowError):
            return None
