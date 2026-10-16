"""Manual holdings are log-only; proven foreign bot owners still fence entries."""
from datetime import UTC, datetime
from decimal import Decimal
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import (
    AccountPosition, Base, BrokerAccount, BrokerOrder, OmsManagedPosition,
    Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms.route_owner import route_owner_conflict


@pytest.fixture(params=["schwab_1m_v2", "orb_schwab", "momentum_warn_live"])
def owners(request):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
        if table.name not in {"market_trade_ticks", "market_quote_ticks"}])
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as unit:
        incoming = Strategy(code=request.param, name="incoming", execution_mode="live")
        other = Strategy(code="momentum_warn_live" if request.param != "momentum_warn_live" else "schwab_1m_v2",
                         name="foreign bot", execution_mode="live")
        unit.add_all([incoming, other])
        unit.flush()
        state = SimpleNamespace(sessions=sessions, incoming=incoming.id, code=incoming.code,
                                other=other.id, other_code=other.code)
    yield state
    engine.dispose()


def account(unit, provider):
    row = BrokerAccount(name="live:schwab_1m_v2" if provider == "schwab" else "live:orb",
                        provider=provider, environment="production")
    unit.add(row)
    unit.flush()
    return row


def conflict(unit, row, code):
    return route_owner_conflict(unit, account_id=row.id, account_name=row.name,
                                symbol="ABC", incoming_strategy=code)


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("net,own", [(1000, None), (1000, 0), (3000, 0), (1010, 10), (10, 0)])
def test_manual_lots_and_lagging_own_vp_are_log_only(owners, provider, net, own, caplog):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        unit.add(AccountPosition(broker_account_id=row.id, symbol="ABC",
                                 quantity=Decimal(net), average_price=Decimal(5)))
        if own is not None:
            unit.add(VirtualPosition(strategy_id=owners.incoming, broker_account_id=row.id,
                                     symbol="ABC", quantity=Decimal(own), average_price=Decimal(5)))
        unit.flush()
        with caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
            assert conflict(unit, row, owners.code) is None
    records = [record.getMessage() for record in caplog.records
               if "[ROUTE-OWNER-MANUAL-PRESENT]" in record.getMessage()]
    assert len(records) == 1
    assert f"incoming={owners.code}" in records[0]
    assert f"net={net}" in records[0] and f"own_vp={own or 0}" in records[0]
    assert "log_only=1" in records[0]


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("proof", ["vp", "managed", "working", "reservation"])
def test_manual_lot_does_not_hide_a_proven_foreign_strategy(owners, provider, proof):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        unit.add(AccountPosition(broker_account_id=row.id, symbol="ABC",
                                 quantity=Decimal(1000), average_price=Decimal(5)))
        if proof == "vp":
            unit.add(VirtualPosition(strategy_id=owners.other, broker_account_id=row.id,
                                     symbol="ABC", quantity=Decimal(10), average_price=Decimal(5)))
        elif proof == "managed":
            unit.add(OmsManagedPosition(strategy_code=owners.other_code, broker_account_name=row.name,
                symbol="ABC", entry_price=Decimal(5), original_quantity=10, current_quantity=10,
                status="open"))
        elif proof == "working":
            unit.add(BrokerOrder(strategy_id=owners.other, broker_account_id=row.id, symbol="ABC",
                side="buy", quantity=Decimal(10), client_order_id="foreign-rest", broker_order_id="foreign-root",
                status="working", order_type="limit", time_in_force="day"))
        else:
            unit.add(TradeIntent(strategy_id=owners.other, broker_account_id=row.id, symbol="ABC",
                side="buy", intent_type="open", quantity=Decimal(10), reason="foreign reservation",
                status="pending", created_at=datetime.now(UTC)))
        unit.flush()
        assert conflict(unit, row, owners.code) == owners.other_code


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_flat_and_terminal_foreign_history_does_not_become_a_live_owner(owners, provider):
    with owners.sessions.begin() as unit:
        row = account(unit, provider)
        unit.add_all([
            AccountPosition(broker_account_id=row.id, symbol="ABC", quantity=Decimal(1000), average_price=Decimal(5)),
            VirtualPosition(strategy_id=owners.other, broker_account_id=row.id, symbol="ABC",
                            quantity=Decimal(0), average_price=Decimal(5)),
            OmsManagedPosition(strategy_code=owners.other_code, broker_account_name=row.name,
                symbol="ABC", entry_price=Decimal(5), original_quantity=10, current_quantity=0, status="closed"),
            BrokerOrder(strategy_id=owners.other, broker_account_id=row.id, symbol="ABC", side="buy",
                quantity=Decimal(10), client_order_id="old-foreign", broker_order_id="old-root",
                status="cancelled", order_type="limit", time_in_force="day"),
            TradeIntent(strategy_id=owners.other, broker_account_id=row.id, symbol="ABC", side="buy",
                intent_type="open", quantity=Decimal(10), reason="old reservation", status="completed"),
        ])
        unit.flush()
        assert conflict(unit, row, owners.code) is None
