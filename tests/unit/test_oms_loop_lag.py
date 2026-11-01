"""Owned passive diagnostics: controlled clocks here, real clocks in the rate binding."""

import asyncio
import gc
import logging
from types import SimpleNamespace
import weakref

import pytest

from project_mai_tai.oms import loop_lag
from project_mai_tai.oms.loop_lag import (
    OmsLoopLagWatchdog, oms_loop_lag_logging, with_oms_loop_lag_logging,
)


class ControlledSleep:
    def __init__(self):
        self.calls = asyncio.Queue()

    async def __call__(self, seconds):
        ready = asyncio.get_running_loop().create_future()
        self.calls.put_nowait((seconds, ready))
        await ready


@pytest.mark.asyncio
async def test_exact_threshold_throttle_and_one_second_sleep_without_catchup(caplog):
    now, ns = [0.0], [0]
    sleep = ControlledSleep()

    def clock_ns():
        ns[0] += 10
        return ns[0]

    logger = logging.getLogger("oms-loop-lag-controlled")
    monitor = OmsLoopLagWatchdog(logger, clock=lambda: now[0], clock_ns=clock_ns, sleep=sleep)
    before = list(gc.callbacks)
    monitor.start()
    task = monitor._task
    callback = monitor._callback
    monitor.start()
    assert monitor._task is task and gc.callbacks.count(callback) == 1
    try:
        with caplog.at_level(logging.WARNING):
            for observed, lines in ((1.1, 0), (2.201, 1), (3.401, 1), (12.201, 2)):
                seconds, ready = await sleep.calls.get()
                assert seconds == 1.0
                now[0] = observed
                ready.set_result(None)
                # Reaching the next owned sleep proves the prior observation completed.
                next_sleep = await sleep.calls.get()
                sleep.calls.put_nowait(next_sleep)
                assert len(caplog.records) == lines
        assert "lag_ms=101.000 gc_gen2_last_ms=UNKNOWN" in caplog.records[0].getMessage()
        assert "lag_ms=7800.000" in caplog.records[1].getMessage()
        assert monitor.sample_count == 4 and monitor.max_sample_ns == 10
        assert monitor.first_sample_at == 1.1 and monitor.last_sample_at == 12.201
    finally:
        await monitor.close()
    assert task.done() and task.cancelled() and monitor._task is None
    assert list(gc.callbacks) == before
    await monitor.close()
    assert list(gc.callbacks) == before


@pytest.mark.asyncio
async def test_only_complete_gen2_updates_last_duration_and_callback_is_owned():
    ns = [100]
    monitor = OmsLoopLagWatchdog(logging.getLogger("oms-loop-lag-gc"), clock_ns=lambda: ns[0])
    before = list(gc.callbacks)
    monitor.start()
    callback = monitor._callback
    try:
        assert monitor.gc_gen2_last_ms is None
        callback("stop", {"generation": 2})
        assert monitor.gc_gen2_last_ms is None
        callback("start", {"generation": 2})
        ns[0] += 1_500_000
        callback("stop", {"generation": 2})
        assert monitor.gc_gen2_last_ms == 1.5
        for generation in (0, 1):
            callback("start", {"generation": generation})
            ns[0] += 9_000_000
            callback("stop", {"generation": generation})
        assert monitor.gc_gen2_last_ms == 1.5
    finally:
        await monitor.close()
    callback("start", {"generation": 2})
    ns[0] += 10_000_000
    callback("stop", {"generation": 2})
    assert monitor.gc_gen2_last_ms == 1.5 and list(gc.callbacks) == before


@pytest.mark.asyncio
async def test_real_worker_gen2_callback_records_without_changing_gc_policy():
    monitor = OmsLoopLagWatchdog(logging.getLogger("oms-loop-lag-real-gc"))
    before, thresholds, enabled = list(gc.callbacks), gc.get_threshold(), gc.isenabled()
    monitor.start()
    try:
        await asyncio.to_thread(gc.collect, 2)
        assert monitor.gc_gen2_last_ms is not None and monitor.gc_gen2_last_ms >= 0
        assert gc.get_threshold() == thresholds and gc.isenabled() is enabled
    finally:
        await monitor.close()
    assert list(gc.callbacks) == before


