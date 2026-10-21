import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.momentum_live.admission import Refusal, STRATEGY_CODE
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.positions_read_receipt import record_receipts, SNAPSHOT_TYPE
from tests.unit.test_control_plane import build_test_session_factory


class ControlledBroker:
    """No network: actual runtime ports run against explicit venue-result fixtures."""

    def __init__(self, clock):
        self.clock = clock
        self.orders = {}
        self.sent = []
        self.cancelled = []
        self.leaf = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
        self.entry_quantity = D(33)
        self.entry_average = D("2.9974")
        self.entry_status = "filled"

    def _adapter_for_account(self, account):
        return self.leaf

    async def submit_order(self, request):
        self.sent.append(request)
        if request.intent_type == "cancel":
            self.cancelled.append(request.client_order_id)
            prior = self.orders[request.client_order_id]
            self.orders[request.client_order_id] = SimpleNamespace(request=prior.request,
                status="cancelled", quantity=prior.quantity, price=prior.price)
        elif request.side == "buy":
            self.orders[request.client_order_id] = SimpleNamespace(request=request, status=self.entry_status,
                quantity=self.entry_quantity, price=self.entry_average)
        else:
            self.orders[request.client_order_id] = SimpleNamespace(request=request, status="accepted", quantity=D(0), price=None)
            if request.metadata.get("webull_exit_only_pair"):
                for coid in self.leaf.exit_pair_leg_client_order_ids(request.client_order_id):
                    self.orders[coid] = SimpleNamespace(request=request, status="accepted", quantity=D(0), price=None)
        report = await self.fetch_order_update(request)
        return [report]

    async def fetch_order_update(self, request):
        value = self.orders.get(request.client_order_id)
        if value is None:
            return None
        return ExecutionReport(value.status, request.client_order_id,
            broker_order_id="venue-"+request.client_order_id, broker_fill_id="exec-"+request.client_order_id+str(value.quantity),
            symbol=request.symbol, side=request.side, intent_type=request.intent_type,
            filled_quantity=value.quantity, fill_price=value.price, origin="broker", reported_at=self.clock(),
            metadata={**request.metadata, "webull_broker_filled_time": self.clock().isoformat()})

    async def cancel_exit_pair(self, *, broker_account_name, symbol, base_client_order_id):
        for coid in self.leaf.exit_pair_leg_client_order_ids(base_client_order_id):
            self.cancelled.append(coid)
            row = self.orders[coid]
            row.status = "cancelled"
        return []

    async def list_account_positions(self, account):
        return [BrokerPositionSnapshot(account, "SCNI", D(133), D(3), as_of=self.clock())]

def setup(*, enabled=True):
    now = [datetime(2026, 10, 14, 14, tzinfo=UTC)]
    settings = Settings(_env_file=None, momentum_live_enabled=enabled, oms_adapter="paper",
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_broker_provider="schwab",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True)
    broker = ControlledBroker(lambda: now[0])
    redis = SimpleNamespace(xadd=AsyncMock(return_value="1-0"))
    service = OmsRiskService(settings, redis, session_factory=build_test_session_factory(), broker_adapter=broker)
    service.seed_runtime_metadata()
    service._latest_quotes_by_symbol["SCNI"] = {"ask": D("3.001"), "bid": D(3), "received_at": now[0]}
    runtime = OmsLiveRuntime(service, clock=lambda: now[0], identity="123:456")
    with service.session_factory() as session:
        record_receipts(session, [(name, now[0], 0) for name in runtime.accounts], recorded_at=now[0])
        session.commit()
    return now, broker, service, runtime


@pytest.mark.asyncio
async def test_actual_multiplexer_unknown_manual_order_is_outside_narrowed_policy_no_new_broker_read():
    _, broker, service, runtime = setup()
    service.broker_adapter = RoutingBrokerAdapter(default_provider="webull",
        provider_by_account={"live:orb": "webull"}, factories_by_provider={"webull": lambda: broker.leaf})
    assert await runtime._foreign_owner("live:orb", "SCNI") is False
    assert broker.sent == []


@pytest.mark.asyncio
async def test_missing_measured_interval_or_stale_sync_refuses_without_broker_read():
    from sqlalchemy import delete
    from project_mai_tai.db.models import DashboardSnapshot
    now, broker, service, runtime = setup()
    now[0] += timedelta(seconds=30, microseconds=56001)
    with pytest.raises(Refusal, match="account_position_sync_stale"):
        await runtime._foreign_owner("live:orb", "SCNI")
    with service.session_factory() as session:
        session.execute(delete(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE))
        session.commit()
    with pytest.raises(Refusal, match="account_position_sync_receipt_UNKNOWN"):
        await runtime._foreign_owner("live:orb", "SCNI")
    assert broker.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("age_ns,allowed", [(30_056_000_000, True), (30_056_000_001, False), (-1, False)])
