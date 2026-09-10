"""Bind confirmation decisions to a logical fan-out opportunity.

Revision ID: 20260910_0020
Revises: 20260904_0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260910_0020"
down_revision = "20260904_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "v2_confirmation_exit_evaluations",
        sa.Column("fanout_slot_id", sa.String(64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_v2_confirmation_exit_evaluations_fanout_slot_id",
        "v2_confirmation_exit_evaluations",
        ["fanout_slot_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_v2_confirmation_exit_evaluations_fanout_slot_id",
        "v2_confirmation_exit_evaluations",
        type_="unique",
    )
    op.drop_column("v2_confirmation_exit_evaluations", "fanout_slot_id")
