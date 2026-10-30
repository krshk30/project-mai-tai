"""SOURCE UNRUN: C1 23:54 early diagnostic; positive recovery is BLOCKED.

Main only after exact source/roster/fence review. Real managed/ATR/WB2 gates and
private SQLite remain in use; only typed provider replies/latency are controlled.
No history-success stub or native history completeness claim is supplied.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Event

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, VirtualPosition
from project_mai_tai.oms import wbexit3
from tests.unit.test_wbattach1_riskoff import make_case as make_case, closes
from tests.unit.test_wbexit2_route import ControlledOwnerBroker, close_case, owner_case, saved
from tests.unit.test_wbexit3_obligation import enable, obligations


class EarlyProbeBroker(ControlledOwnerBroker):
    native_quantity = Decimal(27)
    entered = release = physically_done = None

    async def read_wbexit2_positions(self, account, *, after):
        if self.entered is not None:
            def held_read():
                self.entered.set()
                assert self.release.wait(5), "CONTROLLED held provider read was not released"
                self.physically_done.set()
            await asyncio.to_thread(held_read)
        rows, started, native = await super().read_wbexit2_positions(account, after=after)
        return [replace(row, quantity=self.native_quantity) for row in rows], started, native


async def ledger_zero_case(make_case, *, on=True):
    case = await owner_case(make_case, port_type=EarlyProbeBroker, symbol="AIOK", quantity="27",
        entry="10.91", at_clock=datetime.fromisoformat("2026-10-23T19:04:00+00:00"))
    await enable(case, on)
    with case[1].begin() as db:
        db.scalar(select(VirtualPosition)).quantity = Decimal(0)
    return case


def unchanged_quantity_and_no_wire(case):
    service, factory, broker, entry_id, _ = case
    assert not broker.cancelled and not closes(broker)
    state = saved(case)
    assert Decimal(state["original_quantity"]) == Decimal(state["remaining"]) == 27
    assert state["sell"] is None and state["cancel_bases"] == []
    assert state["invocation"] is None
    with factory() as db:
        row = db.scalar(select(OmsManagedPosition))
        assert row.status == "open" and row.current_quantity == row.original_quantity == 27
        assert db.scalar(select(VirtualPosition.quantity)) == 0
        assert db.get(BrokerOrder, entry_id).quantity == 27
        fills = list(db.scalars(select(Fill)))
        assert len(fills) == 1 and fills[0].side == "buy" and fills[0].quantity == 27


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["27", "10", "unreadable"])
async def test_early_once_probe_does_not_recover_without_complete_history(make_case, reply):
    case = await ledger_zero_case(make_case)
    broker, clock = case[2], case[4]
    broker.positions_unknown = reply == "unreadable"
    if not broker.positions_unknown:
        broker.native_quantity = Decimal(reply)
    for attempt in range(3):
        if attempt:
            clock[0] += timedelta(seconds=61)
        await close_case(case, "CW_TARGET")
        assert broker.positions_reads == attempt + 1  # ONE probe; no later duplicate in this invocation.
        unchanged_quantity_and_no_wire(case)
        duty = obligations(case)
        assert len(duty) == 1 and duty[0]["active_token"] is None
        assert duty[0]["last_guard"] == "ledger_zero_managed_positive"
        assert Decimal(duty[0]["requested"]) == 27
        assert duty[0]["phase"] == ("PAGED" if attempt == 2 else "RETRY")
    # A native 27 is diagnostic, a native 10 never becomes a partial 10 SELL,
    # and unreadable/429 never becomes empty/flat or invented native history.
    assert not callable(getattr(broker, "read_wbexit3_day_history", None))


@pytest.mark.asyncio
async def test_early_probe_OFF_retains_original_zero_ledger_refusal_without_read(make_case):
    case = await ledger_zero_case(make_case, on=False)
    await close_case(case, "CW_TARGET")
    assert case[2].positions_reads == 0 and obligations(case) == []
    unchanged_quantity_and_no_wire(case)


@pytest.mark.asyncio
async def test_early_probe_cancellation_joins_physical_read_before_owner_release(make_case):
    case = await ledger_zero_case(make_case)
    service, _, broker, _, _ = case
    broker.entered, broker.release, broker.physically_done = Event(), Event(), Event()
    key = (broker.account, broker.symbol)
    task = asyncio.create_task(close_case(case, "CW_TARGET"))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(broker.entered.wait, 2), timeout=3)
        assert key in service._wbattach_exit_owners
        assert obligations(case)[0]["active_token"] is not None
        for message in ("first-early-probe-cancel", "second-early-probe-cancel"):
            task.cancel(message)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done() and not broker.physically_done.is_set()
            assert key in service._wbattach_exit_owners
            assert obligations(case)[0]["active_token"] is not None
            assert not broker.cancelled and not closes(broker)
        broker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert broker.physically_done.is_set() and broker.positions_reads == 1
        assert key not in service._wbattach_exit_owners
        assert obligations(case)[0]["active_token"] is None
        unchanged_quantity_and_no_wire(case)
    finally:
        broker.release.set()
        if not task.done():
            task.cancel("fixture-cleanup")
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.wait_for(asyncio.to_thread(broker.physically_done.wait, 3), timeout=4)
