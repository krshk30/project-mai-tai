"""Main only after functional green and fresh authority/fence intake.

Real OMS quote callbacks/eligibility/serial intent path, retained original reject,
controlled later quote/reply. Async boundary is NOT an off-loop SDK proof.
"""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
from time import monotonic

import pytest

from project_mai_tai.oms import service as oms
from project_mai_tai.oms import webull_country_deferred as country
from tests.perf.test_wbattach1_o14_active_oms_rate import LoopStallMonitor
from tests.unit.wbpa2_controlled_transport import ControlledSubmitReply
from tests.unit.wbpa2_episode_port import (BOUNDARY, close_case, drain_ticks, make_case,
                                          observe_original, queued, quote, serial, state)

pytestmark = pytest.mark.skipif(not os.environ.get("WBPA2_CHOW_EPISODE_SOURCE"),
    reason="Requires the exact reviewed private CHOW export and bounded rate profile")


async def physical_join(task):
    if task is None:
        return
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()


@pytest.mark.asyncio
async def test_actual_country_deferred_slot_240_quote_callbacks_per_second_for_60_seconds(
    tmp_path, monkeypatch, record_property,
):
    # Approval is independently established by Main; this pins the admitted
    # receipt, not permission inferred from an environment string alone.
    authority = os.environ["WBPA2_RATE_ASK_BODY_SHA256"]
    assert len(authority) == 64 and all(char in "0123456789abcdef" for char in authority)
    module = Path(oms.__file__).resolve()
    assert module == Path(os.environ["WBPA2_EXPECTED_ROOT"]).resolve() / "src/project_mai_tai/oms/service.py"
    assert hashlib.sha256(module.read_bytes()).hexdigest() == os.environ["WBPA2_EXPECTED_OMS_SOURCE_SHA256"]
    case = await make_case(tmp_path, monkeypatch, replies=[ControlledSubmitReply(
        "rejected", "CONTROLLED measured PRICE_AGGRESSIVE417", error_code="ORDER_RISK_RULE_PRICE_AGGRESSIVE")])
    monitor = timer = actor = None
    counters = {"callbacks": 0, "evaluations": 0, "evaluations_with_country_slot": 0,
                "callback_max_seconds": 0.0, "wake_lateness_max_seconds": 0.0}
    actual_evaluate = country.evaluate

    async def observed_evaluate(service, symbol):
        assert service is case.service
        counters["evaluations"] += 1
        counters["evaluations_with_country_slot"] += state(case) is not None
        return await actual_evaluate(service, symbol)

    monkeypatch.setattr(country, "evaluate", observed_evaluate)
    begin = baseline = None
    evidence = {}
    try:
        observe_original(case)
        assert state(case) is not None
        baseline = case.clock[0]
        begin = monotonic()
        monitor = LoopStallMonitor(begin)
        timer = asyncio.create_task(monitor.run())
        await monitor.ready.wait()
        pulse_delivered = False
        while monotonic() - begin < 60:
            due = begin + counters["callbacks"] / 240
            await asyncio.sleep(max(0, due - monotonic()))
            entered = monotonic()
            counters["wake_lateness_max_seconds"] = max(counters["wake_lateness_max_seconds"], entered - due)
            elapsed = entered - begin
            case.clock[0] = baseline + timedelta(seconds=elapsed)
            # Fresh below-band cache repeatedly exercises real eligibility.
            # One disclosed in-band pulse at45s queues one normal serial actor.
            pulse = elapsed >= 45 and not pulse_delivered
            before = monotonic()
            awaiting_post = pulse_delivered and not case.provider.requests
            await quote(case, BOUNDARY if pulse or awaiting_post else "5.39")
            counters["callback_max_seconds"] = max(counters["callback_max_seconds"], monotonic() - before)
            counters["callbacks"] += 1
            if pulse:
                pulse_delivered = True
                await drain_ticks(case)
                assert len(queued(case)) == 1 and case.provider.entries == []

                async def measured_serial():
                    evidence["serial_start"] = monotonic()
                    try:
                        await serial(case, queued(case)[0])
                    finally:
                        evidence["serial_end"] = monotonic()

                actor = asyncio.create_task(measured_serial())
            if actor is not None and actor.done():
                actor.result()
        end = monotonic()
        monitor.finish(end)
        await drain_ticks(case)
        assert actor is not None and actor.done()
        actor.result()
        assert begin <= evidence["serial_start"] <= evidence["serial_end"] <= end
        assert end - begin >= 60 and counters["callbacks"] / (end - begin) >= 200
        assert monitor.max_stall < .050 and counters["callback_max_seconds"] < .050
        assert counters["wake_lateness_max_seconds"] < .050
        assert counters["evaluations"] >= 12000 and counters["evaluations_with_country_slot"] >= 12000
        assert state(case) is not None  # one controlled reject, bounded residual retry duty
        assert len(queued(case)) == len(case.provider.requests) == len(case.provider.entries) == 1
        assert case.provider.entries[0]["operation"] == "submit"
        assert not case.provider.entries[0]["market_task"]
        assert not case.provider.live_clients
        evidence.update(counters, actual_seconds=end - begin,
            completed_events_per_second=counters["callbacks"] / (end - begin),
            max_loop_stall_seconds=monitor.max_stall, provider_reads=0, provider_submits=1,
            serial_wall_seconds=evidence["serial_end"] - evidence["serial_start"],
            scope="REAL_OMS_CALLBACK_AND_SERIAL_WITH_CONTROLLED_ASYNC_PROVIDER_NOT_NATIVE_OR_SDK_THREAD_PROOF",
            rate_ask_body_sha256=authority)
        print("WBPA2_RATE_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
        for key, value in evidence.items():
            record_property(key, value)
    finally:
        # All tasks finish physically; no task is cancelled to shorten drain.
        if monitor is not None and not monitor.stopped:
            monitor.finish(monotonic())
        try:
            await physical_join(actor)
        finally:
            try:
                await physical_join(timer)
            finally:
                await close_case(case)
