from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_live.admission import Refusal, STRATEGY_CODE
from project_mai_tai.momentum_live.lifecycle import OwnedPosition
from project_mai_tai.momentum_live.protection import NativeProtection, ProtectionPorts, ProtectionRead


def setup(*, session="NORMAL"):
    position = OwnedPosition("00000000-0000-0000-0000-000000000001", "live:schwab_1m_v2",
        "SCNI", "own-entry-coid", "own-entry-broker-id", D(3), D("3.0012"), 1)
    async def read(handle):
        return ProtectionRead(handle.episode_id, handle.account, handle.entry_order_id,
            handle.base_coid, ("own-target", "own-stop") if session == "NORMAL" else (handle.base_coid,),
            True, False, D(0))
    ports = ProtectionPorts(AsyncMock(), AsyncMock(), AsyncMock(return_value=[]),
                            AsyncMock(side_effect=read), AsyncMock())
    return position, ports, NativeProtection(ports)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_rth_payload_has_own_account_quantity_fill_vwap_and_native_sdk_marker(broker):
    position, ports, native = setup()
    if broker == "webull":
        position.account = "live:orb"
    await native.ensure(position, broker=broker, session="NORMAL")
    request = ports.submit.await_args.args[0]
    assert request.strategy_code == STRATEGY_CODE and request.broker_account_name == position.account
    assert request.side == "sell" and request.quantity == 3 and request.order_type == "limit"
    assert request.time_in_force == "day" and request.metadata["session"] == "NORMAL"
    assert request.metadata["bracket_target_price"] == "3.16"
    assert request.metadata["bracket_stop_price"] == "2.55"
    assert request.metadata["exit_only_oco" if broker == "schwab" else "webull_exit_only_pair"] == "true"
    assert position.native_confirmed and position.reserved == 3
    assert request.metadata["entry_broker_order_id"] == "own-entry-broker-id"
    assert len(request.client_order_id) <= 38
    if broker == "schwab":
        from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
        adapter = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
        payload = adapter._build_exit_only_oco_payload(request)
        assert payload["orderStrategyType"] == "OCO"
        assert {row["orderType"] for row in payload["childOrderStrategies"]} == {"LIMIT", "STOP"}
        assert all(row["session"] == "NORMAL" for row in payload["childOrderStrategies"])
    else:
        from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
        adapter = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
        payload = adapter._build_exit_only_pair_payload(request)
        assert {row["order_type"] for row in payload} == {"LIMIT", "STOP_LOSS"}
        assert all(row["support_trading_session"] == "CORE" for row in payload)
        assert all(row["time_in_force"] == "DAY" and len(row["client_order_id"]) <= 40 for row in payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker,session", [("webull", "AM"), ("schwab", "AM"), ("webull", "PM"), ("schwab", "PM")])
async def test_extended_session_only_resting_target_not_unsupported_native_stop(broker, session):
    position, ports, native = setup(session=session)
    await native.ensure(position, broker=broker, session=session)
    request = ports.submit.await_args.args[0]
    assert request.metadata["extended_hours"] == "true"
    assert not {"exit_only_oco", "webull_exit_only_pair", "bracket_stop_price"} & request.metadata.keys()
    assert request.order_type == "limit" and request.time_in_force == "day"
    assert not position.native_confirmed and position.reserved == 3


@pytest.mark.asyncio
async def test_unknown_ack_retains_durable_handle_never_blind_reattaches():
    position, ports, native = setup()
    ports.submit.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await native.ensure(position, broker="schwab", session="NORMAL")
    handle = native.handles[position.episode_id]
    assert handle.status == "submitted_unknown"
    async def unknown(handle):
        return ProtectionRead(handle.episode_id, handle.account, handle.entry_order_id,
            handle.base_coid, (), False, False, D(0))
    ports.read.side_effect = unknown
    await native.ensure(position, broker="schwab", session="NORMAL")
    assert ports.submit.await_count == 1 and position.release_unknown


@pytest.mark.asyncio
async def test_cancel_ack_is_not_release_and_latch_survives_repeated_working_read():
    position, ports, native = setup()
    await native.ensure(position, broker="schwab", session="NORMAL")
    assert not await native.release_for_exit(position)
    assert not await native.release_for_exit(position)
    assert ports.cancel.await_count == 1
    assert position.release_unknown and position.child_ids == ("own-target", "own-stop")
    with pytest.raises(Refusal, match="quantity_unknown"):
        position.claim_exit_quantity(broker_available=D(103), foreign_reserved=D(100))


@pytest.mark.asyncio
async def test_simultaneous_native_fill_after_cancel_reduces_own_close_to_zero():
    position, ports, native = setup()
    await native.ensure(position, broker="schwab", session="NORMAL")
    cancelled = [False]
    async def cancel(handle):
        cancelled[0] = True
    async def read(handle):
        return ProtectionRead(handle.episode_id, handle.account, handle.entry_order_id,
            handle.base_coid, handle.child_ids, not cancelled[0], cancelled[0], D(3) if cancelled[0] else D(0))
    ports.cancel.side_effect = cancel
    ports.read.side_effect = read
    assert await native.release_for_exit(position)
    assert position.remaining == 0
    assert position.claim_exit_quantity(broker_available=D(100), foreign_reserved=D(100)) == 0


@pytest.mark.asyncio
async def test_partial_parent_growth_releases_old_owned_pair_before_larger_pair():
    position, ports, native = setup()
    position.quantity = D(1)
    await native.ensure(position, broker="webull", session="NORMAL")
    position.quantity = D(3)
    cancelled = set()
    async def cancel(handle):
        cancelled.add(handle.base_coid)
    async def read(handle):
        terminal = handle.base_coid in cancelled
        return ProtectionRead(handle.episode_id, handle.account, handle.entry_order_id,
            handle.base_coid, (handle.base_coid+"T", handle.base_coid+"S"), not terminal, terminal, D(0))
    handle = native.handles[position.episode_id]
    handle.child_ids = (handle.base_coid+"T", handle.base_coid+"S")
    ports.cancel.side_effect = cancel
    ports.read.side_effect = read
    await native.ensure(position, broker="webull", session="NORMAL")
    assert ports.cancel.await_count == 1 and ports.submit.await_count == 2
    first, second = [call.args[0] for call in ports.submit.await_args_list]
    assert first.quantity == 1 and second.quantity == 3
    assert first.client_order_id != second.client_order_id


@pytest.mark.asyncio
async def test_foreign_child_identity_never_authorizes_release_or_sell():
    position, ports, native = setup()
    await native.ensure(position, broker="schwab", session="NORMAL")
    async def wrong(handle):
        return ProtectionRead(handle.episode_id, "live:other", handle.entry_order_id,
            handle.base_coid, handle.child_ids, False, True, D(0))
    ports.read.side_effect = wrong
    with pytest.raises(Refusal, match="read_unknown"):
        await native.release_for_exit(position)
    ports.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_atr_holding_cannot_enlarge_own_sell_after_exact_release():
    position, ports, native = setup()
    await native.ensure(position, broker="schwab", session="NORMAL")
    async def terminal(handle):
        return ProtectionRead(handle.episode_id, handle.account, handle.entry_order_id,
            handle.base_coid, handle.child_ids, False, True, D(1))
    ports.read.side_effect = terminal
    assert await native.release_for_exit(position)
    assert position.claim_exit_quantity(broker_available=D(102), foreign_reserved=D(100)) == 2
    assert position.reserved == 2