def test_logger_failure_is_passive_and_still_rate_limited():
    calls = []

    def failed(*args):
        calls.append(args)
        raise RuntimeError("CONTROLLED sink failure")

    monitor = OmsLoopLagWatchdog(SimpleNamespace(warning=failed))
    monitor.gc_gen2_last_ms = 12.345
    monitor._observe(0, .101)
    monitor._observe(1, 1.2)
    monitor._observe(9, 10.1009)
    monitor._observe(10, 10.101)
    assert len(calls) == 2 and calls[0][-1] == "12.345"
    assert monitor.sample_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["return", "exception", "cancel"])
async def test_decorator_preserves_body_result_exception_and_cancellation(outcome):
    before = list(gc.callbacks)
    entered, release = asyncio.Event(), asyncio.Event()
    failure = ValueError("CONTROLLED original body failure")
    result = object()

    class Service:
        logger = logging.getLogger("oms-loop-lag-body")

        @with_oms_loop_lag_logging
        async def run(self):
            assert len(gc.callbacks) == len(before) + 1
            assert any(task.get_name() == "oms-loop-lag" for task in asyncio.all_tasks())
            entered.set()
            if outcome == "exception":
                raise failure
            if outcome == "cancel":
                await release.wait()
            return result

    task = asyncio.create_task(Service().run())
    await entered.wait()
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "exception":
        with pytest.raises(ValueError) as caught:
            await task
        assert caught.value is failure
    else:
        assert await task is result
    assert task.done() and list(gc.callbacks) == before
    assert not any(task.get_name() == "oms-loop-lag" for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_actual_service_startup_failure_still_removes_callback_and_task(monkeypatch):
    from project_mai_tai.oms import service as service_module

    service = service_module.OmsRiskService.__new__(service_module.OmsRiskService)
    service.logger = logging.getLogger("oms-loop-lag-startup")
    before = list(gc.callbacks)
    failure = RuntimeError("CONTROLLED boot failure before DB/provider/Redis use")

    def seed():
        assert len(gc.callbacks) == len(before) + 1
        raise failure

    service.seed_runtime_metadata = seed
    monkeypatch.setattr(service_module, "_install_signal_handlers", lambda stop: None)
    with pytest.raises(RuntimeError) as caught:
        await service.run()
    assert caught.value is failure and list(gc.callbacks) == before
    assert not any(task.get_name() == "oms-loop-lag" for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_monitor_install_failure_does_not_change_body_or_leak_coroutine(monkeypatch):
    before = list(gc.callbacks)

    def failed_create(work, **kwargs):
        raise RuntimeError("CONTROLLED task creation failure")

    monkeypatch.setattr(loop_lag.asyncio, "create_task", failed_create)
    async with oms_loop_lag_logging(logging.getLogger("oms-loop-lag-install")) as monitor:
        assert monitor._task is None and list(gc.callbacks) == before
    assert list(gc.callbacks) == before


@pytest.mark.asyncio
async def test_close_joins_owned_task_despite_repeated_cancellation():
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held_sleep(seconds):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            raise

    before = list(gc.callbacks)
    monitor = OmsLoopLagWatchdog(logging.getLogger("oms-loop-lag-close"), sleep=held_sleep)
    monitor.start()
    owned = monitor._task
    await entered.wait()
    closer = asyncio.create_task(monitor.close())
    await cancelling.wait()
    try:
        for _ in range(2):
            closer.cancel()
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            assert not closer.done() and not owned.done()
            assert list(gc.callbacks) == before
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closer
    assert owned.done() and monitor._task is None


@pytest.mark.asyncio
async def test_closed_monitor_has_no_gc_global_reference_or_cycle():
    monitor = OmsLoopLagWatchdog(logging.getLogger("oms-loop-lag-reference"))
    ref = weakref.ref(monitor)
    monitor.start()
    callback = monitor._callback
    await monitor.close()
    del monitor
    gc.collect(0)
    assert ref() is None and callback not in gc.callbacks
