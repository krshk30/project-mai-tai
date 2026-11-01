"""Deterministic observer controls; no rate controller or APP imports/execution."""

import asyncio
import ast
import gc
import hashlib
import json
from pathlib import Path
import socket
import threading
import weakref

import pytest

from tests.unit.pom_rate_stall_observer import StallCorrelation


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("instrumentation controls must remain offline")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)


class TraceOwner:
    """Controlled existing diagnostic contract, not a native/protection fixture."""

    def __init__(self):
        self.calls = []
        self.tick = 100
        self.error = None

    def _exit_gate_start(self, trace, gate):
        self.calls.append(("start", gate))
        if self.error:
            raise self.error
        self.tick += 10
        return self.tick

    def _exit_gate_end(self, trace, gate, started, outcome):
        self.calls.append(("end", gate))
        if self.error:
            raise self.error
        self.tick += 10
        value = trace["steps"].setdefault(gate, [started, 0, 0, 0, 0, None])
        value[1], value[4], value[5] = self.tick, value[4] + 1, outcome
        return "unchanged_return"


def trace():
    return {"identity": "CONTROLLED-episode", "steps": {}}


def test_nested_same_gate_uses_each_exact_app_start_end_once(monkeypatch):
    observer = StallCorrelation(clock_ns=lambda: 1000)
    owner, frame = TraceOwner(), trace()
    observer.bind_steps(owner, monkeypatch)
    first = owner._exit_gate_start(frame, "positions")
    second = owner._exit_gate_start(frame, "positions")
    assert owner._exit_gate_end(frame, "positions", second, "inner") == "unchanged_return"
    assert owner._exit_gate_end(frame, "positions", first, "outer") == "unchanged_return"
    observer.close()
    intervals = observer.receipt()["step_intervals"]
    assert [(row["start_ns"], row["end_ns"], row["outcome"]) for row in intervals] == [
        (120, 130, "inner"), (110, 140, "outer")]
    assert owner.calls == [("start", "positions"), ("start", "positions"),
                           ("end", "positions"), ("end", "positions")]
    assert all(row["timestamp_basis"] == "exact_APP_start_end" for row in intervals)


@pytest.mark.asyncio
async def test_concurrent_tasks_same_trace_gate_have_distinct_task_identity(monkeypatch):
    owner, frame = TraceOwner(), trace()
    observer = StallCorrelation()
    observer.bind_steps(owner, monkeypatch)
    ready = asyncio.Event()
    started = 0

    async def operation():
        nonlocal started
        begin = owner._exit_gate_start(frame, "foreign_vp")
        started += 1
        if started == 2:
            ready.set()
        await ready.wait()
        owner._exit_gate_end(frame, "foreign_vp", begin, "DONE")

    await asyncio.gather(asyncio.create_task(operation(), name="controlled-a"),
                         asyncio.create_task(operation(), name="controlled-b"))
    observer.close()
    rows = observer.receipt()["step_intervals"]
    assert len(rows) == 2 and len({row["task_id"] for row in rows}) == 2
    assert {row["task_name"] for row in rows} == {"controlled-a", "controlled-b"}
    assert len({row["trace_id"] for row in rows}) == 1
    assert all(row["interval_status"] == "CLOSED" for row in rows)


def test_gc_worker_callbacks_removed_and_preexisting_callback_preserved():
    def existing(phase, info):
        pass

    gc.callbacks.append(existing)
    observer = StallCorrelation()
    observer.install_gc()
    callback = observer._callback
    thread_id = []

    def collect():
        thread_id.append(threading.get_ident())
        gc.collect(0)

    worker = threading.Thread(target=collect)
    try:
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
    finally:
        observer.close()
        assert existing in gc.callbacks
        gc.callbacks.remove(existing)
    rows = observer.receipt()["gc_intervals"]
    assert any(row["thread_id"] == thread_id[0] and row["generation"] == 0
               and row["interval_status"] == "CLOSED" for row in rows)
    assert callback not in gc.callbacks
    assert observer._callback is None
    observer.close()


def test_finally_cleanup_no_callback_or_runtime_reference_cycle(monkeypatch):
    observer, owner = StallCorrelation(), TraceOwner()
    observer_ref, owner_ref = weakref.ref(observer), weakref.ref(owner)
    observer.bind_steps(owner, monkeypatch)
    observer.install_gc()
    callback = observer._callback
    try:
        raise RuntimeError("CONTROLLED primary failure")
    except RuntimeError:
        pass
    finally:
        observer.close()
        monkeypatch.undo()
    del owner, observer
    gc.collect(0)
    assert owner_ref() is None and observer_ref() is None
    assert callback not in gc.callbacks


def test_failed_observation_cannot_change_original_return_or_exception(monkeypatch):
    def broken_clock():
        raise RuntimeError("CONTROLLED diagnostic failure")

    observer, owner, frame = StallCorrelation(clock_ns=broken_clock), TraceOwner(), trace()
    observer.bind_steps(owner, monkeypatch)
    observer.install_gc()
    callback = observer._callback
    started = owner._exit_gate_start(frame, "positions")
    assert started == 110
    assert owner._exit_gate_end(frame, "positions", started, "DONE") == "unchanged_return"
    primary = ValueError("CONTROLLED original error")
    owner.error = primary
    with pytest.raises(ValueError) as caught:
        owner._exit_gate_start(frame, "positions")
    assert caught.value is primary
    with pytest.raises(ValueError) as caught:
        owner._exit_gate_end(frame, "positions", started, "UNKNOWN")
    assert caught.value is primary
    observer.close()
    assert callback not in gc.callbacks and observer.errors > 0
    assert not observer.receipt()["recording_complete"]
    assert len(owner.calls) == 4


