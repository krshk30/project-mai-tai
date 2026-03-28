from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from statistics import mean

from project_mai_tai.events import ReferenceDataPayload
from project_mai_tai.market_data.protocols import SnapshotProvider
from project_mai_tai.strategy_core import ReferenceData

logger = logging.getLogger(__name__)


class ReferenceDataCache:
    def __init__(
        self,
        provider: SnapshotProvider,
        *,
        cache_path: str,
        max_age_hours: int = 24,
        min_price: float = 1.0,
        max_price: float = 10.0,
        lookback_days: int = 20,
    ):
        self.provider = provider
        self.cache_path = Path(cache_path)
        self.max_age_hours = max_age_hours
        self.min_price = min_price
        self.max_price = max_price
        self.lookback_days = lookback_days
        self._data: dict[str, ReferenceData] = {}
        self._updated_at: datetime | None = None

    def load_from_cache(self) -> bool:
        if not self.cache_path.exists():
            logger.info("No reference data cache found at %s", self.cache_path)
            return False

        try:
            raw = json.loads(self.cache_path.read_text())
            updated_at = datetime.fromisoformat(raw["updated_at"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            logger.exception("Failed to read reference data cache")
            return False

        age_hours = (datetime.now() - updated_at).total_seconds() / 3600
        if age_hours > self.max_age_hours:
            logger.info("Reference data cache is stale (%.1fh)", age_hours)
            return False

        self._data = {
            symbol: ReferenceData(
                shares_outstanding=int(entry.get("shares_outstanding", 0) or 0),
                avg_daily_volume=float(entry.get("avg_daily_volume", 0) or 0),
            )
            for symbol, entry in raw.get("tickers", {}).items()
        }
        self._updated_at = updated_at
        logger.info("Loaded reference data cache: %s tickers", len(self._data))
        return True

    def build(self) -> None:
        logger.info("Building reference data cache from provider")
        snapshots = self.provider.fetch_all_snapshots()
        if not snapshots:
            logger.warning("Snapshot provider returned no symbols for reference build")
            self._data = {}
            return

        tickers = self._get_price_filtered_tickers(snapshots)
        avg_volumes = self._compute_avg_volumes()
        shares_outstanding = self.provider.get_ticker_details_batch(
            [ticker for ticker in tickers if ticker in avg_volumes]
        )

        merged: dict[str, ReferenceData] = {}
        for ticker in tickers:
            avg_daily_volume = avg_volumes.get(ticker)
            shares = shares_outstanding.get(ticker)
            if avg_daily_volume is None or shares is None or avg_daily_volume <= 0:
                continue
            merged[ticker] = ReferenceData(
                shares_outstanding=int(shares),
                avg_daily_volume=float(avg_daily_volume),
            )

        self._data = merged
        self._updated_at = datetime.now()
        self._save()
        logger.info("Reference data cache built: %s tickers", len(self._data))

    def get(self, symbol: str) -> ReferenceData | None:
        return self._data.get(symbol)

    def get_many(self, symbols: Iterable[str]) -> dict[str, ReferenceData]:
        return {symbol: ref for symbol in symbols if (ref := self._data.get(symbol)) is not None}

    def as_payloads(self, symbols: Iterable[str]) -> list[ReferenceDataPayload]:
        return [
            ReferenceDataPayload(
                symbol=symbol,
                shares_outstanding=reference.shares_outstanding,
                avg_daily_volume=reference.avg_daily_volume,
            )
            for symbol, reference in self.get_many(symbols).items()
        ]

    def ticker_count(self) -> int:
        return len(self._data)

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": (self._updated_at or datetime.now()).isoformat(),
            "tickers": {
                symbol: {
                    "shares_outstanding": reference.shares_outstanding,
                    "avg_daily_volume": reference.avg_daily_volume,
                }
                for symbol, reference in self._data.items()
            },
        }
        self.cache_path.write_text(json.dumps(payload, indent=2))

    def _get_price_filtered_tickers(self, snapshots) -> list[str]:
        widened_min = self.min_price * 0.5
        widened_max = self.max_price * 1.5
        tickers: list[str] = []
        for snapshot in snapshots:
            price = (
                snapshot.last_trade_price
                or snapshot.minute_close
                or snapshot.day_close
                or snapshot.previous_close
            )
            if price is None:
                continue
            if widened_min <= price <= widened_max:
                tickers.append(snapshot.symbol)
        return tickers

    def _compute_avg_volumes(self) -> dict[str, float]:
        volume_by_ticker = self.provider.get_grouped_daily_multi(days=self.lookback_days)
        return {
            ticker: mean(volumes)
            for ticker, volumes in volume_by_ticker.items()
            if volumes
        }
