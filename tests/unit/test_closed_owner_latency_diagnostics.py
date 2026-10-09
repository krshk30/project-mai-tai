import asyncio
import gc
from types import SimpleNamespace
from time import sleep
from time import monotonic

import pytest

from tests.support.closed_owner_latency_diagnostics import Observer


@pytest.mark.asyncio
async def test_observer_attributes_real_loop_callback_without_swallowing_exception(monkeypatch):
    observer = Observer()
    original = asyncio.Handle._run
    monkeypatch.setattr(asyncio.Handle, "_run", observer.callback(original))
    observer.start()
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context["exception"]))

    def controlled_loop_block():
        sleep(0.040)
        done.set()
        raise ValueError("controlled callback failure")

    try:
        loop.call_soon(controlled_loop_block)
        await done.wait()
    finally:
        observer.stop()
        loop.set_exception_handler(previous_handler)
    callbacks = [row for row in observer.records if row["kind"] == "loop_callback"]
    assert any("controlled_loop_block" in row["callback"] and row["wall_ms"] >= 40
               for row in callbacks)
    stacks = [row for row in observer.records if row["kind"] == "active_loop_stack"]
    assert any(any(frame[1] == "controlled_loop_block" for frame in row["frames"]) for row in stacks)
    assert observer.receipt()["sampler_joined"]
    assert observer.gc_event not in gc.callbacks
    assert len(errors) == 1 and str(errors[0]) == "controlled callback failure"


@pytest.mark.asyncio
async def test_worker_commit_span_propagates_operation_and_preserves_result_and_failure():
    observer = Observer()

    def slow_commit(session):
        sleep(0.030)
        if session.fail:
            raise ValueError("controlled commit failure")
        return "committed"

    wrapped = observer.commit(slow_commit)
    observer.start()
    token = observer.operation.set("EXIT75")
    try:
        assert await asyncio.to_thread(wrapped, SimpleNamespace(fail=False)) == "committed"
        with pytest.raises(ValueError, match="controlled commit failure"):
            await asyncio.to_thread(wrapped, SimpleNamespace(fail=True))
    finally:
        observer.operation.reset(token)
        observer.stop()
    commits = [row for row in observer.records if row["kind"] == "session_commit"]
    assert len(commits) == 2
    assert all(row["operation"] == "EXIT75" and row["thread"] == "worker"
               and row["wall_ms"] >= 30 for row in commits)
    assert any(row["kind"] == "commit_stack"
               and any(frame[1] == "slow_commit" for frame in row["frames"])
               for row in observer.records)
    assert observer.operation.get() is None


@pytest.mark.asyncio
async def test_async_observer_does_not_change_cancellation_fence_or_operation_lifetime():
    observer = Observer()
    started, finish = asyncio.Event(), asyncio.Event()
    completed = []

    async def fenced(_self, _event):
        worker = asyncio.create_task(finish.wait())
        started.set()
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
        completed.append(observer.operation.get())
        if cancelled:
            raise asyncio.CancelledError
        return "accepted"

    wrapped = observer.async_span("intent", fenced, intent=True)
    event = SimpleNamespace(payload=SimpleNamespace(side="sell", intent_type="close", symbol="EXIT75"))
    task = asyncio.create_task(wrapped(None, event))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not completed
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed == ["EXIT75"]
    assert observer.operation.get() is None
    assert observer.records[-1]["kind"] == "intent"


@pytest.mark.asyncio
async def test_async_observer_preserves_success_exception_and_nonclose_scope():
    observer = Observer()
    operations = []

    async def original(_self, event):
        operations.append(observer.operation.get())
        if event.fail:
            raise ValueError("controlled intent failure")
        return event.result

    wrapped = observer.async_span("intent", original, intent=True)
    payload = SimpleNamespace(side="sell", intent_type="close", symbol="EXIT75")
    event = SimpleNamespace(payload=payload, fail=False, result=object())
    assert await wrapped(None, event) is event.result
    event.fail = True
    with pytest.raises(ValueError, match="controlled intent failure"):
        await wrapped(None, event)
    event.fail = False
    payload.side = "buy"
    assert await wrapped(None, event) is event.result
    assert operations == ["EXIT75", "EXIT75", None]
    assert observer.operation.get() is None and len(observer.records) == 2


