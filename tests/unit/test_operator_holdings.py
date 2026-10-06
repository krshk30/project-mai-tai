from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Fill, Strategy
from project_mai_tai.operator_holdings import operator_holding_basis, trading_session_start
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.settings import Settings
from tests.unit.test_reconciliation_service import FakeRedis, build_test_session_factory


NOW = datetime(2026, 10, 6, 19, 22, 35, tzinfo=UTC)


def evidence(**changes):
    result = dict(
        account_name="live:schwab_1m_v2", symbol="IPDN", now=NOW,
        broker_quantity=Decimal("1000"), virtual_quantity=Decimal("0"),
        managed_quantity=Decimal("0"), net_fill_balance=Decimal("0"),
        session_orders=0, session_fills=0, pending_intents=0, unowned_sells=0,
        source_fresh=True, activity_complete=True,
    )
    result.update(changes)
    return result


def test_zero_orders_and_zero_fills_not_net_zero_is_operator_only():
    assert operator_holding_basis(**evidence()) == "zero_session_bot_orders_and_fills"
    assert operator_holding_basis(**evidence(symbol="MOBX", session_orders=2, session_fills=2)) is None


@pytest.mark.parametrize("change", [
    {"session_orders": 1}, {"session_fills": 1}, {"pending_intents": 1},
    {"unowned_sells": 1}, {"source_fresh": False}, {"activity_complete": False},
    {"managed_quantity": Decimal("1")}, {"virtual_quantity": Decimal("1")},
    {"net_fill_balance": Decimal("180")}, {"broker_quantity": Decimal("0")},
    {"broker_quantity": Decimal("NaN")}, {"session_orders": -1},
    {"account_name": "paper:schwab_1m_v2"},
    {"open_virtual_rows": 1}, {"open_managed_rows": 1}, {"pending_orders": 1},
])
def test_unproven_or_bot_activity_does_not_get_general_allowance(change):
    assert operator_holding_basis(**evidence(symbol="MOBX", **change)) is None


def test_ipdn_exact_item_is_dated_not_a_net_zero_shortcut():
    recorded = evidence(session_orders=2, session_fills=2)
    assert operator_holding_basis(**recorded) == "operator_2026_10_06_ipdn_1000_exact_item"
    for change in (
        {"symbol": "APUS"}, {"account_name": "live:orb"},
        {"broker_quantity": Decimal("1127")}, {"broker_quantity": Decimal("999")},
        {"now": datetime(2026, 10, 7, 19, tzinfo=UTC)},
        {"net_fill_balance": Decimal("127")}, {"session_fills": 0},
        {"unowned_sells": 1}, {"pending_intents": 1},
    ):
        assert operator_holding_basis(**(recorded | change)) is None


def test_session_anchor_is_four_et_including_before_anchor_and_dst():
    assert trading_session_start(NOW) == datetime(2026, 10, 6, 8, tzinfo=UTC)
    assert trading_session_start(datetime(2026, 10, 6, 7, tzinfo=UTC)) == datetime(2026, 10, 5, 8, tzinfo=UTC)
    assert trading_session_start(datetime(2026, 12, 6, 19, tzinfo=UTC)) == datetime(2026, 12, 6, 9, tzinfo=UTC)


@pytest.mark.parametrize("account_name", ["live:schwab_1m_v2", "live:orb"])
@pytest.mark.parametrize("shape", ["operator", "round_trip", "aborted", "unowned_sell", "stale"])
def test_real_reconciliation_path_never_confuses_round_trip_or_sell_with_operator(monkeypatch, account_name, shape):
    monkeypatch.setattr("project_mai_tai.reconciliation.service.utcnow", lambda: NOW)
    factory = build_test_session_factory()
    with factory() as session:
        account = BrokerAccount(name=account_name, provider="schwab", environment="production")
        strategy = Strategy(code="schwab_1m_v2", name="ATR", execution_mode="live", metadata_json={})
        session.add_all([account, strategy])
        session.flush()
        session.add(AccountPosition(
            broker_account_id=account.id, symbol="MOBX", quantity=Decimal("1000"),
            average_price=Decimal("1"), market_value=Decimal("1000"),
            source_updated_at=NOW if shape != "stale" else datetime(2026, 10, 6, 18, tzinfo=UTC),
        ))
        if shape in {"round_trip", "aborted", "unowned_sell"}:
            sides = ["buy", "sell"] if shape == "round_trip" else ["sell" if shape == "unowned_sell" else "buy"]
            for i, side in enumerate(sides):
                order = BrokerOrder(
                    broker_account_id=account.id, strategy_id=strategy.id,
                    client_order_id=f"recorded-{shape}-{i}", symbol="MOBX", side=side,
                    order_type="limit", time_in_force="day", quantity=Decimal("248"),
                    status="accepted" if shape == "unowned_sell" else "aborted" if shape == "aborted" else "filled",
                    payload={}, submitted_at=None if shape == "aborted" else NOW, updated_at=NOW,
                )
                session.add(order)
                session.flush()
                if shape == "round_trip":
                    session.add(Fill(
                        order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
                        broker_fill_id=f"fill-{i}", symbol="MOBX", side=side,
                        quantity=Decimal("248"), price=Decimal("1.21"), filled_at=NOW, payload={},
                    ))
        session.commit()
    service = ReconciliationService(Settings(), FakeRedis(), session_factory=factory)
    with factory() as session:
        findings = service._build_position_findings(session)
        if shape == "operator":
            assert findings == []
        else:
            assert len(findings) == 1
            assert findings[0].severity == "critical"
            assert findings[0].payload["direction"] == (
                "unowned_sell" if shape == "unowned_sell" else "broker_ownership_unproven"
            )
        assert session.scalar(select(AccountPosition)).quantity == Decimal("1000")
