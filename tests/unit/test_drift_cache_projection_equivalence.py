"""Projected drift reads must match the frozen ORM read without hydrating entities."""
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
import math
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import Base, BrokerAccount, BrokerOrder, BrokerOrderEvent, Strategy, TradeIntent
from project_mai_tai.oms.service import OmsRiskService, _DriftCancelCandidate
from project_mai_tai.settings import Settings


def original_orm_read(service, session, symbol, quote, tolerance):
    """Frozen read/filter semantics from I1525 before the column projection."""
    query = select(BrokerOrder).where(BrokerOrder.status.in_(service.store.OPEN_ORDER_STATUSES))
    if symbol is not None:
        query = query.where(BrokerOrder.symbol == symbol)
    orders = session.scalars(query).all()
    if not orders:
        return []
    accounts = {a.id: a for a in service.store.list_active_broker_accounts(session)}
    strategies = {s.id: s for s in session.scalars(select(Strategy)).all()}
    candidates = []
    for order in orders:
        if order.intent_id is None:
            continue
        if quote is None:
            if service._is_stop_guard_order(order) or str((order.payload or {}).get("order_type", "")).lower() != "limit":
                continue
            try:
                if float((order.payload or {}).get("limit_price", "")) <= 0:
                    continue
            except (ValueError, TypeError):
                continue
            drift = 0.0
        else:
            drift = service._quote_drift_dollars_against(order, quote)
            if drift is None or drift <= tolerance:
                continue
        intent = session.get(TradeIntent, order.intent_id)
        if intent is None or str(intent.intent_type).lower() != "open":
            continue
        account = accounts.get(order.broker_account_id)
        if account is None:
            continue
        strategy = strategies.get(order.strategy_id)
        candidates.append(_DriftCancelCandidate(
            order_id=order.id, intent_id=order.intent_id, client_order_id=order.client_order_id,
            broker_account_name=account.name, strategy_code=strategy.code if strategy else "",
            symbol=order.symbol, side=order.side, quantity=order.quantity,
            order_type=order.order_type, time_in_force=order.time_in_force,
            existing_metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
            broker_order_id=order.broker_order_id or "",
            limit_price=str((order.payload or {}).get("limit_price", "")),
            intent_created_at=intent.created_at, drift=drift,
            terminal_cancel_reports=service.store.count_terminal_cancel_refusals(session, order_id=order.id)))
    return candidates


@pytest.fixture
def lane(tmp_path):
    decoded = []
    def deserialize(raw):
        decoded.append(raw)
        return json.loads(raw)
    engine = create_engine(f"sqlite:///{tmp_path / 'projection.sqlite'}", json_deserializer=deserialize)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield make_lane(factory, decoded)
    engine.dispose()


def make_lane(factory, decoded=None):
    service = OmsRiskService(settings=Settings(oms_adapter="simulated"),
        redis_client=SimpleNamespace(), session_factory=factory, broker_adapter=SimpleNamespace())
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="test")
        active = BrokerAccount(name="active", provider="schwab", environment="test", is_active=True)
        inactive = BrokerAccount(name="inactive", provider="schwab", environment="test", is_active=False)
        session.add_all([strategy, active, inactive])
        session.commit()
        ids = dict(strategy=strategy.id, active=active.id, inactive=inactive.id, decoded=decoded)
    return service, factory, ids


def seed_order(lane, *, symbol="MI", side="buy", status="accepted", intent_type="open",
               account="active", payload=None, column_type="limit", missing_intent=False,
               no_intent=False, missing_strategy=False, broker_id=True):
    _service, factory, ids = lane
    with factory() as session:
        intent = TradeIntent(strategy_id=ids['strategy'], broker_account_id=ids[account],
            symbol=symbol, side=side, intent_type=intent_type, quantity=Decimal("3.5"),
            reason="CONTROL", status="submitted", payload={},
            created_at=datetime(2026, 10, 9, 17, tzinfo=UTC))
        session.add(intent)
        session.flush()
        order = BrokerOrder(intent_id=None if no_intent else uuid4() if missing_intent else intent.id,
            strategy_id=uuid4() if missing_strategy else ids['strategy'], broker_account_id=ids[account],
            client_order_id=str(uuid4()), broker_order_id=str(uuid4()) if broker_id else None,
            symbol=symbol, side=side, order_type=column_type, time_in_force="gtc",
            quantity=Decimal("3.5"), status=status,
            payload=payload if payload is not None else {"order_type": "limit", "limit_price": "2.55"})
        session.add(order)
        session.commit()
        return order.id


def compare(lane, symbol=None, quote=None, tolerance=0, expected_query_count=None,
            expected_json_decodes=None):
    service, factory, _ids = lane
    hydrated = []
    event.listen(factory, "loaded_as_persistent", lambda _session, instance: hydrated.append(type(instance)))
    with factory() as session:
        expected = original_orm_read(service, session, symbol, quote, tolerance)
    original_loads = list(hydrated)
    hydrated.clear()
    if _ids['decoded'] is not None:
        _ids['decoded'].clear()
    statements = []
    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(factory.kw['bind'], "before_cursor_execute", capture)
    try:
        with factory() as session:
            actual = service._collect_drift_cancel_candidates(session, symbol, quote, tolerance)
            assert not session.identity_map
    finally:
        event.remove(factory.kw['bind'], "before_cursor_execute", capture)
    if expected_query_count is not None:
        assert len(statements) == expected_query_count
    if expected_json_decodes is not None and _ids['decoded'] is not None:
        assert len(_ids['decoded']) == expected_json_decodes
    assert not hydrated

    def normalized(candidates):
        output = {}
        for candidate in candidates:
            data = asdict(candidate)
            if math.isnan(data['drift']):
                data['drift'] = "NAN_PRESERVED"
            output[candidate.order_id] = data
        return output

    assert normalized(actual) == normalized(expected)
    return actual, original_loads


