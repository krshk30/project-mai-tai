"""O14 scope regression: known nonlive follows existing readers; unknown stays fenced."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy, TradeIntent
from project_mai_tai.oms import managed_sell_retirement as retirement, service as oms
from project_mai_tai.oms.managed_sell_prewire import prepare_managed_sell
from tests.unit.test_managed_sell_prewire import attempt, lane as lane
from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite

NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=2)


def bound_scope(code, mode, name):
    strategy = SimpleNamespace(id=uuid4(), code=code, execution_mode=mode)
    account = SimpleNamespace(id=uuid4(), name=name, environment="production")
    intent = SimpleNamespace(id=uuid4(), strategy_id=strategy.id, broker_account_id=account.id,
                             symbol="ABC", created_at=OLD)
    order = SimpleNamespace(strategy_id=strategy.id, broker_account_id=account.id, intent_id=intent.id,
        symbol="ABC", side="sell", submitted_at=OLD, payload={"managed_sell_prepared_at": OLD.isoformat()})
    return order, intent, strategy, account


@pytest.mark.parametrize("code", ["schwab_1m_v2", "orb_schwab"])
@pytest.mark.parametrize("mode,name,intercept", [
    ("live", "live:orb", True),
    ("live", "paper:orb", False), ("paper", "paper:orb", False),
    ("live", "shadow:orb", False), ("shadow", "shadow:orb", False),
    ("paper", "live:orb", False), ("shadow", "live:orb", False),
    (None, "live:orb", True), ("UNKNOWN", "live:orb", True), ("external", "live:orb", True),
    ("paper", "unclassified:orb", True), ("live", "live:", True), ("live", " live:orb", True),
])
def test_bound_old_seller_scope_uses_canonical_alias_not_deployment_environment(code, mode, name, intercept):
    order, intent, strategy, account = bound_scope(code, mode, name)
    assert retirement.requires_read_only_recovery(order, intent, code, NOW,
        strategy=strategy, account=account) is intercept


@pytest.mark.parametrize("mutation", ["missing_strategy", "missing_account", "strategy_id", "account_id",
    "strategy_code", "intent_id", "intent_strategy", "intent_account", "intent_symbol"])
def test_nonlive_exemption_never_comes_from_missing_or_foreign_binding(mutation):
    order, intent, strategy, account = bound_scope("schwab_1m_v2", "paper", "paper:atr")
    if mutation == "missing_strategy":
        strategy = None
    elif mutation == "missing_account":
        account = None
    elif mutation == "strategy_id":
        strategy.id = uuid4()
    elif mutation == "account_id":
        account.id = uuid4()
    elif mutation == "strategy_code":
        strategy.code = "foreign"
    elif mutation == "intent_id":
        intent.id = uuid4()
    elif mutation == "intent_strategy":
        intent.strategy_id = uuid4()
    elif mutation == "intent_account":
        intent.broker_account_id = uuid4()
    else:
        intent.symbol = "OTHER"
    assert retirement.requires_read_only_recovery(order, intent, "schwab_1m_v2", NOW,
        strategy=strategy, account=account)


def test_omitted_scope_arguments_preserve_old_failclosed_helper_contract():
    order, intent, _strategy, _account = bound_scope("schwab_1m_v2", "paper", "paper:atr")
    assert retirement.requires_read_only_recovery(order, intent, "schwab_1m_v2", NOW)


SCOPES = [
    ("paper", "paper", False), ("shadow", "shadow", False),
    ("live", "live", True), ("live", None, True), ("unclassified", "live", True),
]


async def old_sell(lane, namespace, mode):
    service, sessions, row_id, old_name = lane
    suffix = old_name.partition(":")[2]
    account_name = f"{namespace}:{suffix}"
    with sessions.begin() as unit:
        account = unit.scalar(select(BrokerAccount).where(BrokerAccount.name == old_name))
        account.name = account_name
        strategy = unit.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        strategy.execution_mode = mode if mode is not None else "UNKNOWN"
        managed = unit.scalar(select(OmsManagedPosition))
        managed.broker_account_name = account_name
    incoming, request = attempt(account_name)
    identity = await prepare_managed_sell(service, event=incoming, request=request,
        managed_row_id=row_id, now=OLD)
    assert identity is not None
    with sessions.begin() as unit:
        intent = unit.get(TradeIntent, identity)
        intent.created_at = OLD
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.intent_id == identity))
        order.status, order.broker_order_id, order.submitted_at, order.updated_at = "accepted", "original-seller", OLD, OLD
        return service, sessions, account_name, order.id, identity, request


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace,mode,intercept", SCOPES)
async def test_actual_sync_known_nonlive_reaches_original_reader_unknown_live_never_does(
    lane, monkeypatch, namespace, mode, intercept,
):
    service, sessions, account, order_id, _intent_id, request = await old_sell(lane, namespace, mode)
    monkeypatch.setattr(oms, "utcnow", lambda: NOW)
    recovered, reads = [], []

    async def recover(actual_service, identity):
        assert actual_service is service
        recovered.append(identity)
        return False  # Unknown is NOT a retirement success or generic fallback.

    async def detail(wire):
        reads.append(wire)
        return ExecutionReport("accepted", request.client_order_id, broker_order_id="original-seller",
            symbol="ABC", side="sell", intent_type="close", quantity=Decimal(10),
            filled_quantity=Decimal(0), reported_at=NOW, origin="broker")

    async def unchanged_refresh(**_kwargs):
        return {"orders": 0, "terminal_orders": 0, "published_events": []}

    monkeypatch.setattr(retirement, "retire_previous_flat_sell", recover)
    monkeypatch.setattr(service, "_refresh_working_order", unchanged_refresh)
    service.broker_adapter = SimpleNamespace(fetch_order_update=detail)
    await service.sync_broker_orders(account_names=[account])
    assert recovered == ([order_id] if intercept else [])
    assert len(reads) == (0 if intercept else 1)
    with sessions() as unit:
        assert unit.get(BrokerOrder, order_id).status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace,mode,intercept", SCOPES)
async def test_actual_refresh_known_nonlive_reaches_original_pricing_unknown_stays_blocked(
    lane, monkeypatch, namespace, mode, intercept,
):
    service, sessions, account, order_id, intent_id, request = await old_sell(lane, namespace, mode)
    monkeypatch.setattr(oms, "utcnow", lambda: NOW)
    priced = []

    async def pricing(**kwargs):
        priced.append(kwargs["order"].id)
        return None  # Observe the unchanged path, without creating broker mutations.
    monkeypatch.setattr(service, "_build_refreshed_order_metadata", pricing)
    with sessions() as unit:
        result = await service._refresh_working_order(session=unit, order=unit.get(BrokerOrder, order_id),
            intent=unit.get(TradeIntent, intent_id), strategy_code="schwab_1m_v2", broker_account_name=account,
            report=ExecutionReport("accepted", request.client_order_id, broker_order_id="original-seller",
                symbol="ABC", side="sell", intent_type="close", quantity=Decimal(10),
                filled_quantity=Decimal(0), reported_at=NOW, origin="broker"))
    assert priced == ([] if intercept else [order_id])
    assert result == {"orders": 0, "terminal_orders": 0, "published_events": []}
