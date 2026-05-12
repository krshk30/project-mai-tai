from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
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
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
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
