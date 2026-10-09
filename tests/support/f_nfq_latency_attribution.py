"""Diagnostic-only NFQ capture; canonical timing assertions remain unchanged."""

import asyncio
from functools import wraps
import json
import sys
import threading
from time import monotonic, thread_time

import pytest

try:
    from .f_caller_latency_attribution import Observer, frames
except ImportError:
    from f_caller_latency_attribution import Observer, frames


TARGETS = {
    "test_nfq2_slow_save_does_not_stall_loop_and_concurrent_quotes_queue_once",
    "test_combined_slow_eligible_persistence_offloop_duplicate_quotes",
}


class NfqObserver(Observer):
    def gather(self, original):
        @wraps(original)
        def call(*args, **kwargs):
            caller = sys._getframe(1)
            selected = caller.f_code.co_name in TARGETS and len(args) >= 1000
            entry = frames(caller, 8) if selected else None
            del caller
            if not selected:
                return original(*args, **kwargs)
            count = len(args)
            begin, cpu = monotonic(), thread_time()
            future = original(*args, **kwargs)
            self.record("nfq_gather_setup", begin, wall_ms=(monotonic() - begin) * 1000,
                        cpu_ms=(thread_time() - cpu) * 1000, count=count, frames=entry)

            def completed(done):
                self.record("nfq_gather_total", begin, wall_ms=(monotonic() - begin) * 1000,
                            cpu_ms=(thread_time() - cpu) * 1000, count=count,
                            cancelled=done.cancelled(), frames=entry)

            future.add_done_callback(completed)
            return future
        return call

    def run_db(self, original):
        @wraps(original)
        async def call(instance, fn, **kwargs):
            token = self.operation.set("NFQ2")
            begin = monotonic()

            def worker(session):
                started, cpu = monotonic(), thread_time()
                try:
                    return fn(session)
                finally:
                    self.record("nfq_db_function", started,
                                wall_ms=(monotonic() - started) * 1000,
                                cpu_ms=(thread_time() - cpu) * 1000,
                                thread="loop" if threading.get_ident() == self.loop_thread else "worker")

            try:
                return await original(instance, worker, **kwargs)
            finally:
                self.record("nfq_db_await", begin, wall_ms=(monotonic() - begin) * 1000)
                self.operation.reset(token)
        return call


@pytest.fixture(autouse=True)
def nfq_latency_observer(request, monkeypatch):
    if request.node.name not in TARGETS:
        yield
        return
    from sqlalchemy.orm import Session
    from project_mai_tai.oms.service import OmsRiskService

    observer = NfqObserver()
    capture = request.getfixturevalue("capsys")
    with monkeypatch.context() as patch:
        patch.setattr(asyncio.Handle, "_run", observer.callback(asyncio.Handle._run))
        patch.setattr(asyncio, "gather", observer.gather(asyncio.gather))
        patch.setattr(Session, "commit", observer.commit(Session.commit))
        patch.setattr(OmsRiskService, "_run_db", observer.run_db(OmsRiskService._run_db))
        observer.start()
        try:
            yield
        finally:
            observer.stop()
            with capture.disabled():
                print("[F-NFQ-ATTRIBUTION] " + json.dumps({
                    "node": request.node.nodeid,
                    "measurement": "diagnostic observer, NOT an acceptance gate; population in workflow identity",
                    **observer.receipt(),
                }), flush=True)
