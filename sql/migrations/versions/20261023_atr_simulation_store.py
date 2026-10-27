"""Initial SIM-only DDL on installed schema; independent of unapplied L12.

C1 ordering approval 2026-10-23 19:39. Same unapplied revision, not a duplicate.
"""

from alembic import op

from project_mai_tai.atr_simulation.store_schema_20261023 import (
    SIMULATION_TABLES,
    simulation_metadata,
)


revision = "20261023_atr_simulation"
down_revision = "20261015_bot_control"
branch_labels = ("atr_simulation",)
depends_on = None


def upgrade():
    metadata = simulation_metadata()
    expected = {"atr_simulation_inputs", "atr_simulation_consumers",
                "atr_simulation_runs", "atr_simulation_rows"}
    if SIMULATION_TABLES != expected or set(metadata.tables) != expected:
        raise RuntimeError("simulation_only_metadata_required")
    for table in metadata.sorted_tables:
        table.create(op.get_bind(), checkfirst=False)


def downgrade():
    raise RuntimeError("ATRSIM1 evidence is append-only; disable the worker instead of deleting history")