def test_fourteen_working_rows_no_candidates_without_any_orm_hydration(lane):
    for index in range(14):
        seed_order(lane, intent_type="close",
            payload={"order_type": "limit", "limit_price": "2.55", "stop_guard": index % 2 == 0})
    actual, loaded = compare(lane, expected_query_count=1, expected_json_decodes=0)
    assert actual == [] and loaded.count(BrokerOrder) == 14


def test_market_open_orders_skip_payload_materialization_and_lookup_reads(lane):
    for _ in range(25):
        seed_order(lane, payload={"order_type": "market", "limit_price": "2.55"})
    actual, loaded = compare(lane, expected_query_count=1)
    assert actual == [] and loaded.count(BrokerOrder) == 25


@pytest.mark.parametrize("payload_type", ["limit", "LIMIT", "LiMiT", " LIMIT ", "\tlimit\n",
                                         "market", None, 12, ["limit"], {"type": "limit"}])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0}])
def test_cache_type_filter_preserves_python_string_and_whitespace_semantics(lane, payload_type, quote):
    seed_order(lane, payload={"order_type": payload_type, "limit_price": "2.55"})
    compare(lane, quote=quote)


@pytest.mark.parametrize("quote", [None, {"ask": 3.0, "bid": 2.0}])
def test_candidates_preserve_all_fields_metadata_and_exact_target_budget(lane, quote):
    first = seed_order(lane, payload={"order_type": "limit", "limit_price": "2.55",
        "fanout_segment_id": "original", "nested": {"a": 1}, "flag": False})
    second = seed_order(lane, side="sell", missing_strategy=True, broker_id=False)
    service, factory, _ids = lane
    with factory() as session:
        for reason in ("Order in state FILLED cannot be canceled", "Order in state CANCELED cannot be canceled", "unrelated"):
            session.add(BrokerOrderEvent(order_id=first, event_type="rejected",
                event_source="broker", payload={"reason": reason}))
        session.commit()
    actual, _loaded = compare(lane, quote=quote, tolerance=0.01)
    by_id = {c.order_id: c for c in actual}
    assert set(by_id) == {first, second}
    assert by_id[first].terminal_cancel_reports == 2
    assert by_id[second].terminal_cancel_reports == 0
    assert by_id[first].existing_metadata['nested'] == "{'a': 1}"
    assert by_id[first].quantity == Decimal("3.5")
    assert by_id[second].strategy_code == "" and by_id[second].broker_order_id == ""


@pytest.mark.parametrize("changes", [
    {"status": "filled"}, {"status": "cancelled"}, {"status": "rejected"},
    {"account": "inactive"}, {"intent_type": "close"}, {"intent_type": "scale"},
    {"missing_intent": True}, {"no_intent": True},
    {"payload": {"order_type": "limit", "limit_price": "2.55", "stop_guard": " TRUE "}},
    {"payload": {"order_type": "stop_limit", "limit_price": "2.55"}},
    {"payload": {"order_type": "limit", "limit_price": "bad"}},
    {"payload": {"order_type": "limit", "limit_price": None}},
    {"payload": {"order_type": "limit", "limit_price": "0"}},
    {"payload": {"order_type": "limit", "limit_price": "-1"}},
])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0, "bid": 2.0}])
def test_exclusions_match_original_read(lane, changes, quote):
    seed_order(lane, **changes)
    actual, _loaded = compare(lane, quote=quote)
    assert not actual


@pytest.mark.parametrize("status", ["pending", "submitted", "accepted", "partially_filled"])
def test_open_status_and_payload_not_column_type_control_cache(lane, status):
    seed_order(lane, status=status, column_type="market")
    actual, _loaded = compare(lane)
    assert len(actual) == 1 and actual[0].order_type == "market"


@pytest.mark.parametrize("intent_type", ["open", "OPEN", "oPeN", "Open", " open", "open ", "CLOSE", "sCaLe"])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0}])
def test_join_intent_filter_preserves_existing_case_and_whitespace_rules(lane, intent_type, quote):
    seed_order(lane, intent_type=intent_type)
    actual, _loaded = compare(lane, quote=quote)
    assert len(actual) == int(intent_type.lower() == "open")


@pytest.mark.parametrize("quote", [None, {"ask": 3.0}])
def test_original_whitespace_asymmetry_and_symbol_filter_are_preserved(lane, quote):
    seed_order(lane, symbol="MI", payload={"order_type": " LIMIT ", "limit_price": " 2.55 "})
    seed_order(lane, symbol="OTHER")
    actual, _loaded = compare(lane, symbol="MI", quote=quote)
    assert len(actual) == (0 if quote is None else 1)


@pytest.mark.parametrize("price", ["nan", "inf"])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0}])
def test_projection_does_not_add_price_policy(lane, price, quote):
    seed_order(lane, payload={"order_type": "limit", "limit_price": price})
    compare(lane, quote=quote)


@pytest.mark.parametrize("quote", [{}, {"ask": 0}, {"ask": "3"}, {"ask": 2.55}])
def test_quote_readability_and_inside_limit_match_original(lane, quote):
    seed_order(lane)
    actual, _loaded = compare(lane, quote=quote, tolerance=0.01)
    assert not actual
