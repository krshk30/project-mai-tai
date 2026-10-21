"""MOMORDER1 owner controls. Venue states are controlled, not new live evidence."""

from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.momentum_live.lifecycle import OwnedPosition
from project_mai_tai.momentum_live.protection import NativeProtection, ProtectionPorts, ProtectionRead


def protection_case(broker, session, *, quantity=D(31), price=D("3.11")):
    position = OwnedPosition("f9e48766-f9ad-581d-b014-7068e23d5c83",
        "live:orb" if broker == "webull" else "live:schwab_1m_v2",
        "AIXI", "owned-entry", "owned-broker-entry", quantity, price, 1_000_000_000)
    state = {"working": True, "terminal": False, "exited": D(0)}
    calls = []

    async def read(handle):
        calls.append(("read", handle.base_coid))
        children = (handle.base_coid + "T", handle.base_coid + "S") if session == "NORMAL" else (handle.base_coid,)
        return ProtectionRead(handle.episode_id, handle.account, handle.entry_order_id,
            handle.base_coid, children, state["working"], state["terminal"], state["exited"])

    async def cancel(handle):
        calls.append(("cancel", handle.base_coid))
        state.update(working=False, terminal=True)

    ports = ProtectionPorts(AsyncMock(), AsyncMock(), AsyncMock(return_value=[]), read, cancel)
    return position, state, calls, ports, NativeProtection(ports)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("status", ["working", "submitted_unknown", "cancel_requested"])
async def test_am_broker_target_is_only_target_even_when_confirmation_is_unknown(broker, status):
    position, state, _, ports, native = protection_case(broker, "AM")
    await native.ensure(position, broker=broker, session="AM")
    handle = native.handles[position.episode_id]
    handle.status = status
    state.update(working=False, terminal=False)
    await native._observe(position, handle)
    assert position.exit_reason(D("3.27"), quote_ns=2_000_000_000, now_ns=2_000_000_000) is None
    await native.ensure(position, broker=broker, session="AM")
    assert ports.submit.await_count == 1
    assert position.release_unknown
    with pytest.raises(Refusal, match="quantity_unknown"):
        position.claim_exit_quantity(broker_available=D(31), foreign_reserved=D(0))


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("trigger,bid,now_ns", [
    ("hard_stop_15pct", D("2.64"), 2_000_000_000),
    ("time_300s", D("3.11"), 301_000_000_000),
])
async def test_am_working_target_does_not_disable_stop_or_exact_300_second_exit(broker, trigger, bid, now_ns):
    position, _, calls, ports, native = protection_case(broker, "AM")
    await native.ensure(position, broker=broker, session="AM")
    assert position.exit_reason(bid, quote_ns=now_ns, now_ns=now_ns) == trigger
    calls.clear()
    assert await native.release_for_exit(position)
    assert [kind for kind, _ in calls] == ["read", "cancel", "read"]
    assert len({coid for _, coid in calls}) == 1
    assert position.claim_exit_quantity(broker_available=D(31), foreign_reserved=D(0)) == 31
    assert ports.submit.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("session", ["AM", "NORMAL"])
async def test_discretionary_unknown_read_never_cancels_or_releases_the_reservation(broker, session):
    position, state, calls, ports, native = protection_case(broker, session)
    await native.ensure(position, broker=broker, session=session)
    state.update(working=False, terminal=False)
    calls.clear()
    assert not await native.release_for_exit(position)
    assert all(kind == "read" for kind, _ in calls)
    assert position.release_unknown
    assert ports.submit.await_count == 1
    with pytest.raises(Refusal, match="quantity_unknown"):
        position.claim_exit_quantity(broker_available=D(31), foreign_reserved=D(0))


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("session", ["AM", "NORMAL"])
async def test_fill_visible_before_release_wins_without_any_cancel_or_extra_sell(broker, session):
    position, state, calls, ports, native = protection_case(broker, session)
    await native.ensure(position, broker=broker, session=session)
    state.update(working=False, terminal=True, exited=D(31))
    calls.clear()
    assert await native.release_for_exit(position)
    assert position.remaining == 0
    assert all(kind == "read" for kind, _ in calls)
    assert position.claim_exit_quantity(broker_available=D(0), foreign_reserved=D(0)) == 0
    assert ports.submit.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_authoritative_terminal_zero_fill_enables_am_software_target(broker):
    position, state, _, _, native = protection_case(broker, "AM")
    await native.ensure(position, broker=broker, session="AM")
    state.update(working=False, terminal=True)
    await native._observe(position, native.handles[position.episode_id])
    assert position.exit_reason(D("3.27"), quote_ns=2_000_000_000, now_ns=2_000_000_000) == "target_5pct"


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_rth_native_pair_still_owns_target_and_stop_but_not_time_exit(broker):
    position, _, _, ports, native = protection_case(broker, "NORMAL")
    await native.ensure(position, broker=broker, session="NORMAL")
    request = ports.submit.await_args.args[0]
    assert request.metadata["session"] == "NORMAL"
    assert request.metadata["exit_only_oco" if broker == "schwab" else "webull_exit_only_pair"] == "true"
    assert position.native_confirmed
    assert position.exit_reason(D("3.27"), quote_ns=2_000_000_000, now_ns=2_000_000_000) is None
    assert position.exit_reason(D("2.64"), quote_ns=2_000_000_000, now_ns=2_000_000_000) is None
    assert position.exit_reason(D("3.11"), quote_ns=301_000_000_000, now_ns=301_000_000_000) == "time_300s"


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_partial_cancel_releases_only_unfilled_owned_remainder(broker):
    position, state, calls, _, native = protection_case(broker, "AM")
    await native.ensure(position, broker=broker, session="AM")
    state.update(working=False, terminal=True, exited=D(7))
    calls.clear()
    assert await native.release_for_exit(position)
    assert position.remaining == 24
    assert position.claim_exit_quantity(broker_available=D(124), foreign_reserved=D(100)) == 24
    assert all(kind == "read" for kind, _ in calls)