def test_actual_gc_span_has_thread_and_trigger_frame_without_changing_gc_policy():
    observer = Observer()

    class ControlledCycle:
        def __init__(self):
            self.cycle = self

        def __del__(self):
            sleep(0.015)

    enabled, thresholds = gc.isenabled(), gc.get_threshold()
    observer.start()
    try:
        cycle = ControlledCycle()
        del cycle
        gc.collect()
    finally:
        observer.stop()
    records = [row for row in observer.records if row["kind"] == "gc"]
    assert any(row["wall_ms"] >= 15 and row["thread"] == "loop"
               and any(frame[1] == "test_actual_gc_span_has_thread_and_trigger_frame_without_changing_gc_policy"
                       for frame in row["frames"]) for row in records)
    assert gc.isenabled() == enabled and gc.get_threshold() == thresholds


def test_observer_records_are_bounded_and_overflow_is_explicit():
    observer = Observer()
    for index in range(600):
        observer.record("controlled", index)
    observer.start()
    observer.stop()
    receipt = observer.receipt()
    assert receipt["records_total"] == 600 and receipt["dropped"] == 88
    assert len(receipt["records"]) == receipt["capacity"] == 512


@pytest.mark.asyncio
async def test_timer_lateness_is_separate_from_callback_execution_time(monkeypatch):
    observer = Observer()
    monkeypatch.setattr(asyncio.Handle, "_run", observer.callback(asyncio.Handle._run))
    observer.start()
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.call_later(0.001, done.set)
        loop.call_soon(sleep, 0.060)
        await done.wait()
    finally:
        observer.stop()
    assert any(row["kind"] == "late_timer" and row["late_ms"] >= 50
               and row["callback"] == "Event.set" for row in observer.records)
    assert any(row["kind"] == "loop_callback" and row["callback"] == "sleep"
               and row["wall_ms"] >= 60 for row in observer.records)


def test_overflow_preserves_first_and_worst_gc_and_pump_records_with_fixed_total_capacity():
    observer = Observer()
    for kind, thread in (("gc", "worker"), ("pump_deadline", None)):
        for score in (54, 80, 10):
            observer.record(kind, score, thread=thread, wall_ms=score)
    for index in range(3000):
        observer.record("intent", index, wall_ms=20)
    observer.start()
    observer.stop()
    receipt = observer.receipt()
    assert len(receipt["records"]) == 512 and receipt["dropped"] == 2494
    for kind in ("gc", "pump_deadline"):
        assert sorted(row["wall_ms"] for row in receipt["records"] if row["kind"] == kind) == [54, 80]
    assert receipt["protected"] <= 64


@pytest.mark.asyncio
async def test_quote_observer_copies_actual_deadline_without_changing_result_or_clock():
    observer = Observer()
    result = object()

    async def original(_self, event):
        assert event is result
        return event

    wrapped = observer.quote(original)
    observer.start()

    async def quote_pump():
        due = monotonic() + 0.001
        asyncio.get_running_loop().call_soon(sleep, 0.060)
        await asyncio.sleep(max(0, due - monotonic()))
        begin = monotonic()
        assert await wrapped(None, result) is result
        return due, begin

    try:
        due, begin = await quote_pump()
    finally:
        observer.stop()
    receipt = observer.receipt()
    assert receipt["pump_wakes"] == 1
    assert receipt["pump_max_late_ms"] == (begin - due) * 1000 >= 50
    row = next(row for row in receipt["records"] if row["kind"] == "pump_deadline")
    assert row["due"] == due and row["actual"] == begin
    assert row["frames"][0][1] == "quote_pump"
    assert any(row["kind"] == "first_late_pump" for row in receipt["records"])
