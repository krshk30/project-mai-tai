"""BBCI regression: real execution fields, synthetic transport and actor clocks.

8886 interrupted-full receipt: candidate-8886-full-unit.log lines 2493-2772
records both six-minute cases refusing native_cancel_fresh_read_UNKNOWN. The
old fake's bare None denotes invalid/unusable evidence under MOMORDER1, not
transport unavailability. The fixture now raises a real timeout at the fresh
read boundary; ordinary observations stay UNKNOWN. Classification is not mocked.
Original once-only cancel/no-SELL/fill-wins/remaining assertions stay intact.
"""
from dataclasses import replace
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from project_mai_tai.db.models import Fill
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from tests.unit.test_momlive1_exit_recovery import fresh, held, sells
from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup
from tests.unit.test_webull_adapter import _FakeClient, _order
from tests.unit.test_webull_adapter import fake_sdk as fake_sdk
from tests.unit.test_webull_list_primary import Clock, listed, primary, row


def owned_request(coid="ownedT", *, deadline=False):
    return replace(_order(client_order_id=coid, symbol="AIFA"),
        strategy_code="momentum_warn_live", metadata={"episode_id": "episode",
            "momlive_one_shot": "true", "momlive_native_child": "true",
            **({"momlive_entry_deadline_read": "true"} if deadline else {})})


@pytest.mark.parametrize("shape", ["missing", "combo_only"])
def test_missing_child_list_uses_one_exact_budgeted_detail(fake_sdk, shape):
    body = listed() if shape == "missing" else listed({"client_order_id": "combo", "items": [],
                                                      "children": [row("ownedT")]})
    client = _FakeClient({"today": body, "detail": row("ownedT", "FILLED", qty="25", price="3.87")})
    adapter = primary(client)
    report = adapter._fetch_order_blocking(adapter.accounts_by_name["live:orb"], owned_request())
    assert report.event_type == "filled" and report.filled_quantity == 25 and report.fill_price == D("3.87")
    assert client.calls == {"today": 1, "detail": 1}
    assert client.last["detail"].values["client_order_id"] == "ownedT"


def test_deadline_fresh_read_bypasses_stale_working_list(fake_sdk):
    client = _FakeClient({"today": listed(row("ownedT")),
                          "detail": row("ownedT", "FILLED", qty="25", price="3.87")})
    adapter = primary(client)
    report = adapter._fetch_order_blocking(adapter.accounts_by_name["live:orb"], owned_request(deadline=True))
    assert report.filled_quantity == 25
    assert client.calls == {"detail": 1}


def test_bbci_retained_execution_fields_parse_without_combo_lookup(fake_sdk):
    # Retained execution fields; private broker/client/account identities replaced.
    body = {"client_order_id": "ownedT", "order_id": "broker-ownedT", "items": [{
        "symbol": "BBCI", "side": "SELL", "order_type": "LIMIT", "qty": "25",
        "filled_qty": "25", "filled_price": "3.87", "order_status": "FILLED",
        "place_time": "2026-10-15 13:31:26.234+0000",
        "last_filled_time": "2026-10-15 13:32:26.520+0000"}]}
    client = _FakeClient({"today": listed(), "detail": body})
    adapter = primary(client)
    request = replace(owned_request(), symbol="BBCI", side="sell", intent_type="close", quantity=D(25))
    report = adapter._fetch_order_blocking(adapter.accounts_by_name["live:orb"], request)
    assert (report.event_type, report.filled_quantity, report.fill_price) == ("filled", D(25), D("3.87"))
    assert report.reported_at.isoformat() == "2026-10-15T13:32:26.520000+00:00"
    assert client.calls == {"today": 1, "detail": 1}


@pytest.mark.parametrize("defect", ["client", "symbol", "side", "account", "time", "quantity", "status"])
def test_fresh_detail_never_accepts_unbound_or_invalid_fill(fake_sdk, defect):
    body = row("ownedT", "FILLED", qty="25", price="3.87")
    if defect == "client":
        body["client_order_id"] = "foreign"
    elif defect == "account":
        body["account_id"] = "foreign"
    else:
        key, value = {"symbol": ("symbol", "FOREIGN"), "side": ("side", "SELL"),
            "time": ("last_filled_time", "invalid"), "quantity": ("filled_qty", "NaN"),
            "status": ("order_status", "UNRECOGNIZED")}[defect]
        body["items"][0][key] = value
    client = _FakeClient({"today": listed(), "detail": body})
    adapter = primary(client)
    assert adapter._fetch_order_blocking(adapter.accounts_by_name["live:orb"], owned_request()) is None
    assert client.calls == {"today": 1, "detail": 1}


