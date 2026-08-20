"""Q1 — broker_order_events.event_source: who refused, us or the broker.

⛔⭐⭐ EVERY REJECT COUNT BEFORE THIS COLUMN IS CONTAMINATED. `event_type="rejected"` is written
both when the BROKER refused a request we sent and when WE abandoned one that never left the
process. Collapsed into one word, "the broker is rejecting us" and "we are aborting our own
orders" read identically — and they point at completely different code.

⛔ EXISTING ROWS BACKFILL TO "unknown", NOT TO "broker". They are genuinely unclassified: the
information needed to split them was never written down. Marking them "broker" would manufacture
history, and marking them "client" would erase real broker refusals. An honest gap is countable;
a confident wrong label is not — and the gap is exactly what tells a reader that a number spanning
the migration boundary must not be read as a clean split.

Revision ID: 20260820_0015
Revises: 20260730_0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260820_0015"
down_revision = "20260730_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "broker_order_events",
        sa.Column(
            "event_source",
            sa.String(length=16),
            nullable=False,
            server_default="unknown",
        ),
    )
    # Indexed because every contaminated count this column fixes is a GROUP BY on it.
    op.create_index(
        "ix_broker_order_events_event_source",
        "broker_order_events",
        ["event_source"],
    )


def downgrade() -> None:
    op.drop_index("ix_broker_order_events_event_source", table_name="broker_order_events")
    op.drop_column("broker_order_events", "event_source")
