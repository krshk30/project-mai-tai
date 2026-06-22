from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import JSON, Uuid

from project_mai_tai.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    environment: Mapped[str] = mapped_column(String(32), index=True)
    external_account_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    execution_mode: Mapped[str] = mapped_column(String(32), default="shadow")
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    metadata_json: Mapped[dict[str, object]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(ForeignKey("strategies.id"), index=True)
    service_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class MarketDataSubscription(Base):
    __tablename__ = "market_data_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "service_name",
            "symbol",
            "channel",
            name="uq_market_data_subscriptions_service_symbol_channel",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    service_name: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class TradeIntent(Base):
    __tablename__ = "trade_intents"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(ForeignKey("strategies.id"), index=True)
    broker_account_id: Mapped[UUID] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    intent_type: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class BrokerOrder(Base):
    __tablename__ = "broker_orders"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    intent_id: Mapped[UUID | None] = mapped_column(ForeignKey("trade_intents.id"), index=True)
    strategy_id: Mapped[UUID] = mapped_column(ForeignKey("strategies.id"), index=True)
    broker_account_id: Mapped[UUID] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    client_order_id: Mapped[str] = mapped_column(String(128), unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(32))
    time_in_force: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    status: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class BrokerOrderEvent(Base):
    __tablename__ = "broker_order_events"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("broker_orders.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class SchwabIneligibleToday(Base):
    __tablename__ = "schwab_ineligible_today"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "session_date",
            "broker_account_id",
            name="uq_schwab_ineligible_today_symbol_session_account",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    broker_account_id: Mapped[UUID] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    reason_text: Mapped[str] = mapped_column(Text, default="")
    hit_count: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("broker_orders.id"), index=True)
    strategy_id: Mapped[UUID] = mapped_column(ForeignKey("strategies.id"), index=True)
    broker_account_id: Mapped[UUID] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    broker_fill_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class AiTradeReview(Base):
    __tablename__ = "ai_trade_reviews"
    __table_args__ = (
        UniqueConstraint(
            "review_type",
            "cycle_key",
            name="uq_ai_trade_reviews_review_type_cycle_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    intent_id: Mapped[UUID | None] = mapped_column(ForeignKey("trade_intents.id"), index=True)
    strategy_code: Mapped[str] = mapped_column(String(64), index=True)
    broker_account_name: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    review_type: Mapped[str] = mapped_column(String(32), index=True)
    cycle_key: Mapped[str] = mapped_column(String(255), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    verdict: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )


class VirtualPosition(Base):
    __tablename__ = "virtual_positions"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "broker_account_id",
            "symbol",
            name="uq_virtual_positions_strategy_account_symbol",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(ForeignKey("strategies.id"), index=True)
    broker_account_id: Mapped[UUID] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    average_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class AccountPosition(Base):
    __tablename__ = "account_positions"
    __table_args__ = (
        UniqueConstraint(
            "broker_account_id",
            "symbol",
            name="uq_account_positions_account_symbol",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    broker_account_id: Mapped[UUID] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    average_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class RiskCheck(Base):
    __tablename__ = "risk_checks"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    intent_id: Mapped[UUID | None] = mapped_column(ForeignKey("trade_intents.id"), index=True)
    strategy_id: Mapped[UUID] = mapped_column(ForeignKey("strategies.id"), index=True)
    broker_account_id: Mapped[UUID] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    broker_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("broker_accounts.id"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class ReconciliationFinding(Base):
    __tablename__ = "reconciliation_findings"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    reconciliation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("reconciliation_runs.id"),
        index=True,
    )
    order_id: Mapped[UUID | None] = mapped_column(ForeignKey("broker_orders.id"), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    finding_type: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"
    __table_args__ = (
        UniqueConstraint(
            "service_name",
            "instance_name",
            name="uq_service_heartbeats_service_instance",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    service_name: Mapped[str] = mapped_column(String(64), index=True)
    instance_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class SystemIncident(Base):
    __tablename__ = "system_incidents"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    service_name: Mapped[str | None] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DashboardSnapshot(Base):
    __tablename__ = "dashboard_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    snapshot_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )


class StrategyBarHistory(Base):
    __tablename__ = "strategy_bar_history"
    __table_args__ = (
        UniqueConstraint(
            "strategy_code",
            "symbol",
            "interval_secs",
            "bar_time",
            name="uq_strategy_bar_history_strategy_symbol_interval_time",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    strategy_code: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    interval_secs: Mapped[int] = mapped_column(Integer, index=True)
    bar_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    high_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    low_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    close_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    volume: Mapped[int] = mapped_column(Integer)
    trade_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    position_state: Mapped[str] = mapped_column(String(32), default="flat")
    position_quantity: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    decision_status: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"), index=True)
    decision_reason: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    decision_path: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    decision_score: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    decision_score_details: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    indicators_json: Mapped[dict[str, object]] = mapped_column("indicators", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class MarketTradeTick(Base):
    """Append-only Schwab trade ticks (LEVELONE_EQUITIES) for exit replay.

    Capture-only; no strategy/OMS dependency reads this in the live path. Dedup
    on (provider, service, symbol, event_ts, raw_hash) — distinct field-update
    payloads at the same event_ts are kept (different raw_hash); exact re-sends
    collapse. Indexed on (symbol, event_ts) for the replay walk.
    """

    __tablename__ = "market_trade_ticks"
    __table_args__ = (
        UniqueConstraint(
            "provider", "service", "symbol", "event_ts", "raw_hash",
            name="uq_market_trade_ticks_dedupe",
        ),
        Index("ix_market_trade_ticks_symbol_event_ts", "symbol", "event_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text)
    service: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cumulative_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw: Mapped[dict[str, object]] = mapped_column(JSONB().with_variant(JSON(), "sqlite"))
    raw_hash: Mapped[str] = mapped_column(Text)


class MarketQuoteTick(Base):
    """Append-only Schwab quote ticks (LEVELONE_EQUITIES). See MarketTradeTick."""

    __tablename__ = "market_quote_ticks"
    __table_args__ = (
        UniqueConstraint(
            "provider", "service", "symbol", "event_ts", "raw_hash",
            name="uq_market_quote_ticks_dedupe",
        ),
        Index("ix_market_quote_ticks_symbol_event_ts", "symbol", "event_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text)
    service: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    bid_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ask_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    bid_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ask_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cumulative_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw: Mapped[dict[str, object]] = mapped_column(JSONB().with_variant(JSON(), "sqlite"))
    raw_hash: Mapped[str] = mapped_column(Text)


class MarketCaptureTrade(Base):
    """GLOBAL, bot-agnostic capture of raw Polygon/Massive TRADE prints from the
    market-data gateway stream. Written by the central ``market_capture_app``
    consumer (NOT any bot). Append-only, no ``raw`` blob (parsed columns only —
    keeps the high-volume table ~half the size), no unique-dedupe constraint
    (the live feed has no trade id/sequence; restart-replay dupes are rare and
    de-duped at backtest time) so inserts stay cheap at 50-150+/sec. ``event_ts``
    is normalized to true ns before storage (see market_data.tick_time)."""

    __tablename__ = "market_capture_trades"
    __table_args__ = (
        Index("ix_market_capture_trades_symbol_event_ts", "symbol", "event_ts"),
        Index("ix_market_capture_trades_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exchange: Mapped[str | None] = mapped_column(Text, nullable=True)
    conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    cumulative_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class MarketCaptureQuote(Base):
    """GLOBAL, bot-agnostic capture of raw Polygon/Massive L1 QUOTE ticks (bid/ask)
    from the market-data gateway stream. See MarketCaptureTrade. Quotes carry no
    payload timestamp, so ``event_ts`` is stamped from the event ``produced_at``."""

    __tablename__ = "market_capture_quotes"
    __table_args__ = (
        Index("ix_market_capture_quotes_symbol_event_ts", "symbol", "event_ts"),
        Index("ix_market_capture_quotes_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    bid_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ask_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    bid_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ask_size: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ScannerBlacklistEntry(Base):
    __tablename__ = "scanner_blacklist_entries"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="operator")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class OmsManagedPosition(Base):
    """OMS-owned ladder state for schwab_1m_v2 positions (Track-2 Phase-2).

    The OMS is the SOLE writer of these rows (single-writer discipline — the
    reason for a separate table, not extending virtual_positions). One open row
    per (broker_account_name, symbol). Mirrors `exit_logic.Position` so a Position
    can be hydrated/persisted each evaluation. Uses TEXT natural keys (no FKs) and
    JSON (not JSONB) for SQLite-test renderability. Inert when
    `oms_v2_exit_management_enabled` is OFF (no rows written).
    """

    __tablename__ = "oms_managed_positions"
    __table_args__ = (
        Index(
            "uq_oms_managed_positions_open_symbol",
            "broker_account_name",
            "symbol",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    strategy_code: Mapped[str] = mapped_column(String(64), index=True)
    broker_account_name: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    original_quantity: Mapped[int] = mapped_column(Integer)
    current_quantity: Mapped[int] = mapped_column(Integer)
    entry_path: Mapped[str] = mapped_column(String(32), default="")
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    peak_profit_pct: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    current_profit_pct: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    tier: Mapped[int] = mapped_column(Integer, default=1)
    floor_pct: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    floor_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    scales_done: Mapped[list] = mapped_column(JSON, default=list)
    scale_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    config_name: Mapped[str] = mapped_column(String(32), default="make_v2_variant")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )
