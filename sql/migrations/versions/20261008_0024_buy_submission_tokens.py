"""Durable BUY attempts and never-sent admission closures; no data backfill."""

from alembic import op

from project_mai_tai.oms.buy_submission_journal import (
    BuyAdmissionClosure, BuyCoverageEpoch, BuySubmissionToken,
)

revision = "20261008_0024"
down_revision = "20261008_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (BuyCoverageEpoch.__table__, BuySubmissionToken.__table__, BuyAdmissionClosure.__table__):
        table.create(bind=op.get_bind())


def downgrade() -> None:
    for table in (BuyAdmissionClosure.__table__, BuySubmissionToken.__table__, BuyCoverageEpoch.__table__):
        table.drop(bind=op.get_bind())
