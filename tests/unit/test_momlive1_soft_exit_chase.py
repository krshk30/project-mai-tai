"""Owned EH cancel/read/replace, controlled broker transport and clocks only."""
from datetime import timedelta
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from project_mai_tai.positions_read_receipt import record_receipts
from tests.unit.test_momlive1_exit_recovery import fresh, sells
from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup
from tests.unit.momlive1_owned_fixtures import complete_owned_observation


def exit_orders(broker):
    return [request for request in sells(broker) if request.intent_type == "close"]


async def prepared(provider="webull", hour=12, *, send_exit=True):
    now, broker, service, runtime = setup()
    now[0] = now[0].replace(hour=min(hour, 14))
    with service.session_factory() as session:
        service.store.ensure_broker_account(session, name=runtime.accounts[0], provider="schwab", environment="live")
        record_receipts(session, [(name, now[0], 0) for name in runtime.accounts], recorded_at=now[0])
        session.commit()
    fresh(now, service)
    if provider == "schwab":
        leaf = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
        leaf.accounts_by_name = {runtime.accounts[0]: object()}
        native_children = {}
        submit = broker.submit_order
        async def submit_with_children(request):
            reports = await submit(request)
            if request.intent_type == "close" and request.metadata.get("exit_only_oco"):
                native_children[request.client_order_id] = (request.client_order_id + "T", request.client_order_id + "S")
                for child in native_children[request.client_order_id]:
                    broker.orders[child] = SimpleNamespace(request=replace(request, client_order_id=child),
                                                           status="accepted", quantity=D(0), price=None)
            return reports
        broker.submit_order = submit_with_children
        async def raw_order(account, order_id):
            coid = order_id.removeprefix("venue-")
            row = broker.orders.get(coid)
            if row is None:
                return None
            if coid in native_children:
                children = native_children[coid]
                if getattr(broker, "reverse_native_children", False):
                    children = tuple(reversed(children))
                return {"orderId": order_id, "childOrderStrategies": [
                    await raw_order(account, "venue-" + child) for child in children]}
            request = row.request
            status = {"accepted": "WORKING", "partially_filled": "PARTIALLY_FILLED",
                      "filled": "FILLED", "cancelled": "CANCELED", "rejected": "REJECTED"}[row.status]
            if ((getattr(broker, "native_status_unknown", False) and request.metadata.get("exit_only_oco"))
                    or coid in getattr(broker, "unknown_native_coids", set())):
                status = "UNKNOWN"
            return {"orderId": order_id, "status": status, "quantity": str(request.quantity),
                "filledQuantity": str(row.quantity), "orderLegCollection": [{"legId": 1,
                    "instruction": request.side.upper(), "instrument": {"symbol": request.symbol}}],
                "orderActivityCollection": ([{"executionLegs": [{"legId": 1, "quantity": str(row.quantity),
                    "price": str(row.price), "time": now[0].isoformat()}]}] if row.quantity else [])}
        leaf._fetch_order = raw_order
        broker.leaf = leaf
        broker._adapter_for_account = lambda account: leaf
        async def cancel_legs(account, ids):
            for order_id in ids:
                coid = order_id.removeprefix("venue-")
                broker.cancelled.append(coid)
                broker.orders[coid].status = "cancelled"
        broker.cancel_exit_leg_ids = cancel_legs
    await runtime.start()
    try:
        await offer_and_drain(runtime, event(now[0], route=provider))
        await complete_owned_observation(runtime, next(iter(runtime.book.episodes)))
    finally:
        await runtime.close()
    identity = next(iter(runtime.book.episodes))
    if hour > 14:
        now[0] = now[0].replace(hour=hour)
    fresh(now, service, 301)
    if send_exit:
        await runtime._exit((await runtime._observe(identity)).position, "time_300s")
        assert len(exit_orders(broker)) == 1
    return now, broker, service, runtime, identity


async def advance_exit(now, service, runtime, identity, *, seconds=2, bid="2.907"):
    fresh(now, service, seconds)
    service._latest_quotes_by_symbol["SCNI"]["bid"] = D(bid)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 21])
