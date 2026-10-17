"""Source-authored query equivalence controls; no PostgreSQL cost claim. UNRUN."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import event, or_, select
from sqlalchemy.orm import aliased

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, Strategy, VirtualPosition
from project_mai_tai.oms.orb_protection_work import load_protection_work
from tests.unit.test_control_plane import build_test_session_factory


NOW = datetime(2026, 10, 16, 14, tzinfo=UTC)
ACCOUNT = "live:orb"


def original_query():
    """Literal db73 work predicate retained as an independent differential oracle."""
    newer = aliased(BrokerOrder)
    superseded = select(newer.id).where(
        newer.strategy_id == BrokerOrder.strategy_id,
        newer.broker_account_id == BrokerOrder.broker_account_id,
        newer.symbol == BrokerOrder.symbol, newer.side == "buy",
        or_(newer.submitted_at > BrokerOrder.submitted_at,
            (newer.submitted_at == BrokerOrder.submitted_at) & (newer.id > BrokerOrder.id)),
    ).exists()
    has_buy_fill = select(Fill.id).where(
        Fill.order_id == BrokerOrder.id, Fill.side == "buy", Fill.quantity > 0,
    ).exists()
    return select(BrokerOrder.id).join(Strategy).join(
        BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id,
    ).outerjoin(VirtualPosition, (
        (VirtualPosition.strategy_id == BrokerOrder.strategy_id)
        & (VirtualPosition.broker_account_id == BrokerOrder.broker_account_id)
        & (VirtualPosition.symbol == BrokerOrder.symbol)
    )).where(
        Strategy.code == "orb_schwab", BrokerAccount.name == ACCOUNT,
        BrokerOrder.side == "buy", or_(
            (~superseded) & or_(VirtualPosition.quantity > 0,
                VirtualPosition.quantity.is_(None) & has_buy_fill),
            BrokerOrder.payload["orbroute1_protection"]["phase"].as_string().in_(
                {"submitting", "accepted", "working", "unknown"})),
        BrokerOrder.payload["orb_country_route"].as_string() == "true",
    )


@pytest.fixture
def rows():
    factory = build_test_session_factory()
    with factory.begin() as session:
        session.add(Strategy(id=UUID(int=100), code="orb_schwab", name="CONTROLLED ORB", execution_mode="live"))
        session.add(BrokerAccount(id=UUID(int=200), name=ACCOUNT, provider="webull", environment="production"))
    yield factory
    factory.kw["bind"].dispose()


def buy(session, number, *, routed=True, stamp=NOW, phase=None, symbol="ABC", side="buy"):
    payload = {"orb_country_route": "true" if routed else "false"}
    if phase is not None:
        payload["orbroute1_protection"] = {"phase": phase}
    row = BrokerOrder(id=UUID(int=number), strategy_id=UUID(int=100), broker_account_id=UUID(int=200),
        client_order_id=f"CONTROLLED-{number}", broker_order_id=f"native-{number}", symbol=symbol,
        side=side, quantity=Decimal(10), order_type="limit", time_in_force="day", status="filled",
        submitted_at=stamp, payload=payload)
    session.add(row)
    return row


def position(session, quantity, *, symbol="ABC"):
    session.add(VirtualPosition(strategy_id=UUID(int=100), broker_account_id=UUID(int=200),
        symbol=symbol, quantity=Decimal(quantity), average_price=Decimal(10)))


def fill(session, row, number):
    session.add(Fill(order_id=row.id, strategy_id=row.strategy_id, broker_account_id=row.broker_account_id,
        broker_fill_id=f"fill-{number}", symbol=row.symbol, side="buy", quantity=Decimal(1), price=Decimal(10)))


def same_work(factory, expected):
    with factory() as session:
        old = set(session.scalars(original_query()))
        actual = load_protection_work(session, ACCOUNT)
        assert set(actual) == old == {UUID(int=number) for number in expected}
        assert len(actual) == len(set(actual))


@pytest.mark.parametrize("relation,expected", [
    ("newer_unrouted", set()), ("older_unrouted", {1}),
    ("same_time_larger_uuid", set()), ("newer_sell", {1}),
    ("other_symbol", {1}), ("two_nulls", {1, 2}), ("null_candidate", {1}),
])
def test_latest_buy_universe_and_null_comparisons_match_db73(rows, relation, expected):
    with rows.begin() as session:
        null = relation in {"two_nulls", "null_candidate"}
        buy(session, 1, stamp=None if null else NOW)
        buy(session, 2, routed=relation == "two_nulls",
            stamp=None if relation == "two_nulls" else NOW if relation == "same_time_larger_uuid"
            else NOW - timedelta(seconds=1) if relation == "older_unrouted" else NOW + timedelta(seconds=1),
            side="sell" if relation == "newer_sell" else "buy",
            symbol="OTHER" if relation == "other_symbol" else "ABC")
        position(session, 10)
    same_work(rows, expected)


@pytest.mark.parametrize("phase", ["submitting", "accepted", "working", "unknown"])
def test_previous_day_superseded_unknown_pair_with_zero_vp_is_not_dropped(rows, phase):
    with rows.begin() as session:
        buy(session, 1, stamp=NOW - timedelta(days=2), phase=phase)
        buy(session, 2, routed=False, stamp=NOW - timedelta(days=1))
        position(session, 0)
    same_work(rows, {1})


@pytest.mark.parametrize("scope", ["account", "strategy"])
def test_other_account_or_strategy_cannot_supersede_our_entry(rows, scope):
    with rows.begin() as session:
        buy(session, 1)
        newer = buy(session, 2, stamp=NOW + timedelta(seconds=1))
        if scope == "account":
            session.add(BrokerAccount(id=UUID(int=201), name="live:OTHER", provider="webull", environment="production"))
            newer.broker_account_id = UUID(int=201)
        else:
            session.add(Strategy(id=UUID(int=101), code="OTHER", name="CONTROLLED OTHER", execution_mode="live"))
            newer.strategy_id = UUID(int=101)
        position(session, 10)
    same_work(rows, {1})


@pytest.mark.parametrize("vp,fill_count,phase,expected", [
    (10, 0, None, {1}), (None, 2, None, {1}), (None, 0, None, set()),
    (0, 2, None, set()), (0, 2, "released", set()), (0, 2, "settled", set()),
])
def test_older_owned_and_missing_vp_fill_fallback_match_db73(rows, vp, fill_count, phase, expected):
    with rows.begin() as session:
        row = buy(session, 1, stamp=NOW - timedelta(days=2), phase=phase)
        session.flush()
        for number in range(fill_count):
            fill(session, row, number)
        if vp is not None:
            position(session, vp)
    same_work(rows, expected)


@pytest.mark.parametrize("state", ["empty", "unrouted", "settled_zero", "released_zero"])
def test_idle_guard_skips_window_and_fill_query(rows, state):
    if state != "empty":
        with rows.begin() as session:
            buy(session, 1, routed=state != "unrouted",
                phase="settled" if state == "settled_zero" else "released")
            position(session, 10 if state == "unrouted" else 0)
    statements = []
    engine = rows.kw["bind"]
    def note(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", note)
    try:
        with rows() as session:
            assert load_protection_work(session, ACCOUNT) == []
    finally:
        event.remove(engine, "before_cursor_execute", note)
    assert len(statements) == 1
    assert "row_number" not in statements[0].lower() and "fills" not in statements[0].lower()
