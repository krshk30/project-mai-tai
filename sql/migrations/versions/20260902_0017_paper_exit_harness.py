"""Add append-only paper-exit configuration and evidence tables.

Revision ID: 20260902_0017
Revises: 20260828_0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from datetime import UTC, datetime
from uuid import UUID

revision = "20260902_0017"
down_revision = "20260828_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_exit_rule_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("stop_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_exit_rule_configs_effective_at", "paper_exit_rule_configs", ["effective_at"])
    op.create_table(
        "paper_exit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(255), nullable=False),
        sa.Column("logical_id", sa.String(255), nullable=False),
        sa.Column("arm", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("venue", sa.String(16), nullable=False),
        sa.Column("source_fill_id", sa.Uuid(), nullable=True),
        sa.Column("broker_fill_id", sa.String(128), nullable=True),
        sa.Column("config_id", sa.Uuid(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Numeric(18, 8), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["config_id"], ["paper_exit_rule_configs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", name="uq_paper_exit_events_event_key"),
    )
    op.create_index("ix_paper_exit_events_arm", "paper_exit_events", ["arm"])
    op.create_index("ix_paper_exit_events_broker_fill_id", "paper_exit_events", ["broker_fill_id"])
    op.create_index("ix_paper_exit_events_config_id", "paper_exit_events", ["config_id"])
    op.create_index("ix_paper_exit_events_event_type", "paper_exit_events", ["event_type"])
    op.create_index("ix_paper_exit_events_logical_id", "paper_exit_events", ["logical_id"])
    op.create_index("ix_paper_exit_events_observed_at", "paper_exit_events", ["observed_at"])
    op.create_index("ix_paper_exit_events_session_date", "paper_exit_events", ["session_date"])
    op.create_index("ix_paper_exit_events_source_fill_id", "paper_exit_events", ["source_fill_id"])
    op.create_index("ix_paper_exit_events_symbol", "paper_exit_events", ["symbol"])
    op.create_index(
        "ix_paper_exit_events_session_arm",
        "paper_exit_events",
        ["session_date", "arm", "observed_at"],
    )
    op.create_index(
        "ix_paper_exit_events_logical",
        "paper_exit_events",
        ["logical_id", "observed_at"],
    )
    config = sa.table(
        "paper_exit_rule_configs",
        sa.column("id", sa.Uuid()),
        sa.column("target_pct", sa.Numeric(8, 4)),
        sa.column("stop_pct", sa.Numeric(8, 4)),
        sa.column("effective_at", sa.DateTime(timezone=True)),
        sa.column("changed_by", sa.String(128)),
    )
    op.bulk_insert(
        config,
        [
            {
                "id": UUID("7fdbd6cf-c6c5-4fbd-93a1-a1c18ea8f001"),
                "target_pct": 5,
                "stop_pct": 8,
                "effective_at": datetime(1970, 1, 1, tzinfo=UTC),
                "changed_by": "migration-initial-v1",
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("paper_exit_events")
    op.drop_table("paper_exit_rule_configs")
