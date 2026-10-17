"""Disposable PG row-lock proof, not broker execution or throughput evidence."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier, get_ident
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from project_mai_tai.db.models import BrokerOrder, RiskCheck, TradeIntent, VirtualPosition
from project_mai_tai.oms.managed_sell_prewire import prepare_managed_sell
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_killsw1_postgres import pg as pg
from tests.unit.test_managed_sell_prewire import attempt


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,account_name", [
    ("schwab", "live:schwab_1m_v2"), ("webull", "live:orb"),
])
async def test_distinct_services_race_on_same_managed_row_commit_only_one_pending(pg, provider, account_name):
    engine, sessions, *_ = pg
    settings = Settings(_env_file=None, oms_adapter="paper", bot_control_enabled=False)
    first = OmsRiskService(settings, redis_client=None, session_factory=sessions,
                           broker_adapter=SimpleNamespace())
    second = OmsRiskService(settings, redis_client=None, session_factory=sessions,
                            broker_adapter=SimpleNamespace())
    with sessions.begin() as unit:
        strategy = first.store.ensure_strategy(unit, "schwab_1m_v2", name="ATR", execution_mode="live")
        account = first.store.ensure_broker_account(unit, account_name, provider=provider, environment="production")
        account.environment = "production"
        row = first.store.create_managed_position(unit, strategy_code=strategy.code,
            broker_account_name=account_name, symbol="RACE", entry_price=Decimal(5), quantity=10)
        unit.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
            symbol="RACE", quantity=Decimal(10), average_price=Decimal(5)))
        unit.flush()
        identity, strategy_id = str(row.id), strategy.id
    barrier = Barrier(2, timeout=5)
    arrivals = []

    def before_lock(connection, cursor, statement, parameters, context, executemany):
        if "oms_managed_positions" in statement and "FOR UPDATE" in statement:
            arrivals.append(get_ident())
            barrier.wait()

    event.listen(engine, "before_cursor_execute", before_lock)
    requests = []
    try:
        async def reserve(service):
            incoming, request = attempt(account_name)
            incoming.payload.symbol = "RACE"
            # OrderRequest is frozen; retain its actual field contract.
            from dataclasses import replace
            request = replace(request, symbol="RACE")
            requests.append(request)
            return await prepare_managed_sell(service, event=incoming, request=request,
                managed_row_id=identity, now=datetime.now(UTC))

        results = await asyncio.gather(reserve(first), reserve(second))
    finally:
        event.remove(engine, "before_cursor_execute", before_lock)
    assert len(arrivals) == 2 and len(set(arrivals)) == 2
    assert sum(value is not None for value in results) == 1
    with sessions() as unit:
        orders = unit.scalars(select(BrokerOrder).where(BrokerOrder.strategy_id == strategy_id)).all()
        intents = unit.scalars(select(TradeIntent).where(TradeIntent.strategy_id == strategy_id)).all()
        risks = unit.scalars(select(RiskCheck).where(RiskCheck.strategy_id == strategy_id)).all()
        assert len(orders) == len(intents) == len(risks) == 1
        assert orders[0].intent_id == intents[0].id
        assert orders[0].status == intents[0].status == "pending"
        assert orders[0].submitted_at is None and orders[0].broker_order_id is None
        assert orders[0].payload["oms_managed_row_id"] == identity
        assert orders[0].client_order_id in {request.client_order_id for request in requests}
