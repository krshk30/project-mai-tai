"""ORB producer/control/validator seams, authored for execution after 16:00 ET.

SAIQ's INCOMPLETE route is represented by controlled inputs, not a claimed
copy of the Redis incident event or a new broker receipt.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.momentum_live_sec import CountryRecord, CountryRoute, CountryStore
from project_mai_tai.oms import bot_control as control_module
from project_mai_tai.oms import bot_control_runtime as runtime_module
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_runtime import BotControlRuntime, ControlledBrokerAdapter
from project_mai_tai.orb_country_route import (
    OrbCountrySelection, routed_orb_intent_refusal, selection_from_metadata,
)
from project_mai_tai.orb_schwab_exits import ATR_REASON
from project_mai_tai.orb_schwab_order_route import (
    build_orb_schwab_cancel_intent, build_orb_schwab_exit_intent,
    build_orb_schwab_open_intent, build_orb_schwab_reprice_intent,
    orb_schwab_intent_refusal,
)
from project_mai_tai.settings import Settings
from tests.unit.test_orbroute1_oms_chain import OPEN, SCHWAB, WEBULL, selected, setup as setup


def plan(broker="webull", status="INCOMPLETE", *, routed=True, reprice=False):
    settings = Settings(orb_enabled=True, orb_live_schwab_orders_enabled=True,
        orb_country_routing_enabled=routed, orb_webull_account_name=WEBULL,
        strategy_schwab_1m_v2_account_name=SCHWAB,
        strategy_schwab_1m_v2_broker_provider="schwab")
    reason = "sec_us" if broker == "schwab" else "sec_non_us" if status == "READY" else "sec_unknown"
    selection = OrbCountrySelection("SAIQ", SCHWAB if broker == "schwab" else WEBULL,
        CountryRoute(broker, reason, status, True if broker == "schwab" else None, None,
                     OPEN-timedelta(days=1) if status == "READY" else None)) if routed else None
    builder = build_orb_schwab_reprice_intent if reprice else build_orb_schwab_open_intent
    event = builder(settings, "SAIQ", Decimal("4.69"), selection=selection)
    return settings, event.model_copy(update={"produced_at": OPEN})


def stamp(event):
    control = BotControlRuntime.__new__(BotControlRuntime)
    control.boot_orb_entry_ids = frozenset()
    control.states = {"orb_schwab": SimpleNamespace(admission_open=True, generation=7)}
    control.stamp(event.payload.metadata, "orb_schwab", event.produced_at)


@pytest.mark.parametrize("broker,status", [
    ("webull", "INCOMPLETE"), ("webull", "UNKNOWN"),
    ("webull", "FETCH_FAILED"), ("webull", "READY"), ("schwab", "READY"),
])
@pytest.mark.parametrize("stamped", [False, True])
def test_routed_plan_accepts_only_the_decision_envelope_without_mutating_it(broker, status, stamped):
    settings, event = plan(broker, status)
    if stamped:
        stamp(event)
    before = event.model_dump(mode="json")
    assert routed_orb_intent_refusal(event, settings, OPEN) is None
    assert orb_schwab_intent_refusal(event, settings, OPEN) is None
    assert event.model_dump(mode="json") == before
    assert event.payload.quantity == Decimal(10)
    if stamped:
        assert event.payload.metadata["bot_control_generation"] == "7"
        assert event.payload.metadata["bot_control_decided_at"] == OPEN.isoformat()


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("reprice", [False, True])
def test_legacy_and_routed_reprice_checks_preserve_exact_control_stamps(routed, reprice):
    settings, event = plan("schwab", "READY", routed=routed, reprice=reprice)
    stamp(event)
    before = dict(event.payload.metadata)
    now = OPEN+timedelta(minutes=1) if reprice else OPEN
    assert orb_schwab_intent_refusal(event, settings, now) is None
    assert event.payload.metadata == before
    assert event.payload.quantity == (Decimal(0) if reprice else Decimal(10) if routed else Decimal(2))


@pytest.mark.parametrize("key", [
    "unexpected", "bot_control_extra", "bot_control_permit", "bot_control_replace_intent_id",
])
@pytest.mark.parametrize("routed", [False, True])
def test_unknown_keys_are_not_hidden_by_a_prefix_filter(key, routed):
    settings, event = plan("schwab", "READY", routed=routed)
    stamp(event)
    event.payload.metadata[key] = "untrusted"
    assert orb_schwab_intent_refusal(event, settings, OPEN) is not None


@pytest.mark.parametrize("envelope", [
    {"bot_control_generation": "7"},
    {"bot_control_decided_at": OPEN.isoformat()},
    {"bot_control_generation": "-1", "bot_control_decided_at": OPEN.isoformat()},
    {"bot_control_generation": "unknown", "bot_control_decided_at": OPEN.isoformat()},
    {"bot_control_generation": "7", "bot_control_decided_at": "not-a-time"},
    {"bot_control_generation": "7", "bot_control_decided_at": "2026-10-15T13:28:00"},
])
@pytest.mark.parametrize("routed", [False, True])
def test_partial_malformed_and_stop_sentinel_envelopes_stay_fail_closed(envelope, routed):
    settings, event = plan("schwab", "READY", routed=routed)
    event.payload.metadata.update(envelope)
    assert orb_schwab_intent_refusal(event, settings, OPEN) is not None


@pytest.mark.parametrize("problem", ["quantity", "window", "account", "price", "provenance"])
@pytest.mark.parametrize("broker", ["schwab", "webull"])
def test_decision_stamps_do_not_relax_strategy_guards(problem, broker):
    settings, event = plan(broker, "READY" if broker == "schwab" else "INCOMPLETE")
    stamp(event)
    now = OPEN
    if problem == "quantity":
        event.payload.quantity = Decimal(2)
    elif problem == "window":
        now += timedelta(minutes=2)
    elif problem == "account":
        event.payload.broker_account_name = WEBULL if broker == "schwab" else SCHWAB
    elif problem == "price":
        event.payload.metadata["limit_price"] = "100"
    else:
        event.payload.metadata["orb_country_reason"] = "sec_us" if broker == "webull" else "sec_unknown"
    assert orb_schwab_intent_refusal(event, settings, now) is not None


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("cleanup", ["cancel", "close"])
def test_plain_cancel_and_sell_exit_do_not_gain_an_entry_envelope_exemption(broker, cleanup):
    settings, entry = plan(broker, "READY" if broker == "schwab" else "INCOMPLETE")
    selection = selection_from_metadata("SAIQ", entry.payload.broker_account_name, entry.payload.metadata)
    if cleanup == "cancel":
        event = build_orb_schwab_cancel_intent(settings, "SAIQ", selection=selection)
        now = OPEN
    else:
        event = build_orb_schwab_exit_intent(settings, "SAIQ", str(uuid4()), str(uuid4()),
            ATR_REASON, selection=selection, quantity=Decimal(10))
        now = OPEN+timedelta(minutes=2)
    assert orb_schwab_intent_refusal(event, settings, now) is None
    event.payload.metadata.update(bot_control_generation="7", bot_control_decided_at=OPEN.isoformat())
    assert orb_schwab_intent_refusal(event, settings, now) is not None


def enable_native_control(service, factory, broker, monkeypatch):
    # Use the existing OMS fixture's fixed clock for native control persistence,
    # not a replacement implementation of its generation/permit authority.
    from project_mai_tai.oms import service as service_module

    monkeypatch.setattr(control_module, "datetime", service_module.datetime)
    monkeypatch.setattr(runtime_module, "datetime", service_module.datetime)
    with factory.begin() as session:
        strategy = service.store.ensure_strategy(session, "orb_schwab", name="ORB", execution_mode="live")
        strategy.execution_mode = "live"
        accounts = [service.store.ensure_broker_account(session, name, provider=provider, environment="live")
                    for name, provider in ((SCHWAB, "schwab"), (WEBULL, "webull"))]
        strategy_id, account_ids = strategy.id, tuple(row.id for row in accounts)
    control = BotControlRuntime(factory)
    control.store.seed(strategy_id)
    control.states["orb_schwab"] = control.store.hydrate(strategy_id)
    control.account_modes = {SCHWAB: "live", WEBULL: "live"}
    control.boot_orb_entry_ids = frozenset()
    service.settings.bot_control_enabled = True
    service._bot_control_runtime = control
    service.broker_adapter = ControlledBrokerAdapter(broker, control)
    return control, strategy_id, account_ids


def native_selection(factory, settings, is_us):
    if is_us == "INCOMPLETE":
        CountryStore(factory).put(CountryRecord("ABC", "0000000001", None, None, None,
            OPEN-timedelta(days=1), "INCOMPLETE", OPEN.date()))
        return selected(factory, settings, None)
    return selected(factory, settings, is_us)


@pytest.mark.parametrize("is_us", [True, False, None, "INCOMPLETE"])
def test_real_oms_with_native_run_control_submits_one_selected_parent(setup, monkeypatch, is_us):
    service, factory, broker = setup
    selection = native_selection(factory, service.settings, is_us)
    control, _strategy_id, _account_ids = enable_native_control(service, factory, broker, monkeypatch)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal("5.5284"), selection=selection)
    event = event.model_copy(update={"produced_at": OPEN})
    assert orb_schwab_intent_refusal(event, service.settings, OPEN) is None
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.sent) == 1
    request = broker.sent[0]
    assert request.broker_account_name == (SCHWAB if is_us is True else WEBULL)
    assert request.quantity == Decimal(10)
    assert request.metadata["bot_control_generation"] == str(control.states["orb_schwab"].generation)
    assert request.metadata["bot_control_decided_at"] == OPEN.isoformat()
    assert "bot_control_permit" in request.metadata
    assert "bot_control_generation" in event.payload.metadata
    with factory() as session:
        assert len(session.scalars(select(BrokerOrder)).all()) == 1


@pytest.mark.parametrize("is_us", [True, False, None, "INCOMPLETE"])
@pytest.mark.parametrize("fence", ["STOP", "stale_generation"])
def test_real_oms_native_control_still_prevents_dispatch(setup, monkeypatch, is_us, fence):
    service, factory, broker = setup
    selection = native_selection(factory, service.settings, is_us)
    control, strategy_id, account_ids = enable_native_control(service, factory, broker, monkeypatch)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal("5.5284"), selection=selection)
    event = event.model_copy(update={"produced_at": OPEN})
    if fence == "STOP":
        control.store.request(strategy_id=strategy_id, command_id=uuid4(), request_id=uuid4(),
            expected_generation=0, action="STOP", account_ids=account_ids,
            actor_reference="controlled-operator", reason="L8 STOP control")
        control.states["orb_schwab"] = control.store.hydrate(strategy_id)
    else:
        event.payload.metadata.update(bot_control_generation="99", bot_control_decided_at=OPEN.isoformat())
    try:
        asyncio.run(service.process_trade_intent(event))
    except ControlConflict:
        pass  # The native control fence may refuse before a broker report exists.
    assert broker.sent == []
    if fence == "STOP":
        assert broker.previews == []
        # Even a formerly valid decision envelope cannot bypass the actual
        # adapter's STOP authority after strategy-plan validation succeeds.
        metadata = {**event.payload.metadata, "bot_control_generation": "0",
                    "bot_control_decided_at": OPEN.isoformat()}
        request = OrderRequest("l8-controlled-stop-wire", event.payload.broker_account_name,
            "orb_schwab", "ABC", "buy", "open", Decimal(10), "L8_STOP_CONTROL",
            metadata, "STOP_LIMIT", "day")
        with pytest.raises(ControlConflict, match="KILLSW1:entry_scope_not_running"):
            asyncio.run(service.broker_adapter.submit_order(request))
        assert broker.sent == []
