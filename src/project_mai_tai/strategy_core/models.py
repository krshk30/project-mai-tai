from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OHLCVBar:
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: float
    trade_count: int = 1

    @classmethod
    def from_trade(cls, price: float, volume: int, timestamp: float) -> "OHLCVBar":
        return cls(
            open=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            timestamp=timestamp,
            trade_count=1,
        )

    @classmethod
    def flat_fill(cls, price: float, timestamp: float) -> "OHLCVBar":
        return cls(
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0,
            timestamp=timestamp,
            trade_count=0,
        )

    def update(self, price: float, volume: int) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close = price
        self.volume += volume
        self.trade_count += 1

    def as_dict(self) -> dict[str, float | int]:
        return {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "timestamp": self.timestamp,
        }


@dataclass
class DaySnapshot:
    close: float | None = None
    volume: int | None = None
    high: float | None = None
    vwap: float | None = None


@dataclass
class MinuteSnapshot:
    close: float | None = None
    accumulated_volume: int | None = None
    high: float | None = None
    vwap: float | None = None


@dataclass
class LastTrade:
    price: float | None = None
    timestamp_ns: int | None = None


@dataclass
class QuoteSnapshot:
    bid_price: float | None = None
    ask_price: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None


@dataclass
class MarketSnapshot:
    ticker: str
    previous_close: float | None = None
    day: DaySnapshot | None = None
    minute: MinuteSnapshot | None = None
    last_trade: LastTrade | None = None
    last_quote: QuoteSnapshot | None = None
    todays_change_percent: float | None = None
    updated_ns: int | None = None


@dataclass
class ReferenceData:
    shares_outstanding: int = 0
    avg_daily_volume: float = 0.0
