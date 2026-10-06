"""Recorded APUS/IPDN order trees; explicitly counterfactual cancellation/fill races."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from tests.unit.managed_entry_fixtures import bind_managed_entry

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters import schwab as schwab_module
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AccountPosition, BrokerOrder, Fill, Strategy, VirtualPosition
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.service import ArmedHardStop, OmsRiskService
from project_mai_tai.settings import Settings

RECORDED = json.loads((Path(__file__).parents[1] / "fixtures/reserve1/recorded_orders.json").read_text())
ACCT = "live:schwab_1m_v2"


class Redis:
    async def xadd(self, *args, **kwargs):
        return b"1-1"


class RecordedSchwab(SchwabBrokerAdapter):
    def __init__(self, case, *, reject=False, fill_during_release=False, unknown=False,
                 cancel_leaves_working=False):
        self.tree = copy.deepcopy(case["tree"])
        self.accounts_by_name = {ACCT: SchwabAccountConfig("test-account")}
        self.calls = []
        self.requests = []
        self.reject = reject
        self.fill_during_release = fill_during_release
        self.unknown = unknown
        self.cancel_leaves_working = cancel_leaves_working
        self.fill_on_retry_release = False
        self.reject_retry = False
        self.release_count = 0
        self.read_proof = "rejected"
        self.pause = None

    async def _authorized_request_json(self, method, path, *, body=None):
        self.calls.append((method, path))
        if self.pause is not None:
            await self.pause.wait()
        if self.unknown:
            return 503, {}, {}
        if method == "GET":
            if "/orders?" in path:
                return 200, {}, [copy.deepcopy(self.tree)]
            assert path.endswith("/" + str(self.tree["orderId"]))
            return 200, {}, copy.deepcopy(self.tree)
        assert method == "DELETE"
        children = self.tree["childOrderStrategies"][0]["childOrderStrategies"]
        if self.cancel_leaves_working:
            return 200, {}, {}
        for child in children:
            child["status"] = "CANCELED"
            child["remainingQuantity"] = 0
        if self.fill_during_release:
            children[0]["status"] = "FILLED"
            children[0]["filledQuantity"] = self.tree["quantity"]
            children[0]["closeTime"] = RECORDED["apus"]["decision_time"]
            children[0]["orderActivityCollection"] = [{
                "activityType": "EXECUTION", "executionType": "FILL",
                "quantity": self.tree["quantity"],
                "executionLegs": [{"legId": 1, "quantity": self.tree["quantity"],
                                   "price": children[0]["price"],
                                   "time": RECORDED["apus"]["decision_time"]}],
            }]
        return 200, {}, {}

    async def release_native_oco_for_close(self, account, parent):
        self.release_count += 1
        if self.fill_on_retry_release and self.release_count == 2:
            child = self.tree["childOrderStrategies"][0]["childOrderStrategies"][0]
            child["status"] = "FILLED"
            child["filledQuantity"] = self.tree["quantity"]
        return await super().release_native_oco_for_close(account, parent)

    async def submit_order(self, request):
        self.requests.append(request)
        self.calls.append(("SUBMIT", request.client_order_id))
        if self.reject:
            return [ExecutionReport(
                event_type="rejected", origin="broker", client_order_id=request.client_order_id,
                broker_order_id=(str(RECORDED["apus_rejected_close"]["orderId"])
                                 if len(self.requests) == 1 else f"counterfactual-rejected-{len(self.requests)}"),
                symbol=request.symbol, side="sell", intent_type="close", quantity=request.quantity,
                reason=RECORDED["oversold_reason"],
            )]
        return [ExecutionReport(
            event_type="filled", origin="broker", client_order_id=request.client_order_id,
            broker_order_id="recorded-shape-close", broker_fill_id="recorded-shape-fill",
            symbol=request.symbol, side="sell", intent_type="close", quantity=request.quantity,
            filled_quantity=request.quantity,
            fill_price=Decimal(request.metadata["reference_price"]),
        )]

    async def fetch_order_update(self, request):
        self.calls.append(("REJECTION-PROOF", request.metadata["broker_order_id"]))
        if self.read_proof == "unknown":
            return None
        if self.read_proof == "filled":
            return ExecutionReport(
                event_type="filled", origin="broker", client_order_id=request.client_order_id,
                broker_order_id=request.metadata["broker_order_id"], symbol=request.symbol,
                side="sell", intent_type="close", filled_quantity=request.quantity,
            )
        self.reject = self.reject_retry
        return ExecutionReport(
            event_type="rejected", origin="broker", client_order_id=request.client_order_id,
            broker_order_id=request.metadata["broker_order_id"], symbol=request.symbol,
            side="sell", intent_type="close", reason=RECORDED["oversold_reason"],
        )

    async def list_account_positions(self, account):
        # Refused sells must not be interpreted as flat during these tests.
        from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
        return [BrokerPositionSnapshot(
            broker_account_name=account,
            symbol=self.tree["orderLegCollection"][0]["instrument"]["symbol"],
            quantity=Decimal(self.tree["quantity"]), average_price=Decimal("7.62"),
        )]


def harness(monkeypatch, name="apus", **adapter_kwargs):
    case = RECORDED[name]
    now = datetime.fromisoformat(case["decision_time"])
    class BrokerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)
    monkeypatch.setattr(schwab_module, "datetime", BrokerClock)
    monkeypatch.setattr(service_module, "utcnow", lambda: now)
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda *args: True)
    monkeypatch.setattr(service_module, "_extended_hours_session", lambda *args: None)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        table for table in Base.metadata.sorted_tables
        if table.name not in {"market_trade_ticks", "market_quote_ticks"}
    ])
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    adapter = RecordedSchwab(case, **adapter_kwargs)
    service = OmsRiskService(
        Settings(oms_v2_exit_management_enabled=True, oms_v2_exit_close_on_fill_enabled=True,
                 strategy_schwab_1m_v2_account_name=ACCT,
                 strategy_schwab_1m_v2_confirmed_window_enabled=True,
                 oms_v2_cw_floor_exit_enabled=False),
        redis_client=Redis(), session_factory=sessions, broker_adapter=adapter,
    )
    service._cw_target_pct = 5.0
    service._cw_stop_pct = 8.0
    service.logger = logging.getLogger("reserve1-recorded")
    symbol = case["tree"]["orderLegCollection"][0]["instrument"]["symbol"]
    with sessions() as session:
        service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        service.store.ensure_broker_account(session, ACCT, provider="schwab", environment="test")
        session.flush()
        row = service.store.create_managed_position(
            session, strategy_code="schwab_1m_v2", broker_account_name=ACCT, symbol=symbol,
            entry_price=Decimal(case["entry_price"]), quantity=case["tree"]["quantity"],
            entry_path="ATR Flip", entry_time=datetime.fromisoformat(case["entry_time"]),
        )
        entry = bind_managed_entry(session, row)
        entry.client_order_id = case["entry_coid"]
        entry.broker_order_id = str(case["tree"]["orderId"])
        row.entry_client_order_id = entry.client_order_id
        row_id = str(row.id)
        session.commit()
    service._managed_v2_symbols.add((ACCT, symbol))
    service._latest_quotes_by_symbol[symbol] = {
        "bid": float(case["bid"]), "ask": float(case["bid"]) + .01, "received_at": now,
    }
    service._arm_cw_flip_pending(
        (ACCT, symbol), bar_time_ms=int((now - timedelta(seconds=63)).timestamp() * 1000),
        managed_row_id=row_id,
    )
    return service, sessions, adapter, symbol, now


def sell_orders(sessions):
    with sessions() as session:
        return list(session.scalars(select(BrokerOrder).where(BrokerOrder.side == "sell")))


def armed_harness(monkeypatch, *, strategy="schwab_1m_v2", **kwargs):
    """APUS broker tree through the actual hard-stop sender; ORB is a 2-share control.

    ORB uses its production virtual-position/fill binding, never an artificial v2 row.
    Cancellation and child-fill responses are explicit counterfactuals, not claimed live fills.
    """
    service, sessions, adapter, symbol, now = harness(monkeypatch, **kwargs)
    service.settings.strategy_schwab_1m_v2_broker_provider = "schwab"
    service.settings.oms_record_native_oco_exit_fills_enabled = True
    quantity = Decimal(78) if strategy == "schwab_1m_v2" else Decimal(2)
    with sessions() as session:
        row = service.store.get_open_managed_position(session, broker_account_name=ACCT, symbol=symbol)
        entry = session.get(BrokerOrder, row.entry_order_id)
        if strategy == "orb_schwab":
            owner = service.store.ensure_strategy(session, strategy, name="ORB-Schwab")
            session.flush()
            entry.strategy_id = owner.id
            entry.quantity = quantity
            session.delete(row)
            adapter.tree["quantity"] = int(quantity)
            adapter.tree["filledQuantity"] = int(quantity)
            for child in adapter.tree["childOrderStrategies"][0]["childOrderStrategies"]:
                child["quantity"] = int(quantity)
                child["remainingQuantity"] = int(quantity)
        entry_time = datetime.fromisoformat(RECORDED["apus"]["entry_time"])
        session.add_all([
            VirtualPosition(strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
                            symbol=symbol, quantity=quantity, average_price=Decimal("7.62"),
                            opened_at=entry_time),
            AccountPosition(broker_account_id=entry.broker_account_id, symbol=symbol,
                            quantity=quantity, average_price=Decimal("7.62")),
            Fill(order_id=entry.id, strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
                 broker_fill_id="recorded-shape-owned-entry", symbol=symbol, side="buy",
                 quantity=quantity, price=Decimal("7.62"), filled_at=entry_time),
        ])
        session.commit()
    stop = ArmedHardStop(
        strategy_code=strategy, broker_account_name=ACCT, symbol=symbol, quantity=quantity,
        entry_price=Decimal("7.62"), stop_loss_pct=8, stop_price=Decimal("7.0104"),
        quote_max_age_ms=2000, initial_panic_buffer_pct=1.5,
    )
    service._armed_hard_stops[service._hard_stop_key(strategy, ACCT, symbol)] = stop
    return service, sessions, adapter, symbol, now, stop


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_armed_hard_stop_working_schwab_children_release_confirm_then_real_close(monkeypatch, strategy):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch, strategy=strategy)
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert [method for method, _ in adapter.calls] == ["GET", "DELETE", "DELETE", "GET", "SUBMIT"]
    assert len(adapter.requests) == 1 and adapter.requests[0].quantity == stop.quantity
    assert adapter.requests[0].strategy_code == strategy
    assert [order.status for order in sell_orders(sessions)] == ["filled"]
    with sessions() as session:
        position = session.scalar(select(VirtualPosition).join(Strategy).where(Strategy.code == strategy))
        assert position.quantity == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_armed_hard_stop_child_fills_during_release_records_owned_fill_no_second_sell(monkeypatch, strategy):
    service, sessions, adapter, symbol, _, stop = armed_harness(
        monkeypatch, strategy=strategy, fill_during_release=True,
    )
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert adapter.requests == []
    assert [method for method, _ in adapter.calls][:4] == ["GET", "DELETE", "DELETE", "GET"]
    with sessions() as session:
        assert session.scalar(select(Fill.id).where(Fill.side == "sell")) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_armed_hard_stop_unknown_parent_retains_stop_and_never_sends(monkeypatch, strategy):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch, strategy=strategy, unknown=True)
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert adapter.requests == [] and sell_orders(sessions) == []
    assert service._armed_hard_stops[service._hard_stop_key(strategy, ACCT, symbol)] is stop
    assert not stop.close_in_flight


@pytest.mark.asyncio
async def test_armed_hard_stop_known_native_guard_dedup_still_precedes_release(monkeypatch, caplog):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch)
    monkeypatch.setattr(service.logger, "handlers", [caplog.handler])
    async def known_guard(**kwargs):
        return True
    monkeypatch.setattr(service, "_has_active_native_stop_guard_order", known_guard)
    with caplog.at_level(logging.INFO):
        await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert adapter.calls == [] and sell_orders(sessions) == []
    assert "[OMS-V2-CW-FLIP-NOTE]" in caplog.text and "reason=known_native_stop_guard" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_armed_hard_stop_oversold_retries_once_only_after_broker_rejection_and_release(monkeypatch, strategy):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch, strategy=strategy, reject=True)
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert len(adapter.requests) == 2 and adapter.release_count == 2
    assert adapter.requests[1].metadata["reserve1_retry_of"] == adapter.requests[0].client_order_id
    assert [order.status for order in sell_orders(sessions)] == ["rejected", "filled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["unknown", "filled"])
async def test_armed_hard_stop_original_close_unknown_or_filled_never_retries(monkeypatch, proof):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch, reject=True)
    adapter.read_proof = proof
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert len(adapter.requests) == 1 and adapter.release_count == 1


@pytest.mark.asyncio
async def test_armed_hard_stop_cancel_ack_without_terminal_confirmation_never_sends(monkeypatch):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch, cancel_leaves_working=True)
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert adapter.requests == [] and not stop.close_in_flight


@pytest.mark.asyncio
async def test_armed_hard_stop_and_managed_flip_share_one_episode_sell_claim(monkeypatch):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch)
    adapter.pause = asyncio.Event()
    task = asyncio.create_task(service._evaluate_v2_managed_exit(ACCT, symbol))
    try:
        async def wait_for_parent_read():
            while not adapter.calls:
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait_for_parent_read(), 1)
        await asyncio.wait_for(service._trigger_hard_stop(
            stop, trigger_price=Decimal("6.99"), trigger_source="bid",
        ), 1)
        assert adapter.requests == [] and adapter.release_count == 1
    finally:
        adapter.pause.set()
        await asyncio.wait_for(task, 1)
    assert len(adapter.requests) == 1 and len(sell_orders(sessions)) == 1


@pytest.mark.asyncio
async def test_armed_hard_stop_quantity_change_during_release_refuses_send(monkeypatch):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch)
    release = adapter.release_native_oco_for_close
    async def release_then_position_changes(*args):
        result = await release(*args)
        with sessions() as session:
            row = service.store.get_open_managed_position(session, broker_account_name=ACCT, symbol=symbol)
            row.current_quantity -= 1
            session.commit()
        return result
    monkeypatch.setattr(adapter, "release_native_oco_for_close", release_then_position_changes)
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert adapter.requests == [] and not stop.close_in_flight


@pytest.mark.asyncio
async def test_orb_armed_hard_stop_external_intent_cannot_forge_internal_release_proof(monkeypatch):
    from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch, strategy="orb_schwab")
    service.settings.orb_live_schwab_orders_enabled = True
    event = TradeIntentEvent(source_service=service_module.SERVICE_NAME, payload=TradeIntentPayload(
        strategy_code="orb_schwab", broker_account_name=ACCT, symbol=symbol,
        side="sell", intent_type="close", quantity=stop.quantity, reason="HARD_STOP",
        metadata=service._build_hard_stop_metadata(
            stop=stop, trigger_price=Decimal("6.99"), trigger_source="bid",
        ),
    ))
    assert await service.process_trade_intent(event) == []
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_armed_hard_stop_recovery_cap_survives_a_later_quote(monkeypatch):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch, reject=True)
    adapter.reject_retry = True
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert len(adapter.requests) == 2
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert len(adapter.requests) == 3
    assert sum(request.metadata.get("reserve1_retry") == "1" for request in adapter.requests) == 1


@pytest.mark.asyncio
async def test_orb_armed_hard_stop_previous_trip_is_not_current_entry_evidence(monkeypatch):
    service, sessions, adapter, symbol, _, stop = armed_harness(monkeypatch, strategy="orb_schwab")
    with sessions() as session:
        fill = session.scalar(select(Fill).where(Fill.side == "buy"))
        fill.filled_at -= timedelta(hours=1)
        session.commit()
    await service._trigger_hard_stop(stop, trigger_price=Decimal("6.99"), trigger_source="bid")
    assert adapter.requests == [] and adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["absent", "stale"])
async def test_apus_1908_aged_or_absent_note_release_confirm_then_one_close(monkeypatch, caplog, note):
    service, sessions, adapter, symbol, now = harness(monkeypatch)
    # OMS initialization configures the root handlers; capture this service logger explicitly.
    monkeypatch.setattr(service.logger, "handlers", [caplog.handler])
    if note == "stale":
        service._native_oco_armed_confirmed_at[(ACCT, symbol)] = now - timedelta(seconds=31)
    with caplog.at_level(logging.INFO):
        await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert f"state={note}" in caplog.text and "[OMS-V2-CW-FLIP-NOTE]" in caplog.text
    assert "[OMS-V2-CW-FLIP-PROTECTION]" in caplog.text and "status=RELEASED" in caplog.text
    assert [method for method, _ in adapter.calls] == ["GET", "DELETE", "DELETE", "GET", "SUBMIT"]
    assert adapter.release_count == 1
    assert len(adapter.requests) == 1 and adapter.requests[0].quantity == Decimal(78)
    assert [order.status for order in sell_orders(sessions)] == ["filled"]


@pytest.mark.asyncio
async def test_ipdn_1803_fresh_note_control_reuses_release_without_extra_parent_read(monkeypatch):
    service, sessions, adapter, symbol, now = harness(monkeypatch, "ipdn")
    service._native_oco_armed_confirmed_at[(ACCT, symbol)] = now
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert adapter.release_count == 1
    assert [method for method, _ in adapter.calls] == ["GET", "SUBMIT"]
    assert [order.status for order in sell_orders(sessions)] == ["filled"]


@pytest.mark.asyncio
async def test_apus_child_fill_during_cancel_suppresses_software_sell(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch, fill_during_release=True)
    resolved = []
    async def close_resolved(account, sym, *, expected_row_id):
        resolved.append((account, sym, expected_row_id))
        return True
    service._close_resolved_oco_managed_row = close_resolved
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert resolved and adapter.requests == [] and sell_orders(sessions) == []
    assert [method for method, _ in adapter.calls] == ["GET", "DELETE", "DELETE", "GET"]


@pytest.mark.asyncio
async def test_apus_unreadable_parent_never_fails_open_on_send(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch, unknown=True)
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert adapter.requests == [] and sell_orders(sessions) == []


@pytest.mark.asyncio
async def test_apus_cancel_ack_without_terminal_reread_never_authorizes_close(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch, cancel_leaves_working=True)
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert [method for method, _ in adapter.calls] == ["GET", "DELETE", "DELETE", "GET"]
    assert adapter.requests == [] and sell_orders(sessions) == []


@pytest.mark.asyncio
async def test_schwab_software_hard_stop_also_releases_exact_parent(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch)
    service._clear_cw_flip_pending((ACCT, symbol))
    service._latest_quotes_by_symbol[symbol]["bid"] = 6.99
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert [method for method, _ in adapter.calls] == ["GET", "DELETE", "DELETE", "GET", "SUBMIT"]
    assert len(sell_orders(sessions)) == 1
    assert adapter.requests[0].reason.endswith("CW_HARD_STOP")


@pytest.mark.asyncio
async def test_schwab_oversold_recovery_requires_rejection_proof_then_release_once(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch, reject=True)
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert adapter.release_count == 2 and len(adapter.requests) == 2
    assert adapter.calls.index(("REJECTION-PROOF", "1008196843931")) < len(adapter.calls) - 2
    assert adapter.requests[1].metadata["reserve1_retry_of"] == adapter.requests[0].client_order_id
    assert [order.status for order in sell_orders(sessions)] == ["rejected", "filled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["unknown", "filled"])
async def test_schwab_original_close_unknown_or_filled_never_retries(monkeypatch, proof):
    service, sessions, adapter, symbol, _ = harness(monkeypatch, reject=True)
    adapter.read_proof = proof
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert len(adapter.requests) == 1 and adapter.release_count == 1


@pytest.mark.asyncio
async def test_schwab_child_fill_during_oversold_recovery_prevents_second_sell(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch, reject=True)
    adapter.fill_on_retry_release = True
    resolved = []
    async def close_resolved(account, sym, *, expected_row_id):
        resolved.append((account, sym, expected_row_id))
        return True
    service._close_resolved_oco_managed_row = close_resolved
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert resolved and len(adapter.requests) == 1 and adapter.release_count == 2
    assert [order.status for order in sell_orders(sessions)] == ["rejected"]


@pytest.mark.asyncio
async def test_schwab_second_oversold_reject_has_no_recursive_retry(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch, reject=True)
    adapter.reject_retry = True
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert len(adapter.requests) == 2 and adapter.release_count == 2
    assert len(service._reserve1_retry_used) == 1
    assert [order.status for order in sell_orders(sessions)] == ["rejected", "rejected"]


@pytest.mark.asyncio
async def test_schwab_later_evaluation_cannot_repeat_episode_recovery(monkeypatch):
    service, sessions, adapter, symbol, now = harness(monkeypatch, reject=True)
    adapter.reject_retry = True
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    service._a2_should_defer = lambda *args: False
    service._v2_exit_stood_down.clear()
    service._arm_cw_flip_pending(
        (ACCT, symbol), bar_time_ms=int((now - timedelta(seconds=63)).timestamp() * 1000),
        managed_row_id=next(iter(service._reserve1_retry_used))[2],
    )
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    # Existing ladder retries are unchanged; the recovery itself may not repeat.
    assert len(adapter.requests) == 3
    assert sum(request.metadata.get("reserve1_retry") == "1" for request in adapter.requests) == 1


@pytest.mark.asyncio
async def test_existing_exit_backoff_does_not_strip_native_protection(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch)
    service._a2_should_defer = lambda *args: True
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert adapter.requests == [] and adapter.calls == [] and sell_orders(sessions) == []


@pytest.mark.asyncio
async def test_existing_1955_unknown_handover_keeps_explicit_flatten_exception(monkeypatch):
    # Counterfactual APUS order tree at the existing flatten policy boundary, not a new rule.
    service, sessions, adapter, symbol, _ = harness(monkeypatch, unknown=True)
    service._v2_overnight_flatten_due = lambda: True
    async def unknown_handover(*args):
        return False
    service._v2_eod_handover_ready = unknown_handover
    with sessions() as session:
        snapshot = service._read_v2_managed_snapshot(session, ACCT, symbol, True)
    result = await service._emit_v2_exit_on_loop(
        ACCT, symbol, service._hydrate_v2_position(snapshot), snapshot.entry_price,
        kind="OVERNIGHT_FLATTEN", reference_price=7.15, reason="V2_OVERNIGHT_FLATTEN",
        bid=7.15, close_on_fill=True, expected_managed_row_id=snapshot.managed_row_id,
        allow_unconfirmed_overnight=True,
    )
    assert result == "closed" and len(adapter.requests) == 1
    assert adapter.release_count == 0


@pytest.mark.asyncio
async def test_schwab_quantity_change_during_release_never_sells_stale_lot(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch)
    release = service._release_native_oco_for_cw_flip
    async def release_then_change(*args, **kwargs):
        result = await release(*args, **kwargs)
        with sessions() as session:
            row = service.store.get_open_managed_position(session, broker_account_name=ACCT, symbol=symbol)
            row.current_quantity -= 1
            session.commit()
        return result
    service._release_native_oco_for_cw_flip = release_then_change
    await service._evaluate_v2_managed_exit(ACCT, symbol)
    assert adapter.release_count == 1 and adapter.requests == [] and sell_orders(sessions) == []


@pytest.mark.asyncio
async def test_schwab_parallel_quotes_never_submit_two_sells_for_one_lot(monkeypatch):
    service, sessions, adapter, symbol, _ = harness(monkeypatch)
    adapter.pause = asyncio.Event()
    first = asyncio.create_task(service._evaluate_v2_managed_exit(ACCT, symbol))
    while not adapter.calls:
        await asyncio.sleep(0)
    try:
        await asyncio.wait_for(service._evaluate_v2_managed_exit(ACCT, symbol), timeout=1)
    finally:
        adapter.pause.set()
        await first
    assert len(adapter.requests) == 1 and len(sell_orders(sessions)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("record", RECORDED["nxl_rejected_closes"])
async def test_nxl_three_retained_oversold_reports_require_exact_rejected_zero_fill(record):
    service = OmsRiskService.__new__(OmsRiskService)
    request = OrderRequest(
        record["coid"], ACCT, "schwab_1m_v2", "NXL", "sell", "close", Decimal(record["quantity"]),
        "oms_v2_managed_exit:CW_FLIP", metadata={"limit_price": str(record["price"])},
    )
    report = ExecutionReport(
        event_type="rejected", origin="broker", client_order_id=record["coid"],
        broker_order_id=str(record["orderId"]), symbol="NXL", side="sell", intent_type="close",
        reason=RECORDED["oversold_reason"], filled_quantity=Decimal(record["filledQuantity"]),
    )
    class Broker:
        async def fetch_order_update(self, read_request):
            assert read_request.metadata["broker_order_id"] == str(record["orderId"])
            return report
    service.broker_adapter = Broker()
    assert await service._reserve1_original_close_rejected(request, [report])
    assert not await service._reserve1_original_close_rejected(request, [replace(report, origin="client")])
    assert not await service._reserve1_original_close_rejected(request, [replace(report, filled_quantity=Decimal(1))])


@pytest.mark.parametrize("age,state", [(None, "absent"), (0, "fresh"), (30, "fresh"), (31, "stale")])
def test_note_state_logging_measures_absent_fresh_stale(monkeypatch, caplog, age, state):
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = Settings()
    service.logger = logging.getLogger("reserve1-note")
    now = datetime(2026, 10, 6, 19, 8, tzinfo=UTC)
    monkeypatch.setattr(service_module, "utcnow", lambda: now)
    service._native_oco_armed_confirmed_at = {} if age is None else {(ACCT, "APUS"): now - timedelta(seconds=age)}
    with caplog.at_level(logging.INFO):
        service._log_native_oco_note(ACCT, "APUS")
    assert f"state={state}" in caplog.text
    assert f"age_s={'unknown' if age is None else f'{age:.3f}'}" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ok", "failed", "cancelled"])
async def test_sync_pass_start_end_duration_survives_errors(monkeypatch, caplog, outcome):
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = Settings()
    service.logger = logging.getLogger("reserve1-sync")
    async def sync(*, account_names):
        if outcome == "failed":
            raise RuntimeError("broker read failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return {"orders": 1}
    service._sync_broker_state_pass = sync
    ticks = iter([100.0, 131.0])
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with caplog.at_level(logging.INFO):
        if outcome == "ok":
            assert await service.sync_broker_state() == {"orders": 1}
        else:
            with pytest.raises(RuntimeError if outcome == "failed" else asyncio.CancelledError):
                await service.sync_broker_state()
    assert caplog.text.count("phase=start") == 1 and caplog.text.count("phase=end") == 1
    assert f"outcome={outcome}" in caplog.text
    assert "duration_ms=31000.000" in caplog.text and "exceeds_note_age=True" in caplog.text
