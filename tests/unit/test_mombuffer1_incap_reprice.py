"""Synthetic branch/race controls, never incident replay or native acceptance."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, TradeIntent, VirtualPosition
from project_mai_tai.momentum_live.admission import Refusal, Route, price_entry
from project_mai_tai.momentum_live.entry_reprice import MAX_PRICE_UPDATES
from tests.unit.test_momstuck2_prewire_refusal import durable, env as env  # noqa: F401
from tests.unit.test_webull_adapter import fake_sdk as fake_sdk  # noqa: F401


@pytest.fixture
def priced(env):
    h = env
    h.ports.reprice_unsent_entry = h.runtime._reprice_unsent_entry
    h.ports.refuse_local_entry = h.runtime._refuse_local_entry
    return h


def move_at(h, point, ask):
    if point == "after_durable":
        original = h.ports.durable_order

        async def moved(*args):
            await original(*args)
            h.quote["ask"] = ask

        h.ports.durable_order = moved
    else:
        original = h.ports.reprice_unsent_entry

        async def moved(previous, current, episode=None):
            await original(previous, current, episode)
            if episode is not None:
                h.quote["ask"] = ask

        h.ports.reprice_unsent_entry = moved


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["after_durable", "before_wire"])
@pytest.mark.parametrize("ask", [D("3.01"), D("2.94")], ids=["up", "down-quantity-change"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 14], ids=["PM", "RTH"])
async def test_same_row_incap_move_sends_current_price_once(priced, point, ask, provider, hour):
    h = priced
    h.now[0] = h.now[0].replace(hour=hour)
    h.quote["received_at"] = h.now[0]
    signal = replace(h.signal, observed_ns=h.runtime.now_ns(), country_route=provider)
    ids = []
    original = h.ports.durable_order

    async def capture(*args):
        await original(*args)
        parent, order = durable(h)
        ids.append((parent.id, order.id, order.client_order_id))

    h.ports.durable_order = capture
    move_at(h, point, ask)
    identity, request, _ = await h.executor.admit(signal)
    parent, order = durable(h)
    assert (parent.id, order.id, order.client_order_id) == ids[0]
    assert len(h.sent) == 1 and h.sent[0] == request
    # Claude16:27:28: falling ask retains the saved marketable limit AND qty.
    expected_limit = max(D("3.00"), ask)
    assert order.quantity == parent.quantity == request.quantity == int(D(100) / expected_limit)
    assert order.payload["limit_price"] == parent.payload["metadata"]["limit_price"] == str(expected_limit)
    assert request.metadata["reference_price"] == str(expected_limit)
    assert D(request.metadata["limit_price"]) <= D(request.metadata["cap_price"]) == D("3.045")
    assert parent.payload["momlive_episode"]["state"] == "submitting"
    assert order.broker_order_id == "synthetic-native-id"
    assert (await h.executor.admit(signal))[1] is None
    assert len(h.sent) == 1 and h.runtime.book.episodes[identity].first_submit_ns is not None
    with h.service.session_factory() as session:
        assert len(session.scalars(select(BrokerOrder)).all()) == 1


@pytest.mark.asyncio
async def test_both_checkpoints_can_commit_without_a_second_order(priced):
    h = priced
    move_at(h, "after_durable", D("3.01"))
    move_at(h, "before_wire", D("3.02"))
    _, request, _ = await h.executor.admit(h.signal)
    assert request.metadata["limit_price"] == "3.02" and len(h.sent) == 1
    assert durable(h)[1].quantity == request.quantity


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["cap", "stale", "missing", "future", "expired", "control"])
async def test_post_commit_refusal_settles_current_version(priced, fault):
    h = priced
    move_at(h, "after_durable", D("3.01"))
    original = h.ports.reprice_unsent_entry
    committed = []

    async def changed(previous, current, episode=None):
        await original(previous, current, episode)
        committed.append(current)
        if fault == "cap":
            h.quote["ask"] = D("3.05")
        elif fault == "stale":
            h.quote["received_at"] -= timedelta(seconds=3)
        elif fault == "missing":
            h.quote.pop("ask", None)
        elif fault == "future":
            h.quote["received_at"] += timedelta(microseconds=1)
        elif fault == "control":
            def stopped(signal):
                raise Refusal("KILLSW1:entry_scope_not_running")
            h.ports.entry_metadata = stopped
        else:
            h.now[0] += timedelta(seconds=3)

    h.ports.reprice_unsent_entry = changed
    with pytest.raises(Refusal):
        await h.executor.admit(h.signal)
    parent, order = durable(h)
    assert len(committed) == 1 and committed[0].metadata["limit_price"] == "3.01"
    assert parent.status == "cancelled" and order.status == "aborted"
    assert order.payload["limit_price"] == "3.01" and order.quantity == committed[0].quantity
    assert order.payload["momlive_entry_never_dispatched"] is True
    assert not h.sent and h.runtime.book._active_count == 0


@pytest.mark.asyncio
async def test_continuous_quote_churn_is_bounded_and_settles_last_commit(priced):
    h = priced
    move_at(h, "after_durable", D("3.01"))
    original = h.ports.reprice_unsent_entry
    updates = []

    async def churn(previous, current, episode=None):
        await original(previous, current, episode)
        updates.append(current)
        h.quote["ask"] += D(".01")

    h.ports.reprice_unsent_entry = churn
    with pytest.raises(Refusal, match="budget_exhausted"):
        await h.executor.admit(h.signal)
    assert len(updates) == MAX_PRICE_UPDATES == 2
    parent, order = durable(h)
    assert order.payload["limit_price"] == updates[-1].metadata["limit_price"] == "3.02"
    assert order.status == "aborted" and parent.status == "cancelled" and not h.sent
    assert order.payload["momstuck2_refusal"]["context"]["limit"] == "3.03"


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["terminal", "native", "submitted", "permit", "event", "quantity", "payload",
                                      "fill", "vp", "managed", "ack_unknown"])
async def test_cas_refuses_changed_or_dispatched_row_without_overwrite(priced, evidence):
    h = priced
    prepare = h.ports.durable_order
    before = []

    async def changed(*args):
        await prepare(*args)
        with h.service.session_factory() as session:
            order = session.scalar(select(BrokerOrder))
            parent = session.scalar(select(TradeIntent))
            if evidence == "terminal":
                order.status, parent.status = "aborted", "cancelled"
                parent.payload = {**parent.payload, "momlive_episode": {
                    **parent.payload["momlive_episode"], "state": "terminal_no_fill"}}
            elif evidence == "native":
                order.broker_order_id = "unknown-ack-may-have-sent"
            elif evidence == "submitted":
                order.submitted_at = h.now[0]
            elif evidence == "permit":
                order.payload = {**order.payload, "bot_control_permit": "MAY_HAVE_SENT"}
            elif evidence == "event":
                session.add(BrokerOrderEvent(order_id=order.id, event_type="rejected",
                    event_source="unknown", payload={"source": "SYNTHETIC ACK unknown"}))
            elif evidence == "quantity":
                order.quantity += 1
            elif evidence == "payload":
                order.payload = {**order.payload, "signal_price": "foreign-preimage"}
            elif evidence == "ack_unknown":
                order.payload = {**order.payload, "momcancel1_ack_identity": "UNKNOWN"}
            elif evidence == "fill":
                session.add(Fill(order_id=order.id, strategy_id=parent.strategy_id,
                    broker_account_id=parent.broker_account_id, symbol="SPHL", side="buy",
                    quantity=D(1), price=D(3), broker_fill_id="SYNTHETIC-CAS-fill"))
            elif evidence == "vp":
                session.add(VirtualPosition(strategy_id=parent.strategy_id, broker_account_id=parent.broker_account_id,
                    symbol="SPHL", quantity=D(1), average_price=D(3)))
            else:
                session.add(OmsManagedPosition(strategy_code="momentum_warn_live", broker_account_name=h.runtime.accounts[1],
                    symbol="SPHL", entry_price=D(3), original_quantity=1, current_quantity=1, status="open"))
            session.commit()
            before.append((order.status, parent.status, order.quantity, dict(order.payload), dict(parent.payload)))
        h.quote["ask"] = D("3.01")

    h.ports.durable_order = changed
    with pytest.raises(Refusal):
        await h.executor.admit(h.signal)
    parent, order = durable(h)
    assert (order.status, parent.status, order.quantity, order.payload, parent.payload) == before[0]
    assert not h.sent and h.runtime.book._active_count == 1


@pytest.mark.asyncio
async def test_submitting_snapshot_cannot_resurrect_terminal_owner(priced):
    h = priced
    original = h.ports.reprice_unsent_entry

    async def terminal(previous, current, episode=None):
        if episode is not None:
            with h.service.session_factory() as session:
                parent = session.scalar(select(TradeIntent))
                parent.status = "cancelled"
                parent.payload = {**parent.payload, "momlive_episode": {
                    **parent.payload["momlive_episode"], "state": "terminal_no_fill"}}
                session.commit()
        await original(previous, current, episode)

    h.ports.reprice_unsent_entry = terminal
    with pytest.raises(Refusal, match="owner_UNKNOWN"):
        await h.executor.admit(h.signal)
    assert durable(h)[0].payload["momlive_episode"]["state"] == "terminal_no_fill"
    assert not h.sent and h.runtime.book._active_count == 1


@pytest.mark.asyncio
async def test_write_failure_keeps_slot_and_never_submits_old_payload(priced):
    h = priced
    move_at(h, "after_durable", D("3.01"))
    h.ports.reprice_unsent_entry = AsyncMock(side_effect=RuntimeError("SYNTHETIC commit failed"))
    with pytest.raises(RuntimeError, match="commit failed"):
        await h.executor.admit(h.signal)
    assert durable(h)[1].payload["limit_price"] == "3.00"
    assert not h.sent and h.runtime.book._active_count == 1


@pytest.mark.asyncio
async def test_two_slots_and_overlap_idempotence_survive_reprice(priced):
    h = priced
    h.runtime.book.claim(replace(h.signal, symbol="HELD", source_id="SYNTHETIC-held"),
                         now_ns=h.runtime.now_ns())
    move_at(h, "after_durable", D("3.01"))
    await h.executor.admit(h.signal)
    with pytest.raises(Refusal, match="two_reservations_full"):
        await h.executor.admit(replace(h.signal, symbol="THIRD", source_id="SYNTHETIC-third"))
    assert (await h.executor.admit(h.signal))[1] is None and len(h.sent) == 1


@pytest.mark.asyncio
async def test_subdollar_webull_quantity_and_cap_preserved(priced):
    """The old sub-dollar executable entry is now an explicit floor refusal."""
    h = priced
    h.quote["ask"] = D(".80")
    signal = replace(h.signal, signal_price=D(".80"))
    move_at(h, "before_wire", D(".81"))
    with pytest.raises(Refusal, match="^PRICE1-REFUSED$"):
        await h.executor.admit(signal)
    assert durable(h) == (None, None)
    assert not h.sent and h.runtime.book._active_count == 0


def test_subdollar_quantity_cap_geometry_does_not_authorize_dispatch():
    priced = price_entry(D(".80"), D(".81"), quote_ns=1, now_ns=1,
                         route=Route.WEBULL, tick=D(".0001"))
    assert priced.quantity == 123 and priced.quantity >= 100
    assert priced.limit == D(".81") <= priced.cap == D(".81200")


@pytest.mark.asyncio
async def test_eligible_webull_quantity_and_cap_preserved(priced):
    h = priced
    h.quote["ask"] = D("1.00")
    signal = replace(h.signal, signal_price=D("1.00"))
    move_at(h, "before_wire", D("1.01"))
    _, request, _ = await h.executor.admit(signal)
    parent, order = durable(h)
    assert request.quantity == parent.quantity == order.quantity == 99
    assert D(request.metadata["limit_price"]) == D("1.01") <= D(request.metadata["cap_price"]) == D("1.01500")
    assert len(h.sent) == 1 and h.sent[0] == request


@pytest.mark.asyncio
async def test_possible_submit_never_reprices_or_settles_after_ack_uncertainty(priced):
    h = priced
    move_at(h, "after_durable", D("3.01"))
    settle = AsyncMock(wraps=h.runtime._refuse_unsent_entry)
    h.ports.refuse_unsent_entry = settle
    updates = AsyncMock(wraps=h.ports.reprice_unsent_entry)
    h.ports.reprice_unsent_entry = updates
    counts = []

    async def uncertain(request):
        counts.append(updates.await_count)
        h.sent.append(request)
        request.metadata["bot_control_permit"] = "MAY_HAVE_SENT"
        h.quote["ask"] = D("3.02")
        raise TimeoutError("SYNTHETIC ACK unknown")

    h.ports.submit = uncertain
    with pytest.raises(TimeoutError):
        await h.executor.admit(h.signal)
    settle.assert_not_awaited()
    assert updates.await_count == counts[0] and h.runtime.book._active_count == 1
    assert (await h.executor.admit(h.signal))[1] is None and len(h.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["price", "stale", "wire_limit", "wire_quantity"])
async def test_exact_local_no_dispatch_after_permit_is_terminal_without_global_settlement(priced, fault):
    import asyncio
    from project_mai_tai.momentum_live.entry_wire import entry_wire_active, entry_wire_guard

    h = priced
    settle = AsyncMock(wraps=h.runtime._refuse_unsent_entry)
    h.ports.refuse_unsent_entry = settle
    checked = []

    async def delayed_control_and_transport(request):
        request.metadata["bot_control_permit"] = "SYNTHETIC-MAY_HAVE_SENT"
        with h.service.session_factory() as session:
            order = session.scalar(select(BrokerOrder))
            order.payload = {**order.payload, "bot_control_permit": request.metadata["bot_control_permit"]}
            session.commit()
        if fault == "price":
            h.quote["ask"] = D("3.01")
        elif fault == "stale":
            h.now[0] += timedelta(seconds=3)
        # The owner explicitly passes its exact capability to the native worker.
        guard = entry_wire_guard(request)
        assert guard is not None
        await asyncio.to_thread(guard.check,
            quantity=request.quantity + (1 if fault == "wire_quantity" else 0),
            limit="3.01" if fault == "wire_limit" else request.metadata["limit_price"], symbol=request.symbol)
        checked.append(request)
        raise AssertionError("a stale or changed wire reached transport")

    h.ports.submit = delayed_control_and_transport
    with pytest.raises(Refusal):
        await h.executor.admit(h.signal)
    assert not checked and not h.sent and h.runtime.book._active_count == 0
    settle.assert_not_awaited()
    parent, order = durable(h)
    assert order.payload["limit_price"] == "3.00" and order.status == "aborted" and parent.status == "cancelled"
    assert order.payload["bot_control_permit"] == "SYNTHETIC-MAY_HAVE_SENT"
    assert order.payload["mombuffer1_no_dispatch"]["permit_retained"] is True
    assert parent.payload["momlive_episode"]["state"] == "terminal_no_fill"
    restored = await h.service._run_db(lambda session: h.runtime.db.restore_bundle(session,
        h.now[0].replace(hour=0, minute=0, second=0, microsecond=0)), commit=False)
    assert restored["episodes"][0]["state"] == "terminal_no_fill"
    assert (await h.executor.admit(h.signal))[1] is None and not h.sent
    assert not entry_wire_active()


def test_unrelated_wire_has_no_momentum_guard_or_decimal_conversion():
    from project_mai_tai.momentum_live.entry_wire import validate_entry_wire
    validate_entry_wire(quantity=None, limit=None, symbol=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True], ids=["same-serialized-payload", "changed-serialized-payload"])
async def test_schwab_actual_blocking_boundary_checks_serialized_body(priced, monkeypatch, changed):
    import json
    from types import SimpleNamespace
    from unittest.mock import MagicMock, Mock
    from project_mai_tai.broker_adapters import schwab
    from project_mai_tai.momentum_live.entry_wire import entry_wire_check

    _, request, _ = await priced.executor.admit(priced.signal)
    adapter = object.__new__(schwab.SchwabBrokerAdapter)
    adapter.request_timeout_seconds = 1
    body = adapter._build_order_payload(request)
    if changed:
        body["price"] = "3.01"
    response = SimpleNamespace(headers={}, read=lambda: b"{}", getcode=lambda: 201)
    transport = MagicMock()
    transport.return_value.__enter__.return_value = response
    monkeypatch.setattr(schwab.urllib.request, "urlopen", transport)
    check = Mock(return_value={})
    with entry_wire_check(request, check) as guard:
        if changed:
            with pytest.raises(RuntimeError, match="serialized_wire_changed"):
                adapter._blocking_request_json("https://denied.invalid/accounts/A/orders", "POST", {},
                                               json.dumps(body).encode(), entry_guard=guard)
        else:
            assert adapter._blocking_request_json("https://denied.invalid/accounts/A/orders", "POST", {},
                                                   json.dumps(body).encode(), entry_guard=guard)[0] == 201
    check.assert_called_once()
    assert transport.call_count == int(not changed)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_webull_actual_sdk_boundary_checks_after_instrument_wait(priced, fake_sdk, monkeypatch, changed):
    from unittest.mock import Mock
    from project_mai_tai.broker_adapters.webull import WebullAccountConfig
    from project_mai_tai.momentum_live.entry_wire import entry_wire_check
    from tests.unit.test_webull_adapter import _FakeClient, _adapter

    _, request, _ = await priced.executor.admit(priced.signal)
    client = _FakeClient({"place": {"client_order_id": request.client_order_id}})
    adapter = _adapter(client)
    check = Mock(return_value={}, side_effect=RuntimeError("SYNTHETIC final quote changed") if changed else None)
    monkeypatch.setattr(adapter, "_resolve_instrument_id", lambda *args: "SYNTHETIC-INSTRUMENT")
    with entry_wire_check(request, check) as guard:
        reports = adapter._submit_blocking(WebullAccountConfig(account_id="SYNTHETIC-ACCOUNT"), request,
                                          entry_guard=guard)
    check.assert_called_once()
    assert client.calls.get("place", 0) == int(not changed)
    assert reports[0].event_type == ("rejected" if changed else "accepted")
    if changed:
        assert reports[0].origin != "broker"
