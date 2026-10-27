"""Statement-local set proof against unchanged scalar readback reference.

Controlled SQLite rows/compiled PostgreSQL shape only; not a native plan claim.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.dialects import postgresql

from project_mai_tai.brokerdecides1_retirement import (
    KINDS, retire_batch, unresolved_query, validated_retirement_ids, valid_retirement,
)
from project_mai_tai.db.models import (
    BrokerDecidesDispatchEvidence as Evidence, BrokerDecidesDispatchRetirement as Retirement,
    BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, TradeIntent,
)
from tests.unit.test_brokerdecides1_local_census import ACCOUNTS, attempt, census, order
from tests.unit.test_brokerdecides1_retirement import observed, seal_one


def assert_same(session, identity, expected):
    scalar = session.scalar(select(valid_retirement()).select_from(DashboardSnapshot)
        .where(DashboardSnapshot.id == identity))
    valid_ids = set(session.scalars(validated_retirement_ids()))
    assert bool(scalar) is expected
    assert (identity in valid_ids) is expected
    unresolved = {row.id for row in session.scalars(unresolved_query(ACCOUNTS))}
    assert (identity not in unresolved) is expected


@pytest.mark.parametrize("change", ["none", "journal", "order_payload", "native_id", "quantity",
    "parent", "event_source", "event_payload", "account", "strategy", "new_event", "late_fill",
    "missing_event", "new_token_order", "replacement", "missing_evidence", "extra_evidence"])
def test_set_proof_matches_scalar_for_every_economic_mutation(census, change):
    session, accounts, strategy = census
    journal, child = seal_one(census)
    assert_same(session, journal.id, True)
    event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == child.id))
    if change == "journal":
        journal.payload = {**journal.payload, "changed": True}
    elif change == "order_payload":
        child.payload = {**child.payload, "changed": True}
    elif change == "native_id":
        child.broker_order_id = "changed-native"
    elif change == "quantity":
        child.quantity = Decimal("2")
    elif change == "parent":
        parent = session.get(TradeIntent, child.intent_id)
        parent.payload = {**parent.payload, "changed": True}
    elif change == "event_source":
        event.event_source = "unknown"
    elif change == "event_payload":
        event.payload = {**event.payload, "changed": True}
    elif change == "account":
        accounts[0].external_account_id = "changed-account"
    elif change == "strategy":
        strategy.code = "changed-strategy"
    elif change == "new_event":
        session.add(BrokerOrderEvent(order_id=child.id, event_type="pending_cancel",
            event_source="broker", event_at=datetime.now(UTC), payload={}))
    elif change == "late_fill":
        session.add(Fill(order_id=child.id, strategy_id=strategy.id,
            broker_account_id=accounts[0].id, broker_fill_id="controlled-set-late",
            symbol="WORX", side="buy", quantity=Decimal("0.5"), price=Decimal("3"),
            filled_at=datetime.now(UTC)))
    elif change == "missing_event":
        session.delete(event)
    elif change == "new_token_order":
        session.add(BrokerOrder(intent_id=child.intent_id, strategy_id=strategy.id,
            broker_account_id=accounts[0].id, client_order_id="new-unsealed-order", symbol="WORX",
            side="buy", quantity=Decimal("1"), order_type="limit", time_in_force="day",
            status="pending", payload=dict(child.payload)))
    elif change == "replacement":
        session.add(DashboardSnapshot(snapshot_type="atr_reprice_handoff", payload={
            "old": {"client_order_id": child.client_order_id}, "phase": "unknown"}))
    elif change == "missing_evidence":
        session.execute(delete(Evidence).where(Evidence.attempt_id == journal.id,
            Evidence.entity_kind == "order"))
    elif change == "extra_evidence":
        session.add(Evidence(attempt_id=journal.id, entity_kind="fill", entity_id=uuid4(),
            original_state="not-a-real-fill"))
    session.flush()
    assert_same(session, journal.id, change == "none")


@pytest.mark.parametrize("kind", KINDS)
def test_all_eight_sealed_counts_still_required(census, kind):
    session, _, _ = census
    journal, _ = seal_one(census)
    receipt = session.get(Retirement, journal.id)
    receipt.counts = {**receipt.counts, kind: receipt.counts[kind] + 1}
    session.flush()
    assert_same(session, journal.id, False)


def test_shared_parent_account_strategy_are_distinct_entity_members(census):
    session, accounts, strategy = census
    journal = attempt(session)
    first = order(session, accounts, strategy)
    second = BrokerOrder(intent_id=first.intent_id, strategy_id=first.strategy_id,
        broker_account_id=first.broker_account_id, client_order_id="second-client",
        broker_order_id="second-native", symbol=first.symbol, side=first.side,
        quantity=first.quantity, order_type=first.order_type, time_in_force=first.time_in_force,
        status="cancelled", payload=dict(first.payload), submitted_at=first.submitted_at,
        updated_at=first.updated_at)
    session.add(second)
    session.flush()
    session.add(BrokerOrderEvent(order_id=second.id, event_type="cancelled", event_source="broker",
        event_at=first.updated_at, payload={"client_order_id": second.client_order_id,
            "broker_order_id": second.broker_order_id}))
    session.flush()
    _, retired, visited = retire_batch(session, ACCOUNTS, None, observed())
    assert retired == visited == 1
    session.commit()
    counts = session.get(Retirement, journal.id).counts
    assert counts["order"] == counts["event"] == 2
    assert counts["intent"] == counts["account"] == counts["strategy"] == 1
    assert_same(session, journal.id, True)


def test_old_unknown_without_receipt_remains_unresolved_beside_valid_receipt(census):
    session, _, _ = census
    journal, _ = seal_one(census)
    unknown = DashboardSnapshot(snapshot_type="v2_wait_dispatch",
        payload={**journal.payload, "symbol": "UNKNOWN", "attempt_token": "unmatched-old"},
        created_at=datetime.now(UTC) - timedelta(days=3))
    session.add(unknown)
    session.flush()
    assert_same(session, journal.id, True)
    assert_same(session, unknown.id, False)


def test_postgres_global_query_materializes_statement_local_members_and_valid_ids():
    sql = str(unresolved_query(ACCOUNTS).limit(32).compile(dialect=postgresql.dialect()))
    assert sql.count("AS MATERIALIZED") == 2
    assert "UNION ALL" in sql
    assert "LEFT OUTER JOIN" in sql
    assert "NOT IN" in sql
    assert "live_order_count" not in sql  # Correlated scalar reference not global census.
