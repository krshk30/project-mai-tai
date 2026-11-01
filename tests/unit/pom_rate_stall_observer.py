"""Bounded test-only timestamp correlation; overlap never establishes cause."""

import asyncio
import gc
import threading
import time
import weakref


class StallCorrelation:
    LAG_FIELDS = ("deadline_ns", "original_lag_seconds", "post_append_observed_ns")
    STEP_FIELDS = ("event", "observed_ns", "app_start_ns", "app_end_ns", "trace_id",
                   "task_id", "thread_id", "task_name", "episode_id", "gate", "outcome")
    GC_FIELDS = ("event", "observed_ns", "generation", "thread_id", "collected", "uncollectable")

    def __init__(self, *, clock_ns=time.monotonic_ns, lag_cap=30000, step_cap=50000,
                 gc_cap=10000):
        self.clock_ns = clock_ns
        self.caps = {"lag": lag_cap, "step": step_cap, "gc": gc_cap}
        self.rows = {"lag": [], "step": [], "gc": []}
        self.dropped = {key: 0 for key in self.rows}
        self.errors = 0
        self.started_ns = self._now()
        self.ended_ns = None
        self.closed = False
        self._callback = None

    def _now(self):
        try:
            return self.clock_ns()
        except Exception:
            self.errors += 1
            return None

    def _append(self, kind, row):
        if self.closed:
            return
        if len(self.rows[kind]) >= self.caps[kind]:
            self.dropped[kind] += 1
        else:
            self.rows[kind].append(row)

    def lag_sample(self, deadline, lag):
        """Retain the ORIGINAL lag, plus an independently read post-append time."""
        try:
            self._append("lag", (round(deadline * 1e9), lag, self._now()))
        except Exception:
            self.errors += 1

    @staticmethod
    def _context(trace):
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        return (id(trace) if trace is not None else None, id(task) if task else None,
                threading.get_ident(), task.get_name() if task else None,
                trace.get("identity") if isinstance(trace, dict)
                and isinstance(trace.get("identity"), str) else None)

    def step_event(self, event, trace, gate, started, *, observed_ns, ended=None, outcome=None):
        try:
            self._append("step", (event, observed_ns, started, ended, *self._context(trace),
                                  gate, outcome))
        except Exception:
            self.errors += 1

    @staticmethod
    def _app_step(trace, gate):
        try:
            row = trace["steps"][gate]
            return row[1], row[4]
        except Exception:
            return None, None

    def bind_steps(self, runtime, monkeypatch):
        # Weak bound-method references avoid runtime -> wrapper -> runtime cycles.
        start_method = weakref.WeakMethod(runtime._exit_gate_start)
        end_method = weakref.WeakMethod(runtime._exit_gate_end)
        observer = self

        def start(trace, gate):
            observed = observer._now()
            try:
                result = start_method()(trace, gate)
            except BaseException as error:
                observer.step_event("start", trace, gate, None, observed_ns=observed,
                    outcome="original_raised:" + type(error).__name__)
                raise
            observer.step_event("start", trace, gate, result, observed_ns=observed)
            return result

        def end(trace, gate, started, outcome):
            _, before_count = observer._app_step(trace, gate)
            try:
                return end_method()(trace, gate, started, outcome)
            finally:
                ended, after_count = observer._app_step(trace, gate)
                # Prefer the APP's exact end timestamp, never a previous call's aggregate.
                changed = (type(after_count) is int and after_count == (before_count or 0) + 1)
                observer.step_event("end", trace, gate, started, observed_ns=observer._now(),
                    ended=ended if changed else None, outcome=outcome)

        monkeypatch.setattr(runtime, "_exit_gate_start", start)
        monkeypatch.setattr(runtime, "_exit_gate_end", end)

    def install_gc(self):
        if self._callback is not None or self.closed:
            return
        owner = weakref.ref(self)

        def callback(phase, info):
            observer = owner()
            if observer is not None:
                try:
                    observer._append("gc", (phase, observer._now(), info.get("generation"),
                        threading.get_ident(), info.get("collected"), info.get("uncollectable")))
                except Exception:
                    observer.errors += 1

        self._callback = callback
        gc.callbacks.append(callback)

    def close(self):
        if self.closed:
            return
        self.ended_ns = self._now()
        self.closed = True
        callback, self._callback = self._callback, None
        for index, current in enumerate(gc.callbacks):
            if current is callback:
                del gc.callbacks[index]
                break

    @staticmethod
    def _phase(start, end, active_start, active_end):
        if active_start is None:
            return "pre_active_NO_ACTIVE_START_OBSERVED"
        if end < active_start:
            return "pre_active"
        if start < active_start <= end:
            return "crosses_active_start"
        if active_end is None:
            return "active_END_UNKNOWN"
        if start >= active_end:
            return "post_active"
        if end > active_end:
            return "crosses_active_end"
        return "active"

    def _step_intervals(self):
        opened, intervals = {}, []
        for row in self.rows["step"]:
            event = dict(zip(self.STEP_FIELDS, row))
            key = tuple(event[field] for field in
                ("trace_id", "task_id", "thread_id", "episode_id", "gate", "app_start_ns"))
            if event["event"] == "start":
                opened.setdefault(key, []).append(event)
                continue
            stack = opened.get(key, [])
            start = stack.pop() if stack else None
            intervals.append(self._step_interval(start, event))
        for stack in opened.values():
            intervals.extend(self._step_interval(start, None) for start in stack)
        return intervals

    @staticmethod
    def _step_interval(start, end):
        value = start or end
        app_start = value["app_start_ns"]
        app_end = end["app_end_ns"] if end else None
        return {**{key: value[key] for key in ("trace_id", "task_id", "thread_id", "task_name",
                    "episode_id", "gate")},
            "start_ns": app_start if type(app_start) is int else start["observed_ns"] if start else None,
            "end_ns": app_end if type(app_end) is int else end["observed_ns"] if end else None,
            "app_start_ns": app_start, "app_end_ns": app_end,
            "observed_start_ns": start["observed_ns"] if start else None,
            "observed_end_ns": end["observed_ns"] if end else None,
            "timestamp_basis": "exact_APP_start_end" if type(app_start) is int and type(app_end) is int
                else "observed_bounds_APP_timestamp_UNKNOWN",
            "interval_status": "CLOSED" if start and end else "START_UNKNOWN" if end else "END_UNKNOWN",
            "outcome": end["outcome"] if end else None}

    def _gc_intervals(self):
        opened, intervals = {}, []
        for row in self.rows["gc"]:
            event = dict(zip(self.GC_FIELDS, row))
            key = event["thread_id"], event["generation"]
            if event["event"] == "start":
                opened.setdefault(key, []).append(event)
            elif event["event"] == "stop":
                stack = opened.get(key, [])
                start = stack.pop() if stack else None
                intervals.append({"generation": event["generation"], "thread_id": event["thread_id"],
                    "start_ns": start["observed_ns"] if start else None,
                    "end_ns": event["observed_ns"], "interval_status": "CLOSED" if start else "START_UNKNOWN"})
        for stack in opened.values():
            intervals.extend({"generation": start["generation"], "thread_id": start["thread_id"],
                "start_ns": start["observed_ns"], "end_ns": None, "interval_status": "END_UNKNOWN"}
                for start in stack)
        return intervals

    def receipt(self, *, active_start=None, active_end=None):
        snapshot = self.ended_ns if self.closed else self._now()
        steps, collections = self._step_intervals(), self._gc_intervals()
        sample = max(self.rows["lag"], key=lambda row: row[1], default=None)
        maximum = None
        if sample is not None:
            due, lag, observed = sample
            derived = due + round(lag * 1e9)
            active_ns = round(active_start * 1e9) if active_start is not None else None
            end_ns = round(active_end * 1e9) if active_end is not None else None
            maximum = {"deadline_ns": due, "original_lag_seconds": lag,
                "derived_sample_ns": derived, "post_append_observed_ns": observed,
                "observation_uncertainty_ns": observed - derived if observed is not None else None,
                "phase": self._phase(due, derived, active_ns, end_ns),
                "overlapping_steps": [], "overlapping_gc": []}
            for kind, intervals in (("overlapping_steps", steps), ("overlapping_gc", collections)):
                for interval in intervals:
                    start, end = interval["start_ns"], interval["end_ns"]
                    upper = observed if observed is not None else derived
                    # Open/missing boundaries remain explicit, never fabricated as closed.
                    if (start is None or start <= upper) and (end is None or end >= due):
                        if interval["interval_status"] != "CLOSED" or start is None or end is None:
                            overlap = "POSSIBLE_BOUNDARY_UNKNOWN"
                        elif start <= derived:
                            overlap = "DERIVED_LAG_WINDOW"
                        else:
                            overlap = "POST_SAMPLE_OBSERVATION_UNCERTAINTY_ONLY"
                        maximum[kind].append({**interval, "overlap_basis": overlap})
        return {"interpretation": "CORRELATION_ONLY_NOT_CAUSE",
            "sample_time_basis": "derived_sample_ns is deadline + original lag, not an independent wake timestamp; "
                "post_append_observed_ns is independently sampled afterward; their gap bounds observation uncertainty",
            "instrumentation_cost": "test-only sampling/step/GC observations run inside the unchanged measured gates",
            "started_ns": self.started_ns, "ended_ns": self.ended_ns,
            "snapshot_through_ns": snapshot,
            "recording_complete": not self.errors and not any(self.dropped.values()),
            "errors": self.errors, "dropped": dict(self.dropped),
            "lag_fields": self.LAG_FIELDS, "lag_samples": list(self.rows["lag"]),
            "step_fields": self.STEP_FIELDS, "step_events": list(self.rows["step"]),
            "gc_fields": self.GC_FIELDS, "gc_events": list(self.rows["gc"]),
            "step_intervals": steps, "gc_intervals": collections, "max_stall": maximum}
