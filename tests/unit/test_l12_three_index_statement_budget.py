"""Pure migration/control seams; no PG, builds, physical-size or rate proof."""

from contextlib import contextmanager
import importlib.util
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest


CASES = (
    ("ix_l12_dispatch_scope", "dashboard_snapshots"),
    ("ix_l12_order_dispatch_token", "broker_orders"),
    ("ix_l12_control_uncertainty", "bot_control_attempts"),
)
EXPECTED_DDL = (
    "CREATE INDEX ix_l12_dispatch_scope ON dashboard_snapshots (snapshot_type, "
    "(CAST(payload ->> 'kind' AS VARCHAR)), (CAST(payload ->> 'strategy_code' AS VARCHAR)), "
    "(CAST(payload ->> 'account_name' AS VARCHAR)), created_at, id);",
    "CREATE INDEX ix_l12_order_dispatch_token ON broker_orders "
    "((CAST(payload ->> 'clearwait_dispatch_token' AS VARCHAR)));",
    "CREATE INDEX ix_l12_control_uncertainty ON bot_control_attempts (account_id, symbol) "
    "WHERE status IS NULL OR status NOT IN ('TERMINAL', 'PROVEN_NOT_SENT');",
)


@pytest.fixture
def migration():
    path = Path(__file__).resolve().parents[2] / "sql/migrations/versions/20261022_l12_dispatch_retirement.py"
    spec = importlib.util.spec_from_file_location("l12_scoped_index_migration_control", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Connection:
    def __init__(self, *, previous="750ms", cleanup_error=None, set_error=None):
        self.previous = self.current = previous
        self.cleanup_error, self.set_error = cleanup_error, set_error
        self.events = []

    def exec_driver_sql(self, sql):
        self.events.append(("sql", sql))
        if sql == "SHOW statement_timeout":
            return SimpleNamespace(scalar_one=lambda: self.current)
        assert sql == "SET LOCAL statement_timeout = '60000ms'"
        if self.set_error is not None:
            raise self.set_error
        self.current = "1min"

    @contextmanager
    def begin_nested(self):
        before = self.current
        self.events.append(("savepoint", before))
        try:
            yield
        except BaseException:
            self.current = before
            self.events.append(("rollback_savepoint", before))
            raise
        else:
            self.events.append(("release_savepoint", self.current))

    def execute(self, statement, values):
        assert str(statement) == "SELECT set_config('statement_timeout', :previous, true)"
        assert values == {"previous": self.previous}
        self.events.append(("restore", values["previous"]))
        if self.cleanup_error is not None:
            raise self.cleanup_error
        self.current = values["previous"]


class Ops:
    def __init__(self, connection, *, dialect="postgresql", offline=False,
                 ddl_error=None, offline_restore_error=None):
        self.connection, self.ddl_error = connection, ddl_error
        self.offline_restore_error = offline_restore_error
        self.context = SimpleNamespace(dialect=SimpleNamespace(name=dialect), as_sql=offline)
        self.calls = []

    def get_context(self):
        return self.context

    def get_bind(self):
        assert not self.context.as_sql
        return self.connection

    def create_index(self, *args, **kwargs):
        self.calls.append(("index", args, kwargs, self.connection.current))
        if self.ddl_error is not None:
            raise self.ddl_error

    def execute(self, sql):
        self.calls.append(("execute", sql))
        if sql.startswith("SELECT set_config('statement_timeout'") and self.offline_restore_error:
            raise self.offline_restore_error


@pytest.mark.parametrize("name,table", CASES)
@pytest.mark.parametrize("previous", ["750ms", "2s", "0"])
def test_online_exact_index_only_and_actual_previous_budget_restored(migration, name, table, previous):
    connection = Connection(previous=previous)
    migration.op = Ops(connection)
    migration._create_shared_index(name, table, ["CONTROLLED_COLUMN"])
    assert migration.op.calls == [("index", (name, table, ["CONTROLLED_COLUMN"]), {}, "1min")]
    assert connection.current == previous
    assert connection.events == [
        ("sql", "SHOW statement_timeout"), ("savepoint", previous),
        ("sql", "SET LOCAL statement_timeout = '60000ms'"),
        ("release_savepoint", "1min"), ("restore", previous)]


@pytest.mark.parametrize("name,table", CASES)
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_failed_index_rolls_back_savepoint_resets_and_preserves_primary(migration, name, table, error_type):
    primary = error_type("CONTROLLED_DDL_FAILURE")
    connection = Connection()
    migration.op = Ops(connection, ddl_error=primary)
    with pytest.raises(error_type) as caught:
        migration._create_shared_index(name, table, ["CONTROLLED_COLUMN"])
    assert caught.value is primary
    assert connection.current == "750ms"
    assert connection.events[-2:] == [("rollback_savepoint", "750ms"), ("restore", "750ms")]


def test_budget_set_failure_still_restores_without_invoking_index(migration):
    primary = RuntimeError("CONTROLLED_SET_FAILURE")
    connection = Connection(set_error=primary)
    migration.op = Ops(connection)
    with pytest.raises(RuntimeError) as caught:
        migration._create_shared_index(*CASES[0], ["CONTROLLED_COLUMN"])
    assert caught.value is primary and not migration.op.calls
    assert connection.current == "750ms" and connection.events[-1] == ("restore", "750ms")


def test_cleanup_failure_without_primary_cannot_return_success(migration):
    cleanup = RuntimeError("CONTROLLED_CLEANUP")
    migration.op = Ops(Connection(cleanup_error=cleanup))
    with pytest.raises(RuntimeError) as caught:
        migration._create_shared_index(*CASES[0], ["CONTROLLED_COLUMN"])
    assert caught.value is cleanup


def test_cleanup_failure_preserves_original_error_with_type_only_unknown(migration):
    primary = RuntimeError("CONTROLLED_PRIMARY")
    connection = Connection(cleanup_error=ValueError("DO_NOT_COPY_CLEANUP_MESSAGE"))
    migration.op = Ops(connection, ddl_error=primary)
    with pytest.raises(RuntimeError) as caught:
        migration._create_shared_index(*CASES[0], ["CONTROLLED_COLUMN"])
    assert caught.value is primary and connection.current == "750ms"
    assert primary.__notes__ == ["L12 index budget cleanup UNKNOWN: ValueError"]


@pytest.mark.parametrize("name,table", [
    ("unreviewed_index", "dashboard_snapshots"),
    ("ix_l12_dispatch_scope", "broker_orders"),
    ("ix_brokerdecides_dispatch_retirements_token", "brokerdecides_dispatch_retirements"),
])
def test_other_index_or_wrong_table_cannot_obtain_budget(migration, name, table):
    migration.op = Ops(Connection())
    with pytest.raises(ValueError, match="exact shared index/table"):
        migration._create_shared_index(name, table, ["CONTROLLED_COLUMN"])
    assert not migration.op.calls and not migration.op.connection.events


@pytest.mark.parametrize("name,table", CASES)
def test_sqlite_index_path_has_no_postgresql_budget_sql(migration, name, table):
    migration.op = Ops(Connection(), dialect="sqlite")
    migration._create_shared_index(name, table, ["CONTROLLED_COLUMN"])
    assert len(migration.op.calls) == 1 and migration.op.calls[0][0] == "index"
    assert not migration.op.connection.events


def test_offline_index_error_still_emits_reset_and_propagates(migration):
    primary = RuntimeError("CONTROLLED_OFFLINE_GENERATION")
    migration.op = Ops(Connection(), offline=True, ddl_error=primary)
    with pytest.raises(RuntimeError) as caught:
        migration._create_shared_index(*CASES[0], ["CONTROLLED_COLUMN"])
    assert caught.value is primary
    assert migration.op.calls[-1] == ("execute",
        "SELECT set_config('statement_timeout', current_setting('l12.previous_index_statement_timeout'), true)")


def test_offline_cleanup_error_cannot_mask_primary(migration):
    primary = RuntimeError("CONTROLLED_PRIMARY")
    migration.op = Ops(Connection(), offline=True, ddl_error=primary,
        offline_restore_error=ValueError("DO_NOT_COPY_CLEANUP_MESSAGE"))
    with pytest.raises(RuntimeError) as caught:
        migration._create_shared_index(*CASES[0], ["CONTROLLED_COLUMN"])
    assert caught.value is primary
    assert primary.__notes__ == ["L12 offline index budget cleanup UNKNOWN: ValueError"]


def test_actual_offline_upgrade_preserves_three_literal_definitions_and_other_ddl(migration):
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={
        "as_sql": True, "output_buffer": output, "literal_binds": True})
    migration.op = Operations(context)
    migration.upgrade()
    statements = [part.strip() + ";" for part in output.getvalue().split(";") if part.strip()]
    create_indexes = [part for part in statements if part.startswith("CREATE INDEX ix_l12_")]
    assert create_indexes == list(EXPECTED_DDL)
    for index in EXPECTED_DDL:
        offset = statements.index(index)
        assert statements[offset - 2:offset] == [
            "SELECT set_config('l12.previous_index_statement_timeout', current_setting('statement_timeout'), true);",
            "SET LOCAL statement_timeout = '60000ms';"]
        assert statements[offset + 1] == (
            "SELECT set_config('statement_timeout', current_setting('l12.previous_index_statement_timeout'), true);")
    assert sum(part == "SET LOCAL statement_timeout = '60000ms';" for part in statements) == 3
    assert not any("lock_timeout" in part or "CONCURRENTLY" in part for part in statements)
    assert next(i for i, part in enumerate(statements) if part.startswith("SET LOCAL")) > next(
        i for i, part in enumerate(statements) if part.startswith("CREATE TABLE brokerdecides_dispatch_evidence"))
    assert migration.revision == "20261022_l12_retirement"
    assert migration.down_revision == "20261023_atr_aggregate"
