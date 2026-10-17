from datetime import UTC, datetime
from decimal import Decimal

import pytest

from project_mai_tai.reconciliation.ownership import coherent_owned_quantity
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, Strategy, VirtualPosition,
)
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.settings import Settings
from tests.unit.test_reconciliation_service import FakeRedis, build_test_session_factory


def position_findings(*, provider, hour, broker_quantity=102, fill_quantity=2, second_conflict=False,
                      book_quantity=2, negative_virtual=False, managed_quantity=0):
    factory = build_test_session_factory()
    stamp = datetime(2026, 10, 16, hour, 0, tzinfo=UTC)
    with factory() as session:
        account = BrokerAccount(name=f"live:{provider}", provider=provider, environment="production")
        strategy = Strategy(code="schwab_1m_v2", name="ATR", execution_mode="live", metadata_json={})
        session.add_all([account, strategy])
        session.flush()
        if book_quantity:
            session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                symbol="TEST", quantity=Decimal(book_quantity), average_price=Decimal(3), realized_pnl=Decimal(0)))
        if managed_quantity:
            session.add(OmsManagedPosition(strategy_code=strategy.code, broker_account_name=account.name,
                symbol="TEST", entry_price=Decimal(3), original_quantity=managed_quantity,
                current_quantity=managed_quantity, entry_time=stamp, current_profit_pct=Decimal(0),
                peak_profit_pct=Decimal(0), status="open"))
        session.add(AccountPosition(broker_account_id=account.id, symbol="TEST",
            quantity=Decimal(broker_quantity), average_price=Decimal(9), source_updated_at=stamp))
        if fill_quantity:
            order = BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id,
                client_order_id="synthetic-owned", broker_order_id="synthetic-native", symbol="TEST",
                side="buy", order_type="limit", time_in_force="day", quantity=Decimal(fill_quantity),
                status="filled", payload={}, submitted_at=stamp, updated_at=stamp)
            session.add(order)
            session.flush()
            session.add(Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
                broker_fill_id="synthetic-fill", symbol="TEST", side="buy",
                quantity=Decimal(fill_quantity), price=Decimal(3), filled_at=stamp, payload={}))
        if second_conflict or negative_virtual:
            other = Strategy(code="orb_schwab", name="ORB", execution_mode="live", metadata_json={})
            session.add(other)
            session.flush()
            session.add(VirtualPosition(strategy_id=other.id, broker_account_id=account.id,
                symbol="TEST", quantity=Decimal(-1 if negative_virtual else 1), average_price=Decimal(3), realized_pnl=Decimal(0)))
        session.commit()
    service = ReconciliationService(settings=Settings(), redis_client=FakeRedis(), session_factory=factory)
    with factory() as session:
        return service._build_position_findings(session)


@pytest.mark.parametrize("hour", [12, 15, 21], ids=["PM", "RTH", "AH"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_manual_surplus_does_not_conceal_shortage_or_conflict(hour, provider):
    manual = position_findings(provider=provider, hour=hour)
    assert {row.finding_type for row in manual} == {"position_quantity_mismatch", "average_price_mismatch"}
    assert all(row.severity == "info" for row in manual)
    basis = next(row for row in manual if row.finding_type == "average_price_mismatch")
    assert basis.payload["bot_broker_average_price"] is None
    assert basis.payload["basis_status"] == "UNKNOWN_mixed_manual"
    assert basis.payload["account_average_price"] == "9.00000000"
    for quantities in (
        dict(broker_quantity=1), dict(fill_quantity=3), dict(fill_quantity=0),
        dict(second_conflict=True),
    ):
        findings = position_findings(provider=provider, hour=hour, **quantities)
        quantity_findings = [row for row in findings if row.finding_type == "position_quantity_mismatch"]
        assert len(quantity_findings) == 1
        assert quantity_findings[0].severity == "critical"


@pytest.mark.parametrize("hour", [12, 15, 21], ids=["PM", "RTH", "AH"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_owned_fill_without_books_retains_original_critical_direction(hour, provider):
    findings = position_findings(provider=provider, hour=hour, broker_quantity=1,
                                fill_quantity=1, book_quantity=0)
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].payload["direction"] == "broker_position_untracked_by_live_books"
    assert findings[0].payload["ownership"] == "ours_or_conflicting"


@pytest.mark.parametrize("hour", [12, 15, 21], ids=["PM", "RTH", "AH"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_claimed_uncovered_other_strategy_remains_critical_conflict(hour, provider):
    findings = position_findings(provider=provider, hour=hour, second_conflict=True)
    quantity = [row for row in findings if row.finding_type == "position_quantity_mismatch"]
    assert len(quantity) == 1
    assert quantity[0].severity == "critical"
    # TOP21:1x B6: retain the PRE-D category, not a new per-strategy CRITICAL.
    assert quantity[0].payload["direction"] == "broker_more_than_net_fills"


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_negative_virtual_does_not_change_old_count_or_authorize_manual_info(provider):
    rows = position_findings(provider=provider, hour=15, negative_virtual=True)
    quantity = next(row for row in rows if row.finding_type == "position_quantity_mismatch")
    assert quantity.payload["virtual_quantity"] == "2.00000000"
    assert quantity.severity == "critical"
    assert quantity.payload["direction"] == "broker_more_than_net_fills"


def test_offsetting_bot_balances_are_not_manual_proof():
    assert coherent_owned_quantity(
        virtual={"atr": Decimal(2), "orb": Decimal(2)}, managed={},
        fills={"atr": Decimal(3), "orb": Decimal(1)}, tolerance=Decimal(0),
    ) is None


@pytest.mark.parametrize("invalid", [Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_unknown_negative_nonfinite_strategy_never_proves_manual(invalid):
    assert coherent_owned_quantity(
        virtual={"atr": Decimal(2)}, managed={}, fills={"atr": invalid},
        tolerance=Decimal(0),
    ) is None


def test_false_zero_vp_retains_existing_managed_fill_coverage():
    assert coherent_owned_quantity(
        virtual={}, managed={"atr": Decimal(2)}, fills={"atr": Decimal(2)},
        tolerance=Decimal(0),
    ) == 2
    assert coherent_owned_quantity(
        virtual={}, managed={"atr": Decimal(2)}, fills={"atr": Decimal(2)},
        tolerance=Decimal(0), require_positive_virtual=True,
    ) is None


@pytest.mark.parametrize("hour", [12, 15, 21], ids=["PM", "RTH", "AH"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_false_zero_virtual_managed_fill_match_is_not_manual_surplus_proof(provider, hour):
    rows = position_findings(provider=provider, hour=hour, broker_quantity=5,
                            book_quantity=0, managed_quantity=2, fill_quantity=2)
    quantity = next(row for row in rows if row.finding_type == "position_quantity_mismatch")
    assert quantity.severity == "critical"
    assert quantity.payload["direction"] == "broker_more_than_net_fills"
    coherent = position_findings(provider=provider, hour=hour, broker_quantity=5,
                                book_quantity=2, managed_quantity=2, fill_quantity=2)
    assert all(row.severity == "info" for row in coherent)


def test_unknown_strategy_and_conflicting_nonzero_books_do_not_prove_manual():
    assert coherent_owned_quantity(
        virtual={"": Decimal(2)}, managed={}, fills={"": Decimal(2)},
        tolerance=Decimal(0),
    ) is None
    assert coherent_owned_quantity(
        virtual={"atr": Decimal(1)}, managed={"atr": Decimal(2)},
        fills={"atr": Decimal(2)}, tolerance=Decimal(0),
    ) is None
