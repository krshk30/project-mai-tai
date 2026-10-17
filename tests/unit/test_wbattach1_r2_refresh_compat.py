"""R2 preserves actual non-managed replacement while refusing false managed tags."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition, TradeIntent, VirtualPosition
from project_mai_tai.oms import managed_sell_prewire as prewire, service as module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_oms_risk_service import (
    FakeRedis, FakeWorkingOrderRefreshBrokerAdapter, build_test_session_factory,
)


NOW = datetime(2026, 10, 16, 17, tzinfo=UTC)


@pytest.fixture(autouse=True)
def controlled_clock(monkeypatch):
    monkeypatch.setattr(module, "utcnow", lambda: NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["legacy", "unknown-managed", "empty-managed", "unknown-prepared"])
async def test_real_paper_refresh_only_untagged_nonmanaged_uses_legacy_wire(monkeypatch, binding):
    monkeypatch.setattr(module, "_extended_hours_session", lambda now=None: None)
    factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter(bid_price=5.00, ask_price=5.02)
    service = OmsRiskService(Settings(_env_file=None, oms_adapter="simulated", redis_stream_prefix="test"),
        redis_client=FakeRedis(), session_factory=factory, broker_adapter=adapter)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("nonmanaged or unknown-bound replacement must not manufacture a managed preparation")

    monkeypatch.setattr(prewire, "prepare_managed_sell", forbidden)
    account_name = "paper:schwab_1m_v2"
    payload = {"oms_v2_managed_exit": "true", "order_type": "limit", "time_in_force": "day",
               "limit_price": "4.88", "reference_price": "4.88", "price_source": "bid", "session": "AM"}
    if binding == "unknown-managed":
        payload["oms_managed_row_id"] = str(uuid4())
    elif binding == "empty-managed":
        payload["oms_managed_row_id"] = None
    elif binding == "unknown-prepared":
        payload["managed_sell_prepared_at"] = NOW.isoformat()
    with factory.begin() as unit:
        strategy = service.store.ensure_strategy(unit, "schwab_1m_v2", execution_mode="paper")
        account = service.store.ensure_broker_account(unit, account_name,
            provider="schwab", environment="development")
        intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id, symbol="WHLR",
            side="sell", intent_type="close", quantity=Decimal(2), reason="oms_v2_managed_exit:CW_FLOOR",
            status="submitted", payload={"metadata": {}})
        unit.add(intent)
        unit.flush()
        order = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id="schwab_1m_v2-WHLR-close-r2", broker_order_id="ord-123", symbol="WHLR",
            side="sell", order_type="limit", time_in_force="day", quantity=Decimal(2), status="accepted",
            payload=payload, submitted_at=NOW - timedelta(seconds=1), updated_at=NOW)
        unit.add(order)
        unit.flush()
        intent_id, order_id = intent.id, order.id
    with factory() as unit:
        result = await service._refresh_working_order(session=unit, order=unit.get(BrokerOrder, order_id),
            intent=unit.get(TradeIntent, intent_id), strategy_code="schwab_1m_v2", broker_account_name=account_name,
            report=ExecutionReport("accepted", "schwab_1m_v2-WHLR-close-r2", broker_order_id="ord-123",
                symbol="WHLR", side="sell", intent_type="close", quantity=Decimal(2), origin="broker",
                reported_at=NOW))
        unit.commit()
    expected = ["cancel", "close"] if binding == "legacy" else ["cancel"]
    assert [request.intent_type for request in adapter.submit_requests] == expected
    assert result["orders"] == (1 if binding == "legacy" else 0)
    assert result["terminal_orders"] == 1
    with factory() as unit:
        assert unit.scalar(select(OmsManagedPosition)) is None
        assert unit.scalar(select(VirtualPosition)) is None
        orders = unit.scalars(select(BrokerOrder).where(BrokerOrder.symbol == "WHLR")).all()
        assert len(orders) == (2 if binding == "legacy" else 1)
        if binding == "legacy":
            replacement = next(order for order in orders if order.id != order_id)
            assert replacement.status == "accepted" and replacement.quantity == 2
            assert replacement.payload["session"] == "NORMAL"
            assert "managed_sell_prepared_at" not in replacement.payload
            assert "oms_managed_row_id" not in replacement.payload
