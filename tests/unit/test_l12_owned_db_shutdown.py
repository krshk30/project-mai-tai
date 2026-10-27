"""Actual OMS DB thread/session lifecycle; CONTROLLED reads, no native proof."""

import asyncio
from contextlib import contextmanager
from threading import Event

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai import brokerdecides1_sync as sync
from project_mai_tai.brokerdecides1 import AdmissionMemory, decode
from project_mai_tai.oms.service import OmsRiskService
from tests.unit.test_brokerdecides1_sync import native, service


def controlled_world(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(engine)
    entered, release, exited = Event(), Event(), Event()

    @contextmanager
    def factory():
        try:
            with sessions() as session:
                yield session
        finally:
            exited.set()  # AFTER SQLAlchemy's session context has closed.

    async def reader(account):
        return native(account)

    owner = service(monkeypatch, reader)
    owner.session_factory = factory
    owner._run_db = lambda function, **kwargs: OmsRiskService._run_db(owner, function, **kwargs)

    def query(session, *args):
        assert session.scalar(text("SELECT 1")) == 1
        entered.set()
        if not release.wait(10):
            raise TimeoutError("CONTROLLED test release absent")
        assert session.scalar(text("SELECT 1")) == 1
        return frozenset(), frozenset(), frozenset()

    return owner, engine, entered, release, exited, query


async def started(event):
    assert await asyncio.to_thread(event.wait, 10)


async def dispose_after_workers(owner, engine, release):
    release.set()
    await asyncio.gather(*tuple(owner.__dict__.get("_brokerdecides1_db_tasks", ())),
        return_exceptions=True)
    await sync.shutdown_refresh(owner)
    assert owner._brokerdecides1_shutdown_joined
    engine.dispose()


@pytest.mark.asyncio
async def test_finish_cancellation_cannot_outlive_shutdown_into_engine_dispose(monkeypatch):
    owner, engine, entered, release, exited, query = controlled_world(monkeypatch)
    monkeypatch.setattr(sync, "local_evidence", query)
    generation = await sync.begin_pass(owner)
    waiter = asyncio.create_task(sync.finish_pass(owner, generation, sync_succeeded=True))
    try:
        await started(entered)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert not exited.is_set()
        assert len(owner._brokerdecides1_db_tasks) == 1
        shutdown = asyncio.create_task(sync.shutdown_refresh(owner))
        await asyncio.sleep(0)
        assert not shutdown.done() and not owner._brokerdecides1_shutdown_joined
        assert not decode(owner.redis.raw).local_complete
        release.set()
        assert await shutdown and exited.is_set()
        assert not owner._brokerdecides1_db_tasks
        assert not decode(owner.redis.raw).local_complete  # No orphan COMPLETE publish.
        restarted = AdmissionMemory()
        restarted.apply(decode(owner.redis.raw))
        assert not restarted.assess(accounts=sync.account_names(owner.settings), symbol="WORX",
            segment=900, now_ms=1000, max_age_ms=20000).allowed
    finally:
        await dispose_after_workers(owner, engine, release)
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_itself_cancels_tracked_finish_waiter_but_joins_thread(monkeypatch):
    owner, engine, entered, release, exited, query = controlled_world(monkeypatch)
    monkeypatch.setattr(sync, "local_evidence", query)
    generation = await sync.begin_pass(owner)
    waiter = asyncio.create_task(sync.finish_pass(owner, generation, sync_succeeded=True))
    try:
        await started(entered)
        shutdown = asyncio.create_task(sync.shutdown_refresh(owner))
        await asyncio.sleep(0)
        assert not shutdown.done() and not exited.is_set()
        release.set()
        assert await shutdown and exited.is_set()
        assert waiter.cancelled()
        assert not owner._brokerdecides1_pass_waiters
    finally:
        await dispose_after_workers(owner, engine, release)
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancellation_is_deferred_until_actual_session_exit(monkeypatch):
    owner, engine, entered, release, exited, query = controlled_world(monkeypatch)
    waiter = asyncio.create_task(sync._owned_db_delegate(owner,
        lambda: owner._run_db(query, commit=False)))
    try:
        await started(entered)
        shutdown = asyncio.create_task(sync.shutdown_refresh(owner))
        await asyncio.sleep(0)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done() and not exited.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert exited.is_set() and owner._brokerdecides1_shutdown_joined
        await waiter
    finally:
        await dispose_after_workers(owner, engine, release)
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_consumer_terminal_read_cancellation_also_retains_actual_worker(monkeypatch):
    from project_mai_tai import brokerdecides1_retirement as retirement

    owner, engine, entered, release, exited, query = controlled_world(monkeypatch)
    # Use real publish/decode and local memory. The terminal result is CONTROLLED.
    monkeypatch.setattr(sync, "local_evidence", lambda *_: (frozenset(), frozenset(), frozenset()))
    generation = await sync.begin_pass(owner)
    await sync.finish_pass(owner, generation, sync_succeeded=True)
    exited.clear()
    memory = owner.strategy._brokerdecides1_memory = AdmissionMemory()
    memory.apply(decode(owner.redis.raw))
    memory.reserve("YMAT", "CONTROLLED_PENDING")

    def terminal(session, *args):
        query(session)
        return (("YMAT", "CONTROLLED_PENDING"),)

    monkeypatch.setattr(retirement, "load_terminal_tokens", terminal)
    waiter = asyncio.create_task(sync.refresh_consumer(owner))
    try:
        await started(entered)
        shutdown = asyncio.create_task(sync.shutdown_refresh(owner))
        await asyncio.sleep(0)
        assert not shutdown.done() and not exited.is_set()
        assert memory.snapshot is None
        release.set()
        assert await shutdown and exited.is_set()
        assert memory.reservation_tokens() == {"CONTROLLED_PENDING"}
        assert waiter.cancelled()
    finally:
        await dispose_after_workers(owner, engine, release)
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_DB_error_closes_session_and_leaves_unknown_not_partial_complete(monkeypatch):
    owner, engine, entered, release, exited, query = controlled_world(monkeypatch)
    def failure(session, *args):
        query(session)
        raise ValueError("CONTROLLED_SQL_failure")
    monkeypatch.setattr(sync, "local_evidence", failure)
    release.set()
    try:
        generation = await sync.begin_pass(owner)
        await sync.finish_pass(owner, generation, sync_succeeded=True)
        assert exited.is_set() and not decode(owner.redis.raw).local_complete
        assert not owner._brokerdecides1_db_tasks
        assert await sync.shutdown_refresh(owner)
    finally:
        await dispose_after_workers(owner, engine, release)


@pytest.mark.asyncio
async def test_pending_orphan_DB_worker_prevents_another_native_refresh(monkeypatch):
    owner, engine, entered, release, exited, query = controlled_world(monkeypatch)
    waiter = asyncio.create_task(sync._owned_db_delegate(owner,
        lambda: owner._run_db(query, commit=False)))
    try:
        await started(entered)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        sync.schedule_refresh(owner)
        assert "_brokerdecides1_refresh_task" not in owner.__dict__
        assert not exited.is_set()
    finally:
        await dispose_after_workers(owner, engine, release)


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_rejects_new_delegates(monkeypatch):
    owner = service(monkeypatch)
    assert await sync.shutdown_refresh(owner)
    joined = owner._brokerdecides1_shutdown_task
    assert await sync.shutdown_refresh(owner)
    assert owner._brokerdecides1_shutdown_task is joined
    with pytest.raises(ValueError, match="shutdown_UNKNOWN"):
        await sync._owned_db_delegate(owner, lambda: asyncio.sleep(0))
    with pytest.raises(ValueError, match="shutdown_UNKNOWN"):
        await sync.begin_pass(owner)


@pytest.mark.asyncio
async def test_shutdown_owns_begin_publication_waiter_too(monkeypatch):
    owner = service(monkeypatch)
    entered, exited = asyncio.Event(), asyncio.Event()

    async def blocked_set(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    owner.redis.set = blocked_set
    begin = asyncio.create_task(sync.begin_pass(owner))
    await entered.wait()
    assert await sync.shutdown_refresh(owner)
    assert begin.cancelled() and exited.is_set()
    assert not owner._brokerdecides1_publisher.snapshot.local_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("during_join", [False, True])
async def test_external_delegate_cancellation_never_counts_as_actual_thread_join(monkeypatch, during_join):
    owner = service(monkeypatch)
    # CONTROLLED unknown delegate, no actual DB/worker may be disposed here.
    delegate = asyncio.create_task(asyncio.Event().wait())
    owner._brokerdecides1_db_tasks = {delegate}
    if not during_join:
        delegate.cancel()
        await asyncio.gather(delegate, return_exceptions=True)
    shutdown = asyncio.create_task(sync.shutdown_refresh(owner))
    if during_join:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        delegate.cancel()
    with pytest.raises(RuntimeError, match="UNKNOWN_NO_DISPOSE"):
        await shutdown
    assert not owner._brokerdecides1_shutdown_joined
    assert delegate in owner._brokerdecides1_db_tasks
    await asyncio.gather(delegate, return_exceptions=True)
