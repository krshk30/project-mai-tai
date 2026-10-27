"""Controlled SQLite child/matrix proof, NOT real-PG chain or old-code boot."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa

from project_mai_tai.atr_simulation.store_schema_20261023 import simulation_metadata as initial_metadata


from project_mai_tai.atr_simulation import store_schema_20261023_aggregate as schema

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("controlled_aggregate_child",
    ROOT / "sql/migrations/versions/20261023_atr_aggregate_entry.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)

OLD_PAIRS = {("LIVE", "ACTUAL"), ("TODAY", "SUPPLIED"), ("VOLBODY", "QUEUE_AWARE"),
    ("VOLBODY", "OPTIMISTIC"), ("FIRSTBAR", "MODELED_STOP_AT_OBSERVED_PRINT_VOLUME"),
    ("FIRSTBAR_X3", "MODELED_STOP_AT_OBSERVED_PRINT_VOLUME")}
MODES = ("LIVE", "TODAY", "VOLBODY", "FIRSTBAR", "FIRSTBAR_X3", schema.MODE)
MODELS = ("ACTUAL", "SUPPLIED", "QUEUE_AWARE", "OPTIMISTIC",
    "MODELED_STOP_AT_OBSERVED_PRINT_VOLUME", schema.MODEL)


def create(connection, metadata):
    metadata.create_all(connection)
    connection.execute(metadata.tables["atr_simulation_runs"].insert(), dict(
        source_id="CONTROLLED", input_sha256="a" * 64, policy_version="CONTROLLED",
        source_refs=[], coverage={}, result_sha256="b" * 64,
        created_at=datetime(2026, 10, 23, tzinfo=timezone.utc)))


def row(mode, model, key="a" * 64):
    return dict(row_key=key, source_id="CONTROLLED", policy_version="CONTROLLED",
        day=datetime(2026, 10, 23).date(), symbol="CONTROLLED", broker="CONTROLLED",
        account_id="CONTROLLED", strategy="CONTROLLED", flip_id="CONTROLLED",
        segment_id="CONTROLLED", mode=mode, model=model, payload_sha256="c" * 64,
        payload={"CONTROLLED_DDL_FIXTURE_NOT_MODEL_RESULT": True})


@pytest.mark.parametrize("mode,model", [(a, b) for a in MODES for b in MODELS])
def test_new_metadata_accepts_exactly_old_pairs_plus_one_new_pair(mode, model):
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            metadata = schema.simulation_metadata()
            create(connection, metadata)
            statement = metadata.tables["atr_simulation_rows"].insert()
            if (mode, model) in OLD_PAIRS | {(schema.MODE, schema.MODEL)}:
                connection.execute(statement, row(mode, model))
            else:
                with pytest.raises(sa.exc.IntegrityError):
                    connection.execute(statement, row(mode, model))
    finally:
        engine.dispose()


def test_child_changes_no_initial_metadata_columns_or_constraints():
    old, new = initial_metadata(), schema.simulation_metadata()
    for name in old.tables:
        assert list(old.tables[name].columns.keys()) == list(new.tables[name].columns.keys())
    before = {c.name: str(c.sqltext) for c in old.tables["atr_simulation_rows"].constraints
        if isinstance(c, sa.CheckConstraint)}
    after = {c.name: str(c.sqltext) for c in new.tables["atr_simulation_rows"].constraints
        if isinstance(c, sa.CheckConstraint)}
    assert before == schema.CHECKS_BEFORE
    assert after == schema.CHECKS_AFTER
    assert initial_metadata().tables["atr_simulation_rows"].c.mode.type.length == 16


def test_actual_child_roundtrip_zero_new_rows_preserves_every_old_row_value(monkeypatch):
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            metadata = initial_metadata()
            create(connection, metadata)
            connection.execute(metadata.tables["atr_simulation_rows"].insert(), row("LIVE", "ACTUAL"))
            before = connection.exec_driver_sql("SELECT * FROM atr_simulation_rows").all()
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            assert connection.exec_driver_sql("SELECT * FROM atr_simulation_rows").all() == before
            migration.downgrade()
            assert connection.exec_driver_sql("SELECT * FROM atr_simulation_rows").all() == before
            migration.upgrade()
            assert connection.exec_driver_sql("SELECT * FROM atr_simulation_rows").all() == before
    finally:
        engine.dispose()


def test_actual_child_refuses_downgrade_with_new_evidence_and_keeps_all_rows(monkeypatch):
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            metadata = initial_metadata()
            create(connection, metadata)
            connection.execute(metadata.tables["atr_simulation_rows"].insert(), row("LIVE", "ACTUAL"))
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            connection.execute(metadata.tables["atr_simulation_rows"].insert(),
                row(schema.MODE, schema.MODEL, "d" * 64))
            before = connection.exec_driver_sql("SELECT * FROM atr_simulation_rows ORDER BY row_key").all()
            with pytest.raises(RuntimeError, match="append_only_downgrade_refused"):
                migration.downgrade()
            assert connection.exec_driver_sql("SELECT * FROM atr_simulation_rows ORDER BY row_key").all() == before
    finally:
        engine.dispose()


def test_child_is_one_unbranched_additive_revision():
    assert migration.revision == "20261023_atr_aggregate"
    assert migration.down_revision == "20261023_atr_simulation"
    assert migration.branch_labels is None and migration.depends_on is None


def test_unreviewed_backend_refuses_before_any_schema_change(monkeypatch):
    monkeypatch.setattr(migration, "op", SimpleNamespace(get_bind=lambda:
        SimpleNamespace(dialect=SimpleNamespace(name="CONTROLLED_UNREVIEWED"))))
    with pytest.raises(RuntimeError, match="reviewed_simulation_migration_backend"):
        migration.upgrade()
