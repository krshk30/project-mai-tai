"""add oms_managed_positions table (Track-2 Phase-2: OMS-side v2 exit ladder state)

Revision ID: 20260614_0008
Revises: 20260611_0007
Create Date: 2026-06-14 20:00:00

Additive + INERT: no rows are written unless `oms_v2_exit_management_enabled` is on
(Phase-2 slice 1). OMS-owned ladder state for schwab_1m_v2 positions; the OMS is the
sole writer. TEXT natural keys (no FKs), JSON `scales_done` (SQLite-renderable).
"""

from alembic import op
import sqlalchemy as sa


revision = "20260614_0008"
down_revision = "20260611_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oms_managed_positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_code", sa.String(length=64), nullable=False),
        sa.Column("broker_account_name", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("original_quantity", sa.Integer(), nullable=False),
        sa.Column("current_quantity", sa.Integer(), nullable=False),
        sa.Column("entry_path", sa.String(length=32), server_default="", nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("peak_profit_pct", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False),
        sa.Column("current_profit_pct", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False),
        sa.Column("tier", sa.Integer(), server_default="1", nullable=False),
        sa.Column("floor_pct", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("floor_price", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("scales_done", sa.JSON(), nullable=True),
        sa.Column("scale_pnl", sa.Numeric(precision=18, scale=8), server_default="0", nullable=False),
        sa.Column("config_name", sa.String(length=32), server_default="make_v2_variant", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oms_managed_positions")),
    )
    op.create_index(
        op.f("ix_oms_managed_positions_strategy_code"), "oms_managed_positions", ["strategy_code"]
    )
    op.create_index(
        op.f("ix_oms_managed_positions_broker_account_name"), "oms_managed_positions", ["broker_account_name"]
    )
    op.create_index(op.f("ix_oms_managed_positions_symbol"), "oms_managed_positions", ["symbol"])
    op.create_index("ix_oms_managed_positions_status", "oms_managed_positions", ["status"])
    # One OPEN managed position per (account, symbol) — partial unique index.
    op.create_index(
        "uq_oms_managed_positions_open_symbol",
        "oms_managed_positions",
        ["broker_account_name", "symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_oms_managed_positions_open_symbol", table_name="oms_managed_positions")
    op.drop_index("ix_oms_managed_positions_status", table_name="oms_managed_positions")
    op.drop_index(op.f("ix_oms_managed_positions_symbol"), table_name="oms_managed_positions")
    op.drop_index(op.f("ix_oms_managed_positions_broker_account_name"), table_name="oms_managed_positions")
    op.drop_index(op.f("ix_oms_managed_positions_strategy_code"), table_name="oms_managed_positions")
    op.drop_table("oms_managed_positions")
