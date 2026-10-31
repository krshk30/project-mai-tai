"""Actual original OMS sync -> off-loop scalar receipt -> lifetime consumer.

No private sync-pass substitution or DTO-injected receipt. All source controls
are UNRUN; PM covers a current resting LIMIT, not an invented aged PM incident.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerAccount, BrokerOrder
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service as oms_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.reconciliation import service as reconciler_module
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.reconciliation.working_lifetime import live_working_ids
from project_mai_tai.settings import Settings
from tests.unit.test_reconciliation_service import FakeRedis, build_test_session_factory


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("session_name", ["AM", "NORMAL"], ids=["PM", "RTH"])
async def test_original_sync_reaches_resting_consumer_without_pair_dto(monkeypatch, provider, session_name):
    now = datetime(2026, 10, 16, 12 if session_name == "AM" else 19, 47, tzinfo=UTC)
    clock = [now]
    monkeypatch.setattr(oms_module, "utcnow", lambda: clock[0])
    monkeypatch.setattr(reconciler_module, "utcnow", lambda: clock[0])
    factory = build_test_session_factory()
    name = f"live:controlled-{provider}"
    settings = Settings(_env_file=None, oms_adapter="paper", oms_v2_exit_management_enabled=False,
        strategy_schwab_1m_v2_account_name=name if provider == "schwab" else "live:other-schwab",
        strategy_schwab_1m_v2_webull_account_name=name if provider == "webull" else "live:other-webull",
        orb_country_routing_enabled=False, orb_webull_account_name="")
    broker = SimpleNamespace(accounts_by_name={name: SimpleNamespace(account_id="CONTROLLED")},
        submit_order=AsyncMock(side_effect=AssertionError("unexpected trading wire")))
    service = OmsRiskService(settings, SimpleNamespace(xadd=AsyncMock()), session_factory=factory, broker_adapter=broker)
    metadata = {"resting_entry": "true", "session": session_name, "limit_price": "2.01",
        "stop_price": "2", "order_type": "limit" if session_name == "AM" else "stop_limit"}
    if session_name == "AM":
        metadata["oms_v2_eh_resting_entry"] = "true"
    with factory.begin() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="controlled", execution_mode="live")
        account = service.store.ensure_broker_account(session, name, provider=provider, environment="production")
        event = TradeIntentEvent(source_service="schwab_1m_v2", payload=TradeIntentPayload(
            strategy_code=strategy.code, broker_account_name=name, symbol="CONTROL", side="buy",
            quantity=Decimal(2), intent_type="open", reason="controlled resting", metadata=metadata))
        intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        order = service.store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id="controlled-resting-"+provider,
            symbol="CONTROL", side="buy", quantity=Decimal(2), metadata=metadata,
            status="accepted", order_type="LIMIT" if session_name == "AM" else "STOP_LIMIT", time_in_force="day")
        order.broker_order_id = "controlled-native-resting"
        order.submitted_at = order.updated_at = now if session_name == "AM" else now-timedelta(minutes=4)
        intent.updated_at = order.updated_at
        identity, account_id = order.id, account.id

    async def fetch(request):
        assert request.broker_account_name == name and request.symbol == "CONTROL" and request.side == "buy"
        return ExecutionReport("accepted", request.client_order_id, broker_order_id="controlled-native-resting",
            symbol=request.symbol, side="buy", intent_type="open", quantity=request.quantity,
            origin="broker", reported_at=clock[0], metadata={**request.metadata,
                "_webull_list_evidence": json.dumps({"account_id": "CONTROLLED",
                    "client_order_id": request.client_order_id,
                    "acquired_at": clock[0].isoformat(), "truncated": False})})
    broker.fetch_order_update = AsyncMock(side_effect=fetch)
    reconciler = ReconciliationService(settings, redis_client=FakeRedis(), session_factory=factory)
    assert service._sync_broker_orders_pass.__func__ is OmsRiskService._sync_broker_orders_pass
    with factory() as session:
        accounts = {account_id: session.get(BrokerAccount, account_id)}
        assert live_working_ids(session, [session.get(BrokerOrder, identity)], accounts,
            now=clock[0], cadence_seconds=settings.oms_broker_sync_interval_seconds) == set()
        if session_name == "NORMAL":
            assert len(reconciler._build_stuck_order_findings(session)) == 1
    await service.sync_broker_orders(account_names=[name])  # ORIGINAL pass, not replaced.
    broker.fetch_order_update.assert_awaited_once()
    broker.submit_order.assert_not_awaited()
    with factory() as session:
        order = session.get(BrokerOrder, identity)
        accounts = {account_id: session.get(BrokerAccount, account_id)}
        assert live_working_ids(session, [order], accounts, now=clock[0],
            cadence_seconds=settings.oms_broker_sync_interval_seconds) == {identity}
        if session_name == "NORMAL":
            assert reconciler._build_stuck_order_findings(session) == []
            assert reconciler._build_stuck_intent_findings(session) == []
    clock[0] += timedelta(seconds=2*settings.oms_broker_sync_interval_seconds+1)
    with factory() as session:
        accounts = {account_id: session.get(BrokerAccount, account_id)}
        assert live_working_ids(session, [session.get(BrokerOrder, identity)], accounts,
            now=clock[0], cadence_seconds=settings.oms_broker_sync_interval_seconds) == set()
