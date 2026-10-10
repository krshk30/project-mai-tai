"""The diagnostic sampler reads actual backend waits without changing durability."""

import asyncio
import os

import psycopg
import pytest
from sqlalchemy.engine import make_url

import test_cancel_terminal_runtime as runtime
from tests.support.f_caller_latency_attribution import Observer
from tests.support.f_commit_wait_attribution import CommitWaits

sessions = runtime.sessions


@pytest.mark.asyncio
async def test_readonly_sampler_identifies_controlled_postgres_wait_and_drains(sessions):
    observer = Observer()
    waits = CommitWaits(observer)
    url = make_url(os.environ["MAI_TAI_DATABASE_URL"])
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    marker = object()

    def controlled_commit(conn):
        conn.execute("SELECT pg_sleep(0.08)")
        conn.commit()
        return marker

    def worker():
        with psycopg.connect(dsn) as conn:
            return waits.commit(controlled_commit)(conn)

    observer.start()
    waits.start()
    token = observer.operation.set("CONTROLLED-PG-WAIT")
    try:
        assert await asyncio.to_thread(worker) is marker
    finally:
        observer.operation.reset(token)
        waits.stop()
        observer.stop()
    assert not waits.thread.is_alive() and not waits.active
    records = observer.receipt()["records"]
    assert not any(row["kind"] == "pg_wait_observer_unavailable" for row in records)
    assert any(row["kind"] == "pg_commit_wait" and row["wait_event_type"] == "Timeout"
               and row["wait_event"] == "PgSleep" and row["state"] == "active"
               and row["operation"] == "CONTROLLED-PG-WAIT" for row in records)
    assert any(row["kind"] == "dbapi_commit" and row["thread"] == "worker"
               and row["wall_ms"] >= 80 for row in records)
