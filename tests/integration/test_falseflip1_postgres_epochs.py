"""Required real-PostgreSQL regression for millisecond JSON epoch comparisons."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from uuid import uuid4

import pytest
from sqlalchemy import Integer, create_engine, select, text
from sqlalchemy.exc import DataError
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import (
    Base, BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, OmsManagedPosition, Strategy,
)
from project_mai_tai import falseflip1_runtime as runtime
from project_mai_tai.fanout_identity import fanout_slot_id


BAR_MS = 1791468000000
SEGMENT = 1791466440000
NOW = datetime.fromtimestamp((BAR_MS + 62000) / 1000, UTC)
BAR = {"symbol": "ZZFALSEPG", "bar_ms": BAR_MS, "observed_at_ms": BAR_MS + 62000,
       "close": "2.7901", "trail": "3.006326", "state": "short", "phase": "live"}


@pytest.fixture
def postgres_factory():
    dsn = os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not dsn.startswith("postgresql"):
        pytest.fail("MAI_TAI_DATABASE_URL must name the required PostgreSQL CI service")
    engine = create_engine(dsn)
    schema = f"falseflip_epoch_{uuid4().hex}"
    with engine.begin() as connection:
        assert connection.dialect.name == "postgresql"
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(isolated)
        yield sessionmaker(isolated, expire_on_commit=False)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def seed_entry(factory):
    at = datetime.fromtimestamp((BAR_MS + 41000) / 1000, UTC)
    with factory() as session:
        account = BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="live")
        strategy = Strategy(code="schwab_1m_v2", name="epoch regression")
        session.add_all((account, strategy))
        session.flush()
        order = BrokerOrder(broker_account_id=account.id, strategy_id=strategy.id,
            client_order_id="falseflip-pg-entry", symbol=BAR["symbol"], side="buy",
            order_type="stop_limit", time_in_force="day", quantity=197, status="filled",
            submitted_at=at, updated_at=at,
            payload={"fanout_segment_id": SEGMENT, "fanout_slot_id": fanout_slot_id(
                strategy_code="schwab_1m_v2", symbol=BAR["symbol"], segment_id=SEGMENT, slot="resting")})
        session.add(order)
        session.flush()
        managed = OmsManagedPosition(strategy_code=strategy.code, broker_account_name=account.name,
            symbol=order.symbol, entry_order_id=order.id, entry_client_order_id=order.client_order_id,
            entry_price=Decimal("3.05"), original_quantity=197, current_quantity=0,
            status="closed", entry_time=at, updated_at=at + timedelta(seconds=10))
        session.add_all((managed, Fill(order_id=order.id, strategy_id=strategy.id,
            broker_account_id=account.id, symbol=order.symbol, side="buy", quantity=197,
            price=Decimal("3.05"), filled_at=at)))
        session.commit()
        return managed.id, order.id, at


def exercise(factory, path, monkeypatch=None):
    row_id, order_id, at = seed_entry(factory)
    runtime.record_bar(factory, BAR, now=NOW)
    store = runtime.FalseFlipStore(factory)
    before = runtime.FalseBudget(BAR["symbol"], SEGMENT)
    after = before.closed(SEGMENT + 240000, 1)
    store.commit(before, after, now=NOW)
    if path == "invalidation":
        assert runtime.classify_managed_entries(factory, now=NOW) == 1

    if monkeypatch is not None:
        original = runtime.cast

        def int4_mutation(expression, target):
            column = expression.left
            key = expression.right.value
            mutate = ((path == "budget" and key == "segment")
                      or (path in {"record", "classify"} and key == "bar_ms"
                          and column.name == "payload")
                      or (path == "invalidation" and column.name == "entry_classification"))
            return original(expression, Integer if mutate else target)

        monkeypatch.setattr(runtime, "cast", int4_mutation)

    if path == "record":
        runtime.record_bar(factory, BAR, now=NOW)
        with factory() as session:
            bars = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == runtime.BAR_TYPE)).all()
            assert len(bars) == 1 and bars[0].payload["bar_ms"] == BAR_MS
    elif path == "budget":
        store.commit(after, after.cancellation_confirmed(SEGMENT + 240000), now=NOW)
        restored = store.restore(now=NOW)[(BAR["symbol"], SEGMENT)]
        assert restored.segment == SEGMENT and restored.cancelled == (SEGMENT + 240000,)
    elif path == "classify":
        assert runtime.classify_managed_entries(factory, now=NOW) == 1
    else:
        runtime.record_bar(factory, {**BAR, "close": "2.8"}, now=NOW)

    if path in {"classify", "invalidation"}:
        with factory() as session:
            managed = session.get(OmsManagedPosition, row_id)
            order = session.get(BrokerOrder, order_id)
            proof = managed.entry_classification
            assert proof == order.payload["entry_classification"]
            assert proof["bar_ms"] == BAR_MS
            assert proof["classification"] == ("FALSE_FLIP" if path == "classify" else "UNKNOWN")
            assert managed.status == "closed" and managed.current_quantity == 0
            assert managed.updated_at == at + timedelta(seconds=10)
            assert order.updated_at == at


@pytest.mark.parametrize("path", ["record", "budget", "classify", "invalidation"])
def test_real_postgres_millisecond_epoch_roundtrip(postgres_factory, path):
    exercise(postgres_factory, path)


@pytest.mark.parametrize("path", ["record", "budget", "classify", "invalidation"])
def test_real_postgres_int4_mutation_is_out_of_range(postgres_factory, path, monkeypatch):
    with pytest.raises(DataError, match="integer out of range"):
        exercise(postgres_factory, path, monkeypatch)
