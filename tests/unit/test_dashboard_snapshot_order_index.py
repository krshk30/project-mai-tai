from __future__ import annotations

import runpy
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from project_mai_tai.db.models import DashboardSnapshot


INDEX_NAME = "ix_dashboard_snapshots_type_created_id_desc"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "sql"
    / "migrations"
    / "versions"
    / "20260828_0016_dashboard_snapshot_order_index.py"
)


def _model_index():
    return next(index for index in DashboardSnapshot.__table__.indexes if index.name == INDEX_NAME)


def test_model_declares_exact_postgres_order() -> None:
    ddl = " ".join(
        str(CreateIndex(_model_index()).compile(dialect=postgresql.dialect())).split()
    )
    assert ddl == (
        "CREATE INDEX ix_dashboard_snapshots_type_created_id_desc "
        "ON dashboard_snapshots (snapshot_type, created_at DESC, id DESC)"
    )


def test_sqlite_schema_preserves_both_descending_keys() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    DashboardSnapshot.__table__.create(engine)

    with engine.connect() as connection:
        rows = connection.execute(text(f"PRAGMA index_xinfo('{INDEX_NAME}')")).all()

    key_rows = [row for row in rows if row[5] == 1]
    assert [row[2] for row in key_rows] == ["snapshot_type", "created_at", "id"]
    assert [row[3] for row in key_rows] == [0, 1, 1]


def test_migration_upgrade_and_downgrade_use_the_same_contract() -> None:
    namespace = runpy.run_path(str(MIGRATION))
    calls: list[tuple] = []

    class _Op:
        @staticmethod
        def create_index(name, table, columns, *, unique):
            calls.append(("create", name, table, tuple(map(str, columns)), unique))

        @staticmethod
        def drop_index(name, *, table_name):
            calls.append(("drop", name, table_name))

    namespace["upgrade"].__globals__["op"] = _Op
    namespace["downgrade"].__globals__["op"] = _Op
    namespace["upgrade"]()
    namespace["downgrade"]()

    assert calls == [
        (
            "create",
            INDEX_NAME,
            "dashboard_snapshots",
            ("snapshot_type", "created_at DESC", "id DESC"),
            False,
        ),
        ("drop", INDEX_NAME, "dashboard_snapshots"),
    ]
