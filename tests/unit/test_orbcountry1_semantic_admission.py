"""ORBCOUNTRY1 controlled contracts, not historical native replay evidence.

Main must compose the separately proposed Settings/service hooks before the
connected tests. Existing ORBROUTE fixture owns its private DB and fake SDK.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, VirtualPosition
from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.momentum_live_sec import CountryRecord, CountryStore
from project_mai_tai.oms.orb_country import country_route_metadata_matches, verify_country_admission
from project_mai_tai.orb_schwab_order_route import (
    build_orb_schwab_open_intent, build_orb_schwab_reprice_intent,
)
from project_mai_tai.settings import Settings
from tests.unit.test_orbroute1_oms_chain import (
    OPEN, SCHWAB, WEBULL, selected, setup as setup,
)


def metadata(broker="webull", reason="sec_unknown", status="INCOMPLETE", stamp="old"):
    return {"orb_country_route": "true", "orb_country_broker": broker,
            "orb_country_reason": reason, "orb_country_status": status,
            "orb_country_as_of": stamp}


def test_own_switch_is_default_false_not_install_authority():
    assert Settings.model_fields["orb_country_semantic_admission_enabled"].default is False


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("broker,reason,status", [
    ("webull", "sec_unknown", "INCOMPLETE"),
    ("webull", "sec_non_us", "READY"),
    ("schwab", "sec_us", "READY"),
])
def test_only_stamp_equivalence_changes_and_both_inputs_remain_immutable(enabled, broker, reason, status, caplog):
    service = SimpleNamespace(settings=SimpleNamespace(orb_country_semantic_admission_enabled=enabled),
                              logger=logging.getLogger("orbcountry1-test"))
    original = metadata(broker, reason, status)
    current = {**original, "orb_country_as_of": "new"}
    before = deepcopy((original, current))
    with caplog.at_level(logging.INFO):
        assert country_route_metadata_matches(service, original, current, context="controlled") is enabled
    assert (original, current) == before
    if enabled:
        assert "original_as_of=old current_as_of=new" in caplog.text
    else:
        assert "[ORBCOUNTRY1]" not in caplog.text


@pytest.mark.parametrize("field,value", [
    ("orb_country_broker", "schwab"), ("orb_country_reason", "sec_us"),
    ("orb_country_status", "READY"), ("orb_country_status", "FUTURE"),
    ("orb_country_route", "false"), ("orb_country_reason", None),
])
def test_enabled_parent_binding_never_ignores_a_semantic_or_route_marker_change(field, value):
    service = SimpleNamespace(settings=SimpleNamespace(orb_country_semantic_admission_enabled=True),
                              logger=logging.getLogger("orbcountry1-test"))
    original = metadata()
    current = {**original, field: value, "orb_country_as_of": "new"}
    assert not country_route_metadata_matches(service, original, current,
                                              context="parent", keys=tuple(original))


def test_missing_switch_is_original_exact_comparison():
    service = SimpleNamespace(settings=SimpleNamespace(), logger=None)
    original = metadata()
    assert country_route_metadata_matches(service, original, original, context="off")
    assert not country_route_metadata_matches(service, original,
        {**original, "orb_country_as_of": "new"}, context="off")


def restamped_event(setup, is_us, *, enabled=True):
    service, factory, broker = setup
    service.settings.orb_country_semantic_admission_enabled = enabled
    selection = selected(factory, service.settings, is_us)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    store = CountryStore(factory)
    record = store.load().get("ABC")
    if record is None:
        # Explicit CONTROLLED unknown country record, not an ELPW native receipt.
        record = CountryRecord("ABC", None, None, None, None,
                               OPEN - timedelta(days=1), "INCOMPLETE", OPEN.date())
        store.put(record)
        selection = selected(factory, service.settings, None)
        # selected() reads the real store; no forced country/router verdict.
        event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    store.put(replace(record, as_of=OPEN - timedelta(hours=1)))
    return event


@pytest.mark.parametrize("is_us", [True, False, None])
@pytest.mark.parametrize("enabled", [False, True])
def test_real_fresh_store_admission_uses_original_provenance(setup, is_us, enabled):
    service, factory, _broker = setup
    event = restamped_event(setup, is_us, enabled=enabled)
    before = deepcopy(event.payload.metadata)
    assert asyncio.run(verify_country_admission(service, event, OPEN)) is enabled
    assert event.payload.metadata == before


@pytest.mark.parametrize("was_us,now_us", [(True, False), (False, True)])
def test_real_store_us_non_us_transition_still_refused(setup, was_us, now_us):
    service, factory, _broker = setup
    event = restamped_event(setup, was_us)
    store = CountryStore(factory)
    record = store.load()["ABC"]
    store.put(replace(record, is_us=now_us, business_country="NY" if now_us else "A6",
                      incorporation="DE" if now_us else "A6"))
    assert not asyncio.run(verify_country_admission(service, event, OPEN))


@pytest.mark.parametrize("status,stamp", [
    ("INCOMPLETE", OPEN - timedelta(hours=1)),
    ("READY", OPEN + timedelta(seconds=1)),
])
def test_real_router_changed_status_or_future_clock_still_refused(setup, status, stamp):
    service, factory, _broker = setup
    event = restamped_event(setup, True)
    store = CountryStore(factory)
    store.put(replace(store.load()["ABC"], status=status, as_of=stamp,
                      is_us=None if status != "READY" else True))
    assert not asyncio.run(verify_country_admission(service, event, OPEN))


@pytest.mark.parametrize("is_us,account", [(True, SCHWAB), (False, WEBULL)])
@pytest.mark.parametrize("foreign_owner", [False, True])
def test_real_oms_dispatch_and_foreign_owner_fence_after_semantic_refresh(setup, is_us, account, foreign_owner):
    service, factory, broker = setup
    event = restamped_event(setup, is_us)
    original_stamp = event.payload.metadata["orb_country_as_of"]
    if foreign_owner:
        with factory.begin() as session:
            owner = service.store.ensure_strategy(session, "momentum_warn_live", name="MOM", execution_mode="live")
            row = service.store.ensure_broker_account(session, account, provider="schwab" if is_us else "webull",
                                                       environment="production")
            session.add(VirtualPosition(strategy_id=owner.id, broker_account_id=row.id,
                symbol="ABC", quantity=Decimal(91), average_price=Decimal(5)))
    asyncio.run(service.process_trade_intent(event))
    assert event.payload.metadata["orb_country_as_of"] == original_stamp
    if foreign_owner:
        assert not broker.sent and not broker.previews
    else:
        assert len(broker.sent) == 1
        request = broker.sent[0]
        assert (request.broker_account_name, request.strategy_code, request.quantity) == (account, "orb_schwab", Decimal(10))
        assert request.metadata["orb_country_as_of"] == original_stamp
        with factory() as session:
            orders = session.scalars(select(BrokerOrder)).all()
            assert len(orders) == 1
            assert orders[0].payload["orb_country_as_of"] == original_stamp


@pytest.mark.parametrize("enabled,terminal_zero,fresh_us", [
    (False, True, False), (True, False, False),
    (True, True, False), (True, True, True),
])
def test_webull_replacement_keeps_parent_stamp_cancel_zero_and_fresh_route_fences(
        setup, monkeypatch, enabled, terminal_zero, fresh_us, reprice_at=None):
    service, factory, broker = setup
    service.settings.orb_country_semantic_admission_enabled = enabled
    selection = selected(factory, service.settings, False)
    original = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    asyncio.run(service.process_trade_intent(original))
    assert len(broker.sent) == 1
    with factory() as session:
        parent = session.scalar(select(BrokerOrder))
        parent_id = parent.id
        original_stamp = parent.payload["orb_country_as_of"]
    now = reprice_at or OPEN.replace(minute=29)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr("project_mai_tai.oms.orb_country.datetime", Clock)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: now)
    stamp = OPEN - timedelta(hours=1)
    store = CountryStore(factory)
    record = store.load()["ABC"]
    store.put(replace(record, as_of=stamp, is_us=fresh_us,
                      business_country="NY" if fresh_us else "A6",
                      incorporation="DE" if fresh_us else "A6"))
    # New event can carry a refreshed version of the SAME selected route.
    # For the transition negative it deliberately remains the old Webull route;
    # only the fresh actual store read can observe the new Schwab authority.
    refreshed = replace(selection, country=replace(selection.country, as_of=stamp))
    event = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=refreshed)
    if terminal_zero:
        broker.readback = AtrBuyReadback("cancelled_empty", "terminal_cancel_explicit_zero",
            Decimal(0), terminal_cancel=True, broker_status="CANCELLED", broker_order_id="parent-1")
    asyncio.run(service.process_trade_intent(event))
    buys = [r for r in broker.sent if r.intent_type == "open"]
    cancels = [r for r in broker.sent if r.intent_type == "cancel"]
    assert len(cancels) == (1 if enabled and not fresh_us else 0)
    assert len(buys) == (2 if enabled and terminal_zero and not fresh_us else 1)
    with factory() as session:
        parent = session.get(BrokerOrder, parent_id)
        assert parent.payload["orb_country_as_of"] == original_stamp
    if len(buys) == 2:
        assert buys[1].metadata["orb_replacement_of"] == str(parent_id)
        assert buys[1].quantity == Decimal(10)
        assert buys[1].broker_account_name == WEBULL
    assert all(r.strategy_code == "orb_schwab" for r in broker.sent)


def test_source_read_failure_cannot_become_semantic_success(setup, monkeypatch):
    service, _factory, _broker = setup
    event = restamped_event(setup, False)

    def unavailable(_self):
        raise RuntimeError("controlled_source_read_failure")

    monkeypatch.setattr(CountryStore, "load", unavailable)
    with pytest.raises(RuntimeError, match="controlled_source_read_failure"):
        asyncio.run(verify_country_admission(service, event, OPEN))
