"""Previous strategy sessions never lend stale order/reservation ownership."""
from datetime import UTC, datetime, timedelta
import logging
from decimal import Decimal
from uuid import UUID

import pytest

from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition, TradeIntent, VirtualPosition
from project_mai_tai.oms.route_owner import route_owner_conflict
from tests.unit.test_killsw1_manual_admission import account, owners as owners


NOW = datetime(2026, 10, 16, 14, tzinfo=UTC)
START = NOW.replace(hour=8)
SAME = object()


def add_row(unit, owners, row, proof, created, *, submitted=SAME, ordinal=1, parent_owner=None):
    parent = None
    if created is not None:
        parent = TradeIntent(id=UUID(int=ordinal), strategy_id=parent_owner or owners.other,
            broker_account_id=row.id, symbol="ABC", side="buy", intent_type="open",
            quantity=Decimal(10), reason="retained foreign reservation",
            status="pending" if proof == "reservation" else "filled",
            created_at=created, updated_at=NOW)
        unit.add(parent)
        unit.flush()
    if proof == "reservation":
        return parent
    order = BrokerOrder(id=UUID(int=ordinal), intent_id=parent.id if parent is not None else None,
        strategy_id=owners.other, broker_account_id=row.id, symbol="ABC", side="buy",
        quantity=Decimal(10), client_order_id=f"foreign-{ordinal}", broker_order_id=f"root-{ordinal}",
        status="working", order_type="limit", time_in_force="day",
        submitted_at=created if submitted is SAME else submitted, updated_at=NOW)
    unit.add(order)
    unit.flush()
    return order


def check(unit, row, owners, now=NOW):
    unit.flush()
    return route_owner_conflict(unit, account_id=row.id, account_name=row.name,
                                symbol="ABC", incoming_strategy=owners.code, now=now)


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("proof", ["working", "reservation"])
def test_previous_session_row_is_log_only_despite_recent_update(owners, provider, proof, caplog):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        previous = add_row(unit, owners, row, proof, START - timedelta(seconds=1))
        with caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
            assert check(unit, row, owners) is None
        assert previous.status == ("working" if proof == "working" else "pending")
    logs = [record.getMessage() for record in caplog.records if "[ROUTE-OWNER-STALE-ROW]" in record.getMessage()]
    assert len(logs) == 1
    assert all(text in logs[0] for text in (f"account={row.name}", "symbol=ABC",
        f"owner={owners.other_code}", f"row_id={previous.id}", "age_s=21601.000"))


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("proof", ["working", "reservation"])
@pytest.mark.parametrize("created", [START, NOW - timedelta(hours=1)])
def test_same_session_boundary_and_stuck_row_still_refuse(owners, provider, proof, created, caplog):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        add_row(unit, owners, row, proof, created)
        with caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
            assert check(unit, row, owners) == owners.other_code
    assert not any("[ROUTE-OWNER-STALE-ROW]" in record.getMessage()
                   or "[ROUTE-OWNER-UNANCHORED-ROW]" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("proof", ["working", "reservation"])
@pytest.mark.parametrize("now,refuses", [
    (START - timedelta(seconds=1), True), (START, False),
    (datetime(2026, 11, 1, 7, 30, tzinfo=UTC), True),
    (datetime(2026, 11, 1, 9, tzinfo=UTC), False),
])
def test_04et_roll_and_dst_use_strategy_session_not_calendar_date(owners, provider, proof, now, refuses):
    created = (datetime(2026, 10, 31, 12, tzinfo=UTC) if now.month == 11
               else START - timedelta(minutes=10))
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        add_row(unit, owners, row, proof, created)
        assert check(unit, row, owners, now) == (owners.other_code if refuses else None)


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("created,submitted,refuses", [
    (START - timedelta(days=1), NOW - timedelta(minutes=1), True),
    (NOW - timedelta(minutes=1), START - timedelta(days=1), False),
    (NOW + timedelta(minutes=1), None, True),
])
def test_own_submission_precedes_exact_bound_intent_fallback(owners, provider, created, submitted, refuses):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        add_row(unit, owners, row, "working", created, submitted=submitted)
        assert check(unit, row, owners) == (owners.other_code if refuses else None)


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("old", [True, False])
def test_unsubmitted_order_uses_exact_bound_intent_anchor(owners, provider, old):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        add_row(unit, owners, row, "working", START - timedelta(seconds=1) if old else START,
                submitted=None)
        assert check(unit, row, owners) == (None if old else owners.other_code)


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("binding", ["absent", "foreign-parent"])
def test_unknown_age_or_mismatched_parent_keeps_refusal_and_warning(owners, provider, binding, caplog):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        add_row(unit, owners, row, "working", None if binding == "absent" else START - timedelta(days=1),
                submitted=None, parent_owner=owners.incoming if binding == "foreign-parent" else None)
        with caplog.at_level(logging.WARNING, logger="project_mai_tai.oms.route_owner"):
            assert check(unit, row, owners) == owners.other_code
    logs = [record for record in caplog.records if "[ROUTE-OWNER-UNANCHORED-ROW]" in record.getMessage()]
    assert len(logs) == 1 and logs[0].levelno == logging.WARNING
    assert all(text in logs[0].getMessage() for text in (f"account={row.name}", "symbol=ABC",
        f"owner={owners.other_code}", f"row_id={UUID(int=1)}", "refusal_kept=1"))


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("proof", ["working", "reservation"])
def test_stale_first_row_does_not_hide_current_foreign_row(owners, provider, proof):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        add_row(unit, owners, row, proof, START - timedelta(days=1), ordinal=1)
        add_row(unit, owners, row, proof, START, ordinal=2)
        assert check(unit, row, owners) == owners.other_code


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("proof", ["vp", "managed"])
def test_nonzero_vp_and_open_managed_proofs_are_not_age_filtered(owners, provider, proof):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        add_row(unit, owners, row, "working", START - timedelta(days=1))
        if proof == "vp":
            unit.add(VirtualPosition(strategy_id=owners.other, broker_account_id=row.id,
                symbol="ABC", quantity=Decimal(3), average_price=Decimal(5)))
        else:
            unit.add(OmsManagedPosition(strategy_code=owners.other_code, broker_account_name=row.name,
                symbol="ABC", entry_price=Decimal(5), original_quantity=3, current_quantity=3,
                status="open", created_at=START - timedelta(days=30)))
        assert check(unit, row, owners) == owners.other_code


@pytest.mark.parametrize("owners", ["schwab_1m_v2", "orb_schwab"], indirect=True)
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("proof", ["working", "reservation"])
def test_same_day_stuck_bbci_momentum_row_remains_a_foreign_guard(owners, provider, proof):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        assert owners.other_code == "momentum_warn_live"
        retained = add_row(unit, owners, row, proof, NOW.replace(hour=13, minute=31), submitted=None)
        retained.symbol, retained.quantity = "BBCI", Decimal(25)
        retained.updated_at = NOW.replace(hour=15, minute=1, second=24)
        if proof == "working":
            parent = unit.get(TradeIntent, retained.intent_id)
            parent.symbol, parent.side, parent.intent_type = "BBCI", "sell", "close"
            parent.quantity, parent.status = Decimal(25), "pending"
            retained.side = "sell"
        unit.flush()
        assert route_owner_conflict(unit, account_id=row.id, account_name=row.name,
            symbol="BBCI", incoming_strategy=owners.code,
            now=NOW.replace(minute=34)) == "momentum_warn_live"
