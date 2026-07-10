"""add scanner_confirmed_events table (research: momentum-scanner confirm/fade/retention-drop capture)

Revision ID: 20260710_0012
Revises: 20260707_0011
Create Date: 2026-07-10 12:00:00

Additive + INERT unless `scanner_confirmed_capture_enabled` is set (default False).
Read-only research capture of the momentum scanner's confirm/evict decisions: one
CONFIRM row per confirmed candidate (with the confirm_path / rank_score / float the
scanner gated on), one FADE row when a confirmed candidate is pruned, one
RETENTION_DROP row when a feed-retention symbol falls out of the retained set. These
rows reconstruct per-symbol `[confirmed_at -> fade_at -> retention_drop_at]` windows
for the backtest. Byte-identical to before when the flag is off (no rows written).
Natural-key dedupe on (trade_date, symbol, event_type, event_at).
"""

from alembic import op
import sqlalchemy as sa


revision = "20260710_0012"
down_revision = "20260707_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scanner_confirmed_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirm_path", sa.String(length=32), nullable=True),
        sa.Column("rank_score", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("force_watchlist", sa.Boolean(), nullable=True),
        sa.Column("price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("day_volume", sa.BigInteger(), nullable=True),
        sa.Column("float_used", sa.BigInteger(), nullable=True),
        sa.Column("change_pct", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("reconfirm_seq", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scanner_confirmed_events")),
        sa.UniqueConstraint(
            "trade_date",
            "symbol",
            "event_type",
            "event_at",
            name="uq_scanner_confirmed_events_key",
        ),
    )
    op.create_index(
        op.f("ix_scanner_confirmed_events_trade_date"),
        "scanner_confirmed_events",
        ["trade_date"],
    )
    op.create_index(
        op.f("ix_scanner_confirmed_events_symbol"),
        "scanner_confirmed_events",
        ["symbol"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_scanner_confirmed_events_symbol"),
        table_name="scanner_confirmed_events",
    )
    op.drop_index(
        op.f("ix_scanner_confirmed_events_trade_date"),
        table_name="scanner_confirmed_events",
    )
    op.drop_table("scanner_confirmed_events")
