"""add Webull ineligible session cache (dual-broker fan-out)

Revision ID: 20260725_0013
Revises: 20260710_0012
Create Date: 2026-07-25 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260725_0013"
down_revision = "20260710_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webull_ineligible_today",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("session_date", sa.String(length=10), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column("hit_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["broker_account_id"],
            ["broker_accounts.id"],
            name=op.f("fk_webull_ineligible_today_broker_account_id_broker_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webull_ineligible_today")),
        sa.UniqueConstraint(
            "symbol",
            "session_date",
            "broker_account_id",
            name="uq_webull_ineligible_today_symbol_session_account",
        ),
    )
    op.create_index(
        op.f("ix_webull_ineligible_today_symbol"),
        "webull_ineligible_today",
        ["symbol"],
        unique=False,
    )
    op.create_index(
        op.f("ix_webull_ineligible_today_session_date"),
        "webull_ineligible_today",
        ["session_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_webull_ineligible_today_broker_account_id"),
        "webull_ineligible_today",
        ["broker_account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_webull_ineligible_today_broker_account_id"),
        table_name="webull_ineligible_today",
    )
    op.drop_index(
        op.f("ix_webull_ineligible_today_session_date"),
        table_name="webull_ineligible_today",
    )
    op.drop_index(
        op.f("ix_webull_ineligible_today_symbol"),
        table_name="webull_ineligible_today",
    )
    op.drop_table("webull_ineligible_today")
