"""add central market_capture_trades / market_capture_quotes tables

GLOBAL, bot-agnostic capture of raw Polygon/Massive trades + L1 quotes from the
market-data gateway stream (market_capture_app). Append-only, parsed columns
only (no raw blob), no unique-dedupe constraint -> cheap high-volume inserts.
Pruned tight by scripts/prune_market_ticks.py --tables (project-mai-tai-prune-capture.timer).

Revision ID: 20260622_0009
Revises: 20260614_0008
Create Date: 2026-06-22 15:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260622_0009"
down_revision = "20260614_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_capture_trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("exchange", sa.Text(), nullable=True),
        sa.Column("conditions", sa.Text(), nullable=True),
        sa.Column("cumulative_volume", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_capture_trades_symbol_event_ts",
        "market_capture_trades",
        ["symbol", "event_ts"],
    )
    op.create_index(
        "ix_market_capture_trades_received_at",
        "market_capture_trades",
        ["received_at"],
    )

    op.create_table(
        "market_capture_quotes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("bid_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ask_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("bid_size", sa.Integer(), nullable=True),
        sa.Column("ask_size", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_capture_quotes_symbol_event_ts",
        "market_capture_quotes",
        ["symbol", "event_ts"],
    )
    op.create_index(
        "ix_market_capture_quotes_received_at",
        "market_capture_quotes",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_capture_quotes_received_at", table_name="market_capture_quotes")
    op.drop_index("ix_market_capture_quotes_symbol_event_ts", table_name="market_capture_quotes")
    op.drop_table("market_capture_quotes")
    op.drop_index("ix_market_capture_trades_received_at", table_name="market_capture_trades")
    op.drop_index("ix_market_capture_trades_symbol_event_ts", table_name="market_capture_trades")
    op.drop_table("market_capture_trades")