def test_missing_start_end_and_aggregate_end_are_unknown(monkeypatch):
    observer, owner, frame = StallCorrelation(clock_ns=lambda: 500), TraceOwner(), trace()
    observer.bind_steps(owner, monkeypatch)
    owner._exit_gate_start(frame, "terminal_after_prepare")  # Intentionally never ended.
    observer.step_event("end", frame, "positions", 200, observed_ns=300, ended=250)
    observer.lag_sample(0, .0000004)
    receipt = observer.receipt(active_start=0)  # Snapshot before close is supported.
    assert {row["interval_status"] for row in receipt["step_intervals"]} == {
        "END_UNKNOWN", "START_UNKNOWN"}
    assert receipt["max_stall"]["phase"] == "active_END_UNKNOWN"
    assert all(row["overlap_basis"] == "POSSIBLE_BOUNDARY_UNKNOWN"
               for row in receipt["max_stall"]["overlapping_steps"])
    # A preexisting aggregate must not become a fresh exact end timestamp.
    frame["steps"]["unchanged"] = [1, 123, 0, 0, 1, "OLD"]
    owner.error = ValueError("no APP end")
    with pytest.raises(ValueError):
        owner._exit_gate_end(frame, "unchanged", 2, "UNKNOWN")
    observer.close()
    rows = [row for row in observer.receipt()["step_intervals"] if row["gate"] == "unchanged"]
    assert rows[0]["app_end_ns"] is None


def test_max_original_lag_nested_gc_overlap_and_uncertainty_never_claim_cause():
    observer = StallCorrelation(clock_ns=lambda: 160)
    frame = trace()
    observer.step_event("start", frame, "positions", 105, observed_ns=100)
    observer.step_event("start", frame, "foreign_vp", 115, observed_ns=110)
    observer.step_event("end", frame, "foreign_vp", 115, observed_ns=131, ended=130)
    observer.step_event("end", frame, "positions", 105, observed_ns=151, ended=150)
    observer.step_event("start", frame, "tail", 145, observed_ns=144)
    observer.step_event("end", frame, "tail", 145, observed_ns=157, ended=155)
    observer._append("gc", ("start", 118, 2, 9001, 0, 0))
    observer._append("gc", ("stop", 128, 2, 9001, 10, 0))
    original_stalls = [.000000010, .000000030]
    observer.lag_sample(.000000100, original_stalls[0])
    observer.lag_sample(.000000110, original_stalls[1])
    observer.close()
    receipt = observer.receipt(active_start=.000000120, active_end=.000000200)
    maximum = receipt["max_stall"]
    assert original_stalls == [.000000010, .000000030]
    assert maximum["original_lag_seconds"] == max(original_stalls)
    assert maximum["derived_sample_ns"] == 140
    assert maximum["post_append_observed_ns"] == 160
    assert maximum["observation_uncertainty_ns"] == 20
    assert maximum["phase"] == "crosses_active_start"
    assert {row["gate"]: row["overlap_basis"] for row in maximum["overlapping_steps"]} == {
        "positions": "DERIVED_LAG_WINDOW", "foreign_vp": "DERIVED_LAG_WINDOW",
        "tail": "POST_SAMPLE_OBSERVATION_UNCERTAINTY_ONLY"}
    assert maximum["overlapping_gc"][0]["thread_id"] == 9001
    assert receipt["interpretation"] == "CORRELATION_ONLY_NOT_CAUSE"
    assert "not an independent wake timestamp" in receipt["sample_time_basis"]


@pytest.mark.parametrize("start,end,expected", [
    (1, 2, "pre_active"), (9, 11, "crosses_active_start"), (10, 12, "active"),
    (19, 21, "crosses_active_end"), (20, 21, "post_active")])
def test_phase_does_not_reset_original_watcher(start, end, expected):
    assert StallCorrelation._phase(start, end, 10, 20) == expected


def test_caps_disclose_incomplete_recording_without_altering_original_calls(monkeypatch):
    observer, owner, frame = StallCorrelation(lag_cap=1, step_cap=1, gc_cap=1), TraceOwner(), trace()
    observer.bind_steps(owner, monkeypatch)
    started = owner._exit_gate_start(frame, "positions")
    assert owner._exit_gate_end(frame, "positions", started, "DONE") == "unchanged_return"
    observer.lag_sample(1, .01)
    observer.lag_sample(2, .02)
    observer._append("gc", ("start", 0, 2, 1, 0, 0))
    observer._append("gc", ("stop", 1, 2, 1, 0, 0))
    observer.close()
    receipt = observer.receipt()
    assert receipt["dropped"] == {"lag": 1, "step": 1, "gc": 1}
    assert not receipt["recording_complete"] and len(owner.calls) == 2


def test_original_rate_assertions_and_code_identical_after_diagnostic_strip():
    source = Path(__file__).with_name("test_pomexitlog1_v4_rate.py").read_text()
    retained, inside = [], False
    for line in source.splitlines(keepends=True):
        if "# STALL-CORRELATION diagnostics BEGIN" in line:
            assert not inside
            inside = True
        elif "# STALL-CORRELATION diagnostics END" in line:
            assert inside
            inside = False
        elif not inside:
            retained.append(line)
    assert not inside
    # Exact base7e1 bytes; this control never imports or executes the rate module.
    assert hashlib.sha256("".join(retained).encode()).hexdigest() == (
        "ecec23f4230ba22c2e5215e3611eb7246e283734c4c303522c554ae96fa7c27d")
    assertions = [ast.dump(node, include_attributes=False) for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Assert)]
    assert len(assertions) == 22
    assert hashlib.sha256(json.dumps(assertions).encode()).hexdigest() == (
        "d7bcc1b690743a2f92bd90696ca557ae427975877d64214bd0aa9ac34f5724af")
