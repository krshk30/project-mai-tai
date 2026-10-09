"""Scalar intent reads retain quantity, transaction and existing-order identity."""

from decimal import Decimal

import pytest
from sqlalchemy import event

from project_mai_tai.db.models import BrokerOrder, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.store import OmsStore
from tests.unit.test_oms_store import build_test_session_factory


def seed_database(factory):
    store = OmsStore()
    with factory() as session:
        strategy = store.ensure_strategy(session, "macd_30s", name="controlled")
        account = store.ensure_broker_account(session, "paper:scalar", provider="alpaca", environment="test")
        session.commit()
        ids = strategy.id, account.id
    return store, factory, ids


@pytest.fixture
def database():
    factory = build_test_session_factory()
    yield seed_database(factory)
    factory.kw["bind"].dispose()


@pytest.mark.parametrize("quantity", [None, Decimal(0), Decimal(-1), Decimal("1.25")])
def test_virtual_quantity_matches_without_hydrating_position(database, quantity):
    store, factory, (strategy_id, account_id) = database
    if quantity is not None:
        with factory() as session:
            session.add(VirtualPosition(strategy_id=strategy_id, broker_account_id=account_id,
                symbol="MI", quantity=quantity, average_price=1))
            session.commit()
    loaded = []
    event.listen(factory, "loaded_as_persistent", lambda _session, row: loaded.append(row))
    with factory() as session:
        assert store.get_virtual_position_quantity(session, strategy_id=strategy_id,
            broker_account_id=account_id, symbol="MI") == quantity
        assert not loaded and not session.identity_map


@pytest.mark.parametrize("autoflush", [False, True])
def test_virtual_quantity_retains_dirty_identity_and_autoflush_semantics(database, autoflush):
    store, factory, (strategy_id, account_id) = database
    with factory() as session:
        row = VirtualPosition(strategy_id=strategy_id, broker_account_id=account_id,
            symbol="MI", quantity=1, average_price=1)
        session.add(row)
        session.commit()
        row.quantity = Decimal("2.5")
        session.autoflush = autoflush
        assert store.get_virtual_position_quantity(session, strategy_id=strategy_id,
            broker_account_id=account_id, symbol="MI") == Decimal("2.5")
        assert store.get_virtual_position(session, strategy_id=strategy_id,
            broker_account_id=account_id, symbol="MI") is row
        session.rollback()


def test_order_create_and_repeated_report_preserve_exact_identity_and_payload(database):
    store, factory, (strategy_id, account_id) = database
    with factory() as session:
        strategy = store.ensure_strategy(session, "macd_30s", name="controlled")
        account = store.ensure_broker_account(session, "paper:scalar", provider="alpaca", environment="test")
        intent = store.create_trade_intent(session, strategy=strategy, broker_account=account,
            event=TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
                strategy_code="macd_30s", broker_account_name=account.name, symbol="MI", side="buy",
                quantity=Decimal(1), intent_type="open", reason="controlled")))
        args = dict(intent=intent, strategy_id=strategy_id, broker_account_id=account_id,
            client_order_id="scalar-order", symbol="MI", side="buy", quantity=Decimal(1))
        created = store.get_or_create_order(session, **args, metadata={},
            status="accepted", broker_order_id="broker-scalar")
        session.commit()
        created.payload = {"in_transaction": "retained"}
        actual = store.get_or_create_order(session, **args, metadata={"reason": "refused"},
            status="rejected", reject_reason="controlled_reject")
        assert actual is created and actual.broker_order_id == "broker-scalar"
        assert actual.payload == {"reason": "refused", "reject_reason": "controlled_reject"}
        ident = actual.id
        session.commit()
    loaded = []
    event.listen(factory, "loaded_as_persistent", lambda _session, row: loaded.append(row))
    with factory() as session:
        args["intent"] = type("Intent", (), {"id": created.intent_id})()
        actual = store.get_or_create_order(session, **args, metadata={}, status="accepted")
        assert actual.id == ident
        assert len(loaded) == 1 and isinstance(loaded[0], BrokerOrder)
        session.rollback()
