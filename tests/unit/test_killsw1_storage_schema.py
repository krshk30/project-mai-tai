from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError

from project_mai_tai.db.models import (
    Base,
    BotControlAttempt,
    BotControlCommand,
    BotControlState,
    BrokerAccount,
    Strategy,
)

TABLES = {"bot_control_states", "bot_control_commands", "bot_control_attempts"}


def migration():
    path = (
        Path(__file__).resolve().parents[2] / "sql/migrations/versions/20261015_0025_bot_control.py"
    )
    spec = importlib.util.spec_from_file_location("killsw1_schema_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["models", "migration"])
def storage(request):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_fks(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        if request.param == "models":
            Base.metadata.create_all(connection)
        else:
            Base.metadata.create_all(
                connection,
                tables=[
                    table for name, table in Base.metadata.tables.items() if name not in TABLES
                ],
            )
            before = set(inspect(connection).get_table_names())
            module = migration()
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            assert set(inspect(connection).get_table_names()) - before == TABLES
        strategy, account = uuid4(), uuid4()
        connection.execute(
            Strategy.__table__.insert().values(
                id=strategy,
                code="momentum_warn_live",
                name="Momentum LIVE",
                execution_mode="live",
            )
        )
        connection.execute(
            BrokerAccount.__table__.insert().values(
                id=account,
                name="live:orb",
                provider="webull",
                environment="live",
            )
        )
    yield engine, strategy, account
    engine.dispose()


def seed(connection, strategy):
    connection.execute(
        BotControlState.__table__.insert().values(
            strategy_id=strategy,
            execution_mode="live",
            generation=0,
            revision=0,
            desired_state="RUN",
        )
    )


def command(connection, strategy, **overrides):
    values = dict(
        command_id=uuid4(),
        strategy_id=strategy,
        execution_mode="live",
        generation=1,
        expected_generation=0,
        revision=0,
        action="STOP",
        request_id=uuid4(),
        request_hash="a" * 64,
        account_scope={},
        account_scope_hash="b" * 64,
        actor_reference="test-operator",
        reason="operator-stop",
        phase="REQUESTED",
        active=True,
        inventory_census={"complete": False},
    )
    values.update(overrides)
    connection.execute(BotControlCommand.__table__.insert().values(**values))
    return values["command_id"]


def attempt(connection, strategy, account, command_id, **overrides):
    values = dict(
        attempt_id=uuid4(),
        command_id=command_id,
        strategy_id=strategy,
        execution_mode="live",
        generation=1,
        account_id=account,
        symbol="TCRT",
        binding_kind="MOM_PARENT",
        binding_key="owned-episode-1",
        sequence_no=0,
        operation="SUBMIT_EXIT",
        status="MAY_HAVE_SENT",
        revision=0,
    )
    values.update(overrides)
    connection.execute(BotControlAttempt.__table__.insert().values(**values))
    return values["attempt_id"]


def test_schema_has_only_three_control_tables_and_no_implicit_run(storage):
    engine, _, _ = storage
    with engine.connect() as connection:
        assert {
            name
            for name in inspect(connection).get_table_names()
            if name.startswith("bot_control_")
        } == TABLES
        assert connection.execute(select(BotControlState)).first() is None
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("bot_control_states")
        }
        for name in ("desired_state", "generation", "execution_mode"):
            assert columns[name]["default"] is None
            assert not columns[name]["nullable"]


@pytest.mark.parametrize("mode,generation", [("paper", 0), ("shadow", 0), ("live", -1)])
def test_state_rejects_paper_or_negative_generation(storage, mode, generation):
    engine, strategy, _ = storage
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            BotControlState.__table__.insert().values(
                strategy_id=strategy,
                execution_mode=mode,
                generation=generation,
                revision=0,
                desired_state="RUN",
            )
        )


def test_command_scope_requires_explicit_seed(storage):
    engine, strategy, _ = storage
    with pytest.raises(IntegrityError), engine.begin() as connection:
        command(connection, strategy)


@pytest.mark.parametrize("collision", ["request", "active_scope", "generation"])
def test_command_identity_and_single_active_scope_are_unique(storage, collision):
    engine, strategy, _ = storage
    request = uuid4()
    with engine.begin() as connection:
        seed(connection, strategy)
        command(connection, strategy, request_id=request, active=collision != "generation")
    overrides = dict(generation=2, request_id=uuid4())
    if collision == "request":
        overrides["request_id"] = request
    if collision == "generation":
        overrides.update(generation=1, active=False)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        command(connection, strategy, **overrides)


@pytest.mark.parametrize("status", ["PREPARED", "MAY_HAVE_SENT", "WORKING", "UNKNOWN"])
def test_unresolved_exit_owner_cannot_be_duplicated(storage, status):
    engine, strategy, account = storage
    with engine.begin() as connection:
        seed(connection, strategy)
        owner = command(connection, strategy)
        attempt(connection, strategy, account, owner, status=status)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        attempt(connection, strategy, account, owner, sequence_no=1)


def test_folded_binding_identity_and_unknown_quantities(storage):
    engine, strategy, account = storage
    with engine.begin() as connection:
        seed(connection, strategy)
        owner = command(connection, strategy)
        identity = attempt(
            connection, strategy, account, owner, operation="CANCEL_ENTRY", status="UNKNOWN"
        )
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(BotControlAttempt.__table__).where(
                    BotControlAttempt.attempt_id == identity,
                )
            )
            .mappings()
            .one()
        )
        assert row["requested_quantity"] is None
        assert row["remaining_observed"] is None
        assert row["actual_call_completed_at"] is None
    with pytest.raises(IntegrityError), engine.begin() as connection:
        attempt(connection, strategy, account, owner, operation="CANCEL_ENTRY", status="UNKNOWN")


def test_downgrade_preserves_command_history(storage):
    engine, strategy, _ = storage
    with engine.begin() as connection:
        seed(connection, strategy)
        command(connection, strategy, active=False, completed_at=datetime.now(UTC))
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        with pytest.raises(RuntimeError, match="history must be retained"):
            migration().downgrade()
        assert TABLES <= set(inspect(connection).get_table_names())


def test_actual_verified_parent_revision_is_pinned():
    assert migration().down_revision == "20261014_sec_country"
