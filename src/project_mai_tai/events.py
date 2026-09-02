from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
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
    cumulative_volume: int | None = None
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


class HistoricalBarPayload(BaseModel):
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    timestamp: float
    trade_count: int = 1


class HistoricalBarsPayload(BaseModel):
    symbol: str
    interval_secs: int
    bars: list[HistoricalBarPayload] = Field(default_factory=list)


class HistoricalBarsEvent(EventEnvelope):
    event_type: Literal["historical_bars"] = "historical_bars"
    payload: HistoricalBarsPayload


class LiveBarPayload(BaseModel):
    symbol: str
    interval_secs: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    timestamp: float
    trade_count: int = 1
    coverage_started_at: float | None = None


class LiveBarEvent(EventEnvelope):
    event_type: Literal["live_bar"] = "live_bar"
    payload: LiveBarPayload


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


class StrategyBotStatePayload(BaseModel):
    strategy_code: str
    account_name: str
    watchlist: list[str] = Field(default_factory=list)
    prewarm_symbols: list[str] = Field(default_factory=list)
    data_health: dict[str, Any] = Field(default_factory=dict)
    retention_states: list[dict[str, Any]] = Field(default_factory=list)
    positions: list[dict[str, Any]] = Field(default_factory=list)
    pending_open_symbols: list[str] = Field(default_factory=list)
    pending_close_symbols: list[str] = Field(default_factory=list)
    pending_scale_levels: list[str] = Field(default_factory=list)
    daily_pnl: float = 0.0
    closed_today: list[dict[str, Any]] = Field(default_factory=list)
    recent_decisions: list[dict[str, Any]] = Field(default_factory=list)
    indicator_snapshots: list[dict[str, Any]] = Field(default_factory=list)
    bar_counts: dict[str, int] = Field(default_factory=dict)
    last_tick_at: dict[str, str] = Field(default_factory=dict)
    # P1.4 armed-segment observability (default-empty => backward-compatible): every currently-armed
    # CW-v2 segment + entries_held gate. `dangerous` (reconstructed & uncapped) is the boot-hold/P1.3
    # signal the armed_segments_check cron pages on.
    cw_armed_segments: list[dict[str, Any]] = Field(default_factory=list)
    entries_held: bool = False
    restoration_complete: bool = False
    warmup_pending_symbols: list[str] = Field(default_factory=list)
    paper_exit: dict[str, Any] = Field(default_factory=dict)


class StrategyStateSnapshotPayload(BaseModel):
    all_confirmed: list[dict[str, Any]] = Field(default_factory=list)
    watchlist: list[str] = Field(default_factory=list)
    top_confirmed: list[dict[str, Any]] = Field(default_factory=list)
    five_pillars: list[dict[str, Any]] = Field(default_factory=list)
    top_gainers: list[dict[str, Any]] = Field(default_factory=list)
    recent_alerts: list[dict[str, Any]] = Field(default_factory=list)
    top_gainer_changes: list[dict[str, Any]] = Field(default_factory=list)
    alert_warmup: dict[str, Any] = Field(default_factory=dict)
    cycle_count: int = 0
    schwab_prewarm_symbols: list[str] = Field(default_factory=list)
    bots: list[StrategyBotStatePayload] = Field(default_factory=list)


class StrategyStateSnapshotEvent(EventEnvelope):
    """⛔⭐⭐ THIS PAYLOAD IS A FULL SNAPSHOT AND MUST STAY ONE. DO NOT CONVERT IT TO DELTAS.

    Redis runs `allkeys-lru` with only ~7 keys, so it evicts WHOLE STREAM KEYS, not entries
    (~700 evictions/day measured 2026-08-07), and EVERY consumer reads with an in-memory cursor —
    there is not one `xreadgroup` in the codebase. A dropped message is therefore silent: no error,
    no retry, no log line.

    ⭐ THE ONLY REASON THAT IS SURVIVABLE HERE is that this payload is a SNAPSHOT: each message
    supersedes the last, so `_apply_strategy_state_event` rebuilds the watchlist from scratch and a
    lost message self-heals on the next publish. That is why the eviction blast radius does NOT
    reach the entry path.

    ⛔ CONVERTING THIS TO DELTAS WOULD LOOK LIKE AN OPTIMISATION AND WOULD SILENTLY MOVE IT FROM THE
    SAFE CATEGORY INTO THE EXPOSED ONE — every dropped delta would corrupt the watchlist
    permanently, with nothing to catch it. If you need deltas, a DURABLE CONSUMER GROUP
    (`xreadgroup`) must land first.

    ⇒ The rule is snapshot-vs-event, not importance: importance is a judgement that drifts, the
    payload's shape can be CHECKED. Event-carrying streams (`strategy-intents` — trade intents and
    `v2_cw_flip`; `order-events`) are the exposed ones; snapshot streams are safe by construction.
    """

    event_type: Literal["strategy_state_snapshot"] = "strategy_state_snapshot"
    payload: StrategyStateSnapshotPayload


class IsolatedBotStateEvent(EventEnvelope):
    """Per-bot state published by an isolated-service bot (e.g. schwab_1m_v2)
    that does not participate in the strategy-engine's aggregated snapshot.

    Published to its own Redis stream so isolated bots do not overwrite the
    main strategy-state snapshot. Control-plane reads both streams and merges.
    """

    event_type: Literal["isolated_bot_state"] = "isolated_bot_state"
    payload: StrategyBotStatePayload


class ManualStopUpdatePayload(BaseModel):
    scope: Literal["bot", "global"]
    action: Literal["stop", "resume"]
    strategy_code: str | None = None
    symbol: str


class ManualStopUpdateEvent(EventEnvelope):
    event_type: Literal["manual_stop_update"] = "manual_stop_update"
    payload: ManualStopUpdatePayload


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


class OrderEventPayload(BaseModel):
    intent_event_id: UUID | None = None
    intent_db_id: UUID | None = None
    order_db_id: UUID | None = None
    strategy_code: str
    broker_account_name: str
    client_order_id: str
    broker_order_id: str | None = None
    broker_fill_id: str | None = None
    symbol: str
    side: Literal["buy", "sell"]
    intent_type: Literal["open", "scale", "close", "cancel"]
    status: Literal["accepted", "rejected", "filled", "partially_filled", "cancelled"]
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    fill_price: Decimal | None = None
    reason: str
    metadata: dict[str, str] = Field(default_factory=dict)


class OrderEventEvent(EventEnvelope):
    event_type: Literal["order_event"] = "order_event"
    payload: OrderEventPayload


class HeartbeatPayload(BaseModel):
    service_name: str
    instance_name: str
    status: Literal["starting", "healthy", "degraded", "stopping"]
    details: dict[str, str] = Field(default_factory=dict)


class HeartbeatEvent(EventEnvelope):
    event_type: Literal["service_heartbeat"] = "service_heartbeat"
    payload: HeartbeatPayload
