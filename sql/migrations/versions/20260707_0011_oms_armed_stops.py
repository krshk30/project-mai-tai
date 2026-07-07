"""add oms_armed_stops table (F2: durable mirror of _armed_hard_stops for restart-while-holding)

Revision ID: 20260707_0011
Revises: 20260622_0010
Create Date: 2026-07-07 12:00:00

Additive + INERT unless the F2 rehydrate/mirror is enabled. Durable mirror of the OMS
in-memory `_armed_hard_stops` registry so an ORB position stays protected across an OMS
restart (the in-memory trail was never rebuilt on boot -> naked position). OMS-owned by
construction (only the OMS's own stop-guard fills write a row). TEXT natural keys (no FK),
matching oms_managed_positions; full-fidelity ratcheted stop_price/high_water_mark.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260707_0011"
down_revision = "20260622_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oms_armed_stops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_code", sa.String(length=64), nullable=False),
        sa.Column("broker_account_name", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("stop_loss_pct", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("quote_max_age_ms", sa.Integer(), nullable=False),
        sa.Column("initial_panic_buffer_pct", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("trail_pct", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False),
        sa.Column("high_water_mark", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("close_in_flight", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("armed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oms_armed_stops")),
        sa.UniqueConstraint(
            "broker_account_name", "strategy_code", "symbol", name="uq_oms_armed_stops_key"
        ),
    )
    op.create_index(op.f("ix_oms_armed_stops_strategy_code"), "oms_armed_stops", ["strategy_code"])
    op.create_index(
        op.f("ix_oms_armed_stops_broker_account_name"), "oms_armed_stops", ["broker_account_name"]
    )
    op.create_index(op.f("ix_oms_armed_stops_symbol"), "oms_armed_stops", ["symbol"])


def downgrade() -> None:
    op.drop_index(op.f("ix_oms_armed_stops_symbol"), table_name="oms_armed_stops")
    op.drop_index(op.f("ix_oms_armed_stops_broker_account_name"), table_name="oms_armed_stops")
    op.drop_index(op.f("ix_oms_armed_stops_strategy_code"), table_name="oms_armed_stops")
    op.drop_table("oms_armed_stops")
