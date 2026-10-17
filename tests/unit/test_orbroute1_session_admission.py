"""07:0x: expire admission-only rows, never historical protection ownership."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import logging
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition, TradeIntent, VirtualPosition
from project_mai_tai.fanout_segment_store import current_session_anchor
from project_mai_tai.oms import orb_country, service as oms_module
from project_mai_tai.oms.orb_country import PROTECTION, orb_route_owner_conflict
from project_mai_tai.orb_schwab_order_route import build_orb_schwab_open_intent, build_orb_schwab_reprice_intent
from tests.unit.test_orbroute1_oms_chain import OPEN, SCHWAB, WEBULL, selected, setup as setup


SESSIONS = [(13, 28), (14, 0)]  # PM NORMAL staging09:28 and RTH10:00; no PM-entry widening.
# Preserve the original valid 13-28 case IDs; the second positive is just inside
# the unchanged staging window, not a claimed 10:00 ET admission opportunity.
ENTRY_TIMES = [pytest.param(13, 28, 0, id="13-28"), pytest.param(13, 28, 14, id="13-28-14")]
KINDS = ["order", "submitting", "accepted", "working", "unknown"]


def controlled_time(monkeypatch, hour, minute, second=0):
    now = OPEN.replace(hour=hour, minute=minute, second=second)
    monkeypatch.setattr(oms_module, "utcnow", lambda: now)
    return now


def foreign_row(service, factory, account_name, now, age, kind, *, mismatch=False, null_submission=False):
    stamp = current_session_anchor(now) - timedelta(seconds=1) if age == "previous" else now - timedelta(minutes=1)
    with factory.begin() as session:
        account = service.store.ensure_broker_account(session, account_name,
            provider="schwab" if account_name == SCHWAB else "webull", environment="production")
        incoming = service.store.ensure_strategy(session, "orb_schwab", name="ORB", execution_mode="live")
        foreign = service.store.ensure_strategy(session, "momentum_warn_live", name="MOM", execution_mode="live")
        parent = None
        if age != "unanchored":
            parent = TradeIntent(strategy_id=incoming.id if mismatch else foreign.id,
                broker_account_id=account.id, symbol="ABC", side="buy", intent_type="open",
                quantity=Decimal(10), status="filled", reason="CONTROLLED_FOREIGN_HISTORY",
                created_at=stamp, updated_at=now)
            session.add(parent)
            session.flush()
        coid = f"foreign-{uuid4().hex}"
        row = BrokerOrder(strategy_id=foreign.id, broker_account_id=account.id,
            intent_id=parent.id if parent is not None else None, client_order_id=coid,
            broker_order_id=f"native-{uuid4().hex}", symbol="ABC", side="buy", order_type="limit",
            time_in_force="day", quantity=Decimal(10), status="unknown" if kind == "order" else "filled",
            submitted_at=None if age == "unanchored" or null_submission else stamp,
            updated_at=now, payload={} if kind == "order" else {PROTECTION: {"phase": kind}})
        session.add(row)
        session.flush()
        return account.id, incoming.id, foreign.id, row.id, coid


@pytest.mark.parametrize("account", [SCHWAB, WEBULL])
@pytest.mark.parametrize("hour,minute", SESSIONS)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("age", ["previous", "same", "unanchored"])
def test_admission_age_rules_do_not_change_historical_fences(
    setup, monkeypatch, account, hour, minute, kind, age, caplog,
):
    service, factory, broker = setup
    now = controlled_time(monkeypatch, hour, minute)
    account_id, incoming_id, _, row_id, coid = foreign_row(service, factory, account, now, age, kind)
    with factory() as session, caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
        result = orb_route_owner_conflict(session, account_id, account, "ABC", incoming_id, admission=True)
        assert (result is None) is (age == "previous")
        # Identical old row still protects historical reads/release/recovery.
        assert orb_route_owner_conflict(session, account_id, account, "ABC", incoming_id) == ("momentum_warn_live", coid)
        row = session.get(BrokerOrder, row_id)
        assert row.status == ("unknown" if kind == "order" else "filled")
        assert row.payload == ({} if kind == "order" else {PROTECTION: {"phase": kind}})
    if age == "previous":
        assert "[ROUTE-OWNER-STALE-ROW]" in caplog.text
    elif age == "unanchored":
        warnings = [record for record in caplog.records if "[ROUTE-OWNER-UNANCHORED-ROW]" in record.getMessage()]
        assert warnings and all(record.levelno == logging.WARNING for record in warnings)
        assert all(f"row_id={row_id}" in record.getMessage() for record in warnings)
    else:
        assert "[ROUTE-OWNER-STALE-ROW]" not in caplog.text
    assert not broker.sent and not broker.cancels


@pytest.mark.parametrize("account", [SCHWAB, WEBULL])
@pytest.mark.parametrize("mismatch", [False, True])
def test_protection_null_submission_requires_exact_bound_parent(setup, monkeypatch, account, mismatch, caplog):
    service, factory, _ = setup
    now = controlled_time(monkeypatch, 14, 0)
    account_id, incoming_id, _, _, _ = foreign_row(service, factory, account, now, "previous", "unknown",
        mismatch=mismatch, null_submission=True)
    with factory() as session, caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
        result = orb_route_owner_conflict(session, account_id, account, "ABC", incoming_id, admission=True)
    assert (result is not None) is mismatch
    assert ("[ROUTE-OWNER-UNANCHORED-ROW]" in caplog.text) is mismatch


@pytest.mark.parametrize("account", [SCHWAB, WEBULL])
def test_protection_wire_timestamp_is_primary_not_max_parent(setup, monkeypatch, account):
    service, factory, _ = setup
    now = controlled_time(monkeypatch, 14, 0)
    account_id, incoming_id, _, row_id, _ = foreign_row(service, factory, account, now, "previous", "accepted")
    with factory.begin() as session:
        row = session.get(BrokerOrder, row_id)
        session.get(TradeIntent, row.intent_id).created_at = now
    with factory() as session:
        assert orb_route_owner_conflict(session, account_id, account, "ABC", incoming_id, admission=True) is None


@pytest.mark.parametrize("account", [SCHWAB, WEBULL])
def test_old_first_protection_cannot_hide_later_current_protection(setup, monkeypatch, account):
    service, factory, _ = setup
    now = controlled_time(monkeypatch, 14, 0)
    account_id, incoming_id, _, _, _ = foreign_row(service, factory, account, now, "previous", "unknown")
    foreign_row(service, factory, account, now, "same", "working")
    with factory() as session:
        assert orb_route_owner_conflict(session, account_id, account, "ABC", incoming_id, admission=True) is not None


@pytest.mark.parametrize("account", [SCHWAB, WEBULL])
@pytest.mark.parametrize("field", ["account", "symbol"])
def test_null_submission_cannot_borrow_different_account_or_symbol_parent(setup, monkeypatch, account, field, caplog):
    service, factory, _ = setup
    now = controlled_time(monkeypatch, 14, 0)
    account_id, incoming_id, _, row_id, _ = foreign_row(service, factory, account, now, "previous", "unknown",
        null_submission=True)
    with factory.begin() as session:
        parent = session.get(TradeIntent, session.get(BrokerOrder, row_id).intent_id)
        if field == "symbol":
            parent.symbol = "OTHER"
        else:
            other = service.store.ensure_broker_account(session, "live:other", provider="webull", environment="production")
            parent.broker_account_id = other.id
    with factory() as session, caplog.at_level(logging.WARNING, logger="project_mai_tai.oms.route_owner"):
        assert orb_route_owner_conflict(session, account_id, account, "ABC", incoming_id, admission=True) is not None
    assert "[ROUTE-OWNER-UNANCHORED-ROW]" in caplog.text


@pytest.mark.parametrize("account", [SCHWAB, WEBULL])
@pytest.mark.parametrize("proof", ["vp", "managed"])
def test_admission_stale_order_never_exempts_held_or_managed_owner(setup, monkeypatch, account, proof):
    service, factory, _ = setup
    now = controlled_time(monkeypatch, 14, 0)
    account_id, incoming_id, foreign_id, _, _ = foreign_row(service, factory, account, now, "previous", "order")
    with factory.begin() as session:
        if proof == "vp":
            session.add(VirtualPosition(strategy_id=foreign_id, broker_account_id=account_id,
                symbol="ABC", quantity=Decimal(3), average_price=Decimal(5)))
        else:
            session.add(OmsManagedPosition(strategy_code="momentum_warn_live", broker_account_name=account,
                symbol="ABC", entry_price=Decimal(5), original_quantity=3, current_quantity=3,
                status="open", created_at=now - timedelta(days=1)))
    with factory() as session:
        assert orb_route_owner_conflict(session, account_id, account, "ABC", incoming_id, admission=True) is not None


@pytest.mark.parametrize("account,is_us", [(SCHWAB, True), (WEBULL, False)])
@pytest.mark.parametrize("hour,minute,second", ENTRY_TIMES)
@pytest.mark.parametrize("age", ["previous", "same", "unanchored"])
@pytest.mark.parametrize("kind", ["order", "unknown"])
def test_actual_oms_entry_uses_session_aware_wrapper(
    setup, monkeypatch, account, is_us, hour, minute, second, age, kind, caplog,
):
    service, factory, broker = setup
    now = controlled_time(monkeypatch, hour, minute, second)
    selection = selected(factory, service.settings, is_us)
    _, _, _, row_id, _ = foreign_row(service, factory, account, now, age, kind)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    with caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
        asyncio.run(service.process_trade_intent(event))
    assert len(broker.sent) == (1 if age == "previous" else 0)
    if broker.sent:
        assert broker.sent[0].broker_account_name == account and broker.sent[0].quantity == 10
        assert broker.sent[0].side == "buy" and broker.sent[0].intent_type == "open"
        assert "[ROUTE-OWNER-STALE-ROW]" in caplog.text
    if age == "unanchored":
        assert "[ROUTE-OWNER-UNANCHORED-ROW]" in caplog.text
    with factory() as session:
        assert session.get(BrokerOrder, row_id).status == ("unknown" if kind == "order" else "filled")


@pytest.mark.parametrize("hour,minute", [(13, 29), (13, 30)])
@pytest.mark.parametrize("age", ["previous", "same", "unanchored"])
def test_actual_webull_reprice_rechecks_age_after_cancel_without_retry(setup, monkeypatch, hour, minute, age, caplog):
    service, factory, broker = setup
    controlled_time(monkeypatch, 13, 28)
    selection = selected(factory, service.settings, False)
    original = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    asyncio.run(service.process_trade_intent(original))
    now = controlled_time(monkeypatch, hour, minute)
    broker.readback = AtrBuyReadback("cancelled_empty", "terminal_cancel_explicit_zero", Decimal(0),
        terminal_cancel=True, broker_status="CANCELLED", broker_order_id="parent-1")
    submit = broker.submit_order
    inserted = []

    async def insert_foreign_during_cancel(request):
        result = await submit(request)
        if request.intent_type == "cancel" and not inserted:
            inserted.append(foreign_row(service, factory, WEBULL, now, age, "unknown")[3])
        return result

    monkeypatch.setattr(broker, "submit_order", insert_foreign_during_cancel)
    reprice = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=selection)
    with caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
        asyncio.run(service.process_trade_intent(reprice))
        asyncio.run(service.process_trade_intent(reprice))
    buys = [request for request in broker.sent if request.intent_type == "open"]
    cancels = [request for request in broker.sent if request.intent_type == "cancel"]
    assert len(inserted) == len(cancels) == 1
    assert len(buys) == (2 if age == "previous" else 1)
    assert all(request.broker_account_name == WEBULL and request.quantity == 10 for request in broker.sent)
    if age == "previous":
        assert buys[-1].client_order_id == f"orb-r-{reprice.event_id.hex[:24]}"
        assert "[ROUTE-OWNER-STALE-ROW]" in caplog.text
    elif age == "unanchored":
        assert "[ROUTE-OWNER-UNANCHORED-ROW]" in caplog.text
    with factory() as session:
        assert session.get(BrokerOrder, inserted[0]).payload[PROTECTION]["phase"] == "unknown"


@pytest.mark.parametrize("account,is_us", [(SCHWAB, True), (WEBULL, False)])
@pytest.mark.parametrize("age", ["previous", "same", "unanchored"])
@pytest.mark.parametrize("kind", ["order", "unknown"])
def test_actual_oms_new_entry_at_ten_remains_window_refused_before_owner(
    setup, monkeypatch, account, is_us, age, kind, caplog,
):
    service, factory, broker = setup
    now = controlled_time(monkeypatch, 14, 0)  # 10:00 ET is NOT a new-entry admission time.
    selection = selected(factory, service.settings, is_us)
    _, _, _, row_id, _ = foreign_row(service, factory, account, now, age, kind)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    with caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
        results = asyncio.run(service.process_trade_intent(event))
    assert results == []  # This early refusal returns before intent/order persistence.
    refusals = [record.getMessage() for record in caplog.records
        if "[OMS-ORB-SCHWAB-REFUSED]" in record.getMessage()]
    assert len(refusals) == 1 and "reason=orb_country_entry_window_closed" in refusals[0]
    assert f"account={account}" in refusals[0] and "symbol=ABC" in refusals[0]
    assert not broker.sent and not broker.previews and not broker.cancels
    assert "[ROUTE-OWNER-STALE-ROW]" not in caplog.text
    assert "[ROUTE-OWNER-UNANCHORED-ROW]" not in caplog.text
    with factory() as session:
        row = session.get(BrokerOrder, row_id)
        assert row.status == ("unknown" if kind == "order" else "filled")
        assert row.payload == ({} if kind == "order" else {PROTECTION: {"phase": kind}})


@pytest.mark.parametrize("age", ["previous", "same", "unanchored"])
def test_actual_webull_reprice_at_ten_never_cancels_or_replaces_original(setup, monkeypatch, age, caplog):
    service, factory, broker = setup
    controlled_time(monkeypatch, 13, 28)
    selection = selected(factory, service.settings, False)
    original = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    asyncio.run(service.process_trade_intent(original))
    assert len(broker.sent) == 1
    original_coid = broker.sent[0].client_order_id
    now = controlled_time(monkeypatch, 14, 0)
    _, _, _, row_id, _ = foreign_row(service, factory, WEBULL, now, age, "unknown")
    broker.readback = AtrBuyReadback("cancelled_empty", "terminal_cancel_explicit_zero", Decimal(0),
        terminal_cancel=True, broker_status="CANCELLED", broker_order_id="parent-1")
    reprice = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=selection)
    with caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
        first = asyncio.run(service.process_trade_intent(reprice))
        repeated = asyncio.run(service.process_trade_intent(reprice))
    for results in (first, repeated):
        assert results == []
    refusals = [record.getMessage() for record in caplog.records
        if "[OMS-ORB-SCHWAB-REFUSED]" in record.getMessage()]
    assert len(refusals) == 2
    assert all("reason=orb_country_entry_window_closed" in message
        and f"account={WEBULL}" in message and "symbol=ABC" in message for message in refusals)
    assert len(broker.sent) == 1 and broker.sent[0].client_order_id == original_coid
    assert not broker.cancels and not broker.previews
    assert "[ROUTE-OWNER-STALE-ROW]" not in caplog.text
    assert "[ROUTE-OWNER-UNANCHORED-ROW]" not in caplog.text
    with factory() as session:
        original_row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == original_coid))
        assert original_row is not None
        assert original_row.status == "accepted" and "orb_replace_hold" not in original_row.payload
        assert session.get(BrokerOrder, row_id).payload[PROTECTION]["phase"] == "unknown"


def test_wrapper_captures_one_clock_for_shared_and_fallback_checks(setup, monkeypatch):
    service, factory, _ = setup
    now = controlled_time(monkeypatch, 14, 0)
    account_id, incoming_id, _, _, _ = foreign_row(service, factory, WEBULL, now, "previous", "unknown")
    samples = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            samples.append(1)
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(orb_country, "datetime", Clock)
    with factory() as session:
        assert orb_route_owner_conflict(session, account_id, WEBULL, "ABC", incoming_id, admission=True) is None
    assert samples == [1]


def test_historical_path_never_samples_admission_age_clock(setup, monkeypatch):
    service, factory, _ = setup
    now = controlled_time(monkeypatch, 14, 0)
    account_id, incoming_id, _, _, coid = foreign_row(service, factory, WEBULL, now, "previous", "unknown")

    class ForbiddenClock:
        @classmethod
        def now(cls, tz=None):
            pytest.fail("historical ownership must not consult admission age")

    monkeypatch.setattr(orb_country, "datetime", ForbiddenClock)
    with factory() as session:
        assert orb_route_owner_conflict(session, account_id, WEBULL, "ABC", incoming_id) == ("momentum_warn_live", coid)
