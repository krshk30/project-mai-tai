"""New exact held-identity query controls on disposable in-memory SQLite only."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from project_mai_tai.brokerdecides1_closed_owner import held_entry_ids
from project_mai_tai.db.models import Base, BrokerAccount, BrokerOrder, Strategy, TradeIntent


@pytest.fixture
def fixture():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[BrokerAccount.__table__, Strategy.__table__,
        TradeIntent.__table__, BrokerOrder.__table__])
    with Session(engine) as session:
        account = BrokerAccount(id=UUID(int=1), name="live:schwab_1m_v2", provider="schwab", environment="live")
        foreign = BrokerAccount(id=UUID(int=2), name="foreign", provider="webull", environment="live")
        strategy = Strategy(id=UUID(int=3), code="schwab_1m_v2", name="CONTROLLED")
        other = Strategy(id=UUID(int=4), code="orb", name="CONTROLLED")
        session.add_all([account, foreign, strategy, other])
        session.flush()
        for number, symbol, opportunity, acct, strat, side in [
            (11, "CLRO", "1792750000000", account, strategy, "buy"),
            (12, "CLRO", "1792750000001", account, strategy, "buy"),
            (13, "OTHER", "1792750000000", account, strategy, "buy"),
            (14, "CLRO", "1792750000000", foreign, strategy, "buy"),
            (15, "CLRO", "1792750000000", account, other, "buy"),
            (16, "CLRO", "1792750000000", account, strategy, "sell"),
        ]:
            session.add(BrokerOrder(id=UUID(int=number), strategy_id=strat.id, broker_account_id=acct.id,
                client_order_id=str(number), symbol=symbol, side=side, order_type="stop_limit",
                time_in_force="day", quantity=195, status="filled",
                submitted_at=datetime(2026, 10, 20, 12, tzinfo=UTC),
                payload={"fanout_segment_id": opportunity}))
        session.commit()
        yield session
    engine.dispose()


def test_only_exact_held_older_buy_is_selected_not_same_symbol_foreign_or_other_op(fixture):
    assert fixture.scalars(held_entry_ids(("live:schwab_1m_v2",), {"CLRO": 1792750000000})).all() == [UUID(int=11)]


def test_empty_held_scope_never_means_all_old_orders(fixture):
    assert fixture.scalars(held_entry_ids(("live:schwab_1m_v2",), {})).all() == []


def test_nested_entry_intent_subquery_keeps_own_from_scope():
    entry_ids = held_entry_ids(("live:schwab_1m_v2",), {"CLRO": 1792750000000})
    intents = select(BrokerOrder.intent_id).where(BrokerOrder.id.in_(entry_ids)).correlate(None)
    statement = select(BrokerOrder.id).where(BrokerOrder.intent_id.in_(intents))
    compiled = statement.compile(dialect=postgresql.dialect())
    assert str(compiled).count("FROM broker_orders") == 3
    assert "JOIN broker_accounts" in str(compiled) and "JOIN strategies" in str(compiled)
    assert "1792750000000" in compiled.params.values()
    assert "schwab_1m_v2" in compiled.params.values()


@pytest.mark.parametrize("value", [{"clro": 1792750000000}, {"CLRO": True}, {"CLRO": 0},
    {"CLRO": "1792750000000"}, {str(number): number + 1 for number in range(513)}])
def test_ambiguous_or_overflow_held_scope_refused(value):
    with pytest.raises(ValueError):
        held_entry_ids(("live:schwab_1m_v2",), value)
