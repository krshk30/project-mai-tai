"""Audit persistence uses an exclusive lease; counters stay on the caller loop."""
import asyncio
from contextlib import nullcontext
import logging
import threading
from types import SimpleNamespace

import pytest

from project_mai_tai.oms.buy_submission_journal import DurableBuyAdapter
from project_mai_tai.oms.service import OmsRiskService


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_audit_writer_offloop_and_counter_accounting_on_loop(fails, caplog):
    caller = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    def write(*args, **kwargs):
        assert threading.get_ident() != caller
        entered.set()
        assert release.wait(2)
        if fails:
            raise RuntimeError("controlled audit failure")
    service = OmsRiskService.__new__(OmsRiskService)
    service.store = SimpleNamespace(append_order_event=write)
    service.logger = logging.getLogger("controlled-audit")
    service.broker_adapter = DurableBuyAdapter(SimpleNamespace(), None)
    service._order_event_attempts = service._order_event_failures = 0
    session = SimpleNamespace(begin_nested=nullcontext)
    order = SimpleNamespace(symbol="DKI")
    report = SimpleNamespace(client_order_id="exact", event_type="accepted")
    task = asyncio.create_task(service._append_order_event_isolated_awaited(
        session, order=order, report=report, payload={}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0.01)
        assert not task.done()
        assert service._order_event_attempts == 1 and service._order_event_failures == 0
    finally:
        release.set()
        result = await task
    assert result is not fails
    assert service._order_event_failures == int(fails)
    assert ("OMS-ORDER-EVENT-DROPPED" in caplog.text) is fails


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_cancel_waits_for_exclusive_audit_worker_before_session_can_close(fails, caplog):
    entered, release = threading.Event(), threading.Event()
    service = OmsRiskService.__new__(OmsRiskService)
    def write(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        if fails:
            raise RuntimeError("controlled cancelled audit failure")
    service.store = SimpleNamespace(append_order_event=write)
    service.logger = logging.getLogger("controlled-cancelled-audit")
    service.broker_adapter = DurableBuyAdapter(SimpleNamespace(), None)
    service._order_event_attempts = service._order_event_failures = 0
    task = asyncio.create_task(service._append_order_event_isolated_awaited(
        SimpleNamespace(begin_nested=nullcontext), order=SimpleNamespace(),
        report=SimpleNamespace(), payload={}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service._order_event_failures == int(fails)
    assert ("OMS-ORDER-EVENT-DROPPED" in caplog.text) is fails
