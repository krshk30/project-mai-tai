"""SAFEX1 R7 controlled UNKNOWN reads, not live broker acceptance evidence."""

from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from tests.unit.test_momlive1_exit_recovery import fresh
from tests.unit.test_momlive1_soft_exit_chase import exit_orders, prepared


def unknown_original(runtime, broker, coid, *, terminal_also=False, fresh_only=False):
    """Keep the read UNKNOWN until a controlled terminal receipt is available."""
    read = runtime.backend.fetch_entry_update
    state = {"terminal_also": terminal_also, "fresh_attempts": 0}

    async def fetch(request, current, **kwargs):
        if request.client_order_id == coid and (not fresh_only or kwargs.get("fresh")):
            if kwargs.get("fresh"):
                state["fresh_attempts"] += 1
            row = broker.orders[coid]
            if state["terminal_also"] or row.status not in {
                    "filled", "cancelled", "rejected", "expired"}:
                return None
        return await read(request, current, **kwargs)

    runtime.backend.fetch_entry_update = fetch
    return state


async def chase(now, service, runtime, identity, reason):
    fresh(now, service, 2)
    service._latest_quotes_by_symbol["SCNI"]["bid"] = D("2.907")
    position = (await runtime._observe(identity)).position
    await runtime._exit(position, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 21])
@pytest.mark.parametrize("reason", ["hard_stop_15pct", "time_300s"])
async def test_mandatory_persistent_unknown_working_read_cancels_then_terminal_proof_replaces(
        provider, hour, reason, monkeypatch):
    now, broker, service, runtime, identity = await prepared(provider, hour)
    first = exit_orders(broker)[0]
    runtime.runtime[identity]["soft_exit_reason"] = reason
    state = unknown_original(runtime, broker, first.client_order_id)
    warning = Mock(wraps=service.logger.warning)
    monkeypatch.setattr(service.logger, "warning", warning)

    await chase(now, service, runtime, identity, reason)

    assert state["fresh_attempts"] >= 2  # Before cancel and the original-order finally read.
    assert broker.cancelled.count(first.client_order_id) == 1
    assert broker.orders[first.client_order_id].status == "cancelled"
    orders = exit_orders(broker)
    assert len(orders) == 2
    assert orders[1].quantity == first.quantity == 33
    assert orders[1].metadata["limit_price"] == "2.90"
    assert orders[1].reason == reason
    diagnostics = [call.args for call in warning.call_args_list
                   if "SAFEX1-R7-CANCEL-READ-UNKNOWN" in call.args[0]]
    assert len(diagnostics) == 1
    assert diagnostics[0][1:] == (identity, first.client_order_id, reason)

    # The replacement only exists after a real controlled terminal read above.
    row = broker.orders[orders[1].client_order_id]
    row.status, row.quantity, row.price = "filled", D(33), D("2.90")
    await chase(now, service, runtime, identity, reason)
    snapshot = await runtime._inspect(identity)
    assert snapshot["exited"] == snapshot["quantity"] == 33
    assert not snapshot["outstanding_exit"]
    assert len(exit_orders(broker)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_mandatory_cancel_unknown_terminal_still_forbids_second_sell(provider):
    now, broker, service, runtime, identity = await prepared(provider)
    first = exit_orders(broker)[0]
    state = unknown_original(runtime, broker, first.client_order_id, terminal_also=True)

    await chase(now, service, runtime, identity, "time_300s")
    assert broker.cancelled.count(first.client_order_id) == 1
    assert len(exit_orders(broker)) == 1
    assert (await runtime._inspect(identity))["outstanding_exit"]

    state["terminal_also"] = False
    await chase(now, service, runtime, identity, "time_300s")
    assert len(exit_orders(broker)) == 2
    assert broker.cancelled.count(first.client_order_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("fresh_only", [False, True])
async def test_discretionary_reprice_unknown_defers_without_cancel_or_replacement(provider, fresh_only):
    now, broker, service, runtime, identity = await prepared(provider)
    first = exit_orders(broker)[0]
    runtime.runtime[identity]["soft_exit_reason"] = "discretionary_reprice"
    unknown_original(runtime, broker, first.client_order_id, fresh_only=fresh_only)

    for _ in range(3):
        await chase(now, service, runtime, identity, "discretionary_reprice")
    assert first.client_order_id not in broker.cancelled
    assert broker.orders[first.client_order_id].status == "accepted"
    assert len(exit_orders(broker)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_mandatory_unknown_cancel_fill_race_wins_without_replacement(provider):
    now, broker, service, runtime, identity = await prepared(provider)
    first = exit_orders(broker)[0]
    unknown_original(runtime, broker, first.client_order_id)
    submit = broker.submit_order

    async def fill_on_cancel(request):
        if request.intent_type == "cancel" and request.client_order_id == first.client_order_id:
            row = broker.orders[first.client_order_id]
            row.status, row.quantity, row.price = "filled", D(33), D(3)
            raise RuntimeError("controlled 417 after fill")
        return await submit(request)

    broker.submit_order = fill_on_cancel
    await chase(now, service, runtime, identity, "time_300s")
    assert len(exit_orders(broker)) == 1
    assert (await runtime._inspect(identity))["exited"] == 33


@pytest.mark.asyncio
async def test_unknown_mandatory_cancel_requires_known_durable_broker_id():
    now, broker, service, runtime, identity = await prepared()
    first = exit_orders(broker)[0]
    request = replace(first, metadata={k: v for k, v in first.metadata.items()
                                       if k != "broker_order_id"})
    unknown_original(runtime, broker, first.client_order_id)
    values = runtime.runtime[identity]
    assert not await runtime._chase_cancel(identity, request, values,
        mandatory=True, reason="time_300s")
    assert first.client_order_id not in broker.cancelled
    assert "soft_exit_cancel_coid" not in values


@pytest.mark.parametrize("reason,allowed,expected", [
    ("hard_stop_15pct", False, True), ("time_300s", False, True),
    ("KILLSW1_STOP", False, True), ("EOD", False, True),
    ("target_5pct", True, True), ("target_5pct", False, False),
    ("discretionary_reprice", True, False), ("handover", True, False),
])
def test_only_approved_exit_triggers_can_chase_unknown(reason, allowed, expected):
    position = SimpleNamespace(software_target_allowed=allowed)
    assert OmsLiveRuntime._mandatory_chase(position, reason) is expected
