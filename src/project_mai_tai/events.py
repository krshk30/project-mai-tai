from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def stream_name(prefix: str, topic: str) -> str:
    return f"{prefix}:{topic}"


class EventEnvelope(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    source_service: str
    produced_at: datetime = Field(default_factory=utcnow)
    correlation_id: UUID | None = None


class TradeTickPayload(BaseModel):
    symbol: str
    price: Decimal
    size: int
    timestamp_ns: int | None = None
    exchange: str | None = None
    conditions: list[str] = Field(default_factory=list)


class TradeTickEvent(EventEnvelope):
    event_type: Literal["trade_tick"] = "trade_tick"
    payload: TradeTickPayload


class QuoteTickPayload(BaseModel):
    symbol: str
    bid_price: Decimal
    ask_price: Decimal
    bid_size: int | None = None
    ask_size: int | None = None


class QuoteTickEvent(EventEnvelope):
    event_type: Literal["quote_tick"] = "quote_tick"
    payload: QuoteTickPayload


class BarClosedPayload(BaseModel):
    symbol: str
    timeframe: Literal["30s", "1m", "5m", "1d"]
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    started_at: datetime
    ended_at: datetime


class BarClosedEvent(EventEnvelope):
    event_type: Literal["bar_closed"] = "bar_closed"
    payload: BarClosedPayload


class SnapshotRefreshPayload(BaseModel):
    symbols: list[str]
    snapshot_count: int
    completed_at: datetime = Field(default_factory=utcnow)


class SnapshotRefreshEvent(EventEnvelope):
    event_type: Literal["snapshot_refresh"] = "snapshot_refresh"
    payload: SnapshotRefreshPayload


class MarketSnapshotPayload(BaseModel):
    symbol: str
    previous_close: Decimal | None = None
    day_close: Decimal | None = None
    day_volume: int | None = None
    day_high: Decimal | None = None
    day_vwap: Decimal | None = None
    minute_close: Decimal | None = None
    minute_accumulated_volume: int | None = None
    minute_high: Decimal | None = None
    minute_vwap: Decimal | None = None
    last_trade_price: Decimal | None = None
    last_trade_timestamp_ns: int | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    todays_change_percent: Decimal | None = None
    updated_ns: int | None = None


class ReferenceDataPayload(BaseModel):
    symbol: str
    shares_outstanding: int = 0
    avg_daily_volume: Decimal = Decimal("0")


class SnapshotBatchPayload(BaseModel):
    snapshots: list[MarketSnapshotPayload]
    reference_data: list[ReferenceDataPayload] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=utcnow)


class SnapshotBatchEvent(EventEnvelope):
    event_type: Literal["snapshot_batch"] = "snapshot_batch"
    payload: SnapshotBatchPayload


class MarketDataSubscriptionPayload(BaseModel):
    consumer_name: str
    mode: Literal["replace", "add", "remove"] = "replace"
    symbols: list[str] = Field(default_factory=list)


class MarketDataSubscriptionEvent(EventEnvelope):
    event_type: Literal["market_data_subscription"] = "market_data_subscription"
    payload: MarketDataSubscriptionPayload


class TradeIntentPayload(BaseModel):
    strategy_code: str
    broker_account_name: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    intent_type: Literal["open", "scale", "close", "cancel"]
    reason: str
    metadata: dict[str, str] = Field(default_factory=dict)


class TradeIntentEvent(EventEnvelope):
    event_type: Literal["trade_intent"] = "trade_intent"
    payload: TradeIntentPayload


class HeartbeatPayload(BaseModel):
    service_name: str
    instance_name: str
    status: Literal["starting", "healthy", "degraded", "stopping"]
    details: dict[str, str] = Field(default_factory=dict)


class HeartbeatEvent(EventEnvelope):
    event_type: Literal["service_heartbeat"] = "service_heartbeat"
    payload: HeartbeatPayload
