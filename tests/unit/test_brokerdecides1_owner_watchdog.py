"""Detector/dedup controls with async spies ONLY; no phone transport imports/sends."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.unit.test_brokerdecides1_unused_owner_sell import case, NOW
from tests.unit.test_brokerdecides1_crossday_owner import refresh
from project_mai_tai.brokerdecides1_owner_watchdog import owner_watchdog_pass as _owner_poll
from tests.unit._brokerdecides_alert_controls import poll_and_settle_new_task


async def owner_watchdog_pass(service):
    return await poll_and_settle_new_task(_owner_poll, service)


def held_case():
    strategy, state, _ = case(phase="unknown")
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    assert state._brokerdecides1_owner_hold is not None  # Actual admission refusal records the hold.
    return SimpleNamespace(strategy=strategy), state


@pytest.mark.asyncio
async def test_strictly_more_than_five_minutes_then_one_log_and_confirmed_phone(caplog):
    service, state = held_case()
    calls = []
    async def sender(*args, **kwargs):
        calls.append((args, kwargs))
        return True
    service._brokerdecides1_owner_phone = sender
    refresh(service.strategy, NOW + 300000)
    await owner_watchdog_pass(service)
    assert calls == []
    for elapsed in (300001, 360001, 420001):
        refresh(service.strategy, NOW + elapsed)
        await owner_watchdog_pass(service)
    assert len(calls) == 1 and calls[0][1] == dict(priority="urgent", tags="rotating_light")
    assert len([record for record in caplog.records if "[BROKERDECIDES1-OWNER-LOCK]" in record.message]) == 1
    assert state.flip_owner_phase == "unknown"  # Watchdog never grants admission.


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, False, 1, "sent", "exception"])
async def test_unconfirmed_sender_does_not_consume_or_immediately_duplicate_event(outcome):
    service, state = held_case()
    calls = []
    async def sender(*args, **kwargs):
        calls.append(1)
        if outcome == "exception":
            raise OSError("CONTROLLED sender failed")
        return outcome
    service._brokerdecides1_owner_phone = sender
    refresh(service.strategy, NOW + 300001)
    await owner_watchdog_pass(service)
    refresh(service.strategy, NOW + 300002)
    await owner_watchdog_pass(service)
    assert calls == [1] and state._brokerdecides1_owner_hold.delivered is False
    refresh(service.strategy, NOW + 360001)
    await owner_watchdog_pass(service)
    assert len(calls) == 2 and state._brokerdecides1_owner_hold.delivered is False


@pytest.mark.asyncio
async def test_concurrent_polls_share_one_inflight_delivery_without_queue_growth():
    service, state = held_case()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def sender(*args, **kwargs):
        calls.append(1)
        started.set()
        await release.wait()
        return True
    service._brokerdecides1_owner_phone = sender
    refresh(service.strategy, NOW + 300001)
    first = asyncio.create_task(owner_watchdog_pass(service))
    try:
        await started.wait()
        await owner_watchdog_pass(service)
        assert calls == [1]
    finally:
        release.set()
        await first
    assert state._brokerdecides1_owner_hold.delivered is True


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["working", "position", "stale20", "native_unknown", "missing", "wrong_accounts"])
async def test_no_flat_alert_from_stale_or_unproved_books(fault):
    service, state = held_case()
    refresh(service.strategy, NOW + 300001)
    memory, calls = service.strategy._brokerdecides1_memory, []
    snapshot = memory.snapshot
    if fault in {"working", "position"}:
        field = "working" if fault == "working" else "positions"
        memory.apply(replace(snapshot, generation=3, books=(snapshot.books[0],
            replace(snapshot.books[1], **{field: frozenset({"YMAT"})}))))
    elif fault == "stale20":
        service.strategy._now_ms = lambda: NOW + 319901
    elif fault == "native_unknown":
        memory.apply(replace(snapshot, generation=3, books=(snapshot.books[0], replace(snapshot.books[1], complete=False))))
    elif fault == "wrong_accounts":
        service.strategy.settings.strategy_schwab_1m_v2_account_name = "live:FOREIGN"
    else:
        memory.snapshot = None
    async def sender(*args, **kwargs):
        calls.append(1)
        return True
    service._brokerdecides1_owner_phone = sender
    await owner_watchdog_pass(service)
    assert calls == [] and state._brokerdecides1_owner_hold.logged is False


@pytest.mark.asyncio
async def test_local_inflight_can_alert_native_flat_but_is_never_cleared():
    service, state = held_case()
    refresh(service.strategy, NOW + 300001)
    memory, calls = service.strategy._brokerdecides1_memory, []
    memory.apply(replace(memory.snapshot, generation=3, inflight=frozenset({"YMAT"}), local_complete=False))
    async def sender(*args, **kwargs):
        calls.append(1)
        return True
    service._brokerdecides1_owner_phone = sender
    await owner_watchdog_pass(service)
    assert calls == [1] and state.flip_owner_phase == "unknown"
    assert "YMAT" in memory.snapshot.inflight and memory.snapshot.local_complete is False


@pytest.mark.asyncio
async def test_resolved_or_new_owner_does_not_inherit_previous_dedup():
    service, state = held_case()
    old = state._brokerdecides1_owner_hold
    state.flip_owner_phase = "idle"
    await owner_watchdog_pass(service)
    assert state._brokerdecides1_owner_hold is None
    state.flip_owner_phase = "unknown"
    state.flip_owner_opportunity_id += 1
    service.strategy._strict_first_rest_admitted(state, slot="first")
    assert state._brokerdecides1_owner_hold is not old


@pytest.mark.asyncio
async def test_missing_sender_keeps_pending_and_cancellation_keeps_retryable():
    service, state = held_case()
    refresh(service.strategy, NOW + 300001)
    await owner_watchdog_pass(service)
    assert state._brokerdecides1_owner_hold.delivered is False
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    service._brokerdecides1_owner_phone = cancelled
    refresh(service.strategy, NOW + 360001)
    with pytest.raises(asyncio.CancelledError):
        await owner_watchdog_pass(service)
    assert state._brokerdecides1_owner_hold.inflight is False and state._brokerdecides1_owner_hold.delivered is False


@pytest.mark.asyncio
async def test_clock_rollback_does_not_invent_five_minutes_of_refusals():
    service, state = held_case()
    service.strategy._now_ms = lambda: NOW - 1000
    await owner_watchdog_pass(service)
    assert state._brokerdecides1_owner_hold.since_ms == NOW - 1000
    assert state._brokerdecides1_owner_hold.logged is False