async def test_unfilled_eh_exit_replaces_at_new_floored_bid_every_two_seconds(provider, hour):
    now, broker, service, runtime, identity = await prepared(provider, hour)
    first = exit_orders(broker)[0]
    await advance_exit(now, service, runtime, identity, seconds=1.999999)
    assert len(exit_orders(broker)) == 1
    await advance_exit(now, service, runtime, identity, seconds=.000001)
    assert len(exit_orders(broker)) == 2
    assert exit_orders(broker)[1].metadata["limit_price"] == "2.90"
    assert exit_orders(broker)[1].quantity == first.quantity == 33
    assert broker.orders[first.client_order_id].status == "cancelled"
    await advance_exit(now, service, runtime, identity, bid="2.801")
    assert len(exit_orders(broker)) == 3
    assert exit_orders(broker)[2].metadata["limit_price"] == "2.80"
    assert len({request.client_order_id for request in exit_orders(broker)}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_partial_fill_replaces_only_proven_owned_remainder(provider):
    now, broker, service, runtime, identity = await prepared(provider)
    row = broker.orders[exit_orders(broker)[0].client_order_id]
    row.status, row.quantity, row.price = "partially_filled", D(5), D(3)
    await advance_exit(now, service, runtime, identity)
    assert [request.quantity for request in exit_orders(broker)] == [D(33), D(28)]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_fill_racing_cancel_or_417_records_fill_and_never_resends(provider):
    now, broker, service, runtime, identity = await prepared(provider)
    submit = broker.submit_order
    async def fill_on_cancel(request):
        if request.intent_type == "cancel" and request.metadata.get("exit_reason"):
            row = broker.orders[request.client_order_id]
            row.status, row.quantity, row.price = "filled", D(33), D(3)
            raise RuntimeError("417 already filled")
        return await submit(request)
    broker.submit_order = fill_on_cancel
    await advance_exit(now, service, runtime, identity)
    assert len(exit_orders(broker)) == 1
    assert (await runtime._inspect(identity))["exited"] == 33


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_ambiguous_cancel_keeps_original_and_never_double_sells(provider):
    now, broker, service, runtime, identity = await prepared(provider)
    submit, cancel_coids = broker.submit_order, []
    async def ambiguous(request):
        if request.intent_type == "cancel" and request.metadata.get("exit_reason"):
            cancel_coids.append(request.client_order_id)
            raise TimeoutError("no broker acknowledgement")
        return await submit(request)
    broker.submit_order = ambiguous
    await advance_exit(now, service, runtime, identity)
    await advance_exit(now, service, runtime, identity, seconds=1)
    await advance_exit(now, service, runtime, identity, seconds=1)
    assert cancel_coids == [exit_orders(broker)[0].client_order_id] * 2
    assert len(exit_orders(broker)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_cancel_terminal_without_new_fresh_quote_waits_without_replacement(provider):
    now, broker, service, runtime, identity = await prepared(provider)
    submit = broker.submit_order
    async def cancel_and_age(request):
        reports = await submit(request)
        if request.intent_type == "cancel" and request.metadata.get("exit_reason"):
            now[0] += timedelta(seconds=3)
        return reports
    broker.submit_order = cancel_and_age
    await advance_exit(now, service, runtime, identity)
    assert len(exit_orders(broker)) == 1
    await advance_exit(now, service, runtime, identity, seconds=0)
    assert len(exit_orders(broker)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_stale_quote_before_chase_preserves_working_exit_without_blind_cancel(provider):
    now, broker, service, runtime, identity = await prepared(provider)
    original = exit_orders(broker)[0]
    now[0] += timedelta(seconds=3)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert broker.orders[original.client_order_id].status == "accepted"
    assert len(exit_orders(broker)) == 1
    await advance_exit(now, service, runtime, identity, seconds=1)
    assert len(exit_orders(broker)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_restart_after_uncertain_cancel_restarts_clock_not_order(provider):
    now, broker, service, runtime, identity = await prepared(provider)
    values = runtime.runtime[identity]
    values["soft_exit_cancel_coid"] = exit_orders(broker)[0].client_order_id
    values.pop("soft_exit_cancel_completed_ns", None)
    await runtime._save(runtime.book.snapshot()[0])
    fresh(now, service, 2)
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="987:654")
    await restarted.start()
    try:
        assert len(exit_orders(broker)) == 1
        assert restarted.runtime[identity]["soft_exit_cancel_verified_ns"] == restarted.now_ns()
        await restarted.close()  # Stop background workers before direct SQLite-owned steps.
        await advance_exit(now, service, restarted, identity, seconds=1.999999)
        assert len(exit_orders(broker)) == 1
        await advance_exit(now, service, restarted, identity, seconds=.000001)
        assert len(exit_orders(broker)) == 2
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_rth_nonterminal_software_order_behavior_is_not_changed(provider):
    now, broker, service, runtime, identity = await prepared(provider, 14)
    before = len([request for request in broker.sent if request.intent_type == "cancel"])
    await advance_exit(now, service, runtime, identity)
    assert len(exit_orders(broker)) == 1
    assert len([request for request in broker.sent if request.intent_type == "cancel"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("filled", [True, False])
async def test_schwab_unknown_native_status_six_minutes_still_reaches_time_exit(filled):
    now, broker, service, runtime, identity = await prepared("schwab", 14, send_exit=False)
    handle = runtime.native.handles[identity]
    broker.native_status_unknown = True
    runtime.actor.restored = True
    for _ in range(7):
        fresh(now, service, 60)
        await runtime.actor._service_owned(identity)
    assert runtime.runtime[identity]["soft_exit_reason"] == "time_300s"
    assert not exit_orders(broker)
    assert set(broker.cancelled) == {handle.base_coid + "T", handle.base_coid + "S"}
    assert len(broker.cancelled) == 2
    assert handle.status == "cancel_requested"
    broker.native_status_unknown = False
    if filled:
        row = broker.orders[handle.base_coid + "T"]
        row.status, row.quantity, row.price = "filled", D(33), D("3.15")
    fresh(now, service, 2)
    await runtime.actor._service_owned(identity)
    assert len(exit_orders(broker)) == (0 if filled else 1)
    if filled:
        assert runtime.book.episodes[identity].state == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["T", "S"])
@pytest.mark.parametrize("quantity", [5, 33])
@pytest.mark.parametrize("reverse", [False, True])
async def test_schwab_verified_sibling_fill_survives_unknown_and_restart(leg, quantity, reverse):
    now, broker, service, runtime, identity = await prepared("schwab", 14, send_exit=False)
    handle = runtime.native.handles[identity]
    known = broker.orders[handle.base_coid + leg]
    known.status = "filled" if quantity == 33 else "partially_filled"
    known.quantity, known.price = D(quantity), D("3.15")
    broker.unknown_native_coids = {handle.base_coid + ("S" if leg == "T" else "T")}
    broker.reverse_native_children = reverse
    for _ in range(2):
        position = (await runtime._observe(identity)).position
        assert position.exited == quantity
        assert position.release_unknown
        assert handle.status != "terminal"
        assert (await runtime._inspect(identity))["exited"] == quantity
    if quantity < 33:
        assert position.reserved == 33 - quantity
    assert not exit_orders(broker)
    assert broker.orders[handle.base_coid].status == "accepted"
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="987:654")
    await restarted.start()
    await restarted.close()
    assert (await restarted._inspect(identity))["exited"] == quantity
    assert not exit_orders(broker)
    assert broker.orders[handle.base_coid].status == "accepted"


@pytest.mark.parametrize("hour,minute,expected", [(3, 59, False), (4, 0, True), (9, 29, True),
    (9, 30, False), (15, 59, False), (16, 0, True), (19, 59, True), (20, 0, False)])
def test_chase_window_et_boundaries(hour, minute, expected):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    _, _, _, runtime = setup()
    runtime.clock = lambda: datetime(2026, 10, 14, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    assert runtime._soft_exit_chase_window() is expected
