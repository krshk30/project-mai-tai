"""Core projections preserve the old reservation and durable-stop semantics."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsArmedStop, Strategy
from tests.unit.test_intent_order_scalar_reads import seed_database
from tests.unit.test_oms_store import build_test_session_factory


@pytest.fixture
def database():
    factory = build_test_session_factory()
    yield seed_database(factory)
    factory.kw["bind"].dispose()


def order(strategy_id, account_id, *, quantity="1.125", payload=None, **changes):
    values = dict(strategy_id=strategy_id, broker_account_id=account_id,
        client_order_id=uuid4().hex, symbol="MI", side="sell", order_type="limit",
        time_in_force="day", quantity=Decimal(quantity), status="accepted", payload=payload or {})
    return BrokerOrder(**{**values, **changes})


def seed_orders(database):
    store, factory, (strategy_id, account_id) = database
    with factory() as session:
        other_strategy = store.ensure_strategy(session, "other", name="other")
        other_account = store.ensure_broker_account(session, "paper:other",
            provider="alpaca", environment="test")
        session.flush()
        rows = [order(strategy_id, account_id),
            order(other_strategy.id, account_id, quantity="2.25",
                  payload={"native_stop_guard": " TrUe "}),
            order(strategy_id, account_id, quantity="3.375",
                  payload={"native_stop_guard": "false"}),
            order(strategy_id, account_id, quantity="4.5", status="cancelled"),
            order(strategy_id, account_id, quantity="5.625", side="buy"),
            order(strategy_id, account_id, quantity="6.75", symbol="OTHER"),
            order(strategy_id, other_account.id, quantity="7.875")]
        session.add_all(rows)
        session.commit()
        return tuple(row.id for row in rows)


def old_rows(session, store, account_id):
    return session.scalars(select(BrokerOrder).where(
        BrokerOrder.broker_account_id == account_id, BrokerOrder.symbol == "MI",
        BrokerOrder.side == "sell", BrokerOrder.status.in_(store.OPEN_ORDER_STATUSES))).all()


def old_total(session, store, account_id, include):
    return sum((row.quantity for row in old_rows(session, store, account_id)
        if include or str((row.payload or {}).get("native_stop_guard", "")).strip().lower() != "true"),
        Decimal(0))


@pytest.mark.parametrize("include", [True, False])
def test_core_same_rows_values_order_and_total(database, include):
    store, factory, (strategy_id, account_id) = database
    seed_orders(database)
    with factory() as session:
        original = [(row.quantity, row.payload) for row in old_rows(session, store, account_id)]
    with factory() as session:
        columns = BrokerOrder.__table__.c
        projected = list(session.execute(select(columns.quantity, columns.payload).where(
            columns.broker_account_id == account_id, columns.symbol == "MI",
            columns.side == "sell", columns.status.in_(store.OPEN_ORDER_STATUSES))).all())
        assert projected == original
        # Actual intent sessions retain registrations, but no order identity yet.
        strategy = session.get(Strategy, strategy_id)
        account = session.get(BrokerAccount, account_id)
        statements, loaded = [], []
        event.listen(session, "do_orm_execute", lambda state: statements.append(state.is_orm_statement))
        event.listen(session, "loaded_as_persistent", lambda _session, row: loaded.append(row))
        actual = store.get_open_exit_reserved_quantity(session,
            broker_account_id=account_id, symbol="MI", include_native_stop_guard=include)
        assert actual == (Decimal("6.75") if include else Decimal("4.5"))
        assert statements == [False] and not loaded
        assert strategy in session.identity_map.values() and account in session.identity_map.values()


@pytest.mark.parametrize("include", [True, False])
@pytest.mark.parametrize("case", ["loaded", "json_in_place", "dirty", "new", "deleted", "no_autoflush"])
def test_reservation_identity_and_transaction_equivalence(database, include, case):
    store, factory, (strategy_id, account_id) = database
    ids = seed_orders(database)
    with factory() as session:
        retained = None
        if case in {"loaded", "json_in_place", "dirty", "deleted"}:
            retained = session.get(BrokerOrder, ids[0])
        if case == "json_in_place":
            retained.payload["native_stop_guard"] = "true"
            assert not session.dirty
        elif case == "dirty":
            retained.quantity = Decimal("9.25")
        elif case == "new":
            retained = order(strategy_id, account_id, quantity="8.125")
            session.add(retained)
        elif case == "deleted":
            session.delete(retained)
        elif case == "no_autoflush":
            session.autoflush = False
            retained = order(strategy_id, account_id, quantity="8.125")
            session.add(retained)
        statements = []
        event.listen(session, "do_orm_execute", lambda state: statements.append(state.is_orm_statement))
        actual = store.get_open_exit_reserved_quantity(session, broker_account_id=account_id,
            symbol="MI", include_native_stop_guard=include)
        assert statements[0] is True
        assert actual == old_total(session, store, account_id, include)
        if case in {"loaded", "json_in_place", "dirty"}:
            assert session.get(BrokerOrder, ids[0]) is retained
        session.rollback()
    with factory() as session:
        assert old_total(session, store, account_id, include) == (
            Decimal("6.75") if include else Decimal("4.5"))


def test_clean_stale_loaded_order_keeps_original_identity_value(database):
    store, factory, (_, account_id) = database
    ids = seed_orders(database)
    with factory() as session:
        retained = session.get(BrokerOrder, ids[0])
        session.execute(update(BrokerOrder).where(BrokerOrder.id == ids[0]).values(
            quantity=Decimal("20")), execution_options={"synchronize_session": False})
        assert not session.dirty and retained.quantity == Decimal("1.125")
        assert store.get_open_exit_reserved_quantity(session,
            broker_account_id=account_id, symbol="MI") == Decimal("6.75")
        assert old_total(session, store, account_id, True) == Decimal("6.75")
        session.rollback()


def stop(**changes):
    values = dict(strategy_code="schwab_1m_v2", broker_account_name="paper:scalar", symbol="MI",
        quantity=Decimal(5), entry_price=Decimal(2), stop_loss_pct=8,
        stop_price=Decimal("1.84"), quote_max_age_ms=15000, initial_panic_buffer_pct=1,
        trail_pct=5, high_water_mark=Decimal("2.1"), close_in_flight=True)
    return OmsArmedStop(**{**values, **changes})


@pytest.mark.parametrize("case", ["absent", "existing", "loaded", "dirty", "new", "deleted", "no_autoflush"])
def test_stop_delete_equivalent_and_never_commits(database, case):
    store, factory, (strategy_id, _) = database
    key = dict(strategy_code="schwab_1m_v2", broker_account_name="paper:scalar", symbol="MI")
    if case in {"existing", "loaded", "dirty", "deleted", "no_autoflush"}:
        with factory() as session:
            row = stop()
            session.add(row)
            session.commit()
            stop_id = row.id
    with factory() as session:
        registration = session.get(Strategy, strategy_id)
        retained = None
        if case in {"loaded", "dirty", "deleted"}:
            retained = session.get(OmsArmedStop, stop_id)
        if case == "dirty":
            retained.stop_price = Decimal("1.99")
        elif case == "new":
            retained = stop()
            session.add(retained)
        elif case == "deleted":
            session.delete(retained)
        elif case == "no_autoflush":
            session.autoflush = False
        statements, commits = [], []
        event.listen(session, "do_orm_execute", lambda state: statements.append(state.is_orm_statement))
        event.listen(session, "after_commit", lambda _session: commits.append(True))
        store.delete_armed_stop(session, **key)
        assert statements == ([False] if case == "absent" else
            [False, True] if case == "existing" else [True])
        assert registration in session.identity_map.values() and not commits
        assert session.scalar(select(OmsArmedStop.id).where(OmsArmedStop.symbol == "MI")) is None
        if retained is not None and case not in {"deleted"}:
            assert retained not in session
        session.rollback()
    with factory() as session:
        restored = session.scalar(select(OmsArmedStop).where(OmsArmedStop.symbol == "MI"))
        assert (restored is None) is (case in {"absent", "new"})
        if restored is not None:
            assert restored.stop_price == Decimal("1.84")
            assert restored.high_water_mark == Decimal("2.1") and restored.close_in_flight
