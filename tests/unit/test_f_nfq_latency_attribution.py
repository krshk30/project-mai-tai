import asyncio
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.support.f_nfq_latency_attribution import NfqObserver, TARGETS


@pytest.mark.asyncio
async def test_nfq_gather_capture_returns_exact_future_and_preserves_cancellation():
    observer = NfqObserver()
    expected = asyncio.get_running_loop().create_future()
    original = Mock(return_value=expected)
    captured = observer.gather(original)

    async def test_nfq2_slow_save_does_not_stall_loop_and_concurrent_quotes_queue_once():
        return captured(*range(1000), return_exceptions=True)

    actual = await test_nfq2_slow_save_does_not_stall_loop_and_concurrent_quotes_queue_once()
    assert actual is expected
    original.assert_called_once_with(*range(1000), return_exceptions=True)
    actual.cancel()
    with pytest.raises(asyncio.CancelledError):
        await actual
    await asyncio.sleep(0)
    records = list(observer.records)
    assert [row["kind"] for row in records] == ["nfq_gather_setup", "nfq_gather_total"]
    assert records[-1]["cancelled"] is True
    assert all(row["count"] == 1000 for row in records)


@pytest.mark.asyncio
async def test_nfq_gather_observer_ignores_other_callers():
    observer = NfqObserver()
    future = asyncio.get_running_loop().create_future()
    assert observer.gather(Mock(return_value=future))(*range(1000)) is future
    future.set_result("unchanged")
    assert await future == "unchanged"
    assert not observer.records
    assert len(TARGETS) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_nfq_worker_observer_preserves_outcome_thread_and_operation(outcome):
    observer = NfqObserver()
    calls = []
    session = object()

    def fn(actual):
        calls.append((actual, get_ident(), observer.operation.get()))
        if outcome == "error":
            raise ValueError("controlled worker error")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return "unchanged"

    async def original(instance, worker, **kwargs):
        assert instance.label == "service" and kwargs == {"commit": False}
        return await asyncio.to_thread(worker, session)

    wrapped = observer.run_db(original)
    if outcome == "success":
        assert await wrapped(SimpleNamespace(label="service"), fn, commit=False) == "unchanged"
    else:
        error = ValueError if outcome == "error" else asyncio.CancelledError
        with pytest.raises(error):
            await wrapped(SimpleNamespace(label="service"), fn, commit=False)
    assert calls == [(session, calls[0][1], "NFQ2")]
    assert calls[0][1] != observer.loop_thread
    assert observer.operation.get() is None
    assert [row["kind"] for row in observer.records] == ["nfq_db_function", "nfq_db_await"]
    assert observer.records[0]["thread"] == "worker"
