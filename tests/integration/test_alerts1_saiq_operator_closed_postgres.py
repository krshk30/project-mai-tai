"""Explicit main-owned PG claim only; author UNRUN, no provider or live DB."""
from __future__ import annotations

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, Strategy, SystemIncident, TradeIntent
from project_mai_tai.reconciliation.operator_closed_exits import REVIEWED_CLOSES, exclude_reviewed_operator_closes
from tests.unit.test_alerts1_saiq_operator_closed import NOW, add_recorded_incidents


pytestmark = pytest.mark.skipif(
    os.environ.get("ALERTS1_SAIQ_PG_CLAIM") != "MAIN_OWNED_ISOLATED",
    reason="separate main-owned isolated PostgreSQL qualification claim required; not qualified by skip",
)
RULE = REVIEWED_CLOSES[0]


@pytest.fixture
def factory():
    # Pin the already-reviewed main-only CI protocol, never adopt the foreign
    # PG cluster or general credentials. Main must verify PID/datadir ownership
    # before claiming this opt-in module; URL validation cannot prove ownership.
    dsn = os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not dsn.startswith("postgresql"):
        pytest.fail("main-owned PostgreSQL CI URL required")
    url = make_url(dsn)
    if not (url.drivername == "postgresql+psycopg" and url.host is None and url.port is None
            and url.username == "postgres" and url.password is None
            and url.database == "project_mai_tai_test"
            and set(url.query) == {"host", "port"}
            and url.query.get("host") == "/private/tmp/mai-tai-night-ci-20261022/socket"
            and str(url.query.get("port")) == "55442"):
        pytest.fail("URL is not the exact reviewed isolated main CI socket protocol")
    schema = f"alerts1_saiq_{uuid4().hex}"
    admin = create_engine(dsn)
    engine = None
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(dsn, connect_args={"options":
            f"-csearch_path={schema} -c lock_timeout=2000 -c statement_timeout=5000"})
        Base.metadata.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def seed(factory):
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="controlled", execution_mode="live", metadata_json={})
        account = BrokerAccount(name=RULE.account, provider="webull", environment="production")
        session.add_all([strategy, account])
        session.flush()
        order = BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id="schwab_1m_v2-SAIQ-open-2a3bc04ec248", broker_order_id=RULE.native_entry_id,
            symbol="SAIQ", side="buy", order_type="limit", time_in_force="day", quantity=39,
            status="filled", payload={}, submitted_at=RULE.entry_second, updated_at=RULE.entry_second)
        session.add(order)
        session.flush()
        fill = Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
            broker_fill_id="CONTROLLED-PG-copy-of-retained-buy", symbol="SAIQ", side="buy",
            quantity=RULE.quantity, price=RULE.price,
            filled_at=RULE.entry_second + timedelta(microseconds=337000), payload={})
        session.add(fill)
        session.flush()
        rows = add_recorded_incidents(session)
        session.commit()
        return fill.id, order.id, [(row.id, original) for row, original in rows]


def verify(factory, fill_id, order_id, originals, *, audit_count, tagged):
    with factory() as session:
        fill = session.get(Fill, fill_id)
        order = session.get(BrokerOrder, order_id)
        assert fill.quantity == 39 and fill.price == RULE.price and fill.side == "buy"
        assert order.broker_order_id == RULE.native_entry_id and order.status == "filled"
        assert session.scalar(select(func.count()).select_from(Fill)) == 1
        assert session.scalar(select(func.count()).select_from(BrokerOrder)) == 1
        audits = session.scalars(select(SystemIncident).where(SystemIncident.service_name == "oco-capture")).all()
        assert len(audits) == audit_count
        for row_id, original in originals:
            row = session.get(SystemIncident, row_id)
            assert row.status == original["status"] and row.closed_at is None
            assert row.severity == ("info" if tagged else "critical")
            if tagged:
                assert row.payload["operator_closed_allowance"] == RULE.allowance_id
                assert row.payload["operator_closed_entry_fill_id"] == str(fill_id)
                assert {key: value for key, value in row.payload.items()
                        if key not in ("operator_closed_allowance", "operator_closed_entry_fill_id")} == original["payload"]
            else:
                assert row.payload == original["payload"]


def test_postgres_nowait_contention_then_repeat_creates_one_durable_audit(factory):
    fill_id, order_id, originals = seed(factory)
    with factory() as owner, factory() as contender:
        assert exclude_reviewed_operator_closes(owner, [str(fill_id)], now=NOW) == [str(fill_id)]
        owner.flush()
        # Two distinct live PostgreSQL transactions, not an SQLite lock double.
        with pytest.raises(OperationalError) as failure:
            exclude_reviewed_operator_closes(contender, [str(fill_id)], now=NOW)
        assert getattr(failure.value.orig, "sqlstate", None) == "55P03"
        contender.rollback()
        owner.commit()
        assert exclude_reviewed_operator_closes(contender, [str(fill_id)], now=NOW) == [str(fill_id)]
        contender.commit()
    verify(factory, fill_id, order_id, originals, audit_count=1, tagged=True)


def test_postgres_rolled_back_audit_never_becomes_committed_exclusion_proof(factory):
    fill_id, order_id, originals = seed(factory)
    with factory() as abandoned:
        assert exclude_reviewed_operator_closes(abandoned, [str(fill_id)], now=NOW) == [str(fill_id)]
        abandoned.flush()
        abandoned.rollback()
    verify(factory, fill_id, order_id, originals, audit_count=0, tagged=False)
    with factory() as successor:
        assert exclude_reviewed_operator_closes(successor, [str(fill_id)], now=NOW) == [str(fill_id)]
        successor.commit()
    verify(factory, fill_id, order_id, originals, audit_count=1, tagged=True)


def test_postgres_real_bot_sell_intent_without_fill_keeps_exact_incidents_critical(factory):
    fill_id, order_id, originals = seed(factory)
    with factory() as session:
        fill = session.get(Fill, fill_id)
        session.add(TradeIntent(strategy_id=fill.strategy_id, broker_account_id=fill.broker_account_id,
            symbol="SAIQ", side="sell", intent_type="close", quantity=39,
            reason="CONTROLLED real bot exit missing its Fill", status="filled", payload={}, created_at=NOW))
        session.commit()
    with factory() as session:
        assert exclude_reviewed_operator_closes(session, [str(fill_id)], now=NOW) == []
        session.commit()
    verify(factory, fill_id, order_id, originals, audit_count=0, tagged=False)
