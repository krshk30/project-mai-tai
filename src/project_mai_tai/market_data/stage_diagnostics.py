"""Bounded observational timings; never a health verdict or a timeout policy."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import threading
import time


class StageDiagnostics:
    def __init__(self):
        self._lock = threading.Lock()
        self._sequence = 0
        self._records = deque(maxlen=32)
        self._slow = deque(maxlen=8)
        self._counts = {}
        self._local = threading.local()

    def _begin(self, phase):
        with self._lock:
            self._sequence += 1
            # id, phase, queued monotonic, started, done, thread CPU, queued UTC, error
            row = [self._sequence, phase, time.monotonic_ns(), None, None, None,
                   time.time_ns(), None]
            self._records.append(row)
            return row

    def _finish(self, row, cpu, error):
        with self._lock:
            row[4:6] = [time.monotonic_ns(), cpu]
            row[7] = error
            wall = (row[4] - row[2]) / 1e6
            queue = (row[3] - row[2]) / 1e6
            counts = self._counts.setdefault(row[1], [0, 0, 0.0, 0.0, 0.0])
            counts[0] += 1
            counts[1] += wall > 500
            counts[2] = max(counts[2], wall)
            counts[3] = max(counts[3], (cpu or 0) / 1e6)
            counts[4] = max(counts[4], queue)
            if wall > 500:
                self._slow.append(list(row))

    def _sync(self, row, function, args, kwargs):
        with self._lock:
            row[3] = time.monotonic_ns()
        before = time.thread_time_ns()
        error = None
        prior = getattr(self._local, "phases", ())
        self._local.phases = (*prior, row[1])
        try:
            return function(*args, **kwargs)
        except BaseException as exc:
            error = type(exc).__name__[:80]
            raise
        finally:
            self._local.phases = prior
            self._finish(row, time.thread_time_ns() - before, error)

    def sync(self, phase, function, *args, **kwargs):
        return self._sync(self._begin(phase), function, args, kwargs)

    async def thread(self, phase, function, *args, **kwargs):
        row = self._begin(phase)
        return await asyncio.to_thread(self._sync, row, function, args, kwargs)

    async def asynchronous(self, phase, operation):
        row = self._begin(phase)
        with self._lock:
            row[3] = time.monotonic_ns()
        error = None
        try:
            return await operation
        except BaseException as exc:
            error = type(exc).__name__[:80]
            raise
        finally:
            # CPU across an await is not attributable to this task.
            self._finish(row, None, error)

    def in_snapshot_fetch(self):
        return "snapshot_fetch" in getattr(self._local, "phases", ())

    def snapshot(self):
        with self._lock:
            result = {
                "scope": "OBSERVATIONAL_WALL_QUEUE_AND_THREAD_CPU_NOT_GIL_PROOF",
                "columns": ["id", "phase", "queued_mono_ns", "started_mono_ns", "done_mono_ns",
                            "thread_cpu_ns", "queued_utc_ns", "error"],
                "count_columns": ["completed", "over_500ms", "max_wall_ms", "max_thread_cpu_ms", "max_queue_ms"],
                "counts": {name: list(values) for name, values in self._counts.items()},
                "recent": [list(row) for row in list(self._records)[-8:]],
                "slow": [list(row) for row in self._slow],
            }
        # Telemetry must not crowd out the original bounded health/A metrics.
        while len(json.dumps(result, separators=(",", ":")).encode()) > 4096:
            if result["recent"]:
                result["recent"].pop(0)
            elif result["slow"]:
                result["slow"].pop(0)
            else:
                break
        return result
