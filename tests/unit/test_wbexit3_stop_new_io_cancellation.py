"""SOURCE UNRUN: only NEW WB3 C6 I/O boundaries, not whole C6 acceptance.

Real private C6 lease/journal/canonical writers and _run_db remain. Controlled
latency is at DB/SDK ports; no native/owner/quantity/final-proof success stub.
Main/C1 must review the separate EXTERNAL C6 proposal before execution.
"""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
import asyncio
from dataclasses import replace
from datetime import UTC
from threading import Event

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BotControlAttempt, BrokerOrder, Fill, OmsManagedPosition
from project_mai_tai.oms import bot_control_atr as atr, wbexit3_stop
from tests.unit.test_killsw1_atr_stop import book as book, drive
from tests.unit.test_wbexit3_stop_retirement import prior_attempt
from tests.unit.test_wbexit3_stop_original_path import original_case, stop_port, fresh

MARK = "CONTROLLED_wb3_join_target_session"


def observe_target_sessions(service, factory, closed):
    class ObservedSession:
        def __enter__(self):
            self.actual = factory()
            return self.actual.__enter__()

        def __exit__(self, *exception):
            target = self.actual.info.get(MARK)
            try:
                return self.actual.__exit__(*exception)
            finally:
                if target:
                    closed.set()  # AFTER actual commit/rollback/session exit.
    return ObservedSession


async def cancel_twice_while_held(task, entered, finished):
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
    task.cancel("first-C6-cancel")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done() and not finished.is_set()
    task.cancel("second-C6-cancel")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done() and not finished.is_set()


async def release_and_expect_original_cancel(task, release, finished):
    release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await asyncio.wait_for(task, timeout=3)
    assert cancelled.value.args == ("first-C6-cancel",)
    assert finished.is_set()


async def drain_even_OLD_failed_image(task, release, finished):
    release.set()
    if not task.done():
        task.cancel("fixture-cleanup")
    await asyncio.gather(task, return_exceptions=True)
    # OLD can finish its coroutine before the DB/SDK thread; never dispose the
    # private DB merely because that OLD asyncio task has already ended.
    assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 3), timeout=4)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("hours", [12])
