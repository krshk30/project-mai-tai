"""Native AIXI client-abort preimage; mutations below are explicit controls."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import DateTime, Numeric, Uuid, create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import (
    AccountPosition, Base, BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Strategy, TradeIntent,
)
from project_mai_tai.positions_read_receipt import SNAPSHOT_TYPE as RECEIPT_TYPE, record_receipts
from project_mai_tai.v2_removed_wait import RemovedWait, RemovedWaitStore
from tests.unit.test_v2_removed_wait_clear_rules import apply_committed, strategy


NATIVE = json.loads((Path(__file__).parents[1] / "fixtures" /
                     "restartkeep1_aixi_client_abort_native.json").read_text())
WED = datetime(2026, 10, 14, 8, tzinfo=UTC)  # Actual Wednesday 04:00 ET.
NAMES = {"live:schwab_1m_v2", "live:orb"}


def native_row(model, raw):
    values = dict(raw)
    assert not set(values) - set(model.__table__.columns.keys())
    for column in model.__table__.columns:
        value = values.get(column.name)
        if value is None:
            continue
        if isinstance(column.type, DateTime):
            values[column.name] = datetime.fromisoformat(value)
        elif isinstance(column.type, Uuid):
            values[column.name] = UUID(value)
        elif isinstance(column.type, Numeric):
            values[column.name] = Decimal(str(value))
    return model(**values)


def controlled_position_read(factory, observed_at):
    # Complete controlled DB-backed broker replies, NOT an additional native
    # AIXI receipt: the retained vendor packet does not prove today's flatness.
    with factory.begin() as session:
        accounts = session.scalars(select(BrokerAccount).where(BrokerAccount.name.in_(NAMES))).all()
        assert {row.name for row in accounts} == NAMES
        positions = session.scalars(select(AccountPosition).where(
            AccountPosition.broker_account_id.in_([row.id for row in accounts]))).all()
        record_receipts(session, [(row.name, observed_at, sum(
            p.broker_account_id == row.id for p in positions)) for row in accounts],
            recorded_at=observed_at)


@pytest.fixture
def native_book(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "aixi.sqlite"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add_all(native_row(BrokerAccount, row) for row in NATIVE["accounts"])
        session.add(native_row(Strategy, NATIVE["strategy"]))
        session.add(native_row(TradeIntent, NATIVE["intent"]))
        session.add(native_row(BrokerOrder, NATIVE["order"]))
        session.add_all(native_row(BrokerOrderEvent, row) for row in NATIVE["events"])
        session.add(native_row(DashboardSnapshot, NATIVE["request"]))
        session.commit()
    controlled_position_read(factory, WED)
    yield RemovedWaitStore(factory), factory
    engine.dispose()


def test_exact_native_aixi_client_abort_clears_previous_session_request(native_book):
    store, _ = native_book
    assert NATIVE["order"]["id"] == "6463d2d1-3c92-4ed9-b91e-8b37fab287d3"
    assert NATIVE["order"]["broker_order_id"] is None
    assert NATIVE["order"]["status"] == NATIVE["intent"]["status"] == "aborted"
    assert [(row["event_type"], row["event_source"]) for row in NATIVE["events"]] == [
        ("aborted", "client")]
    req = store.restore()["AIXI"]
    assert isinstance(req, RemovedWait) and req.opportunity_id == 1791895622355
    proof, = store.clear_rule_proofs((req,), NAMES, now=WED)
    assert proof.clear and proof.reason == "previous_session_flat"
    strat = strategy(req, store, now=int(WED.timestamp() * 1000))
    strat.scanner_readded("AIXI")
    assert strat._removed_wait_gate_closed("AIXI")
    apply_committed(strat, store, proof)
    assert store.restore() == {} and not strat._removed_wait_gate_closed("AIXI")


@pytest.mark.parametrize("broker_event", ["accepted", "pending", "unknown", "aborted", "filled"])
def test_idless_aborted_order_with_any_broker_event_stays_unknown(native_book, broker_event):
    store, factory = native_book
    with factory() as session:
        session.add(BrokerOrderEvent(id=uuid4(), order_id=UUID(NATIVE["order"]["id"]),
            event_type=broker_event, event_source="broker", event_at=WED, payload={}))
        session.commit()
    req = store.restore()["AIXI"]
    proof, = store.clear_rule_proofs((req,), NAMES, now=WED)
    assert not proof.clear and proof.reason == "working_order"
    assert store.restore() == {"AIXI": req}


@pytest.mark.parametrize("broker_id", ["controlled-real-id", ""])
def test_aborted_order_non_null_broker_id_retains_old_rule(native_book, broker_id):
    store, factory = native_book
    with factory() as session:
        row = session.get(BrokerOrder, UUID(NATIVE["order"]["id"]))
        row.broker_order_id = broker_id
        session.flush()
        row.updated_at = datetime.fromisoformat(NATIVE["order"]["updated_at"])
        session.commit()
    proof, = store.clear_rule_proofs(tuple(store.restore().values()), NAMES, now=WED)
    assert not proof.clear and proof.reason == "working_order"


@pytest.mark.parametrize("status", ["aborted", "rejected"])
def test_client_terminal_predicate_does_not_bypass_settle_window(native_book, status):
    store, factory = native_book
    with factory() as session:
        row = session.get(BrokerOrder, UUID(NATIVE["order"]["id"]))
        row.status, row.updated_at = status, WED
        session.commit()
    proof, = store.clear_rule_proofs(tuple(store.restore().values()), NAMES, now=WED)
    assert not proof.clear and proof.reason == "terminal_row_settling"


@pytest.mark.parametrize("fault", ["missing-primary", "missing-webull", "stale"])
def test_native_terminal_still_requires_fresh_complete_account_receipts(native_book, fault):
    store, factory = native_book
    with factory.begin() as session:
        for row in session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == RECEIPT_TYPE)).all():
            name = row.payload["broker_account_name"]
            if (fault == "missing-primary" and name == "live:schwab_1m_v2"
                    or fault == "missing-webull" and name == "live:orb"):
                session.delete(row)
            elif fault == "stale":
                row.payload = {**row.payload, "read_at": (WED-timedelta(milliseconds=15001)).isoformat()}
    req = store.restore()["AIXI"]
    proof, = store.clear_rule_proofs((req,), NAMES, now=WED)
    assert not proof.clear and proof.reason == "own_position_evidence_UNKNOWN"
    assert store.restore() == {"AIXI": req}
