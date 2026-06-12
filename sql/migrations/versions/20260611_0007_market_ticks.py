"""add market trade/quote tick tables (Schwab LEVELONE capture for replay)

Revision ID: 20260611_0007
Revises: 20260518_0006
Create Date: 2026-06-11 21:30:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260611_0007"
down_revision = "20260518_0006"
branch_labels = None
depends_on = None


def _common_columns() -> list:
    return [
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("cumulative_volume", sa.BigInteger(), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("raw_hash", sa.Text(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "market_trade_ticks",
        *_common_columns(),
        sa.Column("price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "service", "symbol", "event_ts", "raw_hash",
            name="uq_market_trade_ticks_dedupe",
        ),
    )
    op.create_index(
        "ix_market_trade_ticks_symbol_event_ts",
        "market_trade_ticks",
        ["symbol", "event_ts"],
    )

    op.create_table(
        "market_quote_ticks",
        *_common_columns(),
        sa.Column("bid_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ask_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("last_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("bid_size", sa.Integer(), nullable=True),
        sa.Column("ask_size", sa.Integer(), nullable=True),
        sa.Column("last_size", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "service", "symbol", "event_ts", "raw_hash",
            name="uq_market_quote_ticks_dedupe",
        ),
    )
    op.create_index(
        "ix_market_quote_ticks_symbol_event_ts",
        "market_quote_ticks",
        ["symbol", "event_ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_quote_ticks_symbol_event_ts", table_name="market_quote_ticks")
    op.drop_table("market_quote_ticks")
    op.drop_index("ix_market_trade_ticks_symbol_event_ts", table_name="market_trade_ticks")
    op.drop_table("market_trade_ticks")
