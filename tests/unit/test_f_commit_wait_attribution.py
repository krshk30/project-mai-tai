import asyncio
from time import sleep
from types import SimpleNamespace

import pytest

from tests.support.f_caller_latency_attribution import Observer
from tests.support.f_commit_wait_attribution import CommitWaits


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_driver_commit_and_flush_observers_preserve_result_and_failure(fails):
    observer = Observer()
    waits = CommitWaits(observer)
    marker = object()
    connection = SimpleNamespace(info=SimpleNamespace(backend_pid=123))

    def original(value):
        sleep(0.015)
        if fails:
            raise ValueError("controlled transaction failure")
        return marker

    token = observer.operation.set("EXIT13")
    try:
        for wrapped in (waits.commit(original), observer.flush(original)):
            if fails:
                with pytest.raises(ValueError, match="controlled transaction failure"):
                    await asyncio.to_thread(wrapped, connection)
            else:
                assert await asyncio.to_thread(wrapped, connection) is marker
    finally:
        observer.operation.reset(token)
    assert not waits.active
    assert [row["kind"] for row in observer.records] == ["dbapi_commit", "session_flush"]
    assert all(row["wall_ms"] >= 15 and row["thread"] == "worker"
               and row["operation"] == "EXIT13" for row in observer.records)


def test_driver_observer_does_not_read_pid_or_record_outside_close_scope():
    observer = Observer()
    waits = CommitWaits(observer)
    marker = object()
    assert waits.commit(lambda value: value)(marker) is marker
    assert not waits.active and not observer.records
