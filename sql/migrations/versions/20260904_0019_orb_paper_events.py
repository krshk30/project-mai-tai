"""Add the broker-disconnected ORB paper evidence tape.

Revision ID: 20260904_0019
Revises: 20260902_0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260904_0019"
down_revision = "20260902_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orb_paper_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", name="uq_orb_paper_events_event_key"),
    )
    op.create_index("ix_orb_paper_events_event_type", "orb_paper_events", ["event_type"])
    op.create_index("ix_orb_paper_events_session_date", "orb_paper_events", ["session_date"])
    op.create_index("ix_orb_paper_events_symbol", "orb_paper_events", ["symbol"])
    op.create_index("ix_orb_paper_events_observed_at", "orb_paper_events", ["observed_at"])
    op.create_index(
        "ix_orb_paper_events_session_symbol",
        "orb_paper_events",
        ["session_date", "symbol", "observed_at"],
    )


def downgrade() -> None:
    op.drop_table("orb_paper_events")
