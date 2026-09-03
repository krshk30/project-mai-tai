"""Add the effective-dated CONF1 bar count.

Revision ID: 20260902_0018
Revises: 20260902_0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260902_0018"
down_revision = "20260902_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "paper_exit_rule_configs",
        sa.Column("confirmation_bars", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_table(
        "v2_confirmation_exit_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_fill_id", sa.Uuid(), nullable=False),
        sa.Column("source_order_id", sa.Uuid(), nullable=False),
        sa.Column("broker_fill_id", sa.String(128), nullable=False),
        sa.Column("broker_order_id", sa.String(128), nullable=False),
        sa.Column("broker_account_name", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_bar_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atr_state", sa.String(16), nullable=False),
        sa.Column("should_exit", sa.Boolean(), nullable=False),
        sa.Column("confirmation_bars", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Uuid(), nullable=True),
        sa.Column("config_effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["config_id"], ["paper_exit_rule_configs.id"]),
        sa.ForeignKeyConstraint(["source_fill_id"], ["fills.id"]),
        sa.ForeignKeyConstraint(["source_order_id"], ["broker_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_fill_id",
            name="uq_v2_confirmation_exit_evaluations_source_fill_id",
        ),
    )
    op.create_index(
        "ix_v2_confirmation_exit_evaluations_symbol",
        "v2_confirmation_exit_evaluations",
        ["symbol"],
    )


def downgrade() -> None:
    op.drop_table("v2_confirmation_exit_evaluations")
    op.drop_column("paper_exit_rule_configs", "confirmation_bars")
