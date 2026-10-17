"""Actual STOP owner + real receipt producer/writer; controlled broker/journal only."""

import asyncio
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


def hold_real_writer(writer):
    entered, release = Event(), Event()
    original = writer._persist

    def held(batches):
        entered.set()
        if not release.wait(5):
            raise AssertionError("test did not release the real receipt worker")
        original(batches)
    writer._persist = held
    return entered, release


async def finish_writer(writer, task, release):
    release.set()
    try:
        async with asyncio.timeout(5):
            await writer.queue.join()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def stop_wires(broker):
    return [request for request in broker.sent if request.reason == owner.STOP_REASON]


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_cold_actual_stop_snapshot_offer_is_not_committed_wire_permission(setup, monkeypatch, provider):
    service, factory, broker, target, context, _clock, reads, _advance, writer = actual_lifecycle(
        setup, monkeypatch, provider)
    entered, release = hold_real_writer(writer)

    async def exercise():
        task = asyncio.create_task(writer.run())
        try:
            with pytest.raises(ValueError, match="positions_wire_receipt_unknown"):
                await owner._attempt(service, target, uuid4(), context.clock,
                    reason=owner.STOP_REASON, stop_context=context)
            assert await asyncio.to_thread(entered.wait, 1)
            assert reads and not stop_wires(broker)
            with factory() as session:
                assert target.account not in load_receipts(session)
            assert context.journal.rows["native"].status == "TERMINAL"
            assert "SUBMIT_EXIT" not in context.journal.rows
        finally:
            await finish_writer(writer, task, release)
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
        release = Event()
        stopping = None
        try:
            await prime_actual_positions_receipts(service, writer, task, (target.account,), context.clock)
            with factory() as session:
                before = load_receipts(session)[target.account]["read_at"]
            clock[0] += timedelta(milliseconds=1)
            entered, release = hold_real_writer(writer)
            stopping = asyncio.create_task(owner._attempt(service, target, uuid4(), context.clock,
                reason=owner.STOP_REASON, stop_context=context))
            assert await asyncio.to_thread(entered.wait, 1)
            assert not stopping.done() and not stop_wires(broker)
            with factory() as session:
                assert load_receipts(session)[target.account]["read_at"] == before
            assert context.terminal_at is not None and before < context.terminal_at
            release.set()
            async with asyncio.timeout(5):
                assert await stopping is True
                await writer.queue.join()
            assert len(stop_wires(broker)) == 1 and stop_wires(broker)[0].quantity == 10
            with factory() as session:
                committed = load_receipts(session)[target.account]["read_at"]
            assert committed in reads and context.terminal_at < committed <= clock[0]
        finally:
            release.set()
            if stopping is not None and not stopping.done():
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
            await finish_writer(writer, task, release)
    asyncio.run(exercise())


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_stale_committed_receipt_without_new_commit_expires_and_never_sells(setup, monkeypatch, provider):
    service, factory, broker, target, context, clock, _reads, advance, writer = actual_lifecycle(
        setup, monkeypatch, provider)

    async def exercise():
        task = asyncio.create_task(writer.run())
        release = Event()
        try:
            await prime_actual_positions_receipts(service, writer, task, (target.account,), context.clock)
            with factory() as session:
                before = load_receipts(session)[target.account]["read_at"]
            clock[0] += timedelta(seconds=31)
            # The controlled broker read itself consumes another 31s of the SAME
            # aware clock. No runtime deadline, proof threshold or sleep is patched.
            advance[0] = timedelta(seconds=31)
            entered, release = hold_real_writer(writer)
            with pytest.raises(ValueError, match="orb_stop_fresh_position_wait_elapsed"):
                await owner._attempt(service, target, uuid4(), context.clock,
                    reason=owner.STOP_REASON, stop_context=context)
            assert await asyncio.to_thread(entered.wait, 1)
            assert not stop_wires(broker)
            with factory() as session:
                assert load_receipts(session)[target.account]["read_at"] == before
            assert (clock[0] - before).total_seconds() > 30
            assert "SUBMIT_EXIT" not in context.journal.rows
        finally:
            await finish_writer(writer, task, release)
        assert not stop_wires(broker)
    asyncio.run(exercise())
