"""Mendel's three exact-head findings, controlled locally without broker traffic."""
import asyncio
from decimal import Decimal
from threading import Event, Thread, get_ident
from time import monotonic, sleep

import pytest
from sqlalchemy import event, inspect, select

from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
from project_mai_tai.db.models import OmsManagedPosition
from tests.unit.managed_entry_fixtures import bind_managed_entry
from tests.unit.test_edgeprot1_lane_b import drain, quote, setup
from tests.unit.test_edgeprot1_target_cache import row
from tests.unit.test_v2_cw_managed_exit import ACCT, SYM, WEBULL_ACCT


def create(svc, session, account, symbol):
    value = svc.store.create_managed_position(session, strategy_code="schwab_1m_v2",
        broker_account_name=account, symbol=symbol, entry_price=Decimal("2.49"),
        quantity=120, entry_path="ATR Flip")
    bind_managed_entry(session, value)
    session.flush()
    return value


@pytest.mark.timing
@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
async def test_post_reservation_target_read_is_offloop_and_fresh(monkeypatch, account):
    svc, sf, broker = setup(monkeypatch, account, session="PM")
    svc.settings.oms_v2_exit_release_reservation_enabled = True
    released = False
    loop_thread, read_threads, sessions = get_ident(), [], []
    def record_session(state):
        if state.is_select:
            sessions.append((state.session, get_ident(), released))
    event.listen(sf, "do_orm_execute", record_session)
    async def release(**kwargs):
        nonlocal released
        released = True
        return ExitPairReleaseResult(outcome="no_reservation")
    monkeypatch.setattr(svc, "_release_exit_reservation_before_close", release)
    def delay(connection, cursor, statement, parameters, context, executemany):
        if released and statement.lstrip().upper().startswith("SELECT") and (
            "FROM oms_managed_positions" in statement
        ):
            read_threads.append(get_ident())
            sleep(.1)
    engine = sf.kw["bind"]
    event.listen(engine, "before_cursor_execute", delay)
    stopped, stalls = asyncio.Event(), []
    async def meter():
        previous = monotonic()
        while not stopped.is_set():
            await asyncio.sleep(.001)
            now = monotonic()
            stalls.append((now - previous - .001) * 1000)
            previous = now
    probe = asyncio.create_task(meter())
    await asyncio.sleep(.002)
    try:
        await svc._handle_quote_tick_event(quote("2.64"))
        await drain(svc)
        await asyncio.sleep(.002)
    finally:
        stopped.set()
        await probe
        event.remove(engine, "before_cursor_execute", delay)
        event.remove(sf, "do_orm_execute", record_session)
    request, = broker.requests
    assert request.order_type == "limit" and request.metadata["limit_price"] == "2.62"
    print(f"POST_RESERVATION account={account} stall_ms={max(stalls):.3f} reads={len(read_threads)}")
    assert max(stalls) < 50
    assert read_threads and loop_thread not in read_threads
    loop_sessions = {id(session) for session, thread, _ in sessions if thread == loop_thread}
    worker_sessions = {id(session) for session, thread, after in sessions if thread != loop_thread and after}
    assert worker_sessions and not worker_sessions.intersection(loop_sessions)


@pytest.mark.parametrize("outer_commit", [False, True])
def test_savepoint_release_does_not_publish_before_outer_resolution(monkeypatch, outer_commit):
    svc, sf, _ = setup(monkeypatch)
    key = (ACCT, "NESTED")
    with sf() as session:
        created = create(svc, session, *key)
        created_id = created.id
        with session.begin_nested():
            pass
        before_resolution = svc._target_row_cache.get(key)
        session.commit() if outer_commit else session.rollback()
        with sf() as read:
            assert (read.scalar(select(OmsManagedPosition.id).where(
                OmsManagedPosition.id == created_id)) is not None) == outer_commit
        assert before_resolution is None
        assert (svc._target_row_cache.get(key) is not None) == outer_commit


def test_inner_rollback_preserves_outer_create_and_discards_inner_create(monkeypatch):
    svc, sf, _ = setup(monkeypatch)
    with sf() as session:
        outer = create(svc, session, ACCT, "OUTER")
        nested = session.begin_nested()
        create(svc, session, ACCT, "INNER")
        outer.entry_price = Decimal("2.50")
        session.flush()
        nested.rollback()
        session.commit()
    bound = svc._target_row_cache.get((ACCT, "OUTER"))
    assert bound is not None and bound.entry_price == Decimal("2.49")
    assert svc._target_row_cache.get((ACCT, "INNER")) is None
    with sf() as read:
        assert read.scalar(select(OmsManagedPosition.id).where(OmsManagedPosition.symbol == "INNER")) is None


