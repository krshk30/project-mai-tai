"""Synthetic review controls; no execution/incident acceptance claimed by author."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent
from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.momentum_live.entry_wire import entry_wire_check, entry_wire_guard
from tests.unit.test_mombuffer1_incap_reprice import priced  # noqa: F401
from tests.unit.test_momstuck2_prewire_refusal import durable, env  # noqa: F401
from tests.unit.test_webull_adapter import fake_sdk  # noqa: F401


def permit(h, request):
    request.metadata["bot_control_permit"] = "SYNTHETIC-EXACT-PERMIT"
    with h.service.session_factory() as session:
        order = session.scalar(select(BrokerOrder))
        order.payload = {**order.payload, "bot_control_permit": request.metadata["bot_control_permit"]}
        session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["ATR", "ORB", "EXIT", "ACCOUNT", "EQUAL_COPY"])
async def test_exact_request_and_owning_task_only_no_inherited_capability(priced, family):
    _, request, _ = await priced.executor.admit(priced.signal)
    other = replace(request, metadata=dict(request.metadata))
    if family == "ATR":
        other = replace(other, strategy_code="schwab_1m_v2")
    elif family == "ORB":
        other = replace(other, strategy_code="orb_schwab")
    elif family == "EXIT":
        other = replace(other, side="sell", intent_type="close")
    elif family == "ACCOUNT":
        other = replace(other, broker_account_name="live:another")
    with entry_wire_check(request, lambda: {}) as guard:
        assert entry_wire_guard(request) is guard
        if family == "EQUAL_COPY":
            with pytest.raises(RuntimeError, match="entry_object_changed"):
                entry_wire_guard(other)
        elif family == "EXIT":
            # A modified same-order identity is not a legitimate unrelated sell.
            other = replace(other, client_order_id="SYNTHETIC-unrelated-exit")
            assert entry_wire_guard(other) is None
        else:
            assert entry_wire_guard(other) is None

        async def inherited():
            # Even the SAME object in a task inheriting ContextVars has no lease.
            with pytest.raises(RuntimeError, match="entry_task_changed"):
                entry_wire_guard(request)
            if family != "EQUAL_COPY":
                assert entry_wire_guard(other) is None

        await asyncio.create_task(inherited())
        assert await asyncio.to_thread(entry_wire_guard, request) is None


@pytest.mark.asyncio
async def test_unrelated_multileg_post_ignores_inherited_momentum_context(priced, monkeypatch):
    import json
    from project_mai_tai.broker_adapters import schwab

    _, request, _ = await priced.executor.admit(priced.signal)
    adapter = object.__new__(schwab.SchwabBrokerAdapter)
    adapter.request_timeout_seconds = 1
    transport = MagicMock()
    transport.return_value.__enter__.return_value = SimpleNamespace(
        headers={}, read=lambda: b"{}", getcode=lambda: 201)
    monkeypatch.setattr(schwab.urllib.request, "urlopen", transport)

    def unrelated_post():
        # No explicit capability supplied: no global ContextVar POST interception.
        return adapter._blocking_request_json("https://denied.invalid/accounts/OTHER/orders", "POST", {},
            json.dumps({"orderStrategyType": "OCO", "orderLegCollection": [{}, {}]}).encode())

    with entry_wire_check(request, lambda: (_ for _ in ()).throw(AssertionError("unrelated guard ran"))):
        assert await asyncio.to_thread(unrelated_post) == (201, {}, {})
    transport.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["runtime", "timeout", "prior_transport"])
async def test_generic_or_any_prior_wire_uncertainty_never_terminalizes(priced, fault):
    h = priced
    local = AsyncMock(wraps=h.runtime._refuse_local_entry)
    old = AsyncMock(wraps=h.runtime._refuse_unsent_entry)
    h.ports.refuse_local_entry, h.ports.refuse_unsent_entry = local, old

    async def uncertain(request):
        permit(h, request)
        guard = entry_wire_guard(request)
        if fault == "prior_transport":
            guard.before_transport()
            h.quote["ask"] = "3.01"
            # Models a second check after a first POST (including 401) or timeout.
            guard.check(quantity=request.quantity, limit=request.metadata["limit_price"], symbol=request.symbol)
        if fault == "timeout":
            raise TimeoutError("SYNTHETIC ACK unknown")
        raise RuntimeError("SYNTHETIC generic adapter failure")

    h.ports.submit = uncertain
    with pytest.raises((RuntimeError, TimeoutError)):
        await h.executor.admit(h.signal)
    local.assert_not_awaited()
    old.assert_not_awaited()
    parent, order = durable(h)
    assert parent.status == order.status == "pending" and h.runtime.book._active_count == 1
    assert "mombuffer1_no_dispatch" not in order.payload and not h.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["permit", "payload", "native", "submitted", "event", "quantity"])
async def test_local_receipt_never_overwrites_conflicting_durable_evidence(priced, fault):
    h = priced
    settle = h.ports.refuse_local_entry

    async def changed(request, proof):
        with h.service.session_factory() as session:
            order = session.scalar(select(BrokerOrder))
            if fault == "permit":
                order.payload = {**order.payload, "bot_control_permit": "DIFFERENT-PERMIT"}
            elif fault == "payload":
                order.payload = {**order.payload, "foreign_owner_change": "true"}
            elif fault == "native":
                order.broker_order_id = "SYNTHETIC-possibly-sent"
            elif fault == "submitted":
                order.submitted_at = h.now[0]
            elif fault == "quantity":
                order.quantity += 1
            else:
                session.add(BrokerOrderEvent(order_id=order.id, event_type="accepted",
                    event_source="unknown", payload={"source": "SYNTHETIC"}))
            session.commit()
        await settle(request, proof)

    async def refuse(request):
        permit(h, request)
        h.quote["ask"] = "3.01"
        entry_wire_guard(request).check(quantity=request.quantity, limit="3.00", symbol=request.symbol)

    h.ports.refuse_local_entry, h.ports.submit = changed, refuse
    with pytest.raises(Refusal):
        await h.executor.admit(h.signal)
    parent, order = durable(h)
    assert parent.status == order.status == "pending" and h.runtime.book._active_count == 1
    assert "mombuffer1_no_dispatch" not in order.payload and not h.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_actual_adapter_local_guard_after_wait_is_terminal_and_never_calls_transport(
    priced, fake_sdk, monkeypatch, provider,
):
    h = priced
    if provider == "schwab":
        from project_mai_tai.broker_adapters import schwab
        from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig
        adapter = object.__new__(schwab.SchwabBrokerAdapter)
        adapter.settings = SimpleNamespace(schwab_native_bracket_enabled=False)
        adapter.accounts_by_name = {h.runtime.accounts[0]: SchwabAccountConfig(account_hash="SYNTHETIC")}
        adapter.base_url = "https://denied.invalid"
        adapter.request_timeout_seconds = 1
        adapter._refresh_token = None

        async def token(**kwargs):
            h.quote["ask"] = "3.01"
            return "SYNTHETIC-token"

        adapter._get_access_token = token
        transport = MagicMock(side_effect=AssertionError("native POST must not execute"))
        monkeypatch.setattr(schwab.urllib.request, "urlopen", transport)
    else:
        from tests.unit.test_webull_adapter import _FakeClient, _adapter
        client = _FakeClient({})
        adapter = _adapter(client)

        def instrument(*args):
            h.quote["ask"] = "3.01"
            return "SYNTHETIC-instrument"

        monkeypatch.setattr(adapter, "_resolve_instrument_id", instrument)

    async def submit(request):
        permit(h, request)
        return await adapter.submit_order(request)

    h.ports.submit = submit
    with pytest.raises(Refusal, match="quote_changed_after_permit"):
        await h.executor.admit(replace(h.signal, country_route=provider))
    parent, order = durable(h)
    assert parent.status == "cancelled" and order.status == "aborted"
    assert parent.payload["momlive_episode"]["state"] == "terminal_no_fill"
    assert order.payload["bot_control_permit"] == "SYNTHETIC-EXACT-PERMIT"
    assert h.runtime.book._active_count == 0 and not h.sent
    if provider == "schwab":
        transport.assert_not_called()
    else:
        assert client.calls.get("place", 0) == 0
