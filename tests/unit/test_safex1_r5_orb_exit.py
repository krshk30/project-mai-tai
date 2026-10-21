"""SAFEX1 R5 controlled broker replies; not a native execution replay.

The real close coordinator, DB ownership reread, prepare, accounting and incident
writer run unchanged. No tests in this module have been executed by the author.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, Fill, SystemIncident, VirtualPosition
from project_mai_tai.oms import orb_schwab_eod as owner
from project_mai_tai.orb_schwab_exits import BODY_REASON
from tests.unit.test_orbroute1_oms_chain import setup as route_setup, seed_filled_webull
from tests.unit.test_ownmix2_exit_caps import orb_case, later_buy


def close_case(monkeypatch, route_setup, provider):
    if provider == "schwab":
        return orb_case(monkeypatch, raw="9", after_release="9")
    service, factory, broker = route_setup
    entry_id, filled_at = seed_filled_webull(route_setup, monkeypatch)
    broker.positions = [BrokerPositionSnapshot(
        service.settings.orb_webull_account_name, "ABC", Decimal(9), Decimal("5.53"), as_of=filled_at)]
    broker.positions_wire_read_age_seconds = lambda _account: 0.0
    original_read, original_submit = broker.fetch_order_update, broker.submit_order

    async def parent(request):
        report = await original_read(request)
        if request.side == "buy":
            return replace(report, event_type="filled", filled_quantity=Decimal(10),
                           reported_at=filled_at)
        return report

    async def submit(request):
        if request.intent_type != "close":
            return await original_submit(request)
        broker.sent.append(request)
        return [ExecutionReport("filled", request.client_order_id,
            broker_order_id="controlled-r5-close", broker_fill_id="controlled-r5-fill",
            symbol=request.symbol, side="sell", intent_type="close", quantity=request.quantity,
            filled_quantity=request.quantity, fill_price=Decimal("5.6"), origin="broker",
            reported_at=filled_at, metadata=dict(request.metadata))]

    broker.fetch_order_update, broker.submit_order = parent, submit
    return SimpleNamespace(service=service, factory=factory, broker=broker, entry_id=entry_id,
                           clock=[filled_at])


def run_close(case):
    # Enter at the shared coordinator boundary after the existing signal claim.
    # The native protection state stays untouched; Webull's empty state means
    # no pair was posted, not fabricated terminal cancellation evidence.
    with case.factory.begin() as session:
        entry = session.get(BrokerOrder, case.entry_id)
        entry.payload = {**entry.payload, owner._STATE: {"phase": "checking"}}
        from project_mai_tai.db.models import BrokerAccount
        account = session.get(BrokerAccount, entry.broker_account_id)
        target = owner._Target(entry.id, entry.strategy_id, entry.broker_account_id, account.name,
            entry.symbol, entry.broker_order_id, entry.client_order_id, Decimal(10), Decimal(10), True)
    return asyncio.run(owner._attempt(case.service, target, uuid4(), lambda: case.clock[0],
                                      reason=BODY_REASON, start=owner.time(9, 30)))


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_non_eod_underbacking_sells_only_broker_quantity_and_pages(monkeypatch, route_setup, provider):
    case = close_case(monkeypatch, route_setup, provider)
    try:
        assert run_close(case) is True
        requests = case.broker.submitted if provider == "schwab" else case.broker.sent
        closes = [request for request in requests if request.intent_type == "close"]
        assert len(closes) == 1 and closes[0].quantity == 9
        assert closes[0].reason == BODY_REASON
        assert closes[0].metadata["entry_order_id"] == str(case.entry_id)
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 1  # Never infer flat.
            incident, = session.scalars(select(SystemIncident)).all()
            assert incident.severity == "critical" and incident.status == "open"
            assert incident.payload["reason"] == "orb_close_broker_below_own"
            fill, = session.scalars(select(Fill).where(Fill.side == "sell")).all()
            assert fill.quantity == 9
    finally:
        if provider == "schwab":
            case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_underbacked_close_still_rejects_ownership_change_before_prepare(monkeypatch, route_setup, provider):
    case = close_case(monkeypatch, route_setup, provider)
    original_db = case.service._run_db

    async def change_before_prepare(work, **kwargs):
        if work.__name__ == "prepare":
            with case.factory.begin() as session:
                session.scalar(select(VirtualPosition)).quantity = 8
        return await original_db(work, **kwargs)

    case.service._run_db = change_before_prepare
    try:
        with pytest.raises(ValueError, match="owned_balance_changed_before_close|owned_balance_unproven"):
            run_close(case)
        requests = case.broker.submitted if provider == "schwab" else case.broker.sent
        assert not [request for request in requests if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 8
            assert not session.scalars(select(Fill).where(Fill.side == "sell")).all()
    finally:
        if provider == "schwab":
            case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("value", ["not-a-number", "NaN", "Infinity", None])
def test_malformed_other_buy_event_uses_existing_eod_legacy_fallback(monkeypatch, value):
    case = orb_case(monkeypatch, raw="9", after_release="9")
    try:
        order_id = later_buy(case, status="cancelled", submitted_mode="newer")
        with case.factory.begin() as session:
            event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order_id))
            event.payload = {**event.payload, "filled_quantity": value}
        asyncio.run(case.service._orb_schwab_eod_close())
        closes = [request for request in case.broker.submitted if request.intent_type == "close"]
        assert len(closes) == 1 and closes[0].quantity == 9
        assert closes[0].reason == "ORB_END_OF_DAY_CLOSE"
        with case.factory() as session:
            incident, = session.scalars(select(SystemIncident)).all()
            assert "orb_close_entry_fill_UNKNOWN" in incident.payload["reason"]
            assert session.scalar(select(VirtualPosition.quantity)) == 1
            assert not session.scalars(select(Fill).where(Fill.order_id == order_id)).all()
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("conflict", ["actual-fill", "finite-nonzero", "other-filled-order", "identity"])
def test_unknown_event_never_bypasses_actual_fill_or_identity_conflict(monkeypatch, conflict):
    case = orb_case(monkeypatch)
    try:
        order_id = later_buy(case, status="cancelled", submitted_mode="newer",
                             fill_quantity=1 if conflict == "actual-fill" else None)
        with case.factory.begin() as session:
            event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order_id))
            event.payload = {**event.payload, "filled_quantity": "bad"}
            if conflict == "finite-nonzero":
                session.add(BrokerOrderEvent(order_id=order_id, event_type="cancelled",
                    event_source="broker", event_at=case.clock[0], payload={"filled_quantity": "1"}))
            if conflict == "other-filled-order":
                other = session.get(BrokerOrder, order_id)
                session.add(BrokerOrder(intent_id=other.intent_id, strategy_id=other.strategy_id,
                    broker_account_id=other.broker_account_id, client_order_id="controlled-r5-second-lot",
                    broker_order_id="controlled-r5-second-native", symbol=other.symbol, side="buy",
                    quantity=1, status="filled", order_type="limit", time_in_force="day",
                    submitted_at=other.submitted_at, payload=dict(other.payload)))
        if conflict == "identity":
            original_positions = case.broker.list_account_positions

            async def changed_identity(account):
                with case.factory.begin() as session:
                    session.get(BrokerOrder, case.entry_id).broker_order_id = "controlled-changed-native"
                return await original_positions(account)

            case.broker.list_account_positions = changed_identity
        asyncio.run(case.service._orb_schwab_eod_close())
        assert not [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 10
            assert session.scalar(select(SystemIncident)) is not None
    finally:
        case.factory.kw["bind"].dispose()
