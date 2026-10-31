"""Controlled cancellation of the ACTUAL owned claim; no permission substitute."""
import asyncio
import os
import threading

import pytest

from project_mai_tai.oms import webull_country_deferred as country
from tests.unit.wbpa2_episode_port import (BOUNDARY, close_case, drain_ticks, make_case,
                                          observe_original, queued, quote, state)

pytestmark = pytest.mark.skipif(not os.environ.get("WBPA2_CHOW_EPISODE_SOURCE"),
    reason="Requires the exact reviewed private CHOW export")


@pytest.mark.asyncio
async def test_cancelled_serial_handler_physically_joins_actual_offloop_claim_before_return(
    tmp_path, monkeypatch,
):
    case = await make_case(tmp_path, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    actual = country.no_working_reason
    loop_thread = threading.get_ident()
    observations = []
    actor = None

    def held_actual_proof(service, session, event, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5), "controlled proof barrier was not released"
        try:
            result = actual(service, session, event, **kwargs)
            observations.append(result)
            return result  # The actual canonical/segment/order proof decides.
        finally:
            finished.set()

    monkeypatch.setattr(country, "no_working_reason", held_actual_proof)
    try:
        observe_original(case)
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        retry, = queued(case)
        actor = asyncio.create_task(case.service._handle_stream_message({"data": retry.model_dump_json()}))
        case.provider.serial_task = actor
        deadline = asyncio.get_running_loop().time() + 5
        while not entered.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.001)
        actor.cancel()
        await asyncio.sleep(.005)
        actor.cancel()  # Repeated parent cancellation cannot release the proof early.
        await asyncio.sleep(.005)
        assert not actor.done() and not finished.is_set()
        assert case.provider.entries == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await actor
        assert finished.is_set() and observations == [None]
        assert state(case) is None and case.provider.entries == []
    finally:
        release.set()
        if actor is not None:
            while not actor.done():
                try:
                    await asyncio.shield(actor)
                except asyncio.CancelledError:
                    pass
            try:
                actor.result()
            except asyncio.CancelledError:
                pass
        await close_case(case)
