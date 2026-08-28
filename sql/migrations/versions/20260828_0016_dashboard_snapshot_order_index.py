"""Index the dashboard-snapshot append and cursor order.

PR #834 made the shared append helper read the latest row of the same snapshot type before every
insert. The production probe was measured at 6.891 ms / 837 buffers on a 237 MB, 5,068-row table
without this index. The same order is also used by the #823 fanout-outcome keyset cursor.

Revision ID: 20260828_0016
Revises: 20260820_0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260828_0016"
down_revision = "20260820_0015"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_dashboard_snapshots_type_created_id_desc"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "dashboard_snapshots",
        ["snapshot_type", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="dashboard_snapshots")
