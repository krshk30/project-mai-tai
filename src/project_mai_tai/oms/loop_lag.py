"""Passive, service-owned event-loop observations; no trading or GC policy."""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import gc
import time
import weakref


class OmsLoopLagWatchdog:
    def __init__(self, logger, *, clock=time.monotonic, clock_ns=time.monotonic_ns,
                 sleep=asyncio.sleep):
        self.logger = logger
        self._clock, self._clock_ns, self._sleep = clock, clock_ns, sleep
        self._task = None
        self._callback = None
        self._closed = False
        self._gen2_started_ns = None
        self.gc_gen2_last_ms = None
        self._last_log_at = None
        self.sample_count = 0
        self.first_sample_at = self.last_sample_at = None
        self.max_sample_ns = 0

    def start(self):
        if self._closed or self._task is not None:
            return
        owner = weakref.ref(self)

        def callback(phase, info):
            monitor = owner()
            if monitor is None or monitor._closed or info.get("generation") != 2:
                return
            try:
                if phase == "start":
                    monitor._gen2_started_ns = monitor._clock_ns()
                elif phase == "stop":
                    started, monitor._gen2_started_ns = monitor._gen2_started_ns, None
                    if started is not None:
                        monitor.gc_gen2_last_ms = max(0, monitor._clock_ns() - started) / 1e6
            except Exception:
                monitor._gen2_started_ns = None
                # Never log, allocate a history, or change GC policy in its callback.

        self._callback = callback
        gc.callbacks.append(callback)
        work = self._run()
        try:
            self._task = asyncio.create_task(work, name="oms-loop-lag")
        except BaseException:
            work.close()
            self._detach_callback()
            raise

    def _observe(self, expected, observed):
        self.sample_count += 1
        if self.first_sample_at is None:
            self.first_sample_at = observed
        self.last_sample_at = observed
        lag = max(0, observed - expected)
        if observed <= expected + .1 or (
                self._last_log_at is not None and observed - self._last_log_at < 10):
            return
        # Claim before logging: a failing sink must not turn this into a log storm.
        self._last_log_at = observed
        gc_ms = ("UNKNOWN" if self.gc_gen2_last_ms is None
                 else f"{self.gc_gen2_last_ms:.3f}")
        try:
            self.logger.warning("[OMS-LOOP-LAG] lag_ms=%.3f gc_gen2_last_ms=%s", lag * 1000, gc_ms)
        except Exception:
            pass

    async def _run(self):
        try:
            while True:
                expected = self._clock() + 1.0
                await self._sleep(1.0)
                began = self._clock_ns()
                self._observe(expected, self._clock())
                self.max_sample_ns = max(self.max_sample_ns, self._clock_ns() - began)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # A diagnostic failure cannot fail or repeatedly wake the OMS owner.

    def _detach_callback(self):
        callback, self._callback = self._callback, None
        if callback is None:
            return
        for index, current in enumerate(gc.callbacks):
            if current is callback:
                del gc.callbacks[index]
                break

    async def close(self):
        self._closed = True
        self._detach_callback()
        task = self._task
        if task is None:
            return
        task.cancel()
        joined = asyncio.gather(task, return_exceptions=True)
        cancelled = False
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                cancelled = True
        joined.result()
        self._task = None
        if cancelled:
            raise asyncio.CancelledError


@asynccontextmanager
async def oms_loop_lag_logging(logger):
    monitor = OmsLoopLagWatchdog(logger)
    try:
        monitor.start()
    except Exception:
        pass  # Partial installation is still detached by the same owned finally.
    try:
        yield monitor
    finally:
        await monitor.close()


def with_oms_loop_lag_logging(method):
    @wraps(method)
    async def observed(service, *args, **kwargs):
        async with oms_loop_lag_logging(service.logger):
            return await method(service, *args, **kwargs)

    return observed
