from __future__ import annotations

from dataclasses import dataclass

from project_mai_tai.events import (
    HistoricalBarPayload,
    HistoricalBarsPayload,
    LiveBarPayload,
    MarketSnapshotPayload,
    QuoteTickPayload,
    TradeTickPayload,
)


@dataclass(frozen=True)
class SnapshotRecord:
    symbol: str
    previous_close: float | None = None
    day_close: float | None = None
    day_volume: int | None = None
    day_high: float | None = None
    day_vwap: float | None = None
    minute_close: float | None = None
    minute_accumulated_volume: int | None = None
    minute_high: float | None = None
    minute_vwap: float | None = None
    last_trade_price: float | None = None
    last_trade_timestamp_ns: int | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    todays_change_percent: float | None = None
    updated_ns: int | None = None

    def to_payload(self) -> MarketSnapshotPayload:
        return MarketSnapshotPayload(
            symbol=self.symbol,
            previous_close=self.previous_close,
            day_close=self.day_close,
            day_volume=self.day_volume,
            day_high=self.day_high,
            day_vwap=self.day_vwap,
            minute_close=self.minute_close,
            minute_accumulated_volume=self.minute_accumulated_volume,
            minute_high=self.minute_high,
            minute_vwap=self.minute_vwap,
            last_trade_price=self.last_trade_price,
            last_trade_timestamp_ns=self.last_trade_timestamp_ns,
            bid_price=self.bid_price,
            ask_price=self.ask_price,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
            todays_change_percent=self.todays_change_percent,
            updated_ns=self.updated_ns,
        )


@dataclass(frozen=True)
class TradeTickRecord:
    symbol: str
    price: float
    size: int
    timestamp_ns: int | None = None
    cumulative_volume: int | None = None
    exchange: str | None = None
    conditions: tuple[str, ...] = ()

    def to_payload(self) -> TradeTickPayload:
        return TradeTickPayload(
            symbol=self.symbol,
            price=self.price,
            size=self.size,
            timestamp_ns=self.timestamp_ns,
            cumulative_volume=self.cumulative_volume,
            exchange=self.exchange,
            conditions=list(self.conditions),
        )


@dataclass(frozen=True)
class QuoteTickRecord:
    symbol: str
    bid_price: float
    ask_price: float
    bid_size: int | None = None
    ask_size: int | None = None

    def to_payload(self) -> QuoteTickPayload:
        return QuoteTickPayload(
            symbol=self.symbol,
            bid_price=self.bid_price,
            ask_price=self.ask_price,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
        )


@dataclass(frozen=True)
class HistoricalBarRecord:
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: float
    trade_count: int = 1

    def to_payload(self) -> HistoricalBarPayload:
        return HistoricalBarPayload(
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            timestamp=self.timestamp,
            trade_count=self.trade_count,
        )


@dataclass(frozen=True)
class HistoricalBarsRecord:
    symbol: str
    interval_secs: int
    bars: tuple[HistoricalBarRecord, ...]

    def to_payload(self) -> HistoricalBarsPayload:
        return HistoricalBarsPayload(
            symbol=self.symbol,
            interval_secs=self.interval_secs,
            bars=[bar.to_payload() for bar in self.bars],
        )


@dataclass(frozen=True)
class LiveBarRecord:
    symbol: str
    interval_secs: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: float
    trade_count: int = 1

    def to_payload(self) -> LiveBarPayload:
        return LiveBarPayload(
            symbol=self.symbol,
            interval_secs=self.interval_secs,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            timestamp=self.timestamp,
            trade_count=self.trade_count,
        )
