"""Qualify managed SELL recovery before its additional ORM lookups."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

from project_mai_tai.db.models import BrokerAccount, Strategy
from project_mai_tai.oms import service as module
from project_mai_tai.oms.service import OmsRiskService


NOW = datetime(2026, 10, 15, 14, tzinfo=UTC)


@pytest.fixture(autouse=True)
def controlled_clock(monkeypatch):
    monkeypatch.setattr(module, "utcnow", lambda: NOW)


def refresh_case(side, payload):
    service = object.__new__(OmsRiskService)
    service.logger = logging.getLogger(__name__)
    service._rpg_owns_old_order = lambda *_args: False

    async def no_refresh(**_kwargs):
        return None

    service._build_refreshed_order_metadata = no_refresh
    order = SimpleNamespace(side=side, payload=payload, quantity=Decimal(1), symbol="ABC",
        strategy_id=uuid4(), broker_account_id=uuid4(), client_order_id="exact-existing-order",
        submitted_at=NOW - timedelta(days=1))
    intent = SimpleNamespace(reason="existing-exit", intent_type="open" if side == "buy" else "close")
    return service, order, intent


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_code,side,payload", [
    ("schwab_1m_v2", "buy", {"managed_sell_prepared_at": "old"}),
    ("orb_schwab", "buy", {"managed_sell_prepared_at": "old"}),
    ("schwab_1m_v2", "sell", {}),
    ("orb_schwab", "sell", {"oms_v2_managed_exit": "true"}),
    ("foreign_strategy", "sell", {"managed_sell_prepared_at": "old"}),
])
async def test_irrelevant_refresh_does_not_query_managed_sell_scope(strategy_code, side, payload):
    service, order, intent = refresh_case(side, payload)

    class NoScopeReads:
        def get(self, *_args):
            pytest.fail("non-qualifying refresh queried managed SELL scope")

    result = await service._refresh_working_order(session=NoScopeReads(), order=order, intent=intent,
        strategy_code=strategy_code, broker_account_name="live:orb",
        report=SimpleNamespace(filled_quantity=Decimal(0)))
    assert result == {"orders": 0, "terminal_orders": 0, "published_events": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_code,payload", [
    ("schwab_1m_v2", {"oms_v2_managed_exit": "true"}),
    ("schwab_1m_v2", {"managed_sell_prepared_at": "old"}),
    ("orb_schwab", {"managed_sell_prepared_at": "old"}),
])
async def test_qualifying_old_sell_still_queries_scope_and_blocks(strategy_code, payload, caplog):
    service, order, intent = refresh_case("sell", payload)
    reads = []

    class UnknownScope:
        def get(self, model, identity):
            reads.append((model, identity))
            return None

    result = await service._refresh_working_order(session=UnknownScope(), order=order, intent=intent,
        strategy_code=strategy_code, broker_account_name="live:orb",
        report=SimpleNamespace(filled_quantity=Decimal(0)))
    assert reads == [(Strategy, order.strategy_id), (BrokerAccount, order.broker_account_id)]
    assert result == {"orders": 0, "terminal_orders": 0, "published_events": []}
    assert "MANAGED-SELL-OLD-REFRESH-BLOCKED" in caplog.text
