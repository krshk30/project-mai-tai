"""Additional controlled SQLite proof edges; no native/timing qualification."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.brokerdecides1_retirement import KINDS, valid_retirement, validated_retirement_ids
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.db.models import (
    BrokerDecidesDispatchRetirement as Retirement, BrokerOrder, DashboardSnapshot, Fill, OmsManagedPosition,
)
from tests.unit.test_brokerdecides1_install_boot_ruling import installed_case
from tests.unit.test_brokerdecides1_local_census import ACCOUNTS, census
from tests.unit.test_brokerdecides1_retirement import seal_one


def assert_invalid(session, identity):
    assert session.scalar(select(valid_retirement()).select_from(DashboardSnapshot)
        .where(DashboardSnapshot.id == identity)) is False
    assert identity not in set(session.scalars(validated_retirement_ids()))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", ["null", "missing"])
def test_null_or_missing_sealed_count_never_proves_valid(census, kind, mode):
    session, _, _ = census
    journal, _ = seal_one(census)
    receipt = session.get(Retirement, journal.id)
    counts = dict(receipt.counts)
    if mode == "null":
        counts[kind] = None
    else:
        counts.pop(kind)
    receipt.counts = counts
    session.flush()
    assert_invalid(session, journal.id)


@pytest.mark.parametrize("field", ["attempt_token", "symbol", "account_name", "proof_version"])
def test_changed_attempt_or_receipt_identity_is_not_a_cache_grant(census, field):
    session, _, _ = census
    journal, _ = seal_one(census)
    if field == "proof_version":
        session.get(Retirement, journal.id).proof_version = 999
    else:
        journal.payload = {**journal.payload, field: "changed-identity"}
    session.flush()
    assert_invalid(session, journal.id)


def test_new_managed_row_is_part_of_live_set(census):
    session, _, _ = census
    journal, child = seal_one(census)
    session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=ACCOUNTS[0],
        symbol="WORX", entry_order_id=child.id, entry_price=Decimal("3"),
        original_quantity=1, current_quantity=1))
    session.flush()
    assert_invalid(session, journal.id)


def test_replacement_matching_both_client_paths_still_invalidates(census):
    session, _, _ = census
    journal, child = seal_one(census)
    session.add(DashboardSnapshot(snapshot_type="atr_reprice_handoff", payload={
        "old": {"client_order_id": child.client_order_id},
        "replacement": {"client_order_id": child.client_order_id}, "phase": "unknown"}))
    session.flush()
    assert_invalid(session, journal.id)


def test_missing_source_order_reopens_all_its_memberships(census):
    session, _, _ = census
    journal, child = seal_one(census)
    session.delete(child)
    session.flush()
    assert_invalid(session, journal.id)


def test_cross_account_order_reparent_cannot_preserve_proof(census):
    session, accounts, _ = census
    journal, child = seal_one(census)
    child.broker_account_id = accounts[1].id
    session.flush()
    assert_invalid(session, journal.id)
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})


def test_closed_old_and_unknown_worx_are_distinct_symbols_then_late_fill_reactivates_old(census):
    session, _, strategy = census
    unknown_id, _ = installed_case(census)
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})
    child = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "OLD").limit(1))
    session.add(Fill(order_id=child.id, strategy_id=strategy.id, broker_account_id=child.broker_account_id,
        broker_fill_id="runtime-late-old", symbol="OLD", side="buy", quantity=Decimal("0.5"),
        price=Decimal("3"), filled_at=datetime.now(UTC)))
    session.flush()
    inflight, _, _ = local_evidence(session, ACCOUNTS)
    assert inflight == frozenset({"WORX", "OLD"})
    assert "UNRELATED" not in inflight
    assert session.get(Retirement, unknown_id) is None
