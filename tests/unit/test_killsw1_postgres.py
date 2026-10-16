"""Real PG16 locking proofs; never use the application/production DSN."""

import importlib.util
import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import (
    Base,
    BotControlState,
    BrokerAccount,
    BrokerOrder,
    Strategy,
    TradeIntent,
)
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.momentum_live.persistence import LivePersistence
from project_mai_tai.oms.bot_control import BotControlStore, ControlConflict
from project_mai_tai.oms.bot_control_runtime import BotControlRuntime
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore

CONTROL_TABLES = {"bot_control_states", "bot_control_commands", "bot_control_attempts"}


@pytest.fixture
def pg():
    dsn = os.environ.get("KILLSW1_TEST_PG_DSN")
    if not dsn:
        pytest.skip(
            "explicit disposable KILLSW1_TEST_PG_DSN required; PG proof remains unqualified"
        )
    url = make_url(dsn)
    host = url.query.get("host", url.host or "")
    ci_lab = (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and host == "127.0.0.1"
        and url.database == "project_mai_tai_test"
        and url.port == 5432
    )
    assert (
        url.get_backend_name() == "postgresql"
        and url.port is not None
        and (url.port != 5432 or ci_lab)
        and (
            host in {"127.0.0.1", "localhost", "::1"} or host.startswith(("/tmp/", "/private/tmp/"))
        )
    )
    admin = create_engine(url)
    schema = "killsw1_" + uuid4().hex
    with admin.begin() as connection:
        assert 160000 <= int(connection.scalar(text("SHOW server_version_num"))) < 170000
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-csearch_path={schema} -cstatement_timeout=8000"}
    )
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(
                connection,
                tables=[
                    table
                    for name, table in Base.metadata.tables.items()
                    if name not in CONTROL_TABLES
                ],
            )
            path = (
                Path(__file__).resolve().parents[2]
                / "sql/migrations/versions/20261015_0025_bot_control.py"
            )
            spec = importlib.util.spec_from_file_location("killsw1_pg_migration", path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            assert migration.down_revision == "20261014_sec_country"
            before = set(inspect(connection).get_table_names())
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert set(inspect(connection).get_table_names()) - before == CONTROL_TABLES
        sessions = sessionmaker(engine, expire_on_commit=False)
        strategy, account, order = uuid4(), uuid4(), uuid4()
        with sessions.begin() as session:
            session.add(
                Strategy(id=strategy, code="momentum_warn_live", name="MOM", execution_mode="live")
            )
            session.add(
                BrokerAccount(id=account, name="live:orb", environment="live", provider="webull")
            )
            session.flush()
            session.add(
                BrokerOrder(
                    id=order,
                    strategy_id=strategy,
                    broker_account_id=account,
                    client_order_id="owned",
                    symbol="ABC",
                    side="buy",
                    quantity=10,
                    order_type="limit",
                    time_in_force="day",
                    status="pending",
                )
            )
        store = BotControlStore(sessions)
        store.seed(strategy)
        yield engine, sessions, store, strategy, account, order
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def stop(store, strategy, account):
    return store.request(
        strategy_id=strategy,
        command_id=uuid4(),
        request_id=uuid4(),
        expected_generation=0,
        action="STOP",
        account_ids=(account,),
        actor_reference="operator",
        reason="PG barrier proof",
    )


def test_actual_postgres_state_lock_blocks_stop_until_holder_commits(pg):
    engine, sessions, store, strategy, account, order = pg
    started = Event()
    waiter = {}
    actual_lock = store._lock

    def traced_lock(session, strategy_id):
        waiter["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
        started.set()
        return actual_lock(session, strategy_id)

    store._lock = traced_lock
    with sessions.begin() as holder:
        holder.execute(
            select(BotControlState)
            .where(
                BotControlState.strategy_id == strategy,
            )
            .with_for_update()
        ).scalar_one()
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        with ThreadPoolExecutor(max_workers=1) as worker:
            # Submit/observe/release inside one explicit barrier, not a sleep-
            # based assertion that a thread merely had not been scheduled yet.
            def waiting_stop():
                return stop(store, strategy, account)

            future = worker.submit(waiting_stop)
            try:
                assert started.wait(2)
                deadline, blocked = time.monotonic() + 4, False
                with engine.connect() as observer:
                    while time.monotonic() < deadline:
                        blocked = observer.scalar(
                            text("SELECT :holder = ANY(pg_blocking_pids(:waiter))"),
                            {"holder": holder_pid, "waiter": waiter["pid"]},
                        )
                        if blocked:
                            break
                    assert blocked, "PostgreSQL did not observe the row-lock waiter"
                assert not future.done()
            finally:
                holder.commit()
            receipt = future.result(timeout=6)
    assert receipt.generation == 1
    with pytest.raises(ControlConflict, match="entry_fenced"):
        store.authorize_entry(order_id=order, strategy_id=strategy, expected_generation=0)


def test_committed_permit_remains_uncertain_after_stop(pg):
    _, sessions, store, strategy, account, order = pg
    store.authorize_entry(order_id=order, strategy_id=strategy, expected_generation=0)
    stop(store, strategy, account)
    with sessions() as session:
        owned = session.get(BrokerOrder, order)
        assert "MAY_HAVE_SENT" in owned.payload["bot_control_permit"]
        assert owned.status == "pending"
    assert not store.prove_unsent_entry(
        order_id=order, strategy_id=strategy, account_id=account, symbol="ABC"
    )


def shared_admission(pg, code, provider):
    engine, sessions, control_store, _, account_id, _ = pg
    name = "live:orb" if provider == "webull" else "live:schwab_1m_v2"
    strategy_id = uuid4()
    with sessions.begin() as unit:
        account = unit.get(BrokerAccount, account_id)
        account.name, account.provider = name, provider
        unit.add(Strategy(id=strategy_id, code=code, name=code, execution_mode="live"))
    control_store.seed(strategy_id)
    service = OmsRiskService.__new__(OmsRiskService)
    service.session_factory, service.store = sessions, OmsStore()
    service.logger = logging.getLogger("controlled-pg-admission")
    service._bot_control_runtime = BotControlRuntime(sessions)
    asyncio.run(service._bot_control_runtime.refresh())
    return service, strategy_id, account_id, name, engine, sessions


def pending_parent(unit, strategy_id, account_id):
    parent = TradeIntent(
        id=uuid4(),
        strategy_id=strategy_id,
        broker_account_id=account_id,
        symbol="MIX",
        side="buy",
        intent_type="open",
        quantity=Decimal(10),
        reason="controlled-admission-race",
        status="pending",
        payload={},
    )
    unit.add(parent)
    unit.flush()
    return parent


def admission_request(code, name):
    return OrderRequest(
        client_order_id=f"controlled-{uuid4()}",
        strategy_code=code,
        broker_account_name=name,
        symbol="MIX",
        side="buy",
        intent_type="open",
        quantity=Decimal(10),
        reason="controlled-admission-race",
        order_type="limit",
        time_in_force="day",
        metadata={"limit_price": "3"},
    )


@pytest.mark.parametrize("code", ["schwab_1m_v2", "orb_schwab"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_committed_atr_or_orb_parent_fences_mom_during_pre_wire_gap(pg, code, provider):
    service, strategy, account, name, _, sessions = shared_admission(pg, code, provider)
    live = LivePersistence(service.store)
    request = admission_request(code, name)
    with sessions() as original:
        parent = pending_parent(original, strategy, account)
        original.commit()  # Actual caller commit gap: no broker order exists yet.
        with sessions.begin() as contender:
            assert contender.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "MIX")) is None
            with pytest.raises(Refusal, match="foreign_account_symbol_owner"):
                live.claim(
                    contender,
                    episode={"episode_id": str(uuid4()), "symbol": "MIX", "state": "claimed"},
                    account=name,
                    provider=provider,
                    signal={},
                )
        asyncio.run(
            service._prepare_bot_control_entry(original, parent, request, datetime.now(UTC))
        )
    with sessions() as unit:
        rows = unit.scalars(select(BrokerOrder).where(BrokerOrder.symbol == "MIX")).all()
        assert len(rows) == 1 and rows[0].strategy_id == strategy
        assert rows[0].client_order_id == request.client_order_id
        assert rows[0].submitted_at is None and rows[0].status == "pending"
        assert "bot_control_permit" not in rows[0].payload


@pytest.mark.parametrize("code", ["schwab_1m_v2", "orb_schwab"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_mom_reservation_serializes_real_atr_or_orb_pre_wire_and_refuses_second_owner(
    pg, code, provider
):
    service, strategy, account, name, engine, sessions = shared_admission(pg, code, provider)
    live = LivePersistence(service.store)
    request = admission_request(code, name)
    started = Event()

    def competing_prepare():
        with sessions() as unit:
            parent = pending_parent(unit, strategy, account)
            started.set()
            asyncio.run(
                service._prepare_bot_control_entry(unit, parent, request, datetime.now(UTC))
            )

    with sessions.begin() as holder:
        assert live.claim(
            holder,
            episode={"episode_id": str(uuid4()), "symbol": "MIX", "state": "claimed"},
            account=name,
            provider=provider,
            signal={},
        )
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(competing_prepare)
            try:
                assert started.wait(2)
                deadline, waiting_pid = time.monotonic() + 4, None
                with engine.connect() as observer:
                    while time.monotonic() < deadline:
                        waiting_pid = observer.scalar(
                            text(
                                "SELECT pid FROM pg_stat_activity WHERE datname=current_database() "
                                "AND :holder = ANY(pg_blocking_pids(pid)) LIMIT 1"
                            ),
                            {"holder": holder_pid},
                        )
                        if waiting_pid is not None:
                            break
                assert waiting_pid is not None, "No actual PostgreSQL advisory-lock waiter"
                assert not future.done()
            finally:
                holder.commit()
            with pytest.raises(RuntimeError, match="foreign_owner_before_wire_preparation"):
                future.result(timeout=6)
    with sessions() as unit:
        assert unit.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "MIX")) is None
