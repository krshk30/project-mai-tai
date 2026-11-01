"""Controlled exact-entry GLND replay; no real broker/DB connections."""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from tests.unit.oms_sqlite_fixtures import private_session_factory as build_test_session_factory
from tests.unit.oms_sqlite_fixtures import own_service
from datetime import UTC, datetime, timedelta
import ast
from decimal import Decimal as D
import textwrap
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from project_mai_tai.db.models import (
    AccountPosition, BrokerOrder, Fill, OmsManagedPosition, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms.service import OmsRiskService, oco_exit_client_order_id
from project_mai_tai.oms import service as module
from project_mai_tai.settings import Settings

from tests.c7_history import historical_source


def seed_episode(account="live:schwab_1m_v2", *, closed=False, exited=150, hour=13):
    now = datetime(2026, 10, 14, hour, 53, 29, tzinfo=UTC)
    factory = build_test_session_factory()
    service = own_service(OmsRiskService(Settings(_env_file=None, oms_adapter="simulated",
        oms_v2_exit_release_reservation_enabled=True), session_factory=factory))
    with factory() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="ATR", execution_mode="live", metadata_json={})
        broker = service.store.ensure_broker_account(session, account,
            provider="webull" if account == "live:orb" else "schwab", environment="live")
        intent = TradeIntent(strategy_id=strategy.id, broker_account_id=broker.id,
            symbol="GLND", side="buy", intent_type="open", quantity=D(150), status="filled", reason="controlled")
        session.add(intent)
        session.flush()
        coid = "schwab_1m_v2-GLND-open-597d44631b40"
        entry = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id, broker_account_id=broker.id,
            client_order_id=coid, broker_order_id="entry-actual-shape", symbol="GLND", side="buy",
            order_type="limit", time_in_force="day", quantity=D(150), status="filled", payload={})
        session.add(entry)
        session.flush()
        child = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id, broker_account_id=broker.id,
            client_order_id=oco_exit_client_order_id(coid, "1008287070667"), broker_order_id="1008287070667",
            symbol="GLND", side="sell", order_type="oco_exit", time_in_force="day", quantity=D(exited),
            status="filled", payload={"source": "native_oco_child_leg"})
        session.add(child)
        session.flush()
        for order, side, quantity, price, fill_id in (
            (entry, "buy", 150, "3.99", "entry:150"),
            (child, "sell", exited, "4.19", f"1008287070667:{exited}"),
        ):
            session.add(Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=broker.id,
                broker_fill_id=fill_id, symbol="GLND", side=side, quantity=D(quantity), price=D(price),
                filled_at=now - timedelta(seconds=9), payload={}))
        row = OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol="GLND", entry_order_id=entry.id, entry_client_order_id=coid, entry_price=D("3.99"),
            original_quantity=150, current_quantity=0 if closed else 150,
            entry_time=now - timedelta(seconds=9), status="closed" if closed else "open",
            updated_at=now - timedelta(seconds=1))
        session.add(row)
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=broker.id,
            symbol="GLND", quantity=D(0 if closed else 150), average_price=D("3.99")))
        session.add(AccountPosition(broker_account_id=broker.id, symbol="GLND", quantity=D(0),
            average_price=D(0), source_updated_at=now))
        session.commit()
        return service, factory, str(row.id), now, child.id


async def quantity(service, account, row_id):
    return await service._v2_managed_sell_owned_quantity(managed_row_id=row_id,
        strategy_code="schwab_1m_v2", broker_account_name=account, symbol="GLND", requested_quantity=150)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
@pytest.mark.parametrize("hour", [12, 13])
async def test_glnd_stored_own_oco_fill_blocks_close_before_release(account, hour, monkeypatch):
    service, factory, row_id, _, _ = seed_episode(account, hour=hour)
    async def forbidden(*args, **kwargs):
        pytest.fail("known full native exit must not release/cancel/submit again")
    monkeypatch.setattr(service, "_release_exit_reservation_before_close", forbidden)
    monkeypatch.setattr(service.broker_adapter, "submit_order", forbidden)
    with factory() as session:
        result = await service._emit_v2_managed_sell(session, session.get(OmsManagedPosition, UUID(row_id)),
            intent_type="close", quantity=150, reference_price=4.19,
            reason="oms_v2_managed_exit:CW_TARGET", bid=4.19)
    assert not result and result.ownership_refused
    assert await quantity(service, account, row_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
@pytest.mark.parametrize("hour", [12, 13])
async def test_partial_child_preserves_exact_owned_remaining(account, hour):
    service, _, row_id, _, _ = seed_episode(account, exited=90, hour=hour)
    assert await quantity(service, account, row_id) == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("same", [True, False])
async def test_pending_only_blocks_its_exact_managed_episode(same):
    service, factory, row_id, _, child_id = seed_episode(exited=1)
    with factory() as session:
        session.delete(session.get(BrokerOrder, child_id))
        session.commit()
    service._oco_exit_fill_pending[("live:schwab_1m_v2", "GLND")] = SimpleNamespace(
        row_id=row_id if same else str(uuid4()))
    assert await quantity(service, "live:schwab_1m_v2", row_id) == (0 if same else 150)


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["foreign_entry", "zero_price", "foreign_account"])
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
@pytest.mark.parametrize("hour", [12, 13])
async def test_unbound_or_cancelled_zero_price_child_cannot_close_our_episode(defect, account, hour):
    service, factory, row_id, _, child_id = seed_episode(account, hour=hour)
    with factory() as session:
        child = session.get(BrokerOrder, child_id)
        if defect == "foreign_entry":
            child.client_order_id = "different-entry-ocoexit"
        elif defect == "foreign_account":
            child.broker_account_id = service.store.ensure_broker_account(
                session, "live:foreign", provider="schwab", environment="live").id
        else:
            session.query(Fill).filter(Fill.order_id == child_id).one().price = D(0)
        session.commit()
    # A positive exact-own execution with no usable price is UNKNOWN, not
    # unrelated history that can fall back to stale positive ownership.
    assert await quantity(service, account, row_id) == (0 if defect == "zero_price" else 150)


@pytest.mark.asyncio
async def test_original_quantity_guard_mutation_is_detected(monkeypatch):
    source = historical_source("20266cdf", "src/project_mai_tai/oms/service.py")
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OmsRiskService")
    node = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_v2_managed_sell_owned_quantity")
    namespace = dict(module.__dict__)
    exec(textwrap.dedent(ast.get_source_segment(source, node)), namespace)
    monkeypatch.setattr(OmsRiskService, "_v2_managed_sell_owned_quantity",
                        namespace["_v2_managed_sell_owned_quantity"])
    # Isolate the post-release barrier: the independent pre-release barrier is
    # already exercised by the unchanged full-fill/no-release control above.
    async def no_pre_release_evidence(self, **kwargs):
        return None
    monkeypatch.setattr(OmsRiskService, "_v2_managed_sell_native_exit_refusal", no_pre_release_evidence)
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        await test_glnd_stored_own_oco_fill_blocks_close_before_release(
            "live:schwab_1m_v2", 13, monkeypatch)
