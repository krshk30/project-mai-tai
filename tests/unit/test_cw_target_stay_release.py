"""CW target orders rest until a measured per-leg release, not a refresh tick."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition, SystemIncident, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_webull_adapter import _FakeClient, _adapter, _order, fake_sdk  # noqa: F401


def _service(**overrides) -> OmsRiskService:
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = Settings(**overrides)
    return service


def _target_order(account: str, *, tag: str = "CW_TARGET") -> SimpleNamespace:
    return SimpleNamespace(
        broker_account_name=account,
        symbol="NXL",
        side="sell",
        order_type="limit",
        payload={
            "oms_v2_managed_exit": "true",
            "cw_exit_tag": tag,
            "cw_target_managed_row_id": f"row-{account}",
            "order_type": "limit",
            "limit_price": "7.48",
            "session": "AM",
            "extended_hours": "true",
        },
    )


@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
def test_nxl_target_stays_at_748_when_bid_falls_to_741(monkeypatch, account):
    monkeypatch.setattr("project_mai_tai.oms.service._extended_hours_session", lambda now=None: "AM")
    service = _service()
    target = _target_order(account)
    assert service._managed_exit_refresh_exempt(target, bid=7.41)
    assert not service._cw_target_release_due(entry_price=7.1569, bid=7.41)
    assert service._p0a_decline_reason(target, bid=7.41) is None


@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
def test_each_leg_releases_only_at_one_percent_below_its_own_entry(account):
    service = _service()
    _target_order(account)
    assert not service._cw_target_release_due(entry_price=7.1569, bid=7.09)
    assert service._cw_target_release_due(entry_price=7.1569, bid=7.085)
    assert not service._cw_target_release_due(entry_price=7.0, bid=7.085)
    assert service._cw_target_release_due(entry_price=7.0, bid=6.93)


def test_only_tagged_target_is_permanently_refresh_exempt(monkeypatch):
    monkeypatch.setattr("project_mai_tai.oms.service._extended_hours_session", lambda now=None: "AM")
    service = _service()
    assert service._managed_exit_refresh_exempt(_target_order("live:orb"), bid=7.41)
    assert not service._managed_exit_refresh_exempt(
        _target_order("live:orb", tag="CW_HARD_STOP"), bid=7.41
    )
    assert not service._managed_exit_refresh_exempt(
        _target_order("live:orb", tag="CW_FLIP"), bid=7.41
    )


def test_target_stay_flag_defaults_on_and_can_restore_old_refresh(monkeypatch):
    monkeypatch.setattr("project_mai_tai.oms.service._extended_hours_session", lambda now=None: "AM")
    assert Settings().oms_v2_cw_target_stay_enabled is True
    service = _service(oms_v2_cw_target_stay_enabled=False)
    assert not service._managed_exit_refresh_exempt(_target_order("live:orb"), bid=7.41)


def test_target_does_not_hold_across_session_boundary(monkeypatch):
    monkeypatch.setattr("project_mai_tai.oms.service._extended_hours_session", lambda now=None: None)
    service = _service()
    assert not service._managed_exit_refresh_exempt(_target_order("live:orb"), bid=7.60)


class _Broker:
    def __init__(self) -> None:
        self.cancel_outcome = "cancelled"
        self.cancel_outcomes = []
        self.cancel_origin = "broker"
        self.cancels = []
        self.sells = []

    async def fetch_order_update(self, request):
        return ExecutionReport(
            event_type="accepted", client_order_id=request.client_order_id,
            broker_order_id=request.metadata.get("broker_order_id"),
            symbol=request.symbol, side=request.side, intent_type=request.intent_type,
            quantity=request.quantity, metadata=dict(request.metadata),
        )

    async def submit_order(self, request):
        if request.intent_type == "cancel":
            self.cancels.append(request)
            outcome = self.cancel_outcomes.pop(0) if self.cancel_outcomes else self.cancel_outcome
        else:
            self.sells.append(request)
            outcome = "accepted"
        return [ExecutionReport(
            event_type=outcome, client_order_id=request.client_order_id,
            broker_order_id=request.metadata.get("broker_order_id") or "new-sell",
            symbol=request.symbol, side=request.side, intent_type=request.intent_type,
            quantity=request.quantity, metadata=dict(request.metadata),
            origin=self.cancel_origin if outcome == "cancelled" else "broker",
            reason=(
                "cancel requested; awaiting order-detail confirmation"
                if request.intent_type == "cancel" and outcome == "accepted" else ""
            ),
        )]


class _Redis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"


def _db_service(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    broker = _Broker()
    service = OmsRiskService(
        Settings(
            redis_stream_prefix="target-test",
            oms_v2_exit_management_enabled=True,
            oms_v2_exit_close_on_fill_enabled=True,
            oms_v2_overnight_flatten_enabled=True,
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_webull_account_name="live:orb",
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            oms_working_order_refresh_seconds=1,
        ),
        redis_client=_Redis(), session_factory=factory, broker_adapter=broker,
    )
    monkeypatch.setattr("project_mai_tai.oms.service._extended_hours_session", lambda now=None: "AM")
    monkeypatch.setattr(service, "_market_is_fillable", lambda: True)
    return service, factory, broker


def _seed_target(service, factory, account: str, entry: str):
    with factory.begin() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2")
        broker_account = service.store.ensure_broker_account(session, account, provider="schwab" if account.endswith("v2") else "webull", environment="live")
        row = service.store.create_managed_position(
            session, strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol="NXL", entry_price=Decimal(entry), quantity=2, entry_path="ATR Flip",
        )
        intent = service.store.create_trade_intent(
            session, strategy=strategy, broker_account=broker_account,
            event=TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
                strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol="NXL", side="sell", quantity=Decimal("2"),
                intent_type="close", reason="oms_v2_managed_exit:CW_TARGET", metadata={},
            )),
        )
        order = BrokerOrder(
            intent_id=intent.id, strategy_id=strategy.id,
            broker_account_id=broker_account.id, client_order_id=f"target-{account}",
            broker_order_id=f"broker-target-{account}", symbol="NXL", side="sell",
            order_type="limit", time_in_force="day", quantity=Decimal("2"),
            status="accepted", updated_at=datetime.now(UTC) - timedelta(seconds=60),
            payload={
                "oms_v2_managed_exit": "true", "cw_exit_tag": "CW_TARGET",
                "cw_target_managed_row_id": str(row.id), "order_type": "limit",
                "limit_price": "7.48", "session": "AM", "extended_hours": "true",
            },
        )
        session.add(order)
        session.flush()
        return order.id, row.id


@pytest.mark.asyncio
async def test_sync_does_not_cancel_or_reprice_nxl_target_at_741(monkeypatch):
    service, factory, broker = _db_service(monkeypatch)
    for account, entry in (("live:schwab_1m_v2", "7.1569"), ("live:orb", "7.0000")):
        _seed_target(service, factory, account, entry)
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.41, "ask": 7.42, "received_at": datetime.now(UTC),
    }
    await service.sync_broker_orders()
    assert broker.cancels == []
    assert broker.sells == []


@pytest.mark.asyncio
async def test_sync_releases_only_leg_whose_own_entry_crossed_minus_one(monkeypatch):
    service, factory, broker = _db_service(monkeypatch)
    schwab_order, _ = _seed_target(service, factory, "live:schwab_1m_v2", "7.1569")
    webull_order, _ = _seed_target(service, factory, "live:orb", "7.0000")
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.085, "ask": 7.09, "received_at": datetime.now(UTC),
    }
    await service.sync_broker_orders()
    assert [request.broker_account_name for request in broker.cancels] == ["live:schwab_1m_v2"]
    with factory() as session:
        assert session.get(BrokerOrder, schwab_order).status == "cancelled"
        assert session.get(BrokerOrder, webull_order).status == "accepted"
        assert session.scalar(select(OmsManagedPosition).where(OmsManagedPosition.broker_account_name == "live:schwab_1m_v2")).status == "open"
    assert broker.sells == []


@pytest.mark.asyncio
async def test_quote_release_uses_each_leg_entry_and_returns_to_watch(monkeypatch):
    service, factory, broker = _db_service(monkeypatch)
    schwab_order, _ = _seed_target(service, factory, "live:schwab_1m_v2", "7.1569")
    webull_order, _ = _seed_target(service, factory, "live:orb", "7.0000")
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.085, "ask": 7.09, "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service, "_v2_eod_handover_ready", _true_async)
    emitted = []

    async def _emit(*args, **kwargs):
        emitted.append((args, kwargs))
        return "close_submitted"

    monkeypatch.setattr(service, "_emit_v2_exit_on_loop", _emit)
    monkeypatch.setattr(service, "_webull_cw_exit_on_shared_path", _emit)
    await service._evaluate_v2_managed_exit("live:schwab_1m_v2", "NXL")
    await service._evaluate_v2_managed_exit("live:orb", "NXL")
    assert [request.broker_account_name for request in broker.cancels] == ["live:schwab_1m_v2"]
    assert emitted == []
    with factory() as session:
        assert session.get(BrokerOrder, schwab_order).status == "cancelled"
        assert session.get(BrokerOrder, webull_order).status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_new_target_can_be_placed_after_confirmed_release(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    order_id, _ = _seed_target(service, factory, account, "7.1569")
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.085, "ask": 7.09, "received_at": datetime.now(UTC),
    }
    await service.sync_broker_orders()
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "cancelled"
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.60, "ask": 7.61, "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service, "_v2_eod_handover_ready", _true_async)
    emitted = []

    async def _emit(*args, **kwargs):
        emitted.append((args, kwargs))
        return "close_submitted"

    monkeypatch.setattr(service, "_emit_v2_exit_on_loop", _emit)
    monkeypatch.setattr(service, "_webull_cw_exit_on_shared_path", _emit)
    await service._evaluate_v2_managed_exit(account, "NXL")
    assert len(emitted) == 1
    if account == "live:orb":
        assert emitted[0][1]["tag"] == "CW_TARGET"
    else:
        assert emitted[0][1]["reason"] == "oms_v2_managed_exit:CW_TARGET"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_webull_shaped_accepted_then_confirmed_cancel_stays_quiet(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    order_id, _ = _seed_target(service, factory, account, "7.1569")
    broker.cancel_outcomes = ["accepted", "cancelled"]
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.085, "ask": 7.09, "received_at": datetime.now(UTC),
    }
    await service.sync_broker_orders()
    for _ in range(6):
        await service.sync_broker_orders()
    assert len(broker.cancels) == 1
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "accepted"
        assert session.scalars(select(SystemIncident)).all() == []
        assert session.get(BrokerOrder, order_id).payload["cw_target_cancel_requested_at"]
    service._cw_target_cancel_last_attempt[order_id] -= 1.1
    await service.sync_broker_orders()
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "cancelled"
        assert session.scalars(select(SystemIncident)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
@pytest.mark.usefixtures("fake_sdk")
async def test_partially_filled_target_cancels_remaining_order_without_closing_remaining_position(
    monkeypatch, account,
):
    service, factory, broker = _db_service(monkeypatch)
    order_id, row_id = _seed_target(service, factory, account, "7.1569")
    if account == "live:orb":
        prior = _order(
            client_order_id=f"target-{account}", strategy_code="schwab_1m_v2",
            symbol="NXL", side="sell", intent_type="close", quantity=Decimal("100"),
        )
        parsed = await _adapter(_FakeClient({"detail": {
            "order_id": f"broker-target-{account}",
            "items": [{"order_status": "PARTIAL_FILLED", "filled_qty": "40", "filled_price": "7.48"}],
        }})).fetch_order_update(prior)
    else:
        prior = OrderRequest(
            client_order_id=f"target-{account}", broker_account_name=account,
            strategy_code="schwab_1m_v2", symbol="NXL", side="sell",
            intent_type="close", quantity=Decimal("100"), reason="CW_TARGET",
        )
        raw = {
            "orderId": f"broker-target-{account}", "status": "PARTIALLY_FILLED",
            "quantity": 100, "filledQuantity": 40, "enteredTime": "2026-10-01T15:00:00Z",
            "orderActivityCollection": [{"executionLegs": [{
                "quantity": 40, "price": 7.48, "time": "2026-10-01T15:00:01Z",
            }]}],
        }
        parser = object.__new__(SchwabBrokerAdapter)
        parsed = parser._execution_report_from_order(
            request=prior, order=raw, event_type=parser._map_order_status(raw),
            broker_order_id=f"broker-target-{account}",
        )
    assert parsed is not None and parsed.event_type == "partially_filled"
    assert parsed.filled_quantity == Decimal("40")
    remainder = prior.quantity - parsed.filled_quantity
    with factory.begin() as session:
        order = session.get(BrokerOrder, order_id)
        order.quantity = Decimal("100")
        order.status = parsed.event_type
        row = session.get(OmsManagedPosition, row_id)
        row.original_quantity = 100
        row.current_quantity = int(remainder)
    broker.cancel_outcomes = ["accepted", "cancelled"]

    async def attempt() -> bool:
        with factory.begin() as session:
            order = session.get(BrokerOrder, order_id)
            intent = session.get(TradeIntent, order.intent_id)
            return await service._cancel_cw_target_for_release(
                session, order=order, intent=intent,
                account_name=account, reason="one_percent_release",
            )

    assert await attempt() is False
    assert len(broker.cancels) == 1
    assert broker.cancels[0].metadata["broker_order_id"] == f"broker-target-{account}"
    assert broker.sells == []
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "partially_filled"
        assert session.get(OmsManagedPosition, row_id).current_quantity == remainder
    service._cw_target_cancel_last_attempt[order_id] -= 1.1
    assert await attempt() is True
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "cancelled"
        assert session.get(OmsManagedPosition, row_id).current_quantity == remainder
    assert broker.sells == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_unconfirmed_cancel_pages_once_after_ten_seconds(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    order_id, _ = _seed_target(service, factory, account, "7.1569")
    broker.cancel_outcome = "accepted"
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.085, "ask": 7.09, "received_at": datetime.now(UTC),
    }
    await service.sync_broker_orders()
    with factory.begin() as session:
        order = session.get(BrokerOrder, order_id)
        order.payload = {
            **order.payload,
            "cw_target_cancel_requested_at": (datetime.now(UTC) - timedelta(seconds=11)).isoformat(),
        }
    await service.sync_broker_orders()
    await service.sync_broker_orders()
    with factory() as session:
        incidents = session.scalars(select(SystemIncident)).all()
        assert len(incidents) == 1
        assert incidents[0].status == "open"
        assert incidents[0].payload["broker_account_name"] == account
    assert len(broker.cancels) == 1


@pytest.mark.asyncio
async def test_broker_cancel_rejection_pages_immediately(monkeypatch):
    service, factory, broker = _db_service(monkeypatch)
    _seed_target(service, factory, "live:orb", "7.1569")
    broker.cancel_outcome = "rejected"
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.085, "ask": 7.09, "received_at": datetime.now(UTC),
    }
    await service.sync_broker_orders()
    with factory() as session:
        assert session.scalars(select(SystemIncident)).one().status == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_quote_storm_sends_at_most_one_cancel_per_order_per_second(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    order_id, _ = _seed_target(service, factory, account, "7.1569")
    broker.cancel_outcome = "accepted"
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.085, "ask": 7.09, "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service, "_v2_eod_handover_ready", _true_async)
    for _ in range(8):
        await service._evaluate_v2_managed_exit(account, "NXL")
    assert len(broker.cancels) == 1
    service._cw_target_cancel_last_attempt[order_id] -= 1.1
    await service._evaluate_v2_managed_exit(account, "NXL")
    assert len(broker.cancels) == 2
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "accepted"


@pytest.mark.asyncio
async def test_client_only_cancel_claim_cannot_release_reserved_shares(monkeypatch):
    service, factory, broker = _db_service(monkeypatch)
    order_id, _ = _seed_target(service, factory, "live:orb", "7.1569")
    broker.cancel_origin = "client"
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.085, "ask": 7.09, "received_at": datetime.now(UTC),
    }
    await service.sync_broker_orders()
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "accepted"
        assert session.scalars(select(SystemIncident)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_session_end_releases_target_without_repricing(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    order_id, _ = _seed_target(service, factory, account, "7.1569")
    monkeypatch.setattr("project_mai_tai.oms.service._extended_hours_session", lambda now=None: None)
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.50, "ask": 7.51, "received_at": datetime.now(UTC),
    }
    await service.sync_broker_orders()
    assert len(broker.cancels) == 1
    assert not broker.sells
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_overnight_flatten_submits_even_if_target_cancel_unconfirmed(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    _, row_id = _seed_target(service, factory, account, "7.1569")
    broker.cancel_outcome = "accepted"
    service._managed_v2_symbols.add((account, "NXL"))
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.40, "ask": 7.41, "received_at": datetime.now(UTC),
    }
    with factory() as session:
        assert service._read_v2_managed_snapshot(session, account, "NXL", True).dedup_active
    monkeypatch.setattr(service, "_v2_overnight_flatten_due", lambda: True)

    async def _handover(*_args):
        return True

    emitted = []

    async def _emit(acct, symbol, *_args, **kwargs):
        emitted.append((acct, symbol, kwargs))
        return "close_submitted"

    monkeypatch.setattr(service, "_v2_eod_handover_ready", _handover)
    monkeypatch.setattr(service, "_emit_v2_exit_on_loop", _emit)
    await service._v2_overnight_flatten()
    assert [request.broker_account_name for request in broker.cancels] == [account]
    assert len(emitted) == 1
    assert emitted[0][2]["expected_managed_row_id"] == str(row_id)
    assert emitted[0][2]["allow_unconfirmed_overnight"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_hard_stop_waits_for_broker_confirmed_target_cancel(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    _seed_target(service, factory, account, "7.1569")
    broker.cancel_outcome = "accepted"
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 6.40, "ask": 6.41, "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service, "_v2_eod_handover_ready", _true_async)
    emitted = []

    async def _emit(*args, **kwargs):
        emitted.append((args, kwargs))
        return "close_submitted"

    monkeypatch.setattr(service, "_emit_v2_exit_on_loop", _emit)
    monkeypatch.setattr(service, "_webull_cw_exit_on_shared_path", _emit)
    await service._evaluate_v2_managed_exit(account, "NXL")
    assert len(broker.cancels) == 1
    assert emitted == []
    with factory() as session:
        assert session.scalars(select(SystemIncident)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_hard_stop_proceeds_after_target_cancel_is_confirmed(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    order_id, _ = _seed_target(service, factory, account, "7.1569")
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 6.40, "ask": 6.41, "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service, "_v2_eod_handover_ready", _true_async)
    emitted = []

    async def _emit(*args, **kwargs):
        emitted.append((args, kwargs))
        return "close_submitted"

    monkeypatch.setattr(service, "_emit_v2_exit_on_loop", _emit)
    monkeypatch.setattr(service, "_webull_cw_exit_on_shared_path", _emit)
    await service._evaluate_v2_managed_exit(account, "NXL")
    assert len(broker.cancels) == 1
    assert len(emitted) == 1
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_flip_cancels_target_before_emitting_new_exit(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    order_id, row_id = _seed_target(service, factory, account, "7.1569")
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.40, "ask": 7.41, "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service, "_v2_eod_handover_ready", _true_async)
    monkeypatch.setattr(
        service, "_fresh_cw_flip_decision",
        lambda _key: SimpleNamespace(managed_row_id=str(row_id), bar_time_ms=1, decision_id="flip-1"),
    )
    emitted = []

    async def _emit(*args, **kwargs):
        emitted.append((args, kwargs))
        return "close_submitted"

    monkeypatch.setattr(service, "_emit_v2_exit_on_loop", _emit)
    monkeypatch.setattr(service, "_webull_cw_exit_on_shared_path", _emit)
    await service._evaluate_v2_managed_exit(account, "NXL")
    assert len(broker.cancels) == 1
    assert len(emitted) == 1
    with factory() as session:
        assert session.get(BrokerOrder, order_id).status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_confirmation_decision_remains_pending_when_target_cancel_unconfirmed(monkeypatch, account):
    service, factory, broker = _db_service(monkeypatch)
    _, row_id = _seed_target(service, factory, account, "7.1569")
    broker.cancel_outcome = "accepted"
    now = datetime.now(UTC)
    service._latest_quotes_by_symbol["NXL"] = {
        "bid": 7.40, "ask": 7.41, "received_at": now,
    }
    pending = {
        "evaluated_at_ms": str(int((now - timedelta(seconds=1)).timestamp() * 1000)),
        "bound_managed_row_id": str(row_id),
        "source_fill_id": "confirmation-1",
    }
    service.__dict__.setdefault("_confirmation_exit_pending", {})[(account, "NXL")] = pending
    monkeypatch.setattr(service, "_v2_eod_handover_ready", _true_async)

    async def _bound(*_args):
        return str(row_id)

    monkeypatch.setattr(service, "_confirmation_bound_managed_row_id", _bound)
    released = []

    async def _release(*_args):
        released.append(True)
        return "released"

    monkeypatch.setattr(service, "_reconcile_confirmation_exit_protection", _release)
    await service._evaluate_v2_managed_exit(account, "NXL")
    assert len(broker.cancels) == 1
    assert service._confirmation_exit_pending[(account, "NXL")] is pending
    assert released == []


async def _true_async(*_args, **_kwargs):
    return True


@pytest.mark.asyncio
async def test_quote_evaluates_both_managed_legs_without_serial_broker_wait(monkeypatch):
    service, _factory, _broker = _db_service(monkeypatch)
    service._managed_v2_symbols.update({("live:schwab_1m_v2", "NXL"), ("live:orb", "NXL")})
    monkeypatch.setattr(service, "_market_is_fillable", lambda: True)
    monkeypatch.setattr(service, "_cancel_drifted_working_orders", _true_async)
    monkeypatch.setattr(service, "_evaluate_webull_mirror_deferred_resubmits", _true_async)
    entered = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def _evaluate(acct, _symbol):
        entered.add(acct)
        if len(entered) == 2:
            both_entered.set()
        await release.wait()

    monkeypatch.setattr(service, "_evaluate_v2_managed_exit", _evaluate)
    tick = SimpleNamespace(
        payload=SimpleNamespace(symbol="NXL", bid_price=7.40, ask_price=7.41),
        produced_at=datetime.now(UTC),
    )
    task = asyncio.create_task(service._handle_quote_tick_event(tick))
    try:
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        assert entered == {"live:schwab_1m_v2", "live:orb"}
    finally:
        release.set()
        await task