@pytest.mark.parametrize("restore_quantity", [False, True])
def test_delayed_loaded_callback_cannot_undo_later_commit_invalidation(monkeypatch, restore_quantity):
    svc, sf, _ = setup(monkeypatch)
    cache = svc._target_row_cache
    entered, release, failures = Event(), Event(), []
    publish = cache._publish
    def paused(key, value, *args, **kwargs):
        if get_ident() == worker.ident and key == (ACCT, SYM) and value.quantity == 120:
            entered.set()
            assert release.wait(2)
        return publish(key, value, *args, **kwargs)
    monkeypatch.setattr(cache, "_publish", paused)
    def reader():
        try:
            with sf() as session:
                assert row(session).current_quantity == 120
        except BaseException as error:
            failures.append(error)
    worker = Thread(target=reader)
    worker.start()
    try:
        assert entered.wait(2)
        with sf.begin() as session:
            existing = row(session)
            existing.current_quantity = 119
        if restore_quantity:
            with sf.begin() as session:
                row(session).current_quantity = 120
        assert cache.get((ACCT, SYM)) is None
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and not failures
    assert cache.get((ACCT, SYM)) is None
    expected = 120 if restore_quantity else 119
    with sf() as session:
        assert session.scalar(select(OmsManagedPosition.current_quantity)) == expected
    with sf() as session:
        assert row(session).current_quantity == expected
    assert cache.get((ACCT, SYM)).quantity == expected


def test_lazy_result_uses_its_own_dispatch_fence_not_later_session_read(monkeypatch):
    svc, sf, _ = setup(monkeypatch)
    cache = svc._target_row_cache
    with sf() as reader:
        old_result = reader.execute(select(OmsManagedPosition))
        with sf.begin() as writer:
            row(writer).current_quantity = 119
        # This newer dispatch must not replace the earlier result's fence.
        assert reader.scalar(select(OmsManagedPosition.current_quantity)) == 119
        assert old_result.scalar_one().current_quantity == 120
        assert cache.get((ACCT, SYM)) is None
    svc._rehydrate_managed_v2_symbols()
    assert cache.get((ACCT, SYM)).quantity == 119


@pytest.mark.parametrize("rollback_level", [1, 2, 0])
def test_nested_frames_preserve_exact_surviving_creates_and_are_released(monkeypatch, rollback_level):
    svc, sf, _ = setup(monkeypatch)
    cache = svc._target_row_cache
    with sf() as session:
        create(svc, session, ACCT, "OUTER")
        one = session.begin_nested()
        create(svc, session, ACCT, "ONE")
        two = session.begin_nested()
        create(svc, session, ACCT, "TWO")
        two.rollback() if rollback_level == 2 else two.commit()
        one.rollback() if rollback_level == 1 else one.commit()
        assert all(cache.get((ACCT, symbol)) is None for symbol in ("OUTER", "ONE", "TWO"))
        session.commit()
        expected = {"OUTER"} if rollback_level == 1 else {"OUTER", "ONE"}
        if rollback_level == 0:
            expected.add("TWO")
        assert {symbol for symbol in ("OUTER", "ONE", "TWO")
            if cache.get((ACCT, symbol)) is not None} == expected
        assert not any(isinstance(key, tuple) and key[-1] == id(cache) for key in session.info)
        assert len(cache._rows) == 1 + len(expected)


def test_session_close_after_inner_release_does_not_publish_or_keep_frames(monkeypatch):
    svc, sf, _ = setup(monkeypatch)
    cache = svc._target_row_cache
    session = sf()
    create(svc, session, ACCT, "CLOSE")
    with session.begin_nested():
        pass
    session.close()
    assert cache.get((ACCT, "CLOSE")) is None
    assert not any(isinstance(key, tuple) and key[-1] == id(cache) for key in session.info)
    with sf() as read:
        assert read.scalar(select(OmsManagedPosition.id).where(OmsManagedPosition.symbol == "CLOSE")) is None


@pytest.mark.asyncio
async def test_sell_snapshot_is_frozen_scalar_dto_not_orm_or_session(monkeypatch):
    from dataclasses import FrozenInstanceError
    svc, sf, _ = setup(monkeypatch)
    with sf() as session:
        managed_row_id = row(session).id
    value = await svc._run_db(
        lambda session: svc._read_cw_target_sell_snapshot(session, managed_row_id), commit=False)
    assert inspect(value, raiseerr=False) is None
    assert value.managed_row_id == str(managed_row_id) and value.current_quantity == 120
    with pytest.raises(FrozenInstanceError):
        value.current_quantity = 119


def test_nonselect_merge_load_is_unbound_and_does_not_publish(monkeypatch):
    svc, sf, _ = setup(monkeypatch)
    with sf() as session:
        value = row(session)
        session.expunge(value)
    svc._target_row_cache.drop_row(value.id)
    with sf() as session:
        assert session.merge(value, load=False).current_quantity == 120
    assert svc._target_row_cache.get((ACCT, SYM)) is None


def test_outer_commit_with_active_savepoint_publishes_only_final_committed_create(monkeypatch):
    svc, sf, _ = setup(monkeypatch)
    with sf() as session:
        create(svc, session, ACCT, "ACTIVE")
        session.begin_nested()
        create(svc, session, ACCT, "INNER")
        assert svc._target_row_cache.get((ACCT, "ACTIVE")) is None
        assert svc._target_row_cache.get((ACCT, "INNER")) is None
        session.commit()
    assert svc._target_row_cache.get((ACCT, "ACTIVE")) is not None
    assert svc._target_row_cache.get((ACCT, "INNER")) is not None
