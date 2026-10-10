"""Intent database leases finish before their caller may close the Session."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from project_mai_tai.oms.buy_submission_journal import DurableBuyAdapter
from project_mai_tai.oms.service import OmsRiskService


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_intent_db_fences_repeated_cancel_before_session_exit(fails):
    entered, release = threading.Event(), threading.Event()
    caller = threading.get_ident()
    session_closed = []
    service = OmsRiskService.__new__(OmsRiskService)
    service.broker_adapter = DurableBuyAdapter(SimpleNamespace(), None)

    def write(value, *, exact):
        assert threading.get_ident() != caller
        assert (value, exact) == (1, "same-session")
        entered.set()
        assert release.wait(2)
        assert not session_closed
        if fails:
            raise RuntimeError("controlled database error")
        return "persisted"

    async def intent():
        try:
            return await service._intent_db(write, 1, exact="same-session")
        finally:
            session_closed.append(True)

    task = asyncio.create_task(intent())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done() and not session_closed
    finally:
        release.set()
        with pytest.raises(RuntimeError if fails else asyncio.CancelledError):
            await task
    assert session_closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_intent_db_preserves_result_and_nonwrapped_thread(durable):
    caller = threading.get_ident()
    service = OmsRiskService.__new__(OmsRiskService)
    service.broker_adapter = (DurableBuyAdapter(SimpleNamespace(), None)
                              if durable else SimpleNamespace())
    marker = object()

    def read():
        assert (threading.get_ident() != caller) is durable
        return marker

    assert await service._intent_db(read) is marker