async def test_measured_sync_exact_boundary_and_future_original_wire_time(age_ns, allowed):
    now, broker, _, runtime = setup()
    runtime.now_ns = lambda: int(now[0].timestamp()*1e9) + age_ns
    if allowed:
        assert await runtime._foreign_owner("live:orb", "SCNI") is False
    else:
        with pytest.raises(Refusal, match="account_position_sync_stale"):
            await runtime._foreign_owner("live:orb", "SCNI")
    assert broker.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["writer", "naive", "recorded_at_fresh_read_at_old"])
async def test_durable_position_receipt_provenance_and_wire_age_not_recorded_age(defect):
    from sqlalchemy import select
    from project_mai_tai.db.models import DashboardSnapshot
    now, broker, service, runtime = setup()
    with service.session_factory() as session:
        rows = session.scalars(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)).all()
        for row in rows:
            payload = dict(row.payload)
            if defect == "writer":
                payload["writer"] = "untrusted"
            elif defect == "naive":
                payload["read_at"] = now[0].replace(tzinfo=None).isoformat()
            else:
                payload["read_at"] = (now[0]-timedelta(seconds=31)).isoformat()
                payload["recorded_at"] = now[0].isoformat()
            row.payload = payload
        session.commit()
    with pytest.raises(Refusal):
        await runtime._foreign_owner("live:orb", "SCNI")
    assert broker.sent == []


def event(now, *, route="webull"):
    payload = {"session_date": "2026-10-14", "symbol": "SCNI", "source_id": "sealed30",
        "interval_start_ms": 100, "interval_end_ms": 200, "signal_price": "3",
        "observed_ns": int(now.timestamp()*1e9), "day_volume": 50000,
        "country_route": route, "country_category": "sec_unknown_at_decision",
        "country_evidence_json": '{"classification":"UNKNOWN"}'}
    return TradeIntentEvent(source_service="momentum-paper", payload=TradeIntentPayload(
        strategy_code=STRATEGY_CODE, broker_account_name="live:schwab_1m_v2", symbol="SCNI",
        side="buy", quantity=1, intent_type="open", reason="controlled sealed detection",
        metadata={"momlive_signal": json.dumps(payload)}))


async def offer_and_drain(runtime, order):
    assert runtime.actor.offer_intent(order)
    await asyncio.wait_for(runtime.actor.inbox.join(), 3)
    await asyncio.wait_for(runtime.actor.work.queue.join(), 3)


@pytest.mark.asyncio
async def test_actual_oms_ports_entry_actual_fill_vwap_native_proof_and_no_resend():
    now, broker, service, runtime = setup()
    await runtime.start()
    try:
        await offer_and_drain(runtime, event(now[0]))
        assert len(runtime.book.episodes) == 1
        episode = next(iter(runtime.book.episodes.values()))
        assert episode.filled == 33 and episode.state == "held"
        entry = [row for row in broker.sent if row.side == "buy"]
        protect = [row for row in broker.sent if row.metadata.get("webull_exit_only_pair")]
        assert len(entry) == len(protect) == 1
        assert entry[0].broker_account_name == "live:orb" and entry[0].metadata["limit_price"] == "3.01"
        assert protect[0].metadata["bracket_target_price"] == "3.15"
        # Stop-10 wire payload controls: test_momstop10_policy::test_native_stop_wire_geometry_is_ten_percent_only_where_supported.
        assert protect[0].metadata["bracket_stop_price"] == "2.69"  # 2.9974 * .90 = 2.69766, floored.
        assert runtime.actor.dispatch.by_symbol == {"SCNI": (episode.episode_id,)}
        await runtime.publisher.publish_once()
        proof = json.loads(service.redis.xadd.await_args.args[1]["data"])
        assert D(proof["episodes"][0]["filled_quantity"]) == 33
        assert proof["episodes"][0]["account_name"] == "live:orb"
        await offer_and_drain(runtime, event(now[0]))
        assert len([row for row in broker.sent if row.side == "buy"]) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_manual_off_restart_keeps_exact_owned_quote_and_protection_without_new_entry():
    now, broker, service, runtime = setup()
    await runtime.start()
    await offer_and_drain(runtime, event(now[0]))
    await runtime.close()
    service.settings.momentum_live_enabled = False
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="123:456")
    await restarted.start()
    try:
        assert restarted.actor.restored and restarted.actor.dispatch.by_symbol.keys() == {"SCNI"}
        assert not restarted.actor.offer_intent(event(now[0]))
        assert len([row for row in broker.sent if row.side == "buy"]) == 1
        assert len([row for row in broker.sent if row.metadata.get("webull_exit_only_pair")]) == 1
        await restarted.publisher.publish_once()
        proof = json.loads(service.redis.xadd.await_args.args[1]["data"])
        assert proof["entry_admission_enabled"] is False and proof["quote_symbols"] == ["SCNI"]
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_partial_2s_cancel_targets_original_coid_and_protects_owned_slice():
    now, broker, service, runtime = setup()
    broker.entry_quantity = D(10)
    broker.entry_average = D("2.98")
    broker.entry_status = "partially_filled"
    await runtime.start()
    try:
        await offer_and_drain(runtime, event(now[0]))
        episode = next(iter(runtime.book.episodes.values()))
        now[0] += timedelta(seconds=2)
        service._latest_quotes_by_symbol["SCNI"]["received_at"] = now[0]
        await runtime.actor._service_owned(episode.episode_id)
        entry = next(row for row in broker.sent if row.side == "buy" and row.intent_type == "open")
        assert entry.client_order_id in broker.cancelled and episode.cancel_requested
        assert episode.filled == 10 and episode.exited == 0
        protect = next(row for row in broker.sent if row.metadata.get("webull_exit_only_pair"))
        assert protect.quantity == 10 and protect.metadata["bracket_target_price"] == "3.13"
        assert protect.metadata["bracket_stop_price"] == "2.68"  # 2.98 * .90 = 2.682, floored.
        now[0] += timedelta(seconds=1)
        await runtime.actor._service_owned(episode.episode_id)
        assert broker.cancelled.count(entry.client_order_id) == 1 and episode.state == "held"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_disabled_no_owned_state_preserves_old_redis_and_no_producer_io():
    now, broker, service, runtime = setup(enabled=False)
    assert await runtime.start() is False
    service.redis.xadd.assert_not_awaited()
    assert broker.sent == []


