"""Additional actual OMS replacement consumers; controlled SDK, no native claim."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.momentum_live_sec import CountryStore
from project_mai_tai.orb_schwab_order_route import (
    build_orb_schwab_open_intent, build_orb_schwab_reprice_intent,
)
from tests.unit.test_orbroute1_oms_chain import (
    OPEN, SCHWAB, selected, setup as setup,
    test_actual_post_fill_attach_is_owned_quantity_and_trigger_geometry as attach_case,
    test_known_target_unknown_stop_books_once_and_never_releases_or_resends as unknown_stop_case,
)
from tests.unit.test_orbcountry1_semantic_admission import (
    test_webull_replacement_keeps_parent_stamp_cancel_zero_and_fresh_route_fences as webull_case,
    restamped_event,
)
from tests.unit.test_orbroute1_manual_net_092 import (
    test_manual_thousand_shares_allows_routed_ten_and_logs_without_receipt as manual_case,
    test_manual_net_exception_never_exempts_foreign_bot_vp as foreign_owner_case,
)
from tests.unit.test_orbroute1_oms_chain import WEBULL


@pytest.mark.parametrize("quantity", [Decimal(3), Decimal(10)])
def test_enabled_semantics_keeps_original_owned_rth_attach_assertions(setup, monkeypatch, quantity):
    service, _factory, _broker = setup
    service.settings.orb_country_semantic_admission_enabled = True
    attach_case(setup, monkeypatch, quantity)


def test_enabled_semantics_keeps_unknown_sibling_booking_without_owner_release(setup, monkeypatch):
    service, _factory, _broker = setup
    service.settings.orb_country_semantic_admission_enabled = True
    unknown_stop_case(setup, monkeypatch)


@pytest.mark.parametrize("is_us,account", [(True, SCHWAB), (False, WEBULL)])
def test_enabled_semantics_preserves_actual_manual_position_rule(setup, caplog, is_us, account):
    service, _factory, _broker = setup
    service.settings.orb_country_semantic_admission_enabled = True
    manual_case(setup, account, is_us, caplog)


@pytest.mark.parametrize("is_us,account", [(True, SCHWAB), (False, WEBULL)])
def test_enabled_semantics_never_borrows_foreign_bot_quantity(setup, caplog, is_us, account):
    service, _factory, _broker = setup
    service.settings.orb_country_semantic_admission_enabled = True
    foreign_owner_case(setup, account, is_us, caplog)


@pytest.mark.parametrize("is_us", [True, False])
def test_off_actual_oms_keeps_original_stamp_refusal_without_request(setup, is_us):
    service, _factory, broker = setup
    event = restamped_event(setup, is_us, enabled=False)
    asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews


@pytest.mark.parametrize("was_us,now_us,status", [
    (True, False, "READY"), (False, True, "READY"),
    (True, None, "INCOMPLETE"), (False, None, "INCOMPLETE"),
])
def test_actual_oms_changed_broker_or_ready_incomplete_never_dispatches(setup, was_us, now_us, status):
    service, factory, broker = setup
    event = restamped_event(setup, was_us)
    store = CountryStore(factory)
    store.put(replace(store.load()["ABC"], status=status, is_us=now_us,
        business_country="NY" if now_us else "A6", incorporation="DE" if now_us else "A6"))
    asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews


@pytest.mark.parametrize("terminal_zero", [False, True])
def test_actual_webull_rth_replace_retains_zero_proof_and_parent_provenance(setup, monkeypatch, terminal_zero):
    webull_case(setup, monkeypatch, True, terminal_zero, False,
                reprice_at=OPEN.replace(minute=30, second=10))


@pytest.mark.parametrize("second", [15, 16])
def test_actual_webull_rth_cutoff_never_cancels_or_replaces_parent(setup, monkeypatch, second):
    service, factory, broker = setup
    service.settings.orb_country_semantic_admission_enabled = True
    selection = selected(factory, service.settings, False)
    asyncio.run(service.process_trade_intent(build_orb_schwab_open_intent(
        service.settings, "ABC", Decimal(5), selection=selection)))
    assert len(broker.sent) == 1
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN.replace(minute=30, second=second))
    event = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=selection)
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.sent) == 1
    with factory() as session:
        assert len(session.scalars(select(BrokerOrder)).all()) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_actual_webull_parent_naive_stamp_cannot_gain_reprice_permission(setup, monkeypatch, enabled):
    service, factory, broker = setup
    service.settings.orb_country_semantic_admission_enabled = enabled
    selection = selected(factory, service.settings, False)
    original = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    asyncio.run(service.process_trade_intent(original))
    assert len(broker.sent) == 1
    with factory.begin() as session:
        parent = session.scalar(select(BrokerOrder))
        parent.payload = {**parent.payload, "orb_country_as_of":
                          selection.country.as_of.replace(tzinfo=None).isoformat()}
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN.replace(minute=29))
    event = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=selection)
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.sent) == 1


@pytest.mark.parametrize("at", [OPEN.replace(minute=29), OPEN.replace(minute=30, second=10)])
@pytest.mark.parametrize("sdk_outcome", ["confirmed", "unknown", "rejected"])
def test_actual_schwab_parent_replace_preview_prewire_and_unknown_fences(setup, monkeypatch, at, sdk_outcome):
    service, factory, broker = setup
    service.settings.orb_country_semantic_admission_enabled = True
    selection = selected(factory, service.settings, True)
    original = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    asyncio.run(service.process_trade_intent(original))
    assert len(broker.sent) == 1
    with factory() as session:
        parent = session.scalar(select(BrokerOrder))
        parent_id, parent_coid, parent_native = parent.id, parent.client_order_id, parent.broker_order_id
        parent_stamp = parent.payload["orb_country_as_of"]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)

    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: at)
    monkeypatch.setattr("project_mai_tai.oms.orb_country.datetime", Clock)
    store = CountryStore(factory)
    stamp = OPEN - timedelta(hours=1)
    store.put(replace(store.load()["ABC"], as_of=stamp))
    selection = replace(selection, country=replace(selection.country, as_of=stamp))
    replacements = []

    async def sdk_replace(request, old_id):
        replacements.append((request, old_id))
        if sdk_outcome == "unknown":
            return None
        return ExecutionReport("accepted" if sdk_outcome == "confirmed" else "rejected",
            request.client_order_id, broker_order_id="replacement-2", symbol=request.symbol,
            side=request.side, intent_type=request.intent_type, quantity=request.quantity,
            metadata={**request.metadata, "orb_replace_confirmation": "confirmed"},
            reported_at=at, origin="broker")

    # Only the external adapter boundary is controlled; real OMS preview parsing,
    # quote selection, prewire, target lookup and durable receipt handlers run.
    broker.replace_bracket_order = sdk_replace
    event = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=selection)
    asyncio.run(service.process_trade_intent(event))
    assert len(replacements) == 1
    request, old_id = replacements[0]
    assert old_id == parent_native
    assert (request.client_order_id, request.broker_account_name, request.quantity) == (parent_coid, SCHWAB, Decimal(10))
    assert request.prewire_check is not None
    assert len(broker.previews) == 2  # original plus actual replacement preview
    assert len(broker.sent) == 1  # replace is not another submit_order OPEN
    with factory() as session:
        rows = session.scalars(select(BrokerOrder)).all()
        assert len(rows) == 1 and rows[0].id == parent_id
        assert rows[0].payload["orb_country_as_of"] == parent_stamp
        if sdk_outcome == "confirmed":
            assert rows[0].broker_order_id == "replacement-2"
            assert "orb_replace_hold" not in rows[0].payload
        elif sdk_outcome == "unknown":
            assert rows[0].broker_order_id == parent_native
            assert rows[0].payload["orb_replace_hold"]["old_id"] == parent_native
        else:
            assert rows[0].broker_order_id == parent_native
    if sdk_outcome == "unknown":
        # Same original event cannot become an unproved second SDK replacement.
        asyncio.run(service.process_trade_intent(event))
        assert len(replacements) == 1
