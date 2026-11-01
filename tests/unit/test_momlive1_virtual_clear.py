"""Own momentum execution evidence must survive broker snapshot reconciliation."""
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from decimal import Decimal as D
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select

from ops.health import fleet_health_check as health
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Fill, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings
from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from tests.unit.test_control_plane import build_test_session_factory
from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup
from tests.unit.momlive1_owned_fixtures import complete_owned_observation
from tests.unit.test_oms_risk_service import FakeRedis, _capture_service_log


def seed(factory, *, strategy="momentum_warn_live", provider="webull", backing="0"):
    with factory.begin() as session:
        owner = Strategy(code=strategy, name=strategy, execution_mode="live")
        account = BrokerAccount(name="live:controlled", provider=provider, environment="production")
        session.add_all([owner, account])
        session.flush()
        session.add(VirtualPosition(strategy_id=owner.id, broker_account_id=account.id,
            symbol="BBCI", quantity=25, average_price=D("3.68"), realized_pnl=0,
            updated_at=datetime.now(UTC)-timedelta(minutes=5)))
        if backing is not None:
            session.add(AccountPosition(broker_account_id=account.id, symbol="BBCI",
                quantity=D(backing), average_price=0, source_updated_at=datetime.now(UTC)))
        return account.id


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("minimum_age", [0, 24.119])
def test_snapshot_zero_retains_momentum_owned_even_after_clear_age(provider, minimum_age):
    factory = build_test_session_factory()
    account = seed(factory, provider=provider)
    mismatch = []
    with factory.begin() as session:
        assert OmsStore().clear_virtual_positions_without_account_backing(session,
            broker_account_ids=[account], minimum_age_seconds=minimum_age,
            momentum_mismatch_out=mismatch) == []
        position = session.scalar(select(VirtualPosition))
        assert (position.quantity, position.average_price, position.realized_pnl) == (25, D("3.68"), 0)
    assert mismatch == [(account, "BBCI", D(25))]


@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
def test_atr_orb_snapshot_zero_clear_is_unchanged(strategy):
    factory = build_test_session_factory()
    account = seed(factory, strategy=strategy)
    mismatch = []
    with factory.begin() as session:
        assert OmsStore().clear_virtual_positions_without_account_backing(session,
            broker_account_ids=[account], momentum_mismatch_out=mismatch) == [(account, "BBCI", D(25))]
        assert session.scalar(select(VirtualPosition.quantity)) == 0
    assert not mismatch


@pytest.mark.parametrize("backing,selected", [("10", True), ("0", False)])
def test_momentum_backed_or_unread_account_not_reported(backing, selected):
    factory = build_test_session_factory()
    account = seed(factory, backing=backing)
    mismatch = []
    with factory.begin() as session:
        assert OmsStore().clear_virtual_positions_without_account_backing(session,
            broker_account_ids=[account] if selected else [account.__class__(int=1)],
            momentum_mismatch_out=mismatch) == []
        assert session.scalar(select(VirtualPosition.quantity)) == 25
    assert not mismatch


@pytest.mark.asyncio
async def test_actual_sync_logs_critical_mismatch_without_erasing_or_order():
    factory = build_test_session_factory()
    seed(factory)
    service = OmsRiskService(Settings(redis_stream_prefix="controlled", oms_adapter="simulated",
        oms_virtual_position_clear_min_age_seconds=0), FakeRedis(), session_factory=factory)
    reads = []
    async def no_positions(account):
        reads.append(account)
        return []
    service.broker_adapter = SimpleNamespace(list_account_positions=no_positions)
    log = _capture_service_log(service, "test-momentum-virtual-retention")
    await service.sync_broker_state()
    assert reads == ["live:controlled"]
    with factory() as session:
        assert session.scalar(select(VirtualPosition.quantity)) == 25
        assert session.scalar(select(func.count()).select_from(Fill)) == 0
    rows = [line for line in log.messages if "[MOMLIVE1-OWNED-BACKING-MISMATCH]" in line]
    assert len(rows) == 1 and "live:controlled:BBCI=25" in rows[0] and "retained_owned=1" in rows[0]
    assert not any("[VIRTUAL-CLEAR] zeroed" in line for line in log.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_receipt", [None, "origin", "event_type", "filled_quantity",
    "broker_order_id", "client_order_id", "symbol", "side", "intent_type", "account", "quantity", "native",
    "parent_marker", "parent_episode", "children", "protection_symbol", "protection_quantity", "root_intent",
    "restart_after_target", "equivalent_quantity", "root_side", "root_symbol", "root_episode"])
