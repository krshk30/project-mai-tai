"""The fanout test database must model independent concurrent OMS sessions."""

import asyncio
import gc
import threading
import weakref
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event, select

from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipStore
from project_mai_tai.v2_removed_wait import RemovedWait, RemovedWaitStore
from tests.unit.test_confirmation_exit_fanout import SCHWAB, SYMBOL, WEBULL, _FanoutAdapter, _make_sf, _service
from tests.unit.test_flye_prewire_consumer_contract import synthetic_opportunity


@pytest.mark.asyncio
async def test_actual_oms_parallel_reads_use_distinct_connections_and_retain_database():
    service, factory = _service(fanout=True, adapter=_FanoutAdapter())
    database = Path(factory.kw["bind"].url.database)
    retained = weakref.ref(factory)
    barrier = threading.Barrier(2)
    release = threading.Event()
    connections, threads = [], []
    lock = threading.Lock()

    def read(session, managed):
        with lock:
            connections.append(session.connection().connection.dbapi_connection)
            threads.append(threading.get_ident())
        barrier.wait(timeout=5)
        assert release.wait(5)
        if managed:
            row = service.store.get_open_managed_position(
                session, broker_account_name=WEBULL, symbol=SYMBOL)
        else:
            row = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == SYMBOL))
        assert row is not None
        return str(row.id)

    pending = [asyncio.create_task(service._run_db(lambda session, managed=managed: read(session, managed), commit=False))
               for managed in (True, False)]
    try:
        for _ in range(500):
            with lock:
                entered = len(connections) == 2
            if entered:
                break
            await asyncio.sleep(.01)
        assert entered, "the actual OMS workers did not overlap"
        assert len(set(threads)) == 2
        assert connections[0] is not connections[1]
        del factory
        gc.collect()
        assert retained() is not None and database.is_file()
    finally:
        release.set()
        await asyncio.gather(*pending)
    service = None
    pending.clear()
    connections.clear()
    gc.collect()
    assert retained() is None and not database.exists()


@pytest.mark.parametrize("kind", ["owner", "request"])
def test_snapshot_clock_must_order_transactions_not_random_uuids(request, kind):
    """Reproduce frozen-clock restore, without terminal tokens or a clearance proof."""
    factory = _make_sf()
    frozen = datetime(2026, 10, 8, 17, tzinfo=UTC)
    ids = iter(UUID(int=value) for value in (3, 2, 1))
    clock = [frozen]

    def timestamp(_mapper, _connection, row):
        if row.id is None:
            row.id = next(ids)
        if row.created_at is None:
            row.created_at = clock[0]

    event.listen(DashboardSnapshot, "before_insert", timestamp)
    request.addfinalizer(lambda: event.remove(DashboardSnapshot, "before_insert", timestamp))
    if kind == "owner":
        _, _, owner, _, _ = synthetic_opportunity(SCHWAB, False)
        store = FlipEntryOwnershipStore(factory)

        def record(active, at):
            store.record(owner, active=active, reason="clock control", now=at)

        def restore():
            return store.restore_active(now=frozen)
    else:
        barrier_request = RemovedWait("FLYE", int(frozen.timestamp() * 1000), "clock-control",
                                      int(frozen.timestamp() * 1000), (SCHWAB, WEBULL), "retry_exhausted")
        store = RemovedWaitStore(factory)

        def record(active, _at):
            store.record(barrier_request, active)

        restore = store.restore
    record(True, frozen)
    record(False, frozen)
    assert restore(), "equal timestamps must reproduce selection of the older, greater UUID"
    next_transaction = frozen + timedelta(microseconds=1)
    clock[0] = next_transaction
    record(False, next_transaction)
    assert not restore(), "a later transaction must dominate the UUID tie-breaker"
