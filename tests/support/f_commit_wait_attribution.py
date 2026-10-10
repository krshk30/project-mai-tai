"""Optional bounded read-only PostgreSQL waits; never a raw latency gate."""

import os
import threading
from time import monotonic

from sqlalchemy.engine import make_url


class CommitWaits:
    def __init__(self, observer):
        self.observer = observer
        self.active = {}
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.sample, name="f-pg-wait-observer", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(1)

    def sample(self):
        import psycopg
        try:
            url = make_url(os.environ["MAI_TAI_DATABASE_URL"])
            dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
            with psycopg.connect(dsn, autocommit=True, connect_timeout=1,
                    application_name="f_commit_wait_diagnostic",
                    options="-c default_transaction_read_only=on -c statement_timeout=200 -c lock_timeout=100") as conn:
                while not self.stop_event.wait(0.010):
                    active = dict(self.active)
                    now = monotonic()
                    eligible = {pid: value for pid, value in active.items()
                                if now - value[0] >= 0.010}
                    if not eligible:
                        continue
                    rows = conn.execute("""SELECT pid, state, wait_event_type, wait_event,
                        left(query, 180) FROM pg_stat_activity WHERE pid = ANY(%s)""",
                        (list(eligible),)).fetchall()
                    for pid, state, wait_type, wait_event, query in rows:
                        begin, operation = eligible[pid]
                        self.observer.record("pg_commit_wait", begin,
                            wall_ms=(monotonic() - begin) * 1000, sampled_at=monotonic(),
                            operation=operation, backend_pid=pid, thread="worker",
                            state=state, wait_event_type=wait_type, wait_event=wait_event,
                            query=query)
        except Exception as exc:
            self.observer.record("pg_wait_observer_unavailable", monotonic(),
                                 error_type=type(exc).__name__)

    def commit(self, original):
        from functools import wraps
        from time import thread_time

        @wraps(original)
        def call(connection, *args, **kwargs):
            operation = self.observer.operation.get()
            if operation is None:
                return original(connection, *args, **kwargs)
            begin, cpu = monotonic(), thread_time()
            pid = connection.info.backend_pid
            self.active[pid] = (begin, operation)
            try:
                return original(connection, *args, **kwargs)
            finally:
                self.active.pop(pid, None)
                self.observer.record("dbapi_commit", begin,
                    wall_ms=(monotonic() - begin) * 1000,
                    cpu_ms=(thread_time() - cpu) * 1000, operation=operation,
                    backend_pid=pid, thread="loop" if threading.get_ident()
                    == self.observer.loop_thread else "worker")
        return call
