"""Actual STOP owner + real receipt producer/writer; controlled broker/journal only."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4

import pytest

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.oms import orb_schwab_eod as owner, service as oms_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.positions_read_receipt import PositionsReadReceiptWriter, load_receipts
from tests.perf.test_orbroute1_active_oms_rate import prime_actual_positions_receipts
from tests.unit.test_killsw1_orb_owner import prepared
from tests.unit.test_orbroute1_oms_chain import setup as setup


def actual_lifecycle(setup, monkeypatch, provider):
    service, factory, broker, target, context, clock = prepared(setup, monkeypatch, provider)
    # Undo prepared()'s directly seeded receipt fixture. These cases exercise the
    # real service producer and its separate asynchronous writer transaction.
    service.sync_broker_positions = OmsRiskService.sync_broker_positions.__get__(service)
    monkeypatch.setattr(oms_module, "utcnow", lambda: clock[0])
    reads = []
    advance = [timedelta(milliseconds=1)]

    async def positions(account):
        assert account == target.account
        clock[0] += advance[0]
        reads.append(clock[0])
        return [BrokerPositionSnapshot(account, target.symbol, Decimal(10), Decimal("5.53"), as_of=clock[0])]

    broker.list_account_positions = positions
    broker.positions_wire_read_age_seconds = lambda account: 0.0
    writer = PositionsReadReceiptWriter(factory)
    service._positions_read_receipt_writer = writer
    return service, factory, broker, target, context, clock, reads, advance, writer


class HeldReceiptWriter:
    """Own only this writer's physical invocations, including after task cancellation."""

    def __init__(self, writer, monkeypatch):
        self.entered = asyncio.Event()
        self.native_entered, self.release = Event(), Event()
        self.physical = []
        self.pool = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        original = writer._persist
        run_in_executor = loop.run_in_executor

        def held(batches):
            self.native_entered.set()
            loop.call_soon_threadsafe(self.entered.set)
            if not self.release.wait(5):
                raise AssertionError("test did not release the real receipt worker")
            return original(batches)

        def submit(executor, function, *args):
            # Keep real asyncio.to_thread/context propagation. Route only the
            # exact held writer into an owned pool, retaining its native Future.
            target = getattr(function, "args", ())
            if executor is None and target and target[0] is held:
                future = self.pool.submit(function, *args)
                self.physical.append(future)
                return asyncio.wrap_future(future)
            return run_in_executor(executor, function, *args)

        monkeypatch.setattr(writer, "_persist", held)
        monkeypatch.setattr(loop, "run_in_executor", submit)

    async def wait_entered(self, *owned_tasks):
        ready = asyncio.create_task(self.entered.wait())
        try:
            await asyncio.wait((ready, *owned_tasks), return_when=asyncio.FIRST_COMPLETED)
            for task in owned_tasks:
                if task.done():
                    await task  # Surface an owned actor's exception/cancellation unchanged.
                    raise AssertionError("owned actor completed before the held writer was observed")
            await ready
            assert self.native_entered.is_set() and self.physical
            return True
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)

    async def join_physical(self):
        try:
            # Cancelling writer.run() does not complete its native invocation.
            results = await asyncio.gather(*(asyncio.shield(asyncio.wrap_future(future))
                for future in self.physical), return_exceptions=True)
            assert all(future.done() for future in self.physical)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            self.pool.shutdown(wait=True)


