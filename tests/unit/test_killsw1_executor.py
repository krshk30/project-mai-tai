import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from project_mai_tai.oms.bot_control import ControlSnapshot, StopLease
from project_mai_tai.oms.bot_control_executor import StopExecutor, StopProgress


def executor(*, complete=True):
    phases, settled = [], []
    identity, strategy = uuid4(), uuid4()
    lease = StopLease(identity, strategy, 1, "test-owner", 1, (), complete)
    store = SimpleNamespace(
        claim_stop=lambda *args, **kwargs: lease,
        renew_stop=lambda *args, **kwargs: None,
        set_stop_phase=lambda lease, phase, **kwargs: phases.append(phase),
        settle_stop=lambda command, **kwargs: settled.append((command, kwargs)),
    )
    runtime = StopExecutor(store, object())
    states = {"momentum_warn_live": ControlSnapshot(strategy, 1, 0, "STOP", identity)}
    return runtime, states, phases, settled


@pytest.mark.asyncio
async def test_missing_driver_is_blocked_not_flat_or_implicitly_on():
    runtime, states, phases, settled = executor()
    runtime.schedule(states)
    await asyncio.gather(*runtime.tasks.values())
    assert phases == ["BLOCKED_UNKNOWN"]
    assert settled == []
    assert not next(iter(states.values())).admission_open
    await runtime.close()


@pytest.mark.asyncio
async def test_incomplete_account_scope_never_runs_driver_or_settles():
    runtime, states, phases, settled = executor(complete=False)
    called = []

    async def driver(lease, journal):
        called.append(lease)
        return StopProgress("SETTLED", {})

    runtime.register("momentum_warn_live", driver)
    runtime.schedule(states)
    await asyncio.gather(*runtime.tasks.values())
    assert phases == ["BLOCKED_SCOPE"]
    assert called == settled == []
    await runtime.close()


@pytest.mark.asyncio
async def test_hung_scope_does_not_stop_another_scope():
    runtime, states, phases, _ = executor()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(lease, journal):
        entered.set()
        await release.wait()
        return StopProgress("EXIT_WORKING")

    async def fast(lease, journal):
        return StopProgress("BLOCKED_SESSION")

    runtime.register("momentum_warn_live", slow)
    runtime.register("orb_schwab", fast)
    identity = uuid4()
    states["orb_schwab"] = ControlSnapshot(uuid4(), 1, 0, "STOP", identity)
    runtime.schedule(states)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(runtime.tasks[identity], 2)
        assert phases == ["BLOCKED_SESSION"]
        assert not runtime.tasks[next(iter(states.values())).active_command_id].done()
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_settlement_without_census_stays_unknown():
    runtime, states, phases, settled = executor()

    async def bad_driver(lease, journal):
        return StopProgress("SETTLED")

    runtime.register("momentum_warn_live", bad_driver)
    runtime.schedule(states)
    await asyncio.gather(*runtime.tasks.values())
    assert phases == ["BLOCKED_UNKNOWN"]
    assert settled == []
    await runtime.close()
