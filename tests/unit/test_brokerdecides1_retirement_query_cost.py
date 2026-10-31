"""Proof-preserving query-shape controls; not PostgreSQL plan qualification."""

from decimal import Decimal
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.dialects import postgresql

from project_mai_tai.brokerdecides1_retirement import (
    KINDS, entity_set, unresolved_query, valid_retirement,
)
from project_mai_tai.db.models import (
    BrokerDecidesDispatchEvidence as Evidence,
    BrokerDecidesDispatchRetirement as Retirement, BrokerOrderEvent, Fill,
)
from tests.unit.test_brokerdecides1_local_census import ACCOUNTS, census as census
from tests.unit.test_brokerdecides1_retirement import is_valid, seal_one


@pytest.mark.parametrize("kind", KINDS)
def test_each_live_set_count_appears_once_in_full_proof(kind):
    query = select(valid_retirement())
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert sql.count(f"AS live_{kind}_count") == 1


@pytest.mark.parametrize("kind", ["order", "intent", "account", "strategy", "replacement"])
def test_direct_token_projection_does_not_reenter_order_ids(kind):
    model, predicate = entity_set(kind, "exact-token")
    sql = str(select(model.id).where(predicate).compile(dialect=postgresql.dialect()))
    assert "IN (SELECT broker_orders.id" not in sql
    assert "clearwait_dispatch_token" in str(select(model.id).where(predicate)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("kind", KINDS)
def test_sealed_count_disagreement_still_reopens_attempt(census, kind):
    session, _, _ = census
    journal, _ = seal_one(census)
    assert is_valid(session, journal.id) is True
    receipt = session.get(Retirement, journal.id)
    receipt.counts = {**receipt.counts, kind: receipt.counts[kind] + 1}
    session.flush()
    assert is_valid(session, journal.id) is False
    assert journal.id in {row.id for row in session.scalars(unresolved_query(ACCOUNTS))}


@pytest.mark.parametrize("change", ["missing_evidence", "extra_evidence", "changed_order",
    "missing_event", "late_fill"])
def test_evidence_membership_and_preimage_guards_remain_required(census, change):
    session, accounts, strategy = census
    journal, child = seal_one(census)
    if change == "missing_evidence":
        session.execute(delete(Evidence).where(Evidence.attempt_id == journal.id,
            Evidence.entity_kind == "order"))
    elif change == "extra_evidence":
        session.add(Evidence(attempt_id=journal.id, entity_kind="fill", entity_id=child.id,
            original_state="not-a-fill"))
    elif change == "changed_order":
        child.payload = {**child.payload, "clearwait_dispatch_token": "different-token"}
    elif change == "missing_event":
        session.execute(delete(BrokerOrderEvent).where(BrokerOrderEvent.order_id == child.id))
    else:
        session.add(Fill(order_id=child.id, strategy_id=strategy.id,
            broker_account_id=accounts[0].id, broker_fill_id="controlled-query-cost-late",
            symbol="WORX", side="buy", quantity=Decimal("0.5"), price=Decimal("3"),
            filled_at=datetime.now(UTC)))
    session.flush()
    assert is_valid(session, journal.id) is False
    assert journal.id in {row.id for row in session.scalars(unresolved_query(ACCOUNTS))}