@pytest.mark.parametrize("stage", ["scope", "admission", "fetch", "reconcile"])
async def test_NEW_read_boundaries_join_physical_IO_and_never_continue_after_cancel(book, monkeypatch, hours, stage):
    object.__setattr__(book.service.settings, "oms_webull_exit3_enabled", True)
    prior = await prior_attempt(book, "accepted" if stage == "reconcile" else "cancelled")
    service = book.service
    entered, release, finished = Event(), Event(), Event()
    counts = {"fetch": 0, "reconcile": 0}
    original_db, original_reconcile = service._run_db, book.journal.reconcile

    if stage in {"scope", "admission"}:
        monkeypatch.setattr(service, "session_factory", observe_target_sessions(service, book.sessions, finished))
        async def held_db(fn, *, commit=True):
            wanted = "wb3_scope" if stage == "scope" else "read"
            if fn.__name__ != wanted:
                return await original_db(fn, commit=commit)
            def held(db):
                result = fn(db)  # ACTUAL pure DB/lease/binding/backoff logic.
                db.info[MARK] = True
                entered.set()
                assert release.wait(5), "CONTROLLED DB latency not released"
                return result
            return await original_db(held, commit=commit)
        monkeypatch.setattr(service, "_run_db", held_db)

    report = replace(book.prior_report, event_type="cancelled", reported_at=book.clock.now(UTC))
    async def fetch(request):
        counts["fetch"] += 1
        assert request.client_order_id == report.client_order_id
        assert request.metadata["broker_order_id"] == report.broker_order_id
        if stage != "fetch":
            return report
        def SDK_read():
            entered.set()
            try:
                assert release.wait(5), "CONTROLLED SDK read not released"
                return report
            finally:
                finished.set()
        return await asyncio.to_thread(SDK_read)
    monkeypatch.setattr(book.broker, "fetch_order_update", fetch)

    def reconcile(*args, **kwargs):
        counts["reconcile"] += 1
        if stage == "reconcile":
            entered.set()
            try:
                assert release.wait(5), "CONTROLLED journal DB latency not released"
                return original_reconcile(*args, **kwargs)
            finally:
                finished.set()  # AFTER real journal lease validation/commit.
        return original_reconcile(*args, **kwargs)
    monkeypatch.setattr(book.journal, "reconcile", reconcile)
    target = (atr._read_exit(service, book.lease, book.journal, prior) if stage == "scope"
              else wbexit3_stop.read_existing(service, book.lease, book.journal, prior))
    task = asyncio.create_task(target)
    try:
        await cancel_twice_while_held(task, entered, finished)
        assert counts["fetch"] == int(stage in {"fetch", "reconcile"})
        assert counts["reconcile"] == int(stage == "reconcile")
        await release_and_expect_original_cancel(task, release, finished)
        assert not book.submits and book.release_count == 0
        with book.sessions() as db:
            order = db.get(BrokerOrder, prior)
            assert db.get(OmsManagedPosition, book.row).status == "open"
            assert len(list(db.scalars(select(Fill)))) == 1  # Real original BUY only.
            assert bool(order.payload.get(wbexit3_stop.READ_KEY)) == (stage == "reconcile")
            assert bool(order.payload.get(wbexit3_stop.READ_ATTEMPT_KEY)) == (stage != "scope")
            if stage == "reconcile":
                audit = db.scalar(select(BotControlAttempt).where(BotControlAttempt.target_order_id == prior))
                assert audit.status == "TERMINAL"  # Actual positive fetch/canonical/journal, never cancellation inference.
    finally:
        await drain_even_OLD_failed_image(task, release, finished)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("hours", [12])
async def test_NEW_next_identity_DB_join_keeps_REAL_C6_owner_until_session_exit(book, monkeypatch, hours):
    object.__setattr__(book.service.settings, "oms_webull_exit3_enabled", True)
    state = original_case(book, monkeypatch, broker_quantity=1010)
    reads, _ = stop_port(book, monkeypatch, state)
    await drive(book)  # Real original LIST/cap/journal/sender -> first cancelled0.
    await drive(book)  # Separate actual original-ID fetch; no new SELL.
    assert len(book.submits) == len(reads) == 1 and book.release_count == 0
    fresh(book, state, 11)
    entered, release, finished = Event(), Event(), Event()
    actual_next = wbexit3_stop.next_identity
    def held_next(service, db, *args):
        result = actual_next(service, db, *args)  # Actual current lease/terminal/canonical/backoff proof.
        db.info[MARK] = True
        entered.set()
        assert release.wait(5), "CONTROLLED identity read latency not released"
        return result
    monkeypatch.setattr(wbexit3_stop, "next_identity", held_next)
    monkeypatch.setattr(book.service, "session_factory", observe_target_sessions(book.service, book.sessions, finished))
    task = asyncio.create_task(drive(book))
    try:
        await cancel_twice_while_held(task, entered, finished)
        assert book.service.__dict__.get("_control_managed_exit_symbol_owners")
        assert book.service.__dict__.get("_wbattach_exit_owners")
        assert len(book.submits) == 1
        await release_and_expect_original_cancel(task, release, finished)
        assert not book.service.__dict__.get("_control_managed_exit_symbol_owners")
        assert not book.service.__dict__.get("_wbattach_exit_owners")
        assert len(book.submits) == 1 and book.release_count == 0 and state.broker_quantity == 1010
        with book.sessions() as db:
            assert db.get(OmsManagedPosition, book.row).status == "open"
            assert len(list(db.scalars(select(BotControlAttempt).where(BotControlAttempt.operation == "SUBMIT_EXIT")))) == 1
            assert len(list(db.scalars(select(Fill)))) == 1
    finally:
        await drain_even_OLD_failed_image(task, release, finished)
