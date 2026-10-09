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


def process_state():
    modules = sorted(name for name in sys.modules if name.startswith("project_mai_tai"))
    return dict(gc_enabled=gc.isenabled(), thresholds=gc.get_threshold(),
                allocation_counts=gc.get_count(), generation_stats=gc.get_stats(),
                frozen_count=gc.get_freeze_count(), imported_module_count=len(sys.modules),
                project_modules=modules[:256], project_modules_total=len(modules))


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
        self.drift_read = ContextVar("i_close_observer_drift_read", default=False)
        self.records = deque(maxlen=512)
        self.total = 0
        self.first = {}
        self.worst = {}
        self.pump_wakes = 0
        self.pump_max_late = float("-inf")
        self.first_late_pump = False
        self.active = None
        self.stop_event = threading.Event()
        self.sampler = None
        self.gc_started = {}
        self.commits = {}
        self.initial_state = None
        self.final_state = None

    def record(self, kind, begin, **data):
        self.total += 1
        row = dict(sequence=self.total, kind=kind, begin=begin, end=monotonic(), **data)
        self.records.append(row)
        key = (kind, data.get("thread"))
        if key in self.first or len(self.first) < 32:
            self.first.setdefault(key, row)
            previous = self.worst.get(key)
            if previous is None or self.score(row) > self.score(previous):
                self.worst[key] = row

    @staticmethod
    def score(row):
        return max(row.get("wall_ms", 0), row.get("late_ms", 0))

    def quote(self, original):
        @wraps(original)
        async def call(instance, *args, **kwargs):
            caller = sys._getframe(1)
            if caller.f_code.co_name == "quote_pump":
                due, actual = caller.f_locals["due"], caller.f_locals["begin"]
                late = (actual - due) * 1000
                self.pump_wakes += 1
                if self.pump_wakes == 1 or late > self.pump_max_late or late >= 10:
                    data = dict(due=due, actual=actual, late_ms=late,
                                site=(caller.f_code.co_filename, caller.f_lineno),
                                frames=frames(caller, 8) if late >= 10 else [])
                    self.record("pump_deadline", actual, **data)
                    if late >= 10 and not self.first_late_pump:
                        self.record("first_late_pump", actual, **data)
                        self.first_late_pump = True
                self.pump_max_late = max(self.pump_max_late, late)
            del caller
            return await original(instance, *args, **kwargs)
        return call

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
                self.record("active_loop_stack", active[0], wall_ms=(now - active[0]) * 1000,
                            sampled_at=now, thread="loop", callback=active[1], site=active[2],
                            frames=frames(sys._current_frames().get(self.loop_thread)))
            for thread, (begin, operation) in list(self.commits.items()):
                if now - begin >= 0.010:
                    self.record("commit_stack", begin, wall_ms=(now - begin) * 1000,
                                sampled_at=now, operation=operation,
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

    def collect_drift(self, original):
        @wraps(original)
        def call(instance, *args, **kwargs):
            if self.operation.get() is None:
                return original(instance, *args, **kwargs)
            token = self.drift_read.set(True)
            begin, cpu = monotonic(), thread_time()
            result = None
            try:
                result = original(instance, *args, **kwargs)
                return result
            finally:
                self.drift_read.reset(token)
                self.record("drift_read", begin, wall_ms=(monotonic() - begin) * 1000,
                            cpu_ms=(thread_time() - cpu) * 1000, operation=self.operation.get(),
                            thread="loop" if threading.get_ident() == self.loop_thread else "worker",
                            candidate_count=len(result) if result is not None else None)
        return call

    def materialize(self, original):
        @wraps(original)
        def call(result, *args, **kwargs):
            if not self.drift_read.get():
                return original(result, *args, **kwargs)
            caller = sys._getframe(1)
            site = (caller.f_code.co_filename, caller.f_code.co_name, caller.f_lineno)
            del caller
            begin, cpu = monotonic(), thread_time()
            rows = None
            try:
                rows = original(result, *args, **kwargs)
                return rows
            finally:
                self.record("drift_materialize", begin, wall_ms=(monotonic() - begin) * 1000,
                            cpu_ms=(thread_time() - cpu) * 1000, operation=self.operation.get(),
                            thread="loop" if threading.get_ident() == self.loop_thread else "worker",
                            site=site, row_count=len(rows) if rows is not None else None,
                            row_type=type(rows[0]).__name__ if rows else None)
        return call

    def start(self):
        self.initial_state = process_state()
        gc.callbacks.append(self.gc_event)
        self.sampler = threading.Thread(target=self.sample, name="i-loop-observer", daemon=True)
        self.sampler.start()

    def stop(self):
        self.stop_event.set()
        self.sampler.join(1)
        gc.callbacks.remove(self.gc_event)
        self.final_state = process_state()

    def receipt(self):
        # Protect first/worst records before spending the remaining budget on the tail.
        protected = {row["sequence"]: row for row in (*self.first.values(), *self.worst.values())}
        selected = dict(protected)
        for row in reversed(list(self.records)):
            if len(selected) >= self.records.maxlen:
                break
            selected.setdefault(row["sequence"], row)
        records = sorted(selected.values(), key=lambda row: row["sequence"])
        return dict(observer="F/I bounded diagnostic, NOT the raw acceptance gate", clock="monotonic",
                    initial_process_state=self.initial_state, final_process_state=self.final_state,
                    sample_interval_ms=5, span_floor_ms=10, capacity=self.records.maxlen,
                    records_total=self.total, dropped=max(0, self.total - len(records)),
                    protected=len(protected), protected_group_limit=32,
                    pump_wakes=self.pump_wakes,
                    pump_max_late_ms=self.pump_max_late if self.pump_wakes else None,
                    sampler_joined=not self.sampler.is_alive(), records=records)


@pytest.fixture(autouse=True)
def closed_owner_latency_observer(request, monkeypatch):
    if request.node.name != TARGET:
        yield
        return
    from sqlalchemy.engine import Result, ScalarResult
    from sqlalchemy.orm import Session
    from project_mai_tai.oms.buy_submission_journal import DurableBuyAdapter
    from project_mai_tai.oms.service import OmsRiskService

    observer = Observer()
    capture = request.getfixturevalue("capsys")
    with monkeypatch.context() as patch:
        patch.setattr(asyncio.Handle, "_run", observer.callback(asyncio.Handle._run))
        patch.setattr(Session, "commit", observer.commit(Session.commit))
        patch.setattr(Result, "all", observer.materialize(Result.all))
        patch.setattr(ScalarResult, "all", observer.materialize(ScalarResult.all))
        patch.setattr(OmsRiskService, "_collect_drift_cancel_candidates", observer.collect_drift(
            OmsRiskService._collect_drift_cancel_candidates))
        patch.setattr(OmsRiskService, "_handle_quote_tick_event",
                      observer.quote(OmsRiskService._handle_quote_tick_event))
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