async def test_bbci_missing_target_then_broker_zero_keeps_ownership_until_actual_exit(bad_receipt):
    now, broker, service, runtime = setup()
    broker.entry_quantity, broker.entry_average = D(25), D("3.68")
    service._latest_quotes_by_symbol["BBCI"] = {"ask": D("3.85"), "bid": D("3.84"), "received_at": now[0]}
    order = event(now[0])
    signal = json.loads(order.payload.metadata["momlive_signal"])
    signal.update(symbol="BBCI", signal_price="3.8485")
    order = order.model_copy(update={"payload": order.payload.model_copy(update={"symbol": "BBCI",
        "metadata": {"momlive_signal": json.dumps(signal)}})})
    fetch, visible, crash = broker.fetch_order_update, [False], [False]
    async def delayed_children(request):
        if request.metadata.get("momlive_native_child") == "true" and not visible[0]:
            return None
        if crash[0] and request.client_order_id.endswith("S"):
            crash[0] = False
            raise RuntimeError("controlled interruption after target receipt")
        return await fetch(request)
    broker.fetch_order_update = delayed_children
    await runtime.start()
    try:
        await offer_and_drain(runtime, order)
        await complete_owned_observation(runtime, next(iter(runtime.book.episodes)))
    finally:
        await runtime.close()
    identity = next(iter(runtime.book.episodes))
    handle = runtime.native.handles[identity]
    with service.session_factory.begin() as session:
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == handle.account))
        backing = session.scalar(select(AccountPosition).where(AccountPosition.broker_account_id == account.id,
                                                               AccountPosition.symbol == "BBCI"))
        backing.quantity = D(0)  # The broker-zero snapshot after the real target execution.
        session.flush()
        position = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "BBCI"))
        mismatch = []
        assert service.store.clear_virtual_positions_without_account_backing(session,
            broker_account_ids=[account.id], momentum_mismatch_out=mismatch) == []
        assert position.quantity == 25 and mismatch == [(account.id, "BBCI", D(25))]
        assert session.scalar(select(func.count()).select_from(Fill).where(Fill.side == "sell")) == 0
        assert session.scalar(select(func.count()).select_from(BrokerOrder).where(
            BrokerOrder.payload["momlive_native_child"].as_string() == "true")) == 0
    target, stop = handle.child_ids
    broker.orders[target].status, broker.orders[target].quantity, broker.orders[target].price = "filled", D(25), D("3.87")
    broker.orders[stop].status = "cancelled"
    visible[0] = True
    if bad_receipt == "equivalent_quantity":
        with service.session_factory.begin() as session:
            parent = session.get(TradeIntent, UUID(identity))
            payload = dict(parent.payload)
            protection = {**payload["momlive_runtime"]["protection"], "quantity": "25.00000000"}
            parent.payload = {**payload, "momlive_runtime": {**payload["momlive_runtime"], "protection": protection}}
    if bad_receipt and bad_receipt not in {"restart_after_target", "equivalent_quantity"}:
        snapshot = await runtime._inspect(identity)
        root = next(row for row in snapshot["orders"] if row["request"].client_order_id == handle.base_coid)
        child = replace(root["request"], client_order_id=stop,
            metadata={"episode_id": identity, "momlive_strategy": "momentum_warn_live",
                      "momlive_native_child": "true"})
        receipt = await broker.fetch_order_update(child)
        changes = {"origin": "client", "event_type": "accepted", "filled_quantity": D(1),
            "broker_order_id": None, "client_order_id": "foreign-child", "symbol": "FOREIGN",
            "side": "buy", "intent_type": "open"}
        if bad_receipt in changes:
            receipt = replace(receipt, **{bad_receipt: changes[bad_receipt]})
        elif bad_receipt == "account":
            child = replace(child, broker_account_name="live:foreign")
        elif bad_receipt == "quantity":
            child = replace(child, quantity=D(26))
        elif bad_receipt == "native":
            child = replace(child, metadata={**child.metadata, "momlive_native_child": "false"})
        with service.session_factory.begin() as session:
            with pytest.raises(Refusal):
                with session.begin_nested():
                    parent = session.get(TradeIntent, UUID(identity))
                    payload = dict(parent.payload)
                    if bad_receipt == "parent_marker":
                        payload["momlive_parent"] = False
                    elif bad_receipt == "parent_episode":
                        payload["momlive_episode"] = {**payload["momlive_episode"], "episode_id": "foreign"}
                    elif bad_receipt in {"children", "protection_symbol", "protection_quantity"}:
                        protection = dict(payload["momlive_runtime"]["protection"])
                        field, value = {"children": ("child_ids", [stop, "foreign"]),
                            "protection_symbol": ("symbol", "FOREIGN"),
                            "protection_quantity": ("quantity", "26")}[bad_receipt]
                        protection[field] = value
                        payload["momlive_runtime"] = {**payload["momlive_runtime"], "protection": protection}
                    elif bad_receipt in {"root_intent", "root_side", "root_symbol", "root_episode"}:
                        root_order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == handle.base_coid))
                        intent = session.get(TradeIntent, root_order.intent_id)
                        if bad_receipt == "root_intent":
                            intent.intent_type = "open"
                        elif bad_receipt == "root_side":
                            intent.side = "buy"
                        elif bad_receipt == "root_symbol":
                            intent.symbol = "FOREIGN"
                        else:
                            intent.payload = {**intent.payload, "metadata": {"episode_id": "foreign"}}
                    parent.payload = payload
                    session.flush()
                    runtime.db.record_terminal_native_child(session, identity, child, receipt)
            assert session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == stop)) is None
    if bad_receipt == "restart_after_target":
        crash[0] = True
        with pytest.raises(RuntimeError, match="controlled interruption"):
            await runtime._observe(identity)
        with service.session_factory() as session:
            assert session.scalar(select(VirtualPosition.quantity).where(VirtualPosition.symbol == "BBCI")) == 0
            assert session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == stop)) is None
        runtime = OmsLiveRuntime(service, clock=lambda: now[0], identity="124:457")
        try:
            await runtime.start()
        finally:
            await runtime.close()
    observation = await runtime._observe(identity)
    assert observation.exited == 25
    with service.session_factory() as session:
        position = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "BBCI"))
        assert (position.quantity, position.realized_pnl) == (0, D("4.75"))
        assert session.scalar(select(func.count()).select_from(Fill).where(Fill.side == "sell")) == 1
    await runtime._observe(identity)
    assert (await runtime._inspect(identity))["exited"] == 25
    snapshot = await runtime._inspect(identity)
    root = next(row for row in snapshot["orders"] if row["request"].client_order_id == handle.base_coid)
    with service.session_factory.begin() as session:
        outbound = replace(root["request"], client_order_id="new-unowned-sell", quantity=D(1))
        with pytest.raises(Refusal, match="exit_exceeds_strategy_owned_quantity"):
            runtime.db.prepare_order(session, identity, outbound, provider="webull")
    assert not broker.cancelled


@pytest.mark.parametrize("counts,level", [([1, 0], "RED"), ([0, 1], "AMBER"),
    ([0, 0], "GREEN"), ([None, 0], "AMBER")])
def test_fullscan_flags_mismatch_and_unknown_without_extra_page(monkeypatch, counts, level):
    queries, answers = [], iter(counts)
    def scalar(sql):
        queries.append(sql)
        return next(answers)
    monkeypatch.setattr(health, "_scalar_int", scalar)
    result = health.check_momentum_owned_backing()
    assert result[0] == level
    assert all("s.code='momentum_warn_live'" in sql and "vp.quantity>0" in sql for sql in queries)
    assert len(queries) == 2 and "45 seconds" in queries[0]
    assert "vp.updated_at IS NULL" in queries[1] and "ap.quantity IS NULL" in queries[1]
    assert next(spec.alert_class for spec in health.CHECKS if spec.check == health.check_momentum_owned_backing) == health.DIAGNOSTIC