def test_detail_budget_shared_and_no_guessed_master_retry(fake_sdk):
    clock = Clock()
    client = _FakeClient({"today": listed(), "detail": row("ownedT", "FILLED", qty="25", price="3.87")})
    adapter = primary(client, clock)
    for _ in range(20):
        adapter._fetch_order_blocking(adapter.accounts_by_name["live:orb"], owned_request())
    assert client.calls == {"today": 1, "detail": 1}
    # The ordinary fallback did not consume the reserved strict permit.
    adapter._query_budget.claim("detail", "strict-cancel", strict=True)
    assert len(adapter._query_budget.attempts["detail"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("filled_target", [True, False])
async def test_unknown_six_minutes_still_evaluates_read_first_time_exit(filled_target):
    now, broker, service, runtime, identity = await held()
    handle = runtime.native.handles[identity]
    fetch = broker.fetch_order_update
    submit = broker.submit_order
    allow_reads = [False]
    reads, actions = [], []
    async def read(request):
        if request.client_order_id in handle.child_ids:
            reads.append(request.client_order_id)
            if not allow_reads[0]:
                if request.metadata.get("momlive_fresh_order_read") == "true":
                    actions.append(("fresh-unavailable", request.client_order_id))
                    raise TimeoutError("controlled original-order detail transport unavailable")
                return None
        return await fetch(request)
    async def actual_cancel_wire(request):
        if request.intent_type == "cancel":
            assert request.client_order_id in handle.child_ids
            assert request.broker_account_name == handle.account and request.symbol == handle.symbol
            assert request.side == "sell" and request.quantity == 0
            assert ("fresh-unavailable", request.client_order_id) in actions
            actions.append(("cancel-wire", request.client_order_id))
        return await submit(request)
    broker.fetch_order_update = read
    broker.submit_order = actual_cancel_wire
    runtime.actor.restored = True
    runtime.actor._observations.clear()
    for _ in range(7):
        fresh(now, service, 60)
        await runtime.actor._service_owned(identity)
    assert reads and sells(broker) == []
    assert broker.cancelled == list(handle.child_ids)  # Exact owned pair, once; never a SELL.
    assert [coid for kind, coid in actions if kind == "cancel-wire"] == list(handle.child_ids)
    assert all(sum(kind == "cancel-wire" and coid == child for kind, coid in actions) == 1
        for child in handle.child_ids)
    assert runtime.runtime[identity]["soft_exit_reason"] == "time_300s"
    assert runtime.book.episodes[identity].remaining == 33
    assert handle.status == "cancel_requested"
    allow_reads[0] = True
    if filled_target:
        target = broker.orders[handle.child_ids[0]]
        target.status, target.quantity, target.price = "filled", D(33), D("3.15")
        broker.orders[handle.child_ids[1]].status = "cancelled"
    fresh(now, service, 2)
    await runtime.actor._service_owned(identity)
    assert len(sells(broker)) == (0 if filled_target else 1)
    if not filled_target:
        assert sells(broker)[0].quantity == 33
        assert set(broker.cancelled) == set(handle.child_ids)
    else:
        assert runtime.book.episodes[identity].state == "closed"
        assert identity not in runtime.actor.work.owners


@pytest.mark.asyncio
async def test_terminal_target_retained_while_sibling_unknown_then_restart_closes_once():
    now, broker, service, runtime, identity = await held()
    handle = runtime.native.handles[identity]
    target, stop = handle.child_ids
    broker.orders[target].status = "filled"
    broker.orders[target].quantity, broker.orders[target].price = D(33), D("3.15")
    fetch = broker.fetch_order_update
    calls = []
    stop_unknown = [True]
    async def read(request):
        calls.append(request.client_order_id)
        if request.client_order_id == stop and stop_unknown[0]:
            return None
        return await fetch(request)
    broker.fetch_order_update = read
    for _ in range(3):
        observation = await runtime._observe(identity)
        assert observation.exited == 33 and observation.outstanding_exit
        assert observation.position.release_unknown
    assert calls.count(target) == 1
    with service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Fill).where(Fill.side == "sell")) == 1
    stop_unknown[0] = False
    broker.orders[stop].status = "cancelled"
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="987:654")
    await restarted.start()
    try:
        assert restarted.book.episodes[identity].state == "closed"
        assert identity not in restarted.actor.work.owners
        assert calls.count(target) == 1
        assert sells(broker) == []
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_entry_deadline_marks_fresh_read_without_resending():
    now, broker, service, runtime = setup()
    broker.entry_status, broker.entry_quantity = "accepted", D(0)
    await runtime.start()
    try:
        await offer_and_drain(runtime, event(now[0]))
        identity = next(iter(runtime.book.episodes))
        fetch = broker.fetch_order_update
        reads = []
        async def read(request):
            reads.append(request)
            return await fetch(request)
        broker.fetch_order_update = read
        fresh(now, service, 2)
        await runtime.actor._service_owned(identity)
        entry_reads = [r for r in reads if r.side == "buy"]
        assert entry_reads and entry_reads[0].metadata["momlive_entry_deadline_read"] == "true"
        assert len([r for r in broker.sent if r.side == "buy" and r.intent_type == "open"]) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_filled_entry_with_unmeasurable_first_time_does_not_starve_native_detail_budget():
    _, broker, _, runtime, identity = await held()
    assert runtime.runtime[identity]["first_execution_time_status"] == "UNKNOWN"
    fetch = broker.fetch_order_update
    reads = []
    async def read(request):
        reads.append(request)
        return await fetch(request)
    broker.fetch_order_update = read
    await runtime._observe(identity)
    assert not [request for request in reads if request.side == "buy"]
    assert [request for request in reads if request.metadata.get("momlive_native_child")]
