"""Bounded observers for the unchanged real-PG close-duration control only."""

import asyncio
from collections import deque
from contextvars import ContextVar
from functools import wraps
import gc
import json
import sys
import threading
from time import monotonic, thread_time

import pytest


TARGET = "test_actual_oms_240_events_per_second_concurrent_buys_and_30_second_read"


def frames(frame, limit=16):
    result = []
    while frame is not None and len(result) < limit:
        code = frame.f_code
        result.append((code.co_filename, code.co_name, frame.f_lineno))
        frame = frame.f_back
    return result


class Observer:
    def __init__(self):
        self.loop_thread = threading.get_ident()
        self.operation = ContextVar("i_close_observer_operation", default=None)
        self.records = deque(maxlen=512)
        self.total = 0
        self.active = None
        self.stop_event = threading.Event()
        self.sampler = None
        self.gc_started = {}
        self.commits = {}

    def record(self, kind, begin, **data):
        self.total += 1
        self.records.append(dict(kind=kind, begin=begin, end=monotonic(), **data))

    def callback(self, original):
        @wraps(original)
        def run(handle):
            begin, cpu = monotonic(), thread_time()
            callback = handle._callback
            task = getattr(callback, "__self__", None)
            coro = task.get_coro() if isinstance(task, asyncio.Task) else None
            frame = getattr(coro, "cr_frame", None)
            label = getattr(coro or callback, "__qualname__", type(callback).__name__)
            site = (frame.f_code.co_filename, frame.f_lineno) if frame else None
            previous = self.active
            self.active = (begin, label, site)
            try:
                return original(handle)
            finally:
                end = monotonic()
                self.active = previous
                if end - begin >= 0.010:
                    self.record("loop_callback", begin, wall_ms=(end - begin) * 1000,
                                cpu_ms=(thread_time() - cpu) * 1000, callback=label, site=site)
                if isinstance(handle, asyncio.TimerHandle):
                    late = begin - handle.when()
                    if late >= 0.050:
                        self.record("late_timer", begin, late_ms=late * 1000,
                                    callback=label, due=handle.when())
        return run

    def sample(self):
        previous = monotonic()
        while not self.stop_event.wait(0.005):
            now = monotonic()
            if now - previous >= 0.050:
                self.record("sampler_gap", previous, wall_ms=(now - previous) * 1000)
            previous = now
            active = self.active
            if active is not None and now - active[0] >= 0.010:
                self.record("active_loop_stack", now, callback=active[1], site=active[2],
                            frames=frames(sys._current_frames().get(self.loop_thread)))
            for thread, (begin, operation) in list(self.commits.items()):
                if now - begin >= 0.010:
                    self.record("commit_stack", now, operation=operation,
                                thread="loop" if thread == self.loop_thread else "worker",
                                frames=frames(sys._current_frames().get(thread)))

    def gc_event(self, phase, info):
        thread = threading.get_ident()
        if phase == "start":
            self.gc_started[thread] = (monotonic(), thread_time(), self.operation.get())
        elif phase == "stop" and thread in self.gc_started:
            begin, cpu, operation = self.gc_started.pop(thread)
            elapsed = (monotonic() - begin) * 1000
            if elapsed >= 10:
                self.record("gc", begin, wall_ms=elapsed, cpu_ms=(thread_time() - cpu) * 1000,
                            thread="loop" if thread == self.loop_thread else "worker",
                            operation=operation, generation=info["generation"],
                            frames=frames(sys._current_frames().get(thread)))

    def async_span(self, name, original, *, intent=False):
        @wraps(original)
        async def call(instance, *args, **kwargs):
            token = None
            if intent:
                payload = args[0].payload
                if payload.side != "sell" or payload.intent_type != "close":
                    return await original(instance, *args, **kwargs)
                token = self.operation.set(payload.symbol)
            operation = self.operation.get()
            if operation is None:
                return await original(instance, *args, **kwargs)
            begin = monotonic()
            entry_frames = frames(sys._getframe(1), 8) if name == "report_commit_await" else None
            try:
                return await original(instance, *args, **kwargs)
            finally:
                elapsed = (monotonic() - begin) * 1000
                if name in {"intent", "report_commit_await"} or elapsed >= 10:
                    self.record(name, begin, wall_ms=elapsed, operation=operation,
                                entry_frames=entry_frames)
                if token is not None:
                    self.operation.reset(token)
        return call

    def commit(self, original):
        @wraps(original)
        def call(session, *args, **kwargs):
            operation = self.operation.get()
            if operation is None:
                return original(session, *args, **kwargs)
            begin, cpu = monotonic(), thread_time()
            thread = threading.get_ident()
            self.commits[thread] = (begin, operation)
            try:
                return original(session, *args, **kwargs)
            finally:
                self.commits.pop(thread, None)
                self.record("session_commit", begin, wall_ms=(monotonic() - begin) * 1000,
                            cpu_ms=(thread_time() - cpu) * 1000, operation=operation,
                            thread="loop" if thread == self.loop_thread else "worker")
        return call

    def start(self):
        gc.callbacks.append(self.gc_event)
        self.sampler = threading.Thread(target=self.sample, name="i-loop-observer", daemon=True)
        self.sampler.start()

    def stop(self):
        self.stop_event.set()
        self.sampler.join(1)
        gc.callbacks.remove(self.gc_event)

    def receipt(self):
        return dict(observer="I bounded diagnostic, not a threshold waiver", clock="monotonic",
                    sample_interval_ms=5, span_floor_ms=10, capacity=self.records.maxlen,
                    records_total=self.total, dropped=max(0, self.total - len(self.records)),
                    sampler_joined=not self.sampler.is_alive(), records=list(self.records))


@pytest.fixture(autouse=True)
def closed_owner_latency_observer(request, monkeypatch):
    if request.node.name != TARGET:
        yield
        return
    from sqlalchemy.orm import Session
    from project_mai_tai.oms.buy_submission_journal import DurableBuyAdapter
    from project_mai_tai.oms.service import OmsRiskService

    observer = Observer()
    capture = request.getfixturevalue("capsys")
    with monkeypatch.context() as patch:
        patch.setattr(asyncio.Handle, "_run", observer.callback(asyncio.Handle._run))
        patch.setattr(Session, "commit", observer.commit(Session.commit))
        patch.setattr(OmsRiskService, "process_trade_intent",
                      observer.async_span("intent", OmsRiskService.process_trade_intent, intent=True))
        patch.setattr(DurableBuyAdapter, "commit_order_reports", observer.async_span(
            "report_commit_await", DurableBuyAdapter.commit_order_reports))
        for name in ("_record_order_reports", "_publish_order_event", "_flush_dirty_armed_stops"):
            patch.setattr(OmsRiskService, name, observer.async_span(name, getattr(OmsRiskService, name)))
        observer.start()
        try:
            yield
        finally:
            observer.stop()
            with capture.disabled():
                print("[I-LOOP-ATTRIBUTION] " + json.dumps(observer.receipt()), flush=True)
