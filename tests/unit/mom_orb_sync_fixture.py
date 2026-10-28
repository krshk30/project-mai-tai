"""Owned fixture lifecycle, not proof of production shutdown correctness.

NullPool prevents concurrent sessions from sharing/closing one SQLite connection.
The function-scoped test loop's actual executor is joined before engine disposal,
including a DB delegate whose cancelled asyncio waiter no longer owns its thread.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
import os
from threading import Lock

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from project_mai_tai.db.models import Base


@dataclass
class FixtureLifetime:
    services: list = field(default_factory=list)
    checked_out: set = field(default_factory=set)
    cleanup_errors: list = field(default_factory=list)
    lock: object = field(default_factory=Lock)
    drained: bool = True
    disposed: bool = False
    retained: bool = False


@contextmanager
def owned_database(tmp_path):
    path = tmp_path / 'mom-orb-shared-sync.sqlite'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    engine = create_engine(URL.create('sqlite+pysqlite', database=str(path)), future=True,
        connect_args={'check_same_thread': False}, poolclass=NullPool)
    state = FixtureLifetime()

    def checkout(connection, record, proxy):
        with state.lock:
            state.checked_out.add(id(record))

    def checkin(connection, record):
        with state.lock:
            state.checked_out.discard(id(record))

    event.listen(engine, 'checkout', checkout)
    event.listen(engine, 'checkin', checkin)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    factory.compat_lifetime = state
    try:
        Base.metadata.create_all(engine)
        yield factory
    finally:
        with state.lock:
            safe = state.drained and not state.checked_out
        if not safe:
            state.retained = True
            raise RuntimeError('owned_fixture_DB_retained_not_disposed_without_actual_drain')
        engine.dispose()
        state.disposed = True
        if state.cleanup_errors:
            raise RuntimeError('owned_fixture_cleanup_failed:' + ','.join(state.cleanup_errors))


async def await_native_publication(service):
    # Sync now schedules this task AFTER the required original read/publication.
    # Joining is test observation, never an await added to the production sync.
    task = service.__dict__.get('_brokerdecides1_refresh_task')
    if task is None:
        raise AssertionError('expected_actual_L12_background_refresh_task_missing')
    return await asyncio.shield(task)


async def drain_owned_services(factory):
    from project_mai_tai.brokerdecides1_sync import shutdown_refresh

    state = factory.compat_lifetime
    try:
        for service in state.services:
            task = service.__dict__.get('_brokerdecides1_refresh_task')
            if task is not None:
                try:
                    await asyncio.shield(task)
                except BaseException as exc:
                    state.cleanup_errors.append('refresh_' + type(exc).__name__)
            try:
                if await shutdown_refresh(service) is not True:
                    state.cleanup_errors.append('APP_shutdown_not_complete')
            except BaseException as exc:
                state.cleanup_errors.append('shutdown_' + type(exc).__name__)
    finally:
        # This fixture runs on a FUNCTION-scoped loop. Do not reuse it on a shared
        # module/session loop or substitute task cancellation for physical join.
        await asyncio.get_running_loop().shutdown_default_executor()
        with state.lock:
            state.drained = not state.checked_out
    if state.cleanup_errors:
        raise RuntimeError('owned_fixture_cleanup_failed:' + ','.join(state.cleanup_errors))


@asynccontextmanager
async def owned_service(factory, service):
    state = factory.compat_lifetime
    state.services.append(service)
    state.drained = False
    primary = None
    try:
        yield service
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            await drain_owned_services(factory)
        except BaseException:
            # Keep the original test failure; the DB fixture also exposes any
            # cleanup error after safe disposal (or retains an unjoined engine).
            if primary is None:
                raise