async def finish_writer(writer, task, hold, stopping=None):
    if hold is not None:
        hold.release.set()
    drained = asyncio.create_task(writer.queue.join())
    try:
        if stopping is not None:
            # Releasing the held write lets our STOP actor finish naturally;
            # cancellation of its waiter is not proof its DB work completed.
            outcome, = await asyncio.gather(stopping, return_exceptions=True)
            if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                raise outcome
        await asyncio.wait((drained, task), return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            await task
            raise AssertionError("owned receipt writer exited before cleanup")
        await drained
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        drained.cancel()
        await asyncio.gather(drained, return_exceptions=True)
        if hold is not None:
            await hold.join_physical()


def stop_wires(broker):
    return [request for request in broker.sent if request.reason == owner.STOP_REASON]


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_cold_actual_stop_snapshot_offer_is_not_committed_wire_permission(setup, monkeypatch, provider):
    service, factory, broker, target, context, _clock, reads, _advance, writer = actual_lifecycle(
        setup, monkeypatch, provider)
    async def exercise():
        hold = HeldReceiptWriter(writer, monkeypatch)
        task = asyncio.create_task(writer.run())
        try:
            with pytest.raises(ValueError, match="positions_wire_receipt_unknown"):
                await owner._attempt(service, target, uuid4(), context.clock,
                    reason=owner.STOP_REASON, stop_context=context)
            assert await hold.wait_entered(task)
            assert reads and not stop_wires(broker)
            with factory() as session:
                assert target.account not in load_receipts(session)
            assert context.journal.rows["native"].status == "TERMINAL"
            assert "SUBMIT_EXIT" not in context.journal.rows
        finally:
            await finish_writer(writer, task, hold)
        with factory() as session:
            receipt = load_receipts(session)[target.account]
            assert receipt["read_at"] == reads[-1]  # Actual queued wire time, never restamped.
        assert not stop_wires(broker)  # A later commit cannot retroactively authorize the failed call.
    asyncio.run(exercise())


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_predates_release_waits_for_real_delayed_commit_before_one_stop_wire(setup, monkeypatch, provider):
    service, factory, broker, target, context, clock, reads, _advance, writer = actual_lifecycle(
        setup, monkeypatch, provider)

    async def exercise():
        task = asyncio.create_task(writer.run())
        hold = None
        stopping = None
        try:
            await prime_actual_positions_receipts(service, writer, task, (target.account,), context.clock)
            with factory() as session:
                before = load_receipts(session)[target.account]["read_at"]
            clock[0] += timedelta(milliseconds=1)
            hold = HeldReceiptWriter(writer, monkeypatch)
            stopping = asyncio.create_task(owner._attempt(service, target, uuid4(), context.clock,
                reason=owner.STOP_REASON, stop_context=context))
            assert await hold.wait_entered(task, stopping)
            assert not stopping.done() and not stop_wires(broker)
            with factory() as session:
                assert load_receipts(session)[target.account]["read_at"] == before
            assert context.terminal_at is not None and before < context.terminal_at
            hold.release.set()
            async with asyncio.timeout(5):
                assert await stopping is True
                await writer.queue.join()
            assert len(stop_wires(broker)) == 1 and stop_wires(broker)[0].quantity == 10
            with factory() as session:
                committed = load_receipts(session)[target.account]["read_at"]
            assert committed in reads and context.terminal_at < committed <= clock[0]
        finally:
            await finish_writer(writer, task, hold, stopping)
    asyncio.run(exercise())


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_stale_committed_receipt_without_new_commit_expires_and_never_sells(setup, monkeypatch, provider):
    service, factory, broker, target, context, clock, _reads, advance, writer = actual_lifecycle(
        setup, monkeypatch, provider)

    async def exercise():
        task = asyncio.create_task(writer.run())
        hold = None
        try:
            await prime_actual_positions_receipts(service, writer, task, (target.account,), context.clock)
            with factory() as session:
                before = load_receipts(session)[target.account]["read_at"]
            clock[0] += timedelta(seconds=31)
            # The controlled broker read itself consumes another 31s of the SAME
            # aware clock. No runtime deadline, proof threshold or sleep is patched.
            advance[0] = timedelta(seconds=31)
            hold = HeldReceiptWriter(writer, monkeypatch)
            with pytest.raises(ValueError, match="orb_stop_fresh_position_wait_elapsed"):
                await owner._attempt(service, target, uuid4(), context.clock,
                    reason=owner.STOP_REASON, stop_context=context)
            assert await hold.wait_entered(task)
            assert not stop_wires(broker)
            with factory() as session:
                assert load_receipts(session)[target.account]["read_at"] == before
            assert (clock[0] - before).total_seconds() > 30
            assert "SUBMIT_EXIT" not in context.journal.rows
        finally:
            await finish_writer(writer, task, hold)
        assert not stop_wires(broker)
    asyncio.run(exercise())


@pytest.mark.parametrize("failed_actor", ["writer", "stopping"])
def test_owned_readiness_surfaces_actor_failure_without_touching_unrelated_tasks(monkeypatch, failed_actor):
    async def exercise():
        writer = PositionsReadReceiptWriter()
        hold = HeldReceiptWriter(writer, monkeypatch)
        unrelated_release = asyncio.Event()
        unrelated = asyncio.create_task(unrelated_release.wait())

        async def failed():
            raise RuntimeError("owned actor failed")

        task = asyncio.create_task(failed() if failed_actor == "writer" else writer.run())
        stopping = asyncio.create_task(failed()) if failed_actor == "stopping" else None
        actors = (task,) if stopping is None else (task, stopping)
        try:
            with pytest.raises(RuntimeError, match="owned actor failed"):
                await hold.wait_entered(*actors)
            assert not hold.native_entered.is_set() and not hold.physical
            with pytest.raises(RuntimeError, match="owned actor failed"):
                await finish_writer(writer, task, hold, stopping)
            assert not unrelated.done()
        finally:
            unrelated_release.set()
            await unrelated
    asyncio.run(exercise())


def test_owned_cleanup_surfaces_physical_writer_error_even_when_run_logs_it(monkeypatch):
    async def exercise():
        writer = PositionsReadReceiptWriter()

        def fail(_batches):
            raise RuntimeError("physical receipt write failed")

        monkeypatch.setattr(writer, "_persist", fail)
        hold = HeldReceiptWriter(writer, monkeypatch)
        task = asyncio.create_task(writer.run())
        try:
            # Queue input only for helper ownership; no receipt/permission is seeded.
            assert writer.offer([("helper-only", None, 0)])
            assert await hold.wait_entered(task)
        finally:
            with pytest.raises(RuntimeError, match="physical receipt write failed"):
                await finish_writer(writer, task, hold)
        assert hold.physical and all(future.done() for future in hold.physical)
    asyncio.run(exercise())


def test_cancelled_writer_task_does_not_complete_its_physical_invocation(monkeypatch):
    async def exercise():
        writer = PositionsReadReceiptWriter()
        native_release, native_done = Event(), Event()
        native_started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def persist(_batches):
            loop.call_soon_threadsafe(native_started.set)
            try:
                assert native_release.wait(5), "test did not release its physical invocation"
            finally:
                native_done.set()

        monkeypatch.setattr(writer, "_persist", persist)
        hold = HeldReceiptWriter(writer, monkeypatch)
        task = asyncio.create_task(writer.run())
        cleanup = None
        try:
            assert writer.offer([("helper-only", None, 0)])
            assert await hold.wait_entered(task)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert not hold.physical[0].done() and not native_done.is_set()
            cleanup = asyncio.create_task(finish_writer(writer, task, hold))
            await native_started.wait()
            assert not cleanup.done() and not native_done.is_set()
        finally:
            native_release.set()
            if cleanup is None:
                cleanup = asyncio.create_task(finish_writer(writer, task, hold))
            with pytest.raises(asyncio.CancelledError):
                await cleanup
        assert native_done.is_set() and all(future.done() for future in hold.physical)
    asyncio.run(exercise())
