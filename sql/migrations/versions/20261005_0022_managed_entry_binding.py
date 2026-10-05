"""Bind managed positions to their entry; leave legacy rows unbound.

Revision ID: 20261005_0022
Revises: 20260916_0021
"""

import sqlalchemy as sa
from alembic import op

revision = "20261005_0022"
down_revision = "20260916_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("oms_managed_positions", sa.Column("entry_order_id", sa.Uuid(), nullable=True))
    op.add_column(
        "oms_managed_positions", sa.Column("entry_client_order_id", sa.String(128), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("oms_managed_positions", "entry_client_order_id")
    op.drop_column("oms_managed_positions", "entry_order_id")
