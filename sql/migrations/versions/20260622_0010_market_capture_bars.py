"""add market_capture_bars (1-min OHLCV for the daily scanner-qualified universe)

Gathered post-close via Massive REST list_aggs (gather_polygon_universe.py).
Append-only, parsed columns. Pruned tight (14-day) by the prune-capture timer.

Revision ID: 20260622_0010
Revises: 20260622_0009
Create Date: 2026-06-22 17:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260622_0010"
down_revision = "20260622_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_capture_bars",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_secs", sa.Integer(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("vwap", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("transactions", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_capture_bars_symbol_event_ts",
        "market_capture_bars",
        ["symbol", "event_ts"],
    )
    op.create_index(
        "ix_market_capture_bars_received_at",
        "market_capture_bars",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_capture_bars_received_at", table_name="market_capture_bars")
    op.drop_index("ix_market_capture_bars_symbol_event_ts", table_name="market_capture_bars")
    op.drop_table("market_capture_bars")
