"""Core drift reads preserve frozen policy and transaction visibility."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Strategy, TradeIntent
from tests.unit.test_drift_cache_projection_equivalence import (
    compare,
    lane as lane,
    seed_order,
)


@pytest.mark.parametrize("symbol", [None, "DKI"])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0, "bid": 2.0}])
def test_empty_book_uses_one_core_statement(lane, symbol, quote):
    service, factory, _ids = lane
    statements = []

    def capture(state):
        statements.append(state.is_orm_statement)

    with factory() as session:
        event.listen(session, "do_orm_execute", capture)
        assert service._collect_drift_cancel_candidates(session, symbol, quote, 0.01) == []
        assert not session.identity_map
    assert statements == [False]


@pytest.mark.parametrize("eligible", [False, True])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0, "bid": 2.0}])
def test_core_cursor_reads_skip_orm_setup(lane, monkeypatch, eligible, quote):
    service, factory, _ids = lane
    order_id = seed_order(lane, symbol="DKI") if eligible else None
    orm_statements = []

    def capture(state):
        if state.is_orm_statement:
            orm_statements.append(state.statement)

    with factory() as session:
        event.listen(session, "do_orm_execute", capture)
        # Positive control: the former ORM column projection must trip this hook.
        session.execute(select(BrokerOrder.id)).all()
        assert len(orm_statements) == 1
        orm_statements.clear()
        candidates = service._collect_drift_cancel_candidates(session, "DKI", quote, 0.01)
        assert {candidate.order_id for candidate in candidates} == ({order_id} if eligible else set())
        assert not session.identity_map
    # The unchanged per-order terminal-evidence read remains ORM-enabled.
    assert len(orm_statements) == int(eligible)
    assert all(BrokerOrderEvent.__table__.name in str(statement) for statement in orm_statements)


@pytest.mark.parametrize("quote", [None, {"ask": 3.0, "bid": 2.0}])
@pytest.mark.parametrize("changes,expected", [
    ({"symbol": "DKI"}, True),
    ({"symbol": "FLYE", "side": "sell"}, True),
    ({"symbol": "VEEA", "intent_type": "OPEN"}, True),
    ({"account": "inactive"}, False),
    ({"intent_type": "close"}, False),
    ({"intent_type": "scale"}, False),
    ({"status": "filled"}, False),
    ({"missing_intent": True}, False),
    ({"payload": {"order_type": "limit", "limit_price": "2.55",
                  "stop_guard": " TRUE "}}, False),
    ({"payload": {"order_type": "market", "limit_price": "2.55"}}, False),
    ({"column_type": "market"}, True),
])
def test_actual_order_scope_matches_frozen_orm_control(lane, quote, changes, expected):
    order_id = seed_order(lane, **changes)
    actual, _loaded = compare(lane, quote=quote, tolerance=0.01)
    assert {candidate.order_id for candidate in actual} == ({order_id} if expected else set())


@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("change", [
    "order_status", "intent_type", "account_active", "account_name",
    "strategy_code", "quantity", "metadata",
])
def test_dirty_columns_follow_autoflush_not_identity_map(lane, autoflush, change):
    service, factory, ids = lane
    order_id = seed_order(lane, symbol="DKI")
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        intent = session.get(TradeIntent, order.intent_id)
        account = session.get(BrokerAccount, ids["active"])
        strategy = session.get(Strategy, ids["strategy"])
        if change == "order_status":
            order.status = "filled"
        elif change == "intent_type":
            intent.intent_type = "close"
        elif change == "account_active":
            account.is_active = False
        elif change == "account_name":
            account.name = "changed-account"
        elif change == "strategy_code":
            strategy.code = "changed-strategy"
        elif change == "quantity":
            order.quantity = Decimal("9.25")
        else:
            order.payload = {"order_type": "limit", "limit_price": "2.75",
                             "retained": "exact"}
        session.autoflush = autoflush
        candidates = service._collect_drift_cancel_candidates(session, "DKI", None, 0)
        excluded = autoflush and change in {"order_status", "intent_type", "account_active"}
        assert len(candidates) == (0 if excluded else 1)
        if candidates:
            candidate = candidates[0]
            assert candidate.order_id == order_id
            assert candidate.broker_account_name == (
                "changed-account" if autoflush and change == "account_name" else "active")
            assert candidate.strategy_code == (
                "changed-strategy" if autoflush and change == "strategy_code" else "schwab_1m_v2")
            assert candidate.quantity == (
                Decimal("9.25") if autoflush and change == "quantity" else Decimal("3.5"))
            assert candidate.limit_price == (
                "2.75" if autoflush and change == "metadata" else "2.55")
            assert (candidate.existing_metadata.get("retained") == "exact") is (
                autoflush and change == "metadata")
        assert bool(session.dirty) is (not autoflush)
        session.rollback()


@pytest.mark.parametrize("autoflush", [False, True])
def test_unflushed_order_with_uncommitted_parent_visibility(lane, autoflush):
    service, factory, ids = lane
    intent_id, order_id = uuid4(), uuid4()
    with factory() as session:
        intent = TradeIntent(id=intent_id, strategy_id=ids["strategy"],
            broker_account_id=ids["active"], symbol="DKI", side="buy",
            intent_type="open", quantity=Decimal("3.5"), reason="CONTROL",
            status="submitted", payload={}, created_at=datetime(2026, 10, 8, 11, 45, tzinfo=UTC))
        order = BrokerOrder(id=order_id, intent_id=intent_id, strategy_id=ids["strategy"],
            broker_account_id=ids["active"], client_order_id=str(uuid4()),
            broker_order_id="pending-control", symbol="DKI", side="buy", quantity=Decimal("3.5"),
            order_type="limit", time_in_force="day", status="accepted",
            payload={"order_type": "limit", "limit_price": "2.55"})
        # Production creates and flushes the parent intent before adding its order;
        # the FK is immediate on PostgreSQL, with no ORM relationship for sorting.
        session.add(intent)
        session.flush([intent])
        session.add(order)
        session.autoflush = autoflush
        candidates = service._collect_drift_cancel_candidates(session, "DKI", None, 0)
        assert {candidate.order_id for candidate in candidates} == ({order_id} if autoflush else set())
        assert bool(session.new) is (not autoflush)
        session.rollback()
    with factory() as session:
        assert service._collect_drift_cancel_candidates(session, "DKI", None, 0) == []


@pytest.mark.parametrize("autoflush", [False, True])
def test_pending_delete_visibility(lane, autoflush):
    service, factory, _ids = lane
    order_id = seed_order(lane, symbol="DKI")
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        session.delete(order)
        session.autoflush = autoflush
        candidates = service._collect_drift_cancel_candidates(session, "DKI", None, 0)
        assert {candidate.order_id for candidate in candidates} == (set() if autoflush else {order_id})
        assert bool(session.deleted) is (not autoflush)
        session.rollback()


def test_no_autoflush_context_retains_uncommitted_order_status(lane):
    service, factory, _ids = lane
    order_id = seed_order(lane, symbol="DKI")
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        order.status = "filled"
        with session.no_autoflush:
            candidates = service._collect_drift_cancel_candidates(session, "DKI", None, 0)
        assert [candidate.order_id for candidate in candidates] == [order_id]
        assert order in session.dirty
        assert service._collect_drift_cancel_candidates(session, "DKI", None, 0) == []
        session.rollback()


def test_no_autoflush_context_preserves_candidate_fields(lane):
    service, factory, ids = lane
    order_id = seed_order(lane, symbol="DKI")
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        account = session.get(BrokerAccount, ids["active"])
        strategy = session.get(Strategy, ids["strategy"])
        account.name = "pending-account"
        strategy.code = "pending-strategy"
        order.quantity = Decimal("9.25")
        order.payload = {"order_type": "limit", "limit_price": "2.75", "pending": "exact"}
        with session.no_autoflush:
            candidate, = service._collect_drift_cancel_candidates(session, "DKI", None, 0)
            assert candidate.order_id == order_id
            assert candidate.broker_account_name == "active"
            assert candidate.strategy_code == "schwab_1m_v2"
            assert candidate.quantity == Decimal("3.5")
            assert candidate.limit_price == "2.55"
            assert "pending" not in candidate.existing_metadata
            assert len(session.dirty) == 3
        assert session.autoflush
        candidate, = service._collect_drift_cancel_candidates(session, "DKI", None, 0)
        assert candidate.broker_account_name == "pending-account"
        assert candidate.strategy_code == "pending-strategy"
        assert candidate.quantity == Decimal("9.25")
        assert candidate.limit_price == "2.75"
        assert candidate.existing_metadata["pending"] == "exact"
        assert not session.dirty
        session.rollback()


@pytest.mark.parametrize("autoflush", [False, True])
def test_empty_book_preserves_pending_flush_failure(lane, autoflush):
    service, factory, _ids = lane
    with factory() as session:
        session.add(Strategy(code=None, name="invalid-pending", execution_mode="test"))
        session.autoflush = autoflush
        if autoflush:
            with pytest.raises(IntegrityError):
                service._collect_drift_cancel_candidates(session, "DKI", None, 0)
        else:
            assert service._collect_drift_cancel_candidates(session, "DKI", None, 0) == []
            assert len(session.new) == 1
        session.rollback()
