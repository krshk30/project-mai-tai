"""Private SQLite connections and owned OMS worker teardown for tests only."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, copy_context
from functools import partial, wraps
from pathlib import Path
from types import FunctionType, MethodType

import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool


_ACTIVE: ContextVar[OwnedSqlite | None] = ContextVar("owned_oms_sqlite", default=None)
_TO_THREAD = asyncio.to_thread
_SLEEP = asyncio.sleep
_TASK_SETS = (
    "_confirmation_exit_recovery_tasks",
    "_cancel_verify_tasks",
    "_webull_protect_tasks",
    "_brokerdecides1_db_tasks",
    "_brokerdecides1_native_tasks",
    "_brokerdecides1_pass_waiters",
)


class OwnedSqlite:
    """Own only explicitly registered engines, services and their DB futures."""

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(mode=0o700)
        self.engines = []
        self.services = {}
        self.futures = {}
        self.observed = set()
        self.phase = "open"

    def engine(self, *, database_path=None, **kwargs):
        if self.phase not in {"open", "draining"}:
            raise RuntimeError("owned SQLite scope already disposed")
        path = self.directory / f"db-{len(self.engines)}.sqlite" if database_path is None else Path(database_path)
        if not path.resolve().is_relative_to(self.directory.parent.resolve()):
            raise ValueError("SQLite fixture path must stay inside its owned test directory")
        connect_args = {"check_same_thread": False, **kwargs.pop("connect_args", {})}
        engine = create_engine(
            f"sqlite+pysqlite:///{path}", poolclass=NullPool,
            connect_args=connect_args, **kwargs,
        )
        dispose = engine.dispose
        self.engines.append((engine, dispose))
        def dispose_when_joined(close=True):
            if self.phase == "join_unknown":
                raise RuntimeError("owned delegate join UNKNOWN_NO_DISPOSE")
            if self.phase == "disposed":
                return
            if self._pending():
                raise RuntimeError("join owned OMS workers before engine.dispose")
            return dispose(close=close)
        # Preserve actual disposal when existing shutdown guards have joined;
        # fail visibly instead of masking an early dispose with a no-op.
        engine.dispose = dispose_when_joined
        return engine

    def service(self, service, *, run_db=None):
        if self.phase not in {"open", "draining"}:
            raise RuntimeError("owned SQLite scope already disposed")
        if id(service) in self.services:
            return service
        self.services[id(service)] = service
        original = service._run_db if run_db is None else MethodType(run_db, service)
        function = original.__func__
        proxy = _AsyncioProxy(self, service)

        @wraps(original)
        async def owned_run_db(fn, *, commit=True):
            if self.phase not in {"open", "draining"}:
                raise RuntimeError("DB work after owned SQLite teardown")
            # Snapshot globals at invocation, preserving any test's service
            # globals/probes installed after construction. No APP/module global
            # or asyncio attribute is replaced. The real code and current task
            # remain identical, including _ATR_CALL's WBEXIT owner check.
            namespace = {**function.__globals__, "asyncio": proxy}
            cloned = FunctionType(function.__code__, namespace, function.__name__,
                                  function.__defaults__, function.__closure__)
            assert cloned.__code__ is function.__code__
            return await MethodType(cloned, service)(fn, commit=commit)

        service._run_db = owned_run_db
        return service

    def _pending(self):
        tasks = {future for future in self.futures if not future.done()}
        for service in self.services.values():
            for name in _TASK_SETS:
                tasks.update(task for task in service.__dict__.get(name, ()) if not task.done())
            writer = service.__dict__.get("_webull_identity_writer")
            task = getattr(writer, "task", None)
            if task is not None and not task.done():
                tasks.add(task)
            refresh = service.__dict__.get("_brokerdecides1_refresh_task")
            if refresh is not None and not refresh.done():
                tasks.add(refresh)
            tasks.update(task for task in service.__dict__.get("_symbol_tick_work", {}).values()
                         if not task.done())
        return tasks

    async def close(self):
        if self.phase == "disposed":
            return
        self.phase = "draining"
        cancelled = False
        errors = {}
        closed_writers = set()
        closed_refreshes = set()
        closed_symbol_workers = set()
        joined_pools = set()
        join_unknown = False

        def remember(error):
            nonlocal join_unknown
            if isinstance(error, BaseException) and not isinstance(error, asyncio.CancelledError):
                errors[id(error)] = error
                if isinstance(error, RuntimeError) and "UNKNOWN_NO_DISPOSE" in str(error):
                    join_unknown = True

        async def settle(future):
            nonlocal cancelled
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException:
                    break
            if future.cancelled():
                return None
            try:
                return future.result()
            except BaseException as error:
                remember(error)
                return None

        quiet_turns = 0
        while quiet_turns < 2:
            for service in tuple(self.services.values()):
                if id(service) not in closed_symbol_workers and "_symbol_tick_work" in service.__dict__:
                    closed_symbol_workers.add(id(service))
                    admitted = tuple(service._symbol_tick_work.values())
                    await settle(asyncio.create_task(service._shutdown_symbol_tick_work()))
                    for task in admitted:
                        if task.done() and not task.cancelled():
                            remember(task.exception())
                refresh_fields = ("_brokerdecides1_refresh_task", "_brokerdecides1_pass_waiters",
                                  "_brokerdecides1_db_tasks", "_brokerdecides1_native_tasks")
                if id(service) not in closed_refreshes and any(key in service.__dict__ for key in refresh_fields):
                    from project_mai_tai.brokerdecides1_sync import shutdown_refresh
                    closed_refreshes.add(id(service))
                    await settle(asyncio.create_task(shutdown_refresh(service)))
                writer = service.__dict__.get("_webull_identity_writer")
                if writer is not None and id(writer) not in closed_writers:
                    closed_writers.add(id(writer))
                    await settle(asyncio.create_task(writer.close()))
            pending = self._pending()
            if pending:
                quiet_turns = 0
                # Cancelling a waiter is not evidence that its physical thread
                # has exited. Shield every exact owned future through teardown.
                selected = tuple(pending)
                joined = asyncio.gather(*(asyncio.shield(task) for task in selected),
                                        return_exceptions=True)
                results = await settle(joined)
                for task, result in zip(selected, results or (), strict=True):
                    if isinstance(task, asyncio.Task):
                        remember(result)
            else:
                quiet_turns += 1
            # Service work may register follow-up work when its first DB future
            # settles. Reach a fixed point rather than taking one task snapshot.
            try:
                await _SLEEP(0)
            except asyncio.CancelledError:
                cancelled = True
        for service in tuple(self.services.values()):
            writer = service.__dict__.get("_webull_identity_writer")
            pool = getattr(writer, "pool", None)
            if pool is not None and id(pool) not in joined_pools:
                joined_pools.add(id(pool))
                # This exact writer owns this executor. Its close() may use
                # shutdown(wait=False), so also prove physical exit explicitly.
                future = asyncio.get_running_loop().run_in_executor(None, partial(pool.shutdown, wait=True))
                await settle(future)
        for future in self.futures:
            if future not in self.observed and not future.cancelled():
                remember(future.exception())
        if join_unknown:
            self.phase = "join_unknown"
            raise BaseExceptionGroup("owned delegate join UNKNOWN; engines not disposed", list(errors.values()))
        self.phase = "disposed"
        for _engine, dispose in self.engines:
            try:
                dispose()
            except BaseException as error:
                remember(error)
        self.engines.clear()
        self.services.clear()
        self.futures.clear()
        self.observed.clear()
        if errors:
            raise BaseExceptionGroup("unexpected owned OMS background failure after join", list(errors.values()))
        if cancelled:
            raise asyncio.CancelledError()


class _AsyncioProxy:
    def __init__(self, owner, service):
        self.owner, self.service = owner, service

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def to_thread(self, function, /, *args, **kwargs):
        if asyncio.to_thread is not _TO_THREAD:
            raise RuntimeError("owned OMS fixture requires explicit handling of patched to_thread")
        # Same context propagation and executor call as asyncio.to_thread.
        # Shield retains the physical worker if its caller is cancelled before
        # worker entry, without creating a new task around the real _run_db.
        context = copy_context()
        future = asyncio.get_running_loop().run_in_executor(
            None, partial(context.run, function, *args, **kwargs))
        self.owner.futures[future] = self.service
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            raise
        except BaseException:
            self.owner.observed.add(future)
            raise
        self.owner.observed.add(future)
        return result


def private_sqlite_engine(**kwargs):
    owner = _ACTIVE.get()
    if owner is None:
        raise RuntimeError("import the owned_sqlite fixture in this test module")
    return owner.engine(**kwargs)


def private_session_factory():
    """Exact full-Base/expire_on_commit=False pure-provider fixture shape."""
    from project_mai_tai.db.base import Base
    engine = private_sqlite_engine(future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def own_service(service, *, run_db=None):
    owner = _ACTIVE.get()
    if owner is None:
        sessions = service.__dict__.get("session_factory")
        engine = sessions.kw.get("bind") if isinstance(sessions, sessionmaker) else None
        database = engine.url.database if isinstance(engine, Engine) else None
        if (run_db is None and isinstance(engine, Engine) and engine.dialect.name == "sqlite"
                and isinstance(engine.pool, NullPool) and database
                and ":memory:" not in database.lower() and "mode=memory" not in database.lower()
                and str(engine.url.query.get("mode", "")).lower() != "memory"):
            # Existing independently connected file fixtures outside this class
            # remain byte-for-byte unmodified. An opaque factory or memory DB
            # cannot acquire this exception by lacking an active scope.
            return service
        raise RuntimeError("owned_sqlite required for memory, opaque or unconfigured service")
    return owner.service(service, run_db=run_db)


def new_unit_service():
    """Exact empty constructor seam; no arbitrary existing service is accepted."""
    from project_mai_tai.oms.service import OmsRiskService
    service = OmsRiskService.__new__(OmsRiskService)
    owner = _ACTIVE.get()
    return service if owner is None else owner.service(service)


@pytest_asyncio.fixture(autouse=True)
async def owned_sqlite(tmp_path):
    owner = OwnedSqlite(tmp_path / "owned-oms-sqlite")
    token = _ACTIVE.set(owner)
    try:
        yield owner
    finally:
        try:
            await owner.close()
        finally:
            _ACTIVE.reset(token)
