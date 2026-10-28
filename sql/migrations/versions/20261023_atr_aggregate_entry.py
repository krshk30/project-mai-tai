"""C1 22:39: one additional SIM mode/model; no old columns or row changes."""

import sqlalchemy as sa
from alembic import op

from project_mai_tai.atr_simulation.store_schema_20261023_aggregate import (
    CHECKS_AFTER, CHECKS_BEFORE, MODE,
)


revision = "20261023_atr_aggregate"
down_revision = "20261023_atr_simulation"
branch_labels = None
depends_on = None


def _replace(expressions):
    bind = op.get_bind()
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError("reviewed_simulation_migration_backend_required")
    observed = {item["name"] for item in sa.inspect(bind).get_check_constraints("atr_simulation_rows")}
    if not set(CHECKS_BEFORE) <= observed:
        raise RuntimeError("three_existing_simulation_checks_required")
    # PostgreSQL alters only the three checks. SQLite reconstruction is solely
    # for isolated functional fixtures, not a PostgreSQL/full-copy proof.
    with op.batch_alter_table("atr_simulation_rows",
            recreate="always" if bind.dialect.name == "sqlite" else "never") as batch:
        for name in CHECKS_BEFORE:
            batch.drop_constraint(op.f(name), type_="check")
        for name, expression in expressions.items():
            batch.create_check_constraint(op.f(name), expression)


def upgrade():
    _replace(CHECKS_AFTER)


def downgrade():
    present = op.get_bind().execute(sa.text(
        "SELECT 1 FROM atr_simulation_rows WHERE mode=:mode LIMIT 1"), {"mode": MODE}).first()
    if present is not None:
        raise RuntimeError("aggregate_entry_evidence_is_append_only_downgrade_refused")
    _replace(CHECKS_BEFORE)
