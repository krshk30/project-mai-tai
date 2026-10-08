"""Identity-bound FALSEFLIP1 evidence; existing rows remain unclassified."""

import sqlalchemy as sa
from alembic import op

revision = "20261008_0023"
down_revision = "20261005_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("oms_managed_positions", sa.Column("entry_classification", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("oms_managed_positions", "entry_classification")