@pytest.mark.asyncio
async def test_wrong_account_provider_or_simulated_live_cannot_publish_restore_ready():
    for simulated in (True, False):
        now, broker, service, runtime = setup()
        if simulated:
            service.settings.oms_adapter = "simulated"
        else:
            service.settings.strategy_schwab_1m_v2_dual_broker_fanout_enabled = False
        with pytest.raises(Refusal):
            await runtime.start()
        service.redis.xadd.assert_not_awaited()
        assert broker.sent == []


@pytest.mark.asyncio
async def test_missing_measured_account_sync_cannot_open_or_fake_clear():
    from sqlalchemy import delete
    from project_mai_tai.db.models import DashboardSnapshot
    now, broker, service, runtime = setup()
    with service.session_factory() as session:
        session.execute(delete(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE))
        session.commit()
    await runtime.start()
    try:
        await offer_and_drain(runtime, event(now[0]))
        assert broker.sent == [] and runtime.book._active_count == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["manual_holding", "atr_working_buy"])
async def test_recorded_manual_holding_or_known_atr_working_buy_veto_without_broker_read(foreign):
    from sqlalchemy import select
    from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder
    _, broker, service, runtime = setup()
    with service.session_factory() as session:
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == "live:orb"))
        if foreign == "manual_holding":
            session.add(AccountPosition(broker_account_id=account.id, symbol="SCNI", quantity=D(100), average_price=D(3)))
        else:
            strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="ATR", execution_mode="live")
            session.add(BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id, symbol="SCNI",
                client_order_id="known-atr-pending", side="buy", quantity=D(10), status="accepted",
                order_type="limit", time_in_force="day", payload={}))
        session.commit()
    assert await runtime._foreign_owner("live:orb", "SCNI") is True
    assert broker.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("time_metadata", ["last_only", "missing"])
async def test_actual_owned_runtime_unknown_first_uses_durable_send_timer_after_restart(time_metadata):
    now, broker, service, runtime = setup()
    if time_metadata == "missing":
        original = broker.fetch_order_update
        async def no_time(request):
            report = await original(request)
            if report is not None:
                from dataclasses import replace
                return replace(report, metadata={key: value for key, value in report.metadata.items()
                    if key != "webull_broker_filled_time"})
            return report
        broker.fetch_order_update = no_time
    await runtime.start()
    await offer_and_drain(runtime, event(now[0]))
    episode = next(iter(runtime.book.episodes.values()))
    sent = episode.first_submit_ns
    assert episode.first_fill_ns is None
    await runtime.close()
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="123:456")
    await restarted.start()
    try:
        now[0] += timedelta(seconds=300)
        service._latest_quotes_by_symbol["SCNI"]["received_at"] = now[0]
        await restarted.actor._service_owned(episode.episode_id)
        exits = [row for row in broker.sent if row.metadata.get("exit_reason") == "time_300s"]
        assert len(exits) == 1 and exits[0].quantity == 33
        assert exits[0].metadata["clock_source"] == "send"
        assert exits[0].metadata["clock_start_ns"] == str(sent)
        assert restarted.book.episodes[episode.episode_id].first_fill_ns is None
    finally:
        await restarted.close()
