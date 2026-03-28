"""initial runtime schema

Revision ID: 20260328_0001
Revises:
Create Date: 2026-03-28 12:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260328_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broker_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broker_accounts")),
        sa.UniqueConstraint("external_account_id", name=op.f("uq_broker_accounts_external_account_id")),
        sa.UniqueConstraint("name", name=op.f("uq_broker_accounts_name")),
    )
    op.create_index(op.f("ix_broker_accounts_environment"), "broker_accounts", ["environment"], unique=False)
    op.create_index(op.f("ix_broker_accounts_name"), "broker_accounts", ["name"], unique=False)
    op.create_index(op.f("ix_broker_accounts_provider"), "broker_accounts", ["provider"], unique=False)

    op.create_table(
        "strategies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategies")),
        sa.UniqueConstraint("code", name=op.f("uq_strategies_code")),
    )
    op.create_index(op.f("ix_strategies_code"), "strategies", ["code"], unique=False)

    op.create_table(
        "strategy_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("service_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], name=op.f("fk_strategy_runs_strategy_id_strategies")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_runs")),
    )
    op.create_index(op.f("ix_strategy_runs_service_name"), "strategy_runs", ["service_name"], unique=False)
    op.create_index(op.f("ix_strategy_runs_status"), "strategy_runs", ["status"], unique=False)
    op.create_index(op.f("ix_strategy_runs_strategy_id"), "strategy_runs", ["strategy_id"], unique=False)

    op.create_table(
        "market_data_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_name", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_data_subscriptions")),
        sa.UniqueConstraint(
            "service_name",
            "symbol",
            "channel",
            name="uq_market_data_subscriptions_service_symbol_channel",
        ),
    )
    op.create_index(op.f("ix_market_data_subscriptions_channel"), "market_data_subscriptions", ["channel"], unique=False)
    op.create_index(op.f("ix_market_data_subscriptions_service_name"), "market_data_subscriptions", ["service_name"], unique=False)
    op.create_index(op.f("ix_market_data_subscriptions_status"), "market_data_subscriptions", ["status"], unique=False)
    op.create_index(op.f("ix_market_data_subscriptions_symbol"), "market_data_subscriptions", ["symbol"], unique=False)

    op.create_table(
        "trade_intents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("intent_type", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"], name=op.f("fk_trade_intents_broker_account_id_broker_accounts")),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], name=op.f("fk_trade_intents_strategy_id_strategies")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade_intents")),
    )
    op.create_index(op.f("ix_trade_intents_broker_account_id"), "trade_intents", ["broker_account_id"], unique=False)
    op.create_index(op.f("ix_trade_intents_intent_type"), "trade_intents", ["intent_type"], unique=False)
    op.create_index(op.f("ix_trade_intents_status"), "trade_intents", ["status"], unique=False)
    op.create_index(op.f("ix_trade_intents_strategy_id"), "trade_intents", ["strategy_id"], unique=False)
    op.create_index(op.f("ix_trade_intents_symbol"), "trade_intents", ["symbol"], unique=False)

    op.create_table(
        "broker_orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=True),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), nullable=False),
        sa.Column("client_order_id", sa.String(length=128), nullable=False),
        sa.Column("broker_order_id", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("order_type", sa.String(length=32), nullable=False),
        sa.Column("time_in_force", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"], name=op.f("fk_broker_orders_broker_account_id_broker_accounts")),
        sa.ForeignKeyConstraint(["intent_id"], ["trade_intents.id"], name=op.f("fk_broker_orders_intent_id_trade_intents")),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], name=op.f("fk_broker_orders_strategy_id_strategies")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broker_orders")),
        sa.UniqueConstraint("broker_order_id", name=op.f("uq_broker_orders_broker_order_id")),
        sa.UniqueConstraint("client_order_id", name=op.f("uq_broker_orders_client_order_id")),
    )
    op.create_index(op.f("ix_broker_orders_broker_account_id"), "broker_orders", ["broker_account_id"], unique=False)
    op.create_index(op.f("ix_broker_orders_intent_id"), "broker_orders", ["intent_id"], unique=False)
    op.create_index(op.f("ix_broker_orders_status"), "broker_orders", ["status"], unique=False)
    op.create_index(op.f("ix_broker_orders_strategy_id"), "broker_orders", ["strategy_id"], unique=False)
    op.create_index(op.f("ix_broker_orders_symbol"), "broker_orders", ["symbol"], unique=False)

    op.create_table(
        "broker_order_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["broker_orders.id"], name=op.f("fk_broker_order_events_order_id_broker_orders")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broker_order_events")),
    )
    op.create_index(op.f("ix_broker_order_events_event_type"), "broker_order_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_broker_order_events_order_id"), "broker_order_events", ["order_id"], unique=False)

    op.create_table(
        "fills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), nullable=False),
        sa.Column("broker_fill_id", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"], name=op.f("fk_fills_broker_account_id_broker_accounts")),
        sa.ForeignKeyConstraint(["order_id"], ["broker_orders.id"], name=op.f("fk_fills_order_id_broker_orders")),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], name=op.f("fk_fills_strategy_id_strategies")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fills")),
        sa.UniqueConstraint("broker_fill_id", name=op.f("uq_fills_broker_fill_id")),
    )
    op.create_index(op.f("ix_fills_broker_account_id"), "fills", ["broker_account_id"], unique=False)
    op.create_index(op.f("ix_fills_order_id"), "fills", ["order_id"], unique=False)
    op.create_index(op.f("ix_fills_strategy_id"), "fills", ["strategy_id"], unique=False)
    op.create_index(op.f("ix_fills_symbol"), "fills", ["symbol"], unique=False)

    op.create_table(
        "virtual_positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("average_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"], name=op.f("fk_virtual_positions_broker_account_id_broker_accounts")),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], name=op.f("fk_virtual_positions_strategy_id_strategies")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_virtual_positions")),
        sa.UniqueConstraint(
            "strategy_id",
            "broker_account_id",
            "symbol",
            name="uq_virtual_positions_strategy_account_symbol",
        ),
    )
    op.create_index(op.f("ix_virtual_positions_broker_account_id"), "virtual_positions", ["broker_account_id"], unique=False)
    op.create_index(op.f("ix_virtual_positions_strategy_id"), "virtual_positions", ["strategy_id"], unique=False)
    op.create_index(op.f("ix_virtual_positions_symbol"), "virtual_positions", ["symbol"], unique=False)

    op.create_table(
        "account_positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("average_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("market_value", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"], name=op.f("fk_account_positions_broker_account_id_broker_accounts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_positions")),
        sa.UniqueConstraint("broker_account_id", "symbol", name="uq_account_positions_account_symbol"),
    )
    op.create_index(op.f("ix_account_positions_broker_account_id"), "account_positions", ["broker_account_id"], unique=False)
    op.create_index(op.f("ix_account_positions_symbol"), "account_positions", ["symbol"], unique=False)

    op.create_table(
        "risk_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=True),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"], name=op.f("fk_risk_checks_broker_account_id_broker_accounts")),
        sa.ForeignKeyConstraint(["intent_id"], ["trade_intents.id"], name=op.f("fk_risk_checks_intent_id_trade_intents")),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], name=op.f("fk_risk_checks_strategy_id_strategies")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_checks")),
    )
    op.create_index(op.f("ix_risk_checks_broker_account_id"), "risk_checks", ["broker_account_id"], unique=False)
    op.create_index(op.f("ix_risk_checks_intent_id"), "risk_checks", ["intent_id"], unique=False)
    op.create_index(op.f("ix_risk_checks_outcome"), "risk_checks", ["outcome"], unique=False)
    op.create_index(op.f("ix_risk_checks_strategy_id"), "risk_checks", ["strategy_id"], unique=False)

    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"], name=op.f("fk_reconciliation_runs_broker_account_id_broker_accounts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_runs")),
    )
    op.create_index(op.f("ix_reconciliation_runs_broker_account_id"), "reconciliation_runs", ["broker_account_id"], unique=False)
    op.create_index(op.f("ix_reconciliation_runs_status"), "reconciliation_runs", ["status"], unique=False)

    op.create_table(
        "reconciliation_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reconciliation_run_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("finding_type", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["broker_orders.id"], name=op.f("fk_reconciliation_findings_order_id_broker_orders")),
        sa.ForeignKeyConstraint(["reconciliation_run_id"], ["reconciliation_runs.id"], name=op.f("fk_reconciliation_findings_reconciliation_run_id_reconciliation_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_findings")),
    )
    op.create_index(op.f("ix_reconciliation_findings_finding_type"), "reconciliation_findings", ["finding_type"], unique=False)
    op.create_index(op.f("ix_reconciliation_findings_order_id"), "reconciliation_findings", ["order_id"], unique=False)
    op.create_index(op.f("ix_reconciliation_findings_reconciliation_run_id"), "reconciliation_findings", ["reconciliation_run_id"], unique=False)
    op.create_index(op.f("ix_reconciliation_findings_severity"), "reconciliation_findings", ["severity"], unique=False)
    op.create_index(op.f("ix_reconciliation_findings_symbol"), "reconciliation_findings", ["symbol"], unique=False)

    op.create_table(
        "service_heartbeats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_name", sa.String(length=64), nullable=False),
        sa.Column("instance_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_heartbeats")),
        sa.UniqueConstraint("service_name", "instance_name", name="uq_service_heartbeats_service_instance"),
    )
    op.create_index(op.f("ix_service_heartbeats_instance_name"), "service_heartbeats", ["instance_name"], unique=False)
    op.create_index(op.f("ix_service_heartbeats_service_name"), "service_heartbeats", ["service_name"], unique=False)
    op.create_index(op.f("ix_service_heartbeats_status"), "service_heartbeats", ["status"], unique=False)

    op.create_table(
        "system_incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_name", sa.String(length=64), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_incidents")),
    )
    op.create_index(op.f("ix_system_incidents_service_name"), "system_incidents", ["service_name"], unique=False)
    op.create_index(op.f("ix_system_incidents_severity"), "system_incidents", ["severity"], unique=False)
    op.create_index(op.f("ix_system_incidents_status"), "system_incidents", ["status"], unique=False)

    op.create_table(
        "dashboard_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard_snapshots")),
    )
    op.create_index(op.f("ix_dashboard_snapshots_snapshot_type"), "dashboard_snapshots", ["snapshot_type"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_dashboard_snapshots_snapshot_type"), table_name="dashboard_snapshots")
    op.drop_table("dashboard_snapshots")

    op.drop_index(op.f("ix_system_incidents_status"), table_name="system_incidents")
    op.drop_index(op.f("ix_system_incidents_severity"), table_name="system_incidents")
    op.drop_index(op.f("ix_system_incidents_service_name"), table_name="system_incidents")
    op.drop_table("system_incidents")

    op.drop_index(op.f("ix_service_heartbeats_status"), table_name="service_heartbeats")
    op.drop_index(op.f("ix_service_heartbeats_service_name"), table_name="service_heartbeats")
    op.drop_index(op.f("ix_service_heartbeats_instance_name"), table_name="service_heartbeats")
    op.drop_table("service_heartbeats")

    op.drop_index(op.f("ix_reconciliation_findings_symbol"), table_name="reconciliation_findings")
    op.drop_index(op.f("ix_reconciliation_findings_severity"), table_name="reconciliation_findings")
    op.drop_index(op.f("ix_reconciliation_findings_reconciliation_run_id"), table_name="reconciliation_findings")
    op.drop_index(op.f("ix_reconciliation_findings_order_id"), table_name="reconciliation_findings")
    op.drop_index(op.f("ix_reconciliation_findings_finding_type"), table_name="reconciliation_findings")
    op.drop_table("reconciliation_findings")

    op.drop_index(op.f("ix_reconciliation_runs_status"), table_name="reconciliation_runs")
    op.drop_index(op.f("ix_reconciliation_runs_broker_account_id"), table_name="reconciliation_runs")
    op.drop_table("reconciliation_runs")

    op.drop_index(op.f("ix_risk_checks_strategy_id"), table_name="risk_checks")
    op.drop_index(op.f("ix_risk_checks_outcome"), table_name="risk_checks")
    op.drop_index(op.f("ix_risk_checks_intent_id"), table_name="risk_checks")
    op.drop_index(op.f("ix_risk_checks_broker_account_id"), table_name="risk_checks")
    op.drop_table("risk_checks")

    op.drop_index(op.f("ix_account_positions_symbol"), table_name="account_positions")
    op.drop_index(op.f("ix_account_positions_broker_account_id"), table_name="account_positions")
    op.drop_table("account_positions")

    op.drop_index(op.f("ix_virtual_positions_symbol"), table_name="virtual_positions")
    op.drop_index(op.f("ix_virtual_positions_strategy_id"), table_name="virtual_positions")
    op.drop_index(op.f("ix_virtual_positions_broker_account_id"), table_name="virtual_positions")
    op.drop_table("virtual_positions")

    op.drop_index(op.f("ix_fills_symbol"), table_name="fills")
    op.drop_index(op.f("ix_fills_strategy_id"), table_name="fills")
    op.drop_index(op.f("ix_fills_order_id"), table_name="fills")
    op.drop_index(op.f("ix_fills_broker_account_id"), table_name="fills")
    op.drop_table("fills")

    op.drop_index(op.f("ix_broker_order_events_order_id"), table_name="broker_order_events")
    op.drop_index(op.f("ix_broker_order_events_event_type"), table_name="broker_order_events")
    op.drop_table("broker_order_events")

    op.drop_index(op.f("ix_broker_orders_symbol"), table_name="broker_orders")
    op.drop_index(op.f("ix_broker_orders_strategy_id"), table_name="broker_orders")
    op.drop_index(op.f("ix_broker_orders_status"), table_name="broker_orders")
    op.drop_index(op.f("ix_broker_orders_intent_id"), table_name="broker_orders")
    op.drop_index(op.f("ix_broker_orders_broker_account_id"), table_name="broker_orders")
    op.drop_table("broker_orders")

    op.drop_index(op.f("ix_trade_intents_symbol"), table_name="trade_intents")
    op.drop_index(op.f("ix_trade_intents_strategy_id"), table_name="trade_intents")
    op.drop_index(op.f("ix_trade_intents_status"), table_name="trade_intents")
    op.drop_index(op.f("ix_trade_intents_intent_type"), table_name="trade_intents")
    op.drop_index(op.f("ix_trade_intents_broker_account_id"), table_name="trade_intents")
    op.drop_table("trade_intents")

    op.drop_index(op.f("ix_market_data_subscriptions_symbol"), table_name="market_data_subscriptions")
    op.drop_index(op.f("ix_market_data_subscriptions_status"), table_name="market_data_subscriptions")
    op.drop_index(op.f("ix_market_data_subscriptions_service_name"), table_name="market_data_subscriptions")
    op.drop_index(op.f("ix_market_data_subscriptions_channel"), table_name="market_data_subscriptions")
    op.drop_table("market_data_subscriptions")

    op.drop_index(op.f("ix_strategy_runs_strategy_id"), table_name="strategy_runs")
    op.drop_index(op.f("ix_strategy_runs_status"), table_name="strategy_runs")
    op.drop_index(op.f("ix_strategy_runs_service_name"), table_name="strategy_runs")
    op.drop_table("strategy_runs")

    op.drop_index(op.f("ix_strategies_code"), table_name="strategies")
    op.drop_table("strategies")

    op.drop_index(op.f("ix_broker_accounts_provider"), table_name="broker_accounts")
    op.drop_index(op.f("ix_broker_accounts_name"), table_name="broker_accounts")
    op.drop_index(op.f("ix_broker_accounts_environment"), table_name="broker_accounts")
    op.drop_table("broker_accounts")
