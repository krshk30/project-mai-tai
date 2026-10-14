"""Strategy-isolated SEC issuer metadata and aggregate request clock."""

import sqlalchemy as sa
from alembic import op

revision = "20261014_sec_country"
down_revision = "20261008_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "momentum_live_sec_country",
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("cik", sa.String(10), nullable=True),
        sa.Column("business_country", sa.String(16), nullable=True),
        sa.Column("incorporation", sa.String(16), nullable=True),
        sa.Column("is_us", sa.Boolean(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("lookup_day", sa.Date(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=True),
    )
    clock = op.create_table(
        "momentum_live_sec_request_clock",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_request_ns", sa.BigInteger(), nullable=False),
    )
    op.bulk_insert(clock, [{"id": 1, "last_request_ns": 0}])


def downgrade() -> None:
    op.drop_table("momentum_live_sec_request_clock")
    op.drop_table("momentum_live_sec_country")
