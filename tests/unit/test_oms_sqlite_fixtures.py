"""Thread/connection/teardown controls for the owned SQLite fixture helper."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from project_mai_tai.oms.service import OmsRiskService
from tests.unit.oms_sqlite_fixtures import OwnedSqlite, own_service, private_sqlite_engine, new_unit_service
from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite


def service(owner, *, settings=None):
    engine = owner.engine()
    subject = OmsRiskService.__new__(OmsRiskService)
    subject.settings = settings or SimpleNamespace()
    subject.session_factory = sessionmaker(engine)
    return owner.service(subject), engine


@pytest.mark.asyncio
async def test_independent_connections_real_thread_return_commit_and_rollback(owned_sqlite):
    subject, engine = service(owned_sqlite)
    assert isinstance(engine.pool, NullPool)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sample (value INTEGER)"))
    with engine.connect() as left, engine.connect() as right:
        assert left.connection.dbapi_connection is not right.connection.dbapi_connection
    loop_thread = get_ident()
    marker = ContextVar("owned_sqlite_marker", default="absent")
    token = marker.set("copied")
    try:
        def insert(session):
            assert get_ident() != loop_thread and marker.get() == "copied"
            session.execute(text("INSERT INTO sample VALUES (1)"))
            return ("actual", 17)
        assert await subject._run_db(insert) == ("actual", 17)
    finally:
        marker.reset(token)
    await subject._run_db(lambda session: session.execute(text("INSERT INTO sample VALUES (2)")), commit=False)
    error = ValueError("controlled exact failure")
    def fail(session):
        session.execute(text("INSERT INTO sample VALUES (3)"))
        raise error
    with pytest.raises(ValueError) as caught:
        await subject._run_db(fail)
    assert caught.value is error
    with engine.connect() as connection:
        assert connection.scalars(text("SELECT value FROM sample")).all() == [1]


@pytest.mark.asyncio
async def test_same_current_task_and_atr_call_identity_are_preserved(owned_sqlite, monkeypatch):
    from project_mai_tai.oms import webull_attach_riskoff as wb, wbexit2
    subject, _ = service(owned_sqlite, settings=SimpleNamespace(strategy_schwab_1m_v2_wbexit2_enabled=True))
    current = asyncio.current_task()
    call = SimpleNamespace(wb2=True, service=subject, task=current)
    token = wb._ATR_CALL.set(call)
    seen = []
    async def joined(owner, actual_call, work):
        assert asyncio.current_task() is current
        assert owner is subject and actual_call is call
        seen.append(actual_call)
        return await work
    monkeypatch.setattr(wbexit2, "joined", joined)
    try:
        assert await subject._run_db(lambda session: wb._ATR_CALL.get()) is call
        assert seen == [call]
    finally:
        wb._ATR_CALL.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("before_entry", [False, True])
async def test_cancelled_waiter_and_teardown_wait_for_actual_worker_exit(tmp_path, before_entry):
    owner = OwnedSqlite(tmp_path / "controlled-owner")
    subject, engine = service(owner)
    entered, release, exited = Event(), Event(), Event()
    disposed = []
    event.listen(engine, "engine_disposed", lambda *_: disposed.append(exited.is_set()))
    gate = Event()
    def worker(session):
        if before_entry:
            assert gate.wait(5)
        entered.set()
        try:
            assert release.wait(5)
            return session.scalar(text("SELECT 1"))
        finally:
            exited.set()
    task = asyncio.create_task(subject._run_db(worker))
    while not owner.futures:
        await asyncio.sleep(0)
    if not before_entry:
        assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(RuntimeError, match="before engine.dispose"):
        engine.dispose()
    teardown = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    assert not teardown.done() and not disposed and not exited.is_set()
    gate.set()
    assert await asyncio.to_thread(entered.wait, 5)
    release.set()
    await teardown
    assert exited.is_set() and disposed == [True]


@pytest.mark.asyncio
async def test_owned_followup_work_drains_without_adopting_foreign_task(owned_sqlite):
    subject, _ = service(owned_sqlite)
    hold = asyncio.Event()
    foreign = asyncio.create_task(hold.wait())
    trace = []
    async def followup():
        await subject._run_db(lambda session: trace.append(session.scalar(text("SELECT 1"))))
    async def first():
        await subject._run_db(lambda session: 0)
        subject._confirmation_exit_recovery_tasks.add(asyncio.create_task(followup()))
    subject._confirmation_exit_recovery_tasks = {asyncio.create_task(first())}
    try:
        await owned_sqlite.close()
        assert trace == [1] and not foreign.done()
        with pytest.raises(RuntimeError, match="after owned SQLite teardown"):
            await subject._run_db(lambda session: None)
    finally:
        hold.set()
        await foreign


@pytest.mark.asyncio
async def test_executor_probe_remains_visible_and_global_asyncio_unchanged(owned_sqlite, monkeypatch):
    subject, _ = service(owned_sqlite)
    loop = asyncio.get_running_loop()
    original = loop.run_in_executor
    to_thread = asyncio.to_thread
    seen = []
    def probe(executor, callback, *args):
        seen.append(executor)
        return original(executor, callback, *args)
    monkeypatch.setattr(loop, "run_in_executor", probe)
    assert await subject._run_db(lambda session: 31) == 31
    assert seen == [None] and asyncio.to_thread is to_thread


@pytest.mark.asyncio
async def test_fixture_context_supports_plain_factory_and_service_registration(owned_sqlite):
    engine = private_sqlite_engine(future=True)
    subject = OmsRiskService.__new__(OmsRiskService)
    subject.settings = SimpleNamespace()
    subject.session_factory = sessionmaker(engine)
    assert own_service(subject) is subject
    assert own_service(subject) is subject
    assert await subject._run_db(lambda session: session.scalar(text("SELECT 1"))) == 1


@pytest.mark.asyncio
async def test_cancel_before_executor_entry_retains_the_exact_queued_worker(tmp_path, monkeypatch):
    owner = OwnedSqlite(tmp_path / "queued-owner")
    subject, engine = service(owner)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    blocked, release, entered = Event(), Event(), Event()
    def occupy():
        blocked.set()
        assert release.wait(5)
    occupying = executor.submit(occupy)
    assert await asyncio.to_thread(blocked.wait, 5)
    original = loop.run_in_executor
    monkeypatch.setattr(loop, "run_in_executor", lambda _executor, callback, *args:
                        original(executor, callback, *args))
    task = asyncio.create_task(subject._run_db(lambda session: entered.set()))
    while not owner.futures:
        await asyncio.sleep(0)
    try:
        assert not entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError, match="before engine.dispose"):
            engine.dispose()
        teardown = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        assert not teardown.done() and not entered.is_set()
        release.set()
        await teardown
        assert entered.is_set() and occupying.done()
    finally:
        release.set()
        await owner.close()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_cancelled_teardown_still_joins_physical_session_before_disposal(tmp_path):
    owner = OwnedSqlite(tmp_path / "cancelled-teardown")
    subject, engine = service(owner)
    entered, release, exited = Event(), Event(), Event()
    disposed = []
    event.listen(engine, "engine_disposed", lambda *_: disposed.append(exited.is_set()))
    def worker(session):
        entered.set()
        try:
            assert release.wait(5)
            return session.scalar(text("SELECT 1"))
        finally:
            exited.set()
    work = asyncio.create_task(subject._run_db(worker))
    assert await asyncio.to_thread(entered.wait, 5)
    teardown = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    teardown.cancel()
    await asyncio.sleep(0)
    assert not teardown.done() and not disposed
    release.set()
    assert await work == 1
    with pytest.raises(asyncio.CancelledError):
        await teardown
    assert disposed == [True]


@pytest.mark.asyncio
async def test_closing_one_scope_never_adopts_another_owners_worker(tmp_path):
    left = OwnedSqlite(tmp_path / "left-owner")
    right = OwnedSqlite(tmp_path / "right-owner")
    subject, _ = service(right)
    entered, release = Event(), Event()
    def worker(session):
        entered.set()
        assert release.wait(5)
        return session.scalar(text("SELECT 1"))
    task = asyncio.create_task(subject._run_db(worker))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await left.close()
        assert left.phase == "disposed" and right.phase == "open" and not task.done()
        release.set()
        assert await task == 1
    finally:
        release.set()
        await right.close()


@pytest.mark.asyncio
async def test_cancel_during_writer_close_cannot_cancel_its_owned_worker(tmp_path):
    owner = OwnedSqlite(tmp_path / "writer-close")
    subject, engine = service(owner)
    entered, release = asyncio.Event(), asyncio.Event()
    trace = []
    class Writer:
        task = None
        async def close(self):
            entered.set()
            await release.wait()
            trace.append(await subject._run_db(lambda session: session.scalar(text("SELECT 1"))))
    writer = Writer()
    subject._webull_identity_writer = writer
    event.listen(engine, "engine_disposed", lambda *_: trace.append("disposed"))
    teardown = asyncio.create_task(owner.close())
    await entered.wait()
    teardown.cancel()
    await asyncio.sleep(0)
    assert not teardown.done() and trace == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await teardown
    assert trace == [1, "disposed"]


@pytest.mark.asyncio
async def test_unobserved_worker_failure_surfaces_only_after_actual_session_close(tmp_path):
    owner = OwnedSqlite(tmp_path / "unobserved-error")
    subject, engine = service(owner)
    sessions = subject.session_factory
    entered, release, session_closed = Event(), Event(), Event()
    @contextmanager
    def factory():
        try:
            with sessions() as session:
                yield session
        finally:
            session_closed.set()
    subject.session_factory = factory
    disposed = []
    event.listen(engine, "engine_disposed", lambda *_: disposed.append(session_closed.is_set()))
    error = ValueError("unobserved controlled worker error")
    def worker(session):
        session.scalar(text("SELECT 1"))
        entered.set()
        assert release.wait(5)
        raise error
    work = asyncio.create_task(subject._run_db(worker))
    assert await asyncio.to_thread(entered.wait, 5)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    teardown = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    assert not teardown.done() and not session_closed.is_set()
    release.set()
    with pytest.raises(BaseExceptionGroup) as caught:
        await teardown
    assert caught.value.exceptions == (error,)
    assert session_closed.is_set() and disposed == [True]


@pytest.mark.asyncio
async def test_teardown_yields_even_when_business_sleep_is_a_noop(tmp_path, monkeypatch):
    owner = OwnedSqlite(tmp_path / "business-sleep-stub")
    subject, _ = service(owner)
    trace = []
    async def no_sleep(delay):
        trace.append(("business_sleep", delay))
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await asyncio.sleep(0)
    subject._confirmation_exit_recovery_tasks = set()
    async def followup():
        trace.append(await subject._run_db(lambda session: session.scalar(text("SELECT 1"))))
    def later():
        subject._confirmation_exit_recovery_tasks.add(asyncio.create_task(followup()))
    asyncio.get_running_loop().call_soon(later)
    await owner.close()
    assert trace == [("business_sleep", 0), 1]
    assert asyncio.sleep is no_sleep


@pytest.mark.asyncio
async def test_existing_refresh_shutdown_joins_exact_native_delegate_without_cancelling_it(tmp_path):
    owner = OwnedSqlite(tmp_path / "native-delegate")
    subject, engine = service(owner)
    release = asyncio.Event()
    async def native():
        await release.wait()
        return "CONTROLLED native result"
    delegate = asyncio.create_task(native())
    subject._brokerdecides1_native_tasks = {delegate}
    teardown = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    assert not teardown.done() and not delegate.cancelled()
    release.set()
    await teardown
    assert delegate.result() == "CONTROLLED native result"
    assert subject._brokerdecides1_closing and subject._brokerdecides1_shutdown_joined


@pytest.mark.asyncio
async def test_externally_cancelled_delegate_still_refuses_disposal(tmp_path):
    owner = OwnedSqlite(tmp_path / "unknown-delegate")
    subject, engine = service(owner)
    delegate = asyncio.create_task(asyncio.Event().wait())
    delegate.cancel()
    await asyncio.gather(delegate, return_exceptions=True)
    subject._brokerdecides1_native_tasks = {delegate}
    disposed = []
    event.listen(engine, "engine_disposed", lambda *_: disposed.append(True))
    with pytest.raises(BaseExceptionGroup, match="join UNKNOWN"):
        await owner.close()
    assert not disposed and not subject._brokerdecides1_shutdown_joined
    with pytest.raises(RuntimeError, match="UNKNOWN_NO_DISPOSE"):
        engine.dispose()


@pytest.mark.asyncio
async def test_queued_symbol_worker_finishes_db_before_engine_disposal(tmp_path):
    owner = OwnedSqlite(tmp_path / "queued-symbol-worker")
    subject, engine = service(owner)
    admitted, release = asyncio.Event(), asyncio.Event()
    trace = []
    async def work():
        admitted.set()
        await release.wait()
        trace.append(await subject._run_db(lambda session: session.scalar(text("SELECT 1"))))
    event.listen(engine, "engine_disposed", lambda *_: trace.append("disposed"))
    assert subject._schedule_symbol_tick_work(("controlled", "SYM"), work)
    await admitted.wait()
    assert not owner.futures
    teardown = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    assert not teardown.done() and trace == []
    release.set()
    await teardown
    assert trace == [1, "disposed"]
    assert subject._symbol_tick_work_closing and not subject._symbol_tick_work
    assert not subject._schedule_symbol_tick_work(("controlled", "SYM"), work)


def test_inactive_scope_preserves_only_existing_file_nullpool_service(tmp_path, owned_sqlite):
    from tests.unit.oms_sqlite_fixtures import _ACTIVE
    engine = create_engine(f"sqlite:///{tmp_path / 'already-private.sqlite'}", poolclass=NullPool)
    subject = OmsRiskService.__new__(OmsRiskService)
    subject.session_factory = sessionmaker(engine)
    original = subject._run_db
    subject._run_db = original
    before = dict(subject.__dict__)
    asyncio_thread = asyncio.to_thread
    token = _ACTIVE.set(None)
    try:
        assert own_service(subject) is subject
        assert subject.__dict__ == before and subject._run_db is original
        assert not owned_sqlite.services and not owned_sqlite.futures and not owned_sqlite.engines
        assert asyncio.to_thread is asyncio_thread
    finally:
        _ACTIVE.reset(token)
        engine.dispose()


@pytest.mark.parametrize("kind", ["memory-null", "memory-static", "file-static", "empty-memory", "opaque", "missing"])
def test_inactive_scope_cannot_pass_memory_static_or_unproven_factory(tmp_path, kind):
    from tests.unit.oms_sqlite_fixtures import _ACTIVE
    subject = OmsRiskService.__new__(OmsRiskService)
    engine = None
    if kind == "opaque":
        subject.session_factory = lambda: None
    elif kind != "missing":
        url = f"sqlite:///{tmp_path / 'file.sqlite'}" if kind == "file-static" else (
            "sqlite://" if kind == "empty-memory" else "sqlite:///:memory:")
        engine = create_engine(url, poolclass=StaticPool if "static" in kind else NullPool)
        subject.session_factory = sessionmaker(engine)
    before = dict(subject.__dict__)
    token = _ACTIVE.set(None)
    try:
        with pytest.raises(RuntimeError, match="owned_sqlite required"):
            own_service(subject)
        assert subject.__dict__ == before
        with pytest.raises(RuntimeError, match="owned_sqlite fixture"):
            private_sqlite_engine()
    finally:
        _ACTIVE.reset(token)
        if engine is not None:
            engine.dispose()


def test_explicit_empty_constructor_outside_scope_has_no_existing_object_bypass():
    from tests.unit.oms_sqlite_fixtures import _ACTIVE
    function = OmsRiskService._run_db
    token = _ACTIVE.set(None)
    try:
        subject = new_unit_service()
        assert type(subject) is OmsRiskService and subject.__dict__ == {}
        assert subject._run_db.__func__ is function and OmsRiskService._run_db is function
    finally:
        _ACTIVE.reset(token)


@pytest.mark.asyncio
async def test_explicit_empty_constructor_inside_scope_tracks_later_db_initialization(owned_sqlite):
    subject = new_unit_service()
    assert type(subject) is OmsRiskService and owned_sqlite.services[id(subject)] is subject
    subject.settings = SimpleNamespace()
    subject.session_factory = sessionmaker(private_sqlite_engine())
    assert await subject._run_db(lambda session: session.scalar(text("SELECT 1"))) == 1
    assert len(owned_sqlite.futures) == 1
