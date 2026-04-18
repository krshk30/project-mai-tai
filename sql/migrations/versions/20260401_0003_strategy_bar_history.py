"""add strategy bar history

Revision ID: 20260401_0003
Revises: 20260329_0002
Create Date: 2026-04-01 22:45:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260401_0003"
down_revision = "20260329_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_bar_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_code", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("interval_secs", sa.Integer(), nullable=False),
        sa.Column("bar_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("high_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("low_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("close_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.Column("trade_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("position_state", sa.String(length=32), nullable=False),
        sa.Column("position_quantity", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("decision_status", sa.String(length=32), server_default=sa.text("''"), nullable=False),
        sa.Column("decision_reason", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("decision_path", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("decision_score", sa.String(length=32), server_default=sa.text("''"), nullable=False),
        sa.Column("decision_score_details", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("indicators", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_bar_history")),
        sa.UniqueConstraint(
            "strategy_code",
            "symbol",
            "interval_secs",
            "bar_time",
            name="uq_strategy_bar_history_strategy_symbol_interval_time",
        ),
    )
    op.create_index(op.f("ix_strategy_bar_history_bar_time"), "strategy_bar_history", ["bar_time"], unique=False)
    op.create_index(
        op.f("ix_strategy_bar_history_decision_status"),
        "strategy_bar_history",
        ["decision_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_strategy_bar_history_interval_secs"),
        "strategy_bar_history",
        ["interval_secs"],
        unique=False,
    )
    op.create_index(
        op.f("ix_strategy_bar_history_strategy_code"),
        "strategy_bar_history",
        ["strategy_code"],
        unique=False,
    )
    op.create_index(op.f("ix_strategy_bar_history_symbol"), "strategy_bar_history", ["symbol"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_strategy_bar_history_symbol"), table_name="strategy_bar_history")
    op.drop_index(op.f("ix_strategy_bar_history_strategy_code"), table_name="strategy_bar_history")
    op.drop_index(op.f("ix_strategy_bar_history_interval_secs"), table_name="strategy_bar_history")
    op.drop_index(op.f("ix_strategy_bar_history_decision_status"), table_name="strategy_bar_history")
    op.drop_index(op.f("ix_strategy_bar_history_bar_time"), table_name="strategy_bar_history")
    op.drop_table("strategy_bar_history")
