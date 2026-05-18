"""index broker_order_events.event_at

Revision ID: 20260518_0006
Revises: 20260511_0005
Create Date: 2026-05-18 11:00:00
"""

from alembic import op


revision = "20260518_0006"
down_revision = "20260511_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_broker_order_events_event_at"),
        "broker_order_events",
        ["event_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_broker_order_events_event_at"),
        table_name="broker_order_events",
    )
