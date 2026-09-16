"""Add the broker-disconnected Momentum paper evidence tape.

Revision ID: 20260916_0021
Revises: 20260910_0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260916_0021"
down_revision = "20260910_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "momentum_paper_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(255), nullable=False),
        sa.Column("logical_id", sa.String(255), nullable=False),
        sa.Column("strategy_code", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", name="uq_momentum_paper_events_event_key"),
    )
    for column in (
        "logical_id",
        "strategy_code",
        "event_type",
        "session_date",
        "symbol",
        "observed_at",
    ):
        op.create_index(
            f"ix_momentum_paper_events_{column}",
            "momentum_paper_events",
            [column],
        )
    op.create_index(
        "ix_momentum_paper_events_session_strategy",
        "momentum_paper_events",
        ["session_date", "strategy_code", "observed_at"],
    )
    op.create_index(
        "ix_momentum_paper_events_logical",
        "momentum_paper_events",
        ["logical_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_table("momentum_paper_events")
