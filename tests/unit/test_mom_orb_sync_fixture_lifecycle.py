"""Fixture mechanics, not a replacement for Arendt's real APP shutdown controls."""
import asyncio
import os
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from tests.unit.mom_orb_sync_fixture import await_native_publication, owned_database, owned_service


def test_private_file_pool_never_shares_one_SQLite_connection(tmp_path):
    with owned_database(tmp_path) as factory:
        engine = factory.kw['bind']
        assert isinstance(engine.pool, NullPool)
        assert os.stat(engine.url.database).st_mode & 0o777 == 0o600
        with engine.connect() as first, engine.connect() as second:
            assert first.connection.driver_connection is not second.connection.driver_connection
            assert first.scalar(text('SELECT count(*) FROM broker_orders')) == 0
            assert second.scalar(text('SELECT count(*) FROM broker_orders')) == 0
        state = factory.compat_lifetime
        assert not state.checked_out
    assert state.disposed and not state.retained


def test_open_connection_refuses_disposal_and_retains_own_database(tmp_path):
    connection = engine = None
    try:
        with pytest.raises(RuntimeError, match='retained_not_disposed_without_actual_drain'):
            with owned_database(tmp_path) as factory:
                engine = factory.kw['bind']
                state = factory.compat_lifetime
                connection = engine.connect()
                assert connection.scalar(text('SELECT 1')) == 1
        assert state.retained and not state.disposed
        assert connection.scalar(text('SELECT 1')) == 1
    finally:
        if connection is not None:
            connection.close()
        if engine is not None:
            engine.dispose()  # Own connection is now closed; never dispose while in use.


@pytest.mark.asyncio(loop_scope='function')
async def test_publication_observer_waits_for_actual_task_completion():
    gate, observed = asyncio.Event(), []

    async def publish():
        await gate.wait()
        observed.append('published')
        return 'actual-result'

    service = SimpleNamespace(_brokerdecides1_refresh_task=asyncio.create_task(publish()))
    waiting = asyncio.create_task(await_native_publication(service))
    try:
        await asyncio.sleep(0)
        assert not waiting.done() and observed == []
        gate.set()
        assert await waiting == 'actual-result' and observed == ['published']
    finally:
        gate.set()
        await asyncio.gather(service._brokerdecides1_refresh_task, waiting, return_exceptions=True)


@pytest.mark.asyncio(loop_scope='function')
async def test_cancelled_waiter_does_not_allow_disposal_before_real_SQL_delegate_finishes(tmp_path):
    started, release, finished = Event(), Event(), Event()
    state = None
    try:
        with pytest.raises(RuntimeError, match='refresh_CancelledError'):
            with owned_database(tmp_path) as factory:
                state = factory.compat_lifetime

                def original_db_delegate():
                    with factory() as session:
                        assert session.scalar(text('SELECT 1')) == 1
                        started.set()
                        assert release.wait(5), 'CONTROLLED lifecycle release missing'
                        assert not state.disposed
                        assert session.scalar(text('SELECT count(*) FROM broker_orders')) == 0
                    finished.set()

                task = asyncio.create_task(asyncio.to_thread(original_db_delegate))
                service = SimpleNamespace(_brokerdecides1_refresh_task=task)
                async with owned_service(factory, service):
                    assert await asyncio.to_thread(started.wait, 5)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    assert not finished.is_set() and not state.disposed
                    asyncio.get_running_loop().call_later(.01, release.set)
        assert finished.is_set() and state.drained and state.disposed and not state.retained
        assert state.cleanup_errors == ['refresh_CancelledError']
    finally:
        release.set()


@pytest.mark.asyncio(loop_scope='function')
async def test_original_assertion_survives_service_join_and_safe_disposal(tmp_path):
    completed = []
    with pytest.raises(AssertionError, match='CONTROLLED primary assertion'):
        with owned_database(tmp_path) as factory:
            state = factory.compat_lifetime

            async def original_refresh():
                def original_db_delegate():
                    with factory() as session:
                        completed.append(session.scalar(text('SELECT 1')))
                await asyncio.to_thread(original_db_delegate)

            service = SimpleNamespace(_brokerdecides1_refresh_task=asyncio.create_task(original_refresh()))
            async with owned_service(factory, service):
                raise AssertionError('CONTROLLED primary assertion')
    assert completed == [1] and state.drained and state.disposed and not state.cleanup_errors
