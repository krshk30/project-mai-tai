"""add scanner blacklist entries

Revision ID: 20260329_0002
Revises: 20260328_0001
Create Date: 2026-03-29 13:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260329_0002"
down_revision = "20260328_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scanner_blacklist_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scanner_blacklist_entries")),
        sa.UniqueConstraint("symbol", name=op.f("uq_scanner_blacklist_entries_symbol")),
    )
    op.create_index(
        op.f("ix_scanner_blacklist_entries_symbol"),
        "scanner_blacklist_entries",
        ["symbol"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_scanner_blacklist_entries_symbol"), table_name="scanner_blacklist_entries")
    op.drop_table("scanner_blacklist_entries")
