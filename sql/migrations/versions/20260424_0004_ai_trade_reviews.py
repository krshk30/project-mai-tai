"""add ai trade reviews

Revision ID: 20260424_0004
Revises: 20260401_0003
Create Date: 2026-04-24 18:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260424_0004"
down_revision = "20260401_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_trade_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=True),
        sa.Column("strategy_code", sa.String(length=64), nullable=False),
        sa.Column("broker_account_name", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("review_type", sa.String(length=32), nullable=False),
        sa.Column("cycle_key", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["intent_id"], ["trade_intents.id"], name=op.f("fk_ai_trade_reviews_intent_id_trade_intents")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_trade_reviews")),
        sa.UniqueConstraint(
            "review_type",
            "cycle_key",
            name="uq_ai_trade_reviews_review_type_cycle_key",
        ),
    )
    op.create_index(op.f("ix_ai_trade_reviews_broker_account_name"), "ai_trade_reviews", ["broker_account_name"], unique=False)
    op.create_index(op.f("ix_ai_trade_reviews_cycle_key"), "ai_trade_reviews", ["cycle_key"], unique=False)
    op.create_index(op.f("ix_ai_trade_reviews_intent_id"), "ai_trade_reviews", ["intent_id"], unique=False)
    op.create_index(op.f("ix_ai_trade_reviews_review_type"), "ai_trade_reviews", ["review_type"], unique=False)
    op.create_index(op.f("ix_ai_trade_reviews_strategy_code"), "ai_trade_reviews", ["strategy_code"], unique=False)
    op.create_index(op.f("ix_ai_trade_reviews_symbol"), "ai_trade_reviews", ["symbol"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_trade_reviews_symbol"), table_name="ai_trade_reviews")
    op.drop_index(op.f("ix_ai_trade_reviews_strategy_code"), table_name="ai_trade_reviews")
    op.drop_index(op.f("ix_ai_trade_reviews_review_type"), table_name="ai_trade_reviews")
    op.drop_index(op.f("ix_ai_trade_reviews_intent_id"), table_name="ai_trade_reviews")
    op.drop_index(op.f("ix_ai_trade_reviews_cycle_key"), table_name="ai_trade_reviews")
    op.drop_index(op.f("ix_ai_trade_reviews_broker_account_name"), table_name="ai_trade_reviews")
    op.drop_table("ai_trade_reviews")
