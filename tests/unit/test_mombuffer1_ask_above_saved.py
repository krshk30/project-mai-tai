"""UNRUN upward-only controls; not actual quote/cache/native acceptance."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.momentum_live.admission import Refusal, Route
from project_mai_tai.momentum_live.entry_reprice import UnsentEntryConflict, marketable_entry, validate_entry_reprice
from project_mai_tai.momentum_live.entry_wire import entry_wire_guard
from tests.unit.test_mombuffer1_incap_reprice import durable, env, move_at, priced  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["after_durable", "before_wire"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 14], ids=["PM", "RTH"])
@pytest.mark.parametrize("ask", [D("3.00"), D("2.94"), D("2.50")])
async def test_falling_or_flat_keeps_saved_limit_quantity_and_one_order(priced, point, provider, hour, ask):
    h = priced
    h.now[0] = h.now[0].replace(hour=hour)
    h.quote["received_at"] = h.now[0]
    signal = replace(h.signal, observed_ns=h.runtime.now_ns(), country_route=provider)
    calls, identities = [], []
    prepare, reprice = h.ports.durable_order, h.ports.reprice_unsent_entry

    async def capture(*args):
        await prepare(*args)
        parent, order = durable(h)
        identities.append((parent.id, order.id, order.client_order_id))

    async def observed(previous, current, episode=None):
        calls.append((previous, current, episode))
        await reprice(previous, current, episode)

    h.ports.durable_order, h.ports.reprice_unsent_entry = capture, observed
    move_at(h, point, ask)
    _, request, _ = await h.executor.admit(signal)
    parent, order = durable(h)
    assert (parent.id, order.id, order.client_order_id) == identities[0]
    assert request.metadata["limit_price"] == request.metadata["reference_price"] == "3.00"
    assert parent.quantity == order.quantity == request.quantity == D(33)
    assert order.payload["limit_price"] == parent.payload["metadata"]["limit_price"] == "3.00"
    assert len(calls) == 1 and calls[0][2] is not None  # Required owner transition, not repricing.
    assert all(previous == current for previous, current, _ in calls)
    assert len(h.sent) == 1 and h.sent[0] == request
    assert (await h.executor.admit(signal))[1] is None and len(h.sent) == 1
    with h.service.session_factory() as session:
        assert len(session.scalars(select(BrokerOrder)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("final_ask", [D("2.90"), D("3.03")], ids=["fall-send", "third-rise-refuse"])
async def test_mixed_falls_do_not_spend_two_upward_update_budget(priced, final_ask):
    h = priced
    move_at(h, "after_durable", D("3.01"))
    reprice, calls = h.ports.reprice_unsent_entry, []
    next_quotes = iter((D("2.94"), D("3.02"), final_ask))

    async def mixed(previous, current, episode=None):
        await reprice(previous, current, episode)
        calls.append((previous, current, episode))
        h.quote["ask"] = next(next_quotes)

    h.ports.reprice_unsent_entry = mixed
    if final_ask > D("3.02"):
        with pytest.raises(Refusal, match="budget_exhausted"):
            await h.executor.admit(h.signal)
        assert not h.sent
    else:
        _, request, _ = await h.executor.admit(h.signal)
        assert request.metadata["limit_price"] == "3.02" and len(h.sent) == 1
    assert len(calls) == 3  # Two upward prices plus one unchanged submitting snapshot.
    assert sum(previous != current for previous, current, _ in calls) == 2
    parent, order = durable(h)
    assert order.payload["limit_price"] == parent.payload["metadata"]["limit_price"] == "3.02"
    assert order.quantity == parent.quantity == D(33)
    if final_ask > D("3.02"):
        assert order.status == "aborted" and parent.status == "cancelled"
        assert order.payload["momstuck2_refusal"]["context"]["limit"] == "3.03"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stale", "missing", "future", "expired", "control", "cap"])
async def test_saved_marketable_limit_never_bypasses_fresh_gates(priced, fault):
    h = priced
    original = h.ports.durable_order

    async def changed(*args):
        await original(*args)
        h.quote["ask"] = D("2.94")
        if fault == "stale":
            h.quote["received_at"] -= timedelta(seconds=3)
        elif fault == "missing":
            h.quote.pop("ask")
        elif fault == "future":
            h.quote["received_at"] += timedelta(seconds=1)
        elif fault == "expired":
            h.now[0] += timedelta(seconds=3)
        elif fault == "cap":
            h.quote["ask"] = D("4.00")
        else:
            def stopped(signal):
                raise Refusal("SYNTHETIC-control-stopped")
            h.ports.entry_metadata = stopped

    h.ports.durable_order = changed
    with pytest.raises(Refusal):
        await h.executor.admit(h.signal)
    parent, order = durable(h)
    assert not h.sent and order.status == "aborted" and parent.status == "cancelled"
    assert order.quantity == parent.quantity == D(33)
    assert order.payload["limit_price"] == "3.00"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_sub1_retains_saved_notional_quantity_not_larger_fresh_ask_quantity(priced, provider):
    h = priced
    h.quote["ask"] = D(".99")
    signal = replace(h.signal, signal_price=D(".99"), country_route=provider)
    move_at(h, "after_durable", D(".50"))
    _, request, _ = await h.executor.admit(signal)
    parent, order = durable(h)
    assert request.metadata["limit_price"] == "0.99"
    assert order.quantity == parent.quantity == request.quantity == D(101)
    assert request.quantity * D(request.metadata["limit_price"]) <= D(100)
    assert request.quantity >= 100 and len(h.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("ask", [D("2.94"), D("3.00"), D("3.01")])
@pytest.mark.parametrize("serialized_changed", [False, True])
async def test_native_callback_retains_marketability_not_serialized_equality(priced, provider, ask, serialized_changed):
    h = priced
    signal = replace(h.signal, country_route=provider)
    global_settle = AsyncMock(wraps=h.ports.refuse_unsent_entry)
    h.ports.refuse_unsent_entry = global_settle
    permit = "SYNTHETIC-MAY-HAVE-SENT"

    async def native_boundary(request):
        request.metadata["bot_control_permit"] = permit
        with h.service.session_factory() as session:
            order = session.scalar(select(BrokerOrder))
            order.payload = {**order.payload, "bot_control_permit": permit}
            session.commit()
        h.quote["ask"] = ask
        guard = entry_wire_guard(request)
        assert guard is not None
        await asyncio.to_thread(guard.check, quantity=request.quantity,
            limit="2.94" if serialized_changed else request.metadata["limit_price"], symbol=request.symbol)
        guard.before_transport()
        h.sent.append(request)
        return [ExecutionReport("accepted", request.client_order_id, broker_order_id="synthetic-native-id",
            symbol=request.symbol, side="buy", intent_type="open", quantity=request.quantity,
            origin="broker", reported_at=h.now[0])]

    h.ports.submit = native_boundary
    if ask <= D("3.00") and not serialized_changed:
        _, request, _ = await h.executor.admit(signal)
        assert request.metadata["limit_price"] == "3.00" and request.quantity == D(33)
        assert len(h.sent) == 1
    else:
        with pytest.raises(Refusal):
            await h.executor.admit(signal)
        assert not h.sent
        parent, order = durable(h)
        assert parent.status == "cancelled" and order.status == "aborted"
    global_settle.assert_not_awaited()
    assert durable(h)[1].payload["bot_control_permit"] == permit
    assert durable(h)[1].payload["limit_price"] == "3.00"


@pytest.mark.asyncio
async def test_direct_durable_downward_update_and_hidden_nonprice_change_are_rejected(priced):
    h = priced
    captured, original = [], h.ports.durable_order

    async def capture(*args):
        await original(*args)
        saved = args[1]
        down = replace(saved, quantity=D(34), metadata={**saved.metadata,
            "limit_price": "2.94", "reference_price": "2.94"})
        with pytest.raises(UnsentEntryConflict, match="downward_reprice"):
            await h.runtime._reprice_unsent_entry(saved, down)
        assert durable(h)[1].payload["limit_price"] == saved.metadata["limit_price"]
        captured.append(saved)

    h.ports.durable_order = capture
    await h.executor.admit(h.signal)
    saved = captured[0]
    down = replace(saved, quantity=D(34), metadata={**saved.metadata,
        "limit_price": "2.94", "reference_price": "2.94"})
    assert marketable_entry(saved, down, Route.WEBULL) is saved
    with pytest.raises(Refusal, match="downward_reprice"):
        validate_entry_reprice(saved, down, Route.WEBULL)
    changed = replace(down, metadata={**down.metadata,
        "session": "AM" if saved.metadata["session"] == "NORMAL" else "NORMAL"})
    with pytest.raises(Refusal, match="sealed_entry_changed"):
        marketable_entry(saved, changed, Route.WEBULL)
