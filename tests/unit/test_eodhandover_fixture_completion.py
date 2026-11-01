"""Ownership controls for functional EOD fixture waits, not timing gates."""
import asyncio
from types import SimpleNamespace
import threading

import pytest

from tests.unit import test_eodhandover1_sync_gaps as fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_fixture_wait_retains_actual_worker_until_owner_releases_and_joins(monkeypatch, interruption):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    workers = []

    def unit():
        workers.append(threading.get_ident())
        entered.set()
        try:
            assert release.wait(5)
            return "physical unit result"
        finally:
            closed.set()

    physical = asyncio.create_task(asyncio.to_thread(unit))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert workers == [workers[0]] and workers[0] != threading.get_ident()
        if interruption == "timeout":
            monkeypatch.setattr(fixture, "FIXTURE_COMPLETION_TIMEOUT", .001)
            with pytest.raises(TimeoutError):
                await fixture.wait_fixture_completion(physical)
        else:
            waiter = asyncio.create_task(fixture.wait_fixture_completion(physical))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert not physical.cancelled() and not physical.done() and not closed.is_set()
    finally:
        release.set()
        result = await physical
    assert result == "physical unit result" and closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("drain", [fixture.drain_recovery, fixture.drain_quote_workers])
async def test_sibling_drain_keeps_exact_service_task_owned_on_timeout(monkeypatch, drain):
    entered, release = asyncio.Event(), asyncio.Event()

    async def owned():
        entered.set()
        await release.wait()
        return "owned result"

    physical = asyncio.create_task(owned())
    service = SimpleNamespace(
        _confirmation_exit_recovery_tasks={physical}, _symbol_tick_work={"owned-symbol": physical},
    )
    foreign = asyncio.create_task(release.wait())
    try:
        await entered.wait()
        monkeypatch.setattr(fixture, "FIXTURE_COMPLETION_TIMEOUT", .001)
        with pytest.raises(TimeoutError):
            await drain(service)
        assert not physical.done() and not physical.cancelled()
        assert not foreign.done() and not foreign.cancelled()
    finally:
        release.set()
        assert await physical == "owned result"
        await foreign


@pytest.mark.asyncio
async def test_fixture_completion_preserves_actual_exception_identity():
    error = RuntimeError("controlled unit failure")

    async def fail():
        raise error

    task = asyncio.create_task(fail())
    with pytest.raises(RuntimeError) as caught:
        await fixture.wait_fixture_completion(task)
    assert caught.value is error and task.exception() is error
