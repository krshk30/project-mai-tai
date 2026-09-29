from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport, OrderRequest
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, Fill, Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.orb_schwab_order_route import (
    build_orb_schwab_cancel_intent,
    build_orb_schwab_open_intent,
    build_orb_schwab_reprice_intent,
    orb_schwab_intent_refusal,
    publish_orb_schwab_intent,
)
from project_mai_tai.runtime_registry import strategy_registration_map
from project_mai_tai.settings import Settings

OPEN = datetime(2026, 9, 29, 13, 28, tzinfo=UTC)
ACCOUNT = "live:schwab_1m_v2"


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        orb_enabled=True,
        orb_live_schwab_orders_enabled=enabled,
        strategy_schwab_1m_v2_account_name=ACCOUNT,
        strategy_schwab_1m_v2_broker_provider="schwab",
        redis_stream_prefix="test",
    )


class _Redis:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.streams: list[str] = []

    async def xadd(self, stream, fields, **_kwargs):
        self.streams.append(stream)
        self.events.append(json.loads(fields["data"]))
        return "1-0"


class _Broker:
    def __init__(
        self, *, preview_accepted: bool = True, positions=None, parent_status: str = "accepted"
    ) -> None:
        self.preview_accepted = preview_accepted
        self.positions = positions or []
        self.parent_status = parent_status
        self.submitted = []
        self.previewed = []
        self.replaced = []
        self.replace_report = "accepted"
        self.exit_detail = None

    async def list_account_positions(self, _account):
        return self.positions

    async def preview_bracket_order(self, request):
        self.previewed.append(request)
        if self.preview_accepted:
            return 200, {"status": "ACCEPTED", "orderValidationResult": {"rejects": []}}
        return 200, {"status": "REJECTED", "orderValidationResult": {"rejects": ["invalid"]}}

    async def submit_order(self, request):
        self.submitted.append(request)
        return [
            ExecutionReport(
                event_type="cancelled" if request.intent_type == "cancel" else "accepted",
                client_order_id=request.client_order_id,
                broker_order_id="SCHWAB-ORB-1",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        return ExecutionReport(
            event_type=self.parent_status,
            client_order_id=request.client_order_id,
            broker_order_id=request.metadata.get("broker_order_id", "SCHWAB-ORB-1"),
            symbol=request.symbol,
            side="buy",
            intent_type="open",
            quantity=request.quantity,
            filled_quantity=Decimal("1") if self.parent_status == "filled" else Decimal("0"),
        )

    async def replace_bracket_order(self, request, broker_order_id):
        self.replaced.append((request, broker_order_id))
        if self.replace_report == "unknown":
            return None
        return ExecutionReport(
            event_type=self.replace_report,
            client_order_id=request.client_order_id,
            broker_order_id="SCHWAB-ORB-2" if self.replace_report == "accepted" else broker_order_id,
            symbol=request.symbol,
            side="buy",
            intent_type="open",
            quantity=request.quantity,
            metadata=dict(request.metadata),
            origin="broker",
        )

    async def fetch_oco_exit_fill(self, _account, _symbol, _client_id, **_kwargs):
        return self.exit_detail


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _service(monkeypatch, *, enabled=True, broker=None):
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN)
    monkeypatch.setattr(
        "project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
        lambda *_args: (True, "nonnegative", 0.1),
    )
    broker = broker or _Broker()
    factory = _factory()
    service = OmsRiskService(
        settings=_settings(enabled=enabled),
        redis_client=_Redis(),  # type: ignore[arg-type]
        session_factory=factory,
        broker_adapter=broker,  # type: ignore[arg-type]
    )
    service._get_session_symbol_block_reason = lambda **_kwargs: asyncio.sleep(0, result=None)
    return service, factory, broker


def test_paper_orb_registration_remains_paper_when_live_route_registered() -> None:
    registrations = strategy_registration_map(_settings())
    assert registrations["orb"].execution_mode == "paper"
    assert registrations["orb"].account_name == "paper:orb"
    assert registrations["orb_schwab"].execution_mode == "live"
    assert registrations["orb_schwab"].account_name == ACCOUNT
    assert "orb_schwab" not in strategy_registration_map(_settings(enabled=False))


def test_intent_builder_is_exact_two_share_stop_limit_bracket() -> None:
    event = build_orb_schwab_open_intent(_settings(), "clro", Decimal("5.5284"))
    assert event.payload.strategy_code == "orb_schwab"
    assert event.payload.broker_account_name == ACCOUNT
    assert event.payload.symbol == "CLRO"
    assert event.payload.quantity == 2
    assert event.payload.metadata["bracket_entry_type"] == "STOP_LIMIT"
    assert event.payload.metadata["bracket_target_price"] == "5.81"
    assert event.payload.metadata["bracket_stop_price"] == "5.09"
    assert orb_schwab_intent_refusal(event, _settings(), OPEN) is None


def test_rejected_orb_entry_stop_never_routes_to_market_fallback(monkeypatch) -> None:
    service, _factory, _broker = _service(monkeypatch)
    request = OrderRequest(
        client_order_id="ORB-1",
        broker_account_name=ACCOUNT,
        strategy_code="orb_schwab",
        symbol="CLRO",
        side="buy",
        intent_type="open",
        quantity=Decimal("2"),
        reason="ORB_FIXED_HIGH_SCHWAB_STOP_LIMIT",
    )
    rejection = ExecutionReport(
        event_type="rejected",
        client_order_id="ORB-1",
        reason="STOP PRICE NOT ACCEPTED",
    )
    assert service._stop_reject_reason(request=request, reports=[rejection]) is None


def test_contract_rejects_wrong_account_quantity_and_bracket() -> None:
    settings = _settings()
    event = build_orb_schwab_open_intent(settings, "CLRO", Decimal("5.5284"))
    event.payload.quantity = Decimal("3")
    assert orb_schwab_intent_refusal(event, settings, OPEN) == "orb_schwab_unsupported_intent"
    event.payload.quantity = Decimal("2")
    event.payload.metadata["bracket_stop_price"] = "0.01"
    assert orb_schwab_intent_refusal(event, settings, OPEN) == "orb_schwab_invalid_bracket"
    event.payload.metadata["bracket_stop_price"] = "5.09"
    event.payload.metadata["exit_only_oco"] = "true"
    assert orb_schwab_intent_refusal(event, settings, OPEN) == "orb_schwab_invalid_bracket"
    del event.payload.metadata["exit_only_oco"]
    event.payload.broker_account_name = "live:orb"
    assert orb_schwab_intent_refusal(event, settings, OPEN) == "orb_schwab_wrong_account"


def test_contract_refuses_before_open_and_at_window_end() -> None:
    settings = _settings()
    event = build_orb_schwab_open_intent(settings, "CLRO", Decimal("5.5284"))
    assert orb_schwab_intent_refusal(event, settings, OPEN - timedelta(seconds=1)) == (
        "orb_schwab_outside_entry_window"
    )
    assert orb_schwab_intent_refusal(event, settings, OPEN + timedelta(minutes=30)) == (
        "orb_schwab_outside_entry_window"
    )


def test_publisher_uses_only_the_oms_intent_stream() -> None:
    settings = _settings()
    redis = _Redis()
    event = build_orb_schwab_open_intent(settings, "CLRO", Decimal("5.5284"))
    asyncio.run(publish_orb_schwab_intent(redis, settings, event, OPEN))  # type: ignore[arg-type]
    assert len(redis.events) == 1
    assert redis.streams == ["test:strategy-intents"]
    assert redis.events[0]["payload"]["strategy_code"] == "orb_schwab"


def test_oms_previews_then_submits_only_valid_intent(monkeypatch) -> None:
    service, factory, broker = _service(monkeypatch)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    result = asyncio.run(service.process_trade_intent(event))
    assert result[0].payload.status == "accepted"
    assert len(broker.previewed) == len(broker.submitted) == 1
    assert broker.submitted[0].quantity == 2
    assert broker.submitted[0].metadata["stop_price"] == "5.53"
    with factory() as session:
        assert session.scalar(select(TradeIntent).where(TradeIntent.symbol == "CLRO")) is not None


def test_oms_refuses_negative_macd_without_preview_or_order(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch)
    monkeypatch.setattr(
        "project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
        lambda *_args: (False, "negative", -0.1),
    )
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(event)) == []
    assert broker.previewed == broker.submitted == []


def test_oms_refuses_unaccepted_preview_without_order(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch, broker=_Broker(preview_accepted=False))
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    result = asyncio.run(service.process_trade_intent(event))
    assert result[0].payload.status == "rejected"
    assert len(broker.previewed) == 1
    assert broker.submitted == []


def test_oms_refuses_existing_v2_order_on_same_account_symbol(monkeypatch) -> None:
    service, factory, broker = _service(monkeypatch)
    with factory.begin() as session:
        strategy = Strategy(code="schwab_1m_v2", name="ATR", execution_mode="live")
        account = BrokerAccount(name=ACCOUNT, provider="schwab", environment="live")
        session.add_all((strategy, account))
        session.flush()
        session.add(
            BrokerOrder(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id="v2-clro-rest",
                symbol="CLRO",
                side="buy",
                order_type="STOP_LIMIT",
                time_in_force="day",
                quantity=Decimal("2"),
                status="accepted",
            )
        )
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    result = asyncio.run(service.process_trade_intent(event))
    assert result[0].payload.status == "rejected"
    assert broker.previewed == broker.submitted == []


def test_oms_refuses_orb_order_when_feature_disabled(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch, enabled=False)
    event = build_orb_schwab_open_intent(_settings(), "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(event)) == []
    assert broker.previewed == broker.submitted == []


def test_oms_refuses_a_held_broker_position(monkeypatch) -> None:
    broker = _Broker(
        positions=[BrokerPositionSnapshot(ACCOUNT, "CLRO", Decimal("1"), Decimal("5.50"))]
    )
    service, _factory_, broker = _service(monkeypatch, broker=broker)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(event)) == []
    assert broker.previewed == broker.submitted == []


def test_one_orb_order_per_symbol_session_even_after_broker_cancel(monkeypatch) -> None:
    service, factory, broker = _service(monkeypatch)
    first = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(first))[0].payload.status == "accepted"
    with factory.begin() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "CLRO"))
        order.status = "cancelled"
    retry = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    result = asyncio.run(service.process_trade_intent(retry))
    assert result[0].payload.reason == "orb_schwab_entry_already_attempted_today"
    assert len(broker.submitted) == 1


def test_v2_open_refused_while_orb_buy_is_working(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch)
    orb_event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(orb_event))[0].payload.status == "accepted"
    v2_event = TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=ACCOUNT,
            symbol="CLRO",
            side="buy",
            quantity=Decimal("2"),
            intent_type="open",
            reason="ATR_ENTRY",
            metadata={"order_type": "stop_limit", "stop_price": "5.53", "limit_price": "5.56"},
        ),
    )
    result = asyncio.run(service.process_trade_intent(v2_event))
    assert result[0].payload.reason == "v2_orb_schwab_buy_order_open"
    assert len(broker.submitted) == 1


def test_v2_open_refused_after_orb_entry_fills(monkeypatch) -> None:
    service, factory, broker = _service(monkeypatch)
    orb_event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(orb_event))[0].payload.status == "accepted"
    with factory.begin() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "CLRO"))
        order.status = "filled"
        session.add(
            VirtualPosition(
                strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id,
                symbol="CLRO",
                quantity=Decimal("2"),
                average_price=Decimal("5.53"),
            )
        )
    v2_event = TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=ACCOUNT,
            symbol="CLRO",
            side="buy",
            quantity=Decimal("2"),
            intent_type="open",
            reason="ATR_ENTRY",
            metadata={"order_type": "stop_limit", "stop_price": "5.53", "limit_price": "5.56"},
        ),
    )
    result = asyncio.run(service.process_trade_intent(v2_event))
    assert result[0].payload.reason == "v2_orb_schwab_position_held"
    assert len(broker.submitted) == 1


def test_negative_completed_bar_can_cancel_only_unfilled_parent(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch)
    open_event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(open_event))[0].payload.status == "accepted"
    cancel_event = build_orb_schwab_cancel_intent(service.settings, "CLRO")
    result = asyncio.run(service.process_trade_intent(cancel_event))
    assert result[0].payload.status == "cancelled"
    assert [request.intent_type for request in broker.submitted] == ["open", "cancel"]


def test_unfilled_parent_cancel_survives_unavailable_position_list(monkeypatch) -> None:
    service, _factory, broker = _service(monkeypatch)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(event))[0].payload.status == "accepted"

    async def unavailable(_account):
        raise RuntimeError("position list unavailable")

    broker.list_account_positions = unavailable
    cancel = build_orb_schwab_cancel_intent(service.settings, "CLRO")
    result = asyncio.run(service.process_trade_intent(cancel))
    assert result[0].payload.status == "cancelled"
    assert [request.intent_type for request in broker.submitted] == ["open", "cancel"]


def test_negative_completed_bar_never_cancels_a_filled_parent(monkeypatch) -> None:
    broker = _Broker()
    service, _factory_, broker = _service(monkeypatch, broker=broker)
    open_event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(open_event))[0].payload.status == "accepted"
    broker.parent_status = "filled"
    cancel_event = build_orb_schwab_cancel_intent(service.settings, "CLRO")
    result = asyncio.run(service.process_trade_intent(cancel_event))
    assert result[0].payload.reason == "orb_schwab_cancel_entry_not_unfilled_working"
    assert [request.intent_type for request in broker.submitted] == ["open"]


def test_fourth_and_fifth_bar_reprice_same_broker_parent_without_second_buy(monkeypatch) -> None:
    service, factory, broker = _service(monkeypatch)
    original = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(original))[0].payload.status == "accepted"
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN + timedelta(minutes=1))
    fourth = build_orb_schwab_reprice_intent(service.settings, "CLRO", Decimal("5.6000"))
    assert asyncio.run(service.process_trade_intent(fourth))[0].payload.status == "accepted"
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN + timedelta(minutes=2))
    fifth = build_orb_schwab_reprice_intent(service.settings, "CLRO", Decimal("5.7000"))
    assert asyncio.run(service.process_trade_intent(fifth))[0].payload.status == "accepted"
    assert len(broker.submitted) == 1
    assert [broker_id for _request, broker_id in broker.replaced] == [
        "SCHWAB-ORB-1", "SCHWAB-ORB-2"
    ]
    with factory() as session:
        rows = session.scalars(select(BrokerOrder).where(BrokerOrder.symbol == "CLRO")).all()
        assert len(rows) == 1
        assert rows[0].payload["stop_price"] == "5.70"


def test_reprice_unknown_keeps_original_row_and_does_not_send_second_buy(monkeypatch) -> None:
    service, factory, broker = _service(monkeypatch)
    original = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    asyncio.run(service.process_trade_intent(original))
    broker.replace_report = "unknown"
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN + timedelta(minutes=1))
    event = build_orb_schwab_reprice_intent(service.settings, "CLRO", Decimal("5.6"))
    result = asyncio.run(service.process_trade_intent(event))
    assert result[0].payload.reason == "orb_schwab_reprice_broker_outcome_unknown"
    assert len(broker.submitted) == 1
    with factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "CLRO"))
        assert order.broker_order_id == "SCHWAB-ORB-1"
        assert order.payload["stop_price"] == "5.53"


def test_reprice_filled_parent_is_refused_before_broker_replace(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch)
    original = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    asyncio.run(service.process_trade_intent(original))
    broker.parent_status = "filled"
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN + timedelta(minutes=1))
    event = build_orb_schwab_reprice_intent(service.settings, "CLRO", Decimal("5.6"))
    result = asyncio.run(service.process_trade_intent(event))
    assert result[0].payload.reason == "orb_schwab_cancel_entry_not_unfilled_working"
    assert broker.replaced == []


def test_oms_backstop_cancels_still_working_parent_at_cutoff(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch)
    original = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    asyncio.run(service.process_trade_intent(original))
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN + timedelta(minutes=32))
    asyncio.run(service._orb_schwab_watchdog())
    assert [request.intent_type for request in broker.submitted] == ["open", "cancel"]


def test_owned_schwab_child_sell_is_durably_booked_once(monkeypatch) -> None:
    service, factory, broker = _service(monkeypatch)
    original = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    asyncio.run(service.process_trade_intent(original))
    with factory.begin() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "CLRO"))
        order.status = "filled"
        session.add(
            VirtualPosition(
                strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id,
                symbol="CLRO",
                quantity=Decimal("2"),
                average_price=Decimal("5.53"),
            )
        )
    broker.exit_detail = {
        "broker_order_id": "SCHWAB-ORB-CHILD-1",
        "quantity": Decimal("2"),
        "price": Decimal("5.81"),
        "filled_at": OPEN + timedelta(minutes=10),
    }
    asyncio.run(service._poll_orb_schwab_child_exits())
    asyncio.run(service._poll_orb_schwab_child_exits())
    with factory() as session:
        position = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "CLRO"))
        exits = session.scalars(select(Fill).where(Fill.symbol == "CLRO", Fill.side == "sell")).all()
        assert position.quantity == 0
        assert len(exits) == 1
        assert exits[0].price == Decimal("5.81")


class _EodBroker(_Broker):
    def __init__(self):
        super().__init__()
        self.quantity = Decimal("0")
        self.after_release_quantity = Decimal("2")
        self.release_result = "released"
        self.release_calls = []
        self.sequence = []
        self.close_outcome = "filled"

    async def list_account_positions(self, account):
        self.sequence.append("positions")
        if not self.quantity:
            return []
        return [BrokerPositionSnapshot(
            broker_account_name=account, symbol="CLRO", quantity=self.quantity,
            average_price=Decimal("5.53"),
        )]

    async def fetch_order_update(self, request):
        self.sequence.append("parent")
        return ExecutionReport(
            event_type="filled", client_order_id=request.client_order_id,
            broker_order_id=request.metadata["broker_order_id"], symbol=request.symbol,
            side="buy", quantity=Decimal("2"), filled_quantity=Decimal("2"),
        )

    async def release_native_oco_for_close(self, account, parent_id):
        self.sequence.append("release")
        self.release_calls.append((account, parent_id))
        self.quantity = self.after_release_quantity
        return self.release_result

    async def submit_order(self, request):
        if request.intent_type != "close":
            return await super().submit_order(request)
        self.sequence.append("sell")
        self.submitted.append(request)
        if self.close_outcome == "unknown":
            raise RuntimeError("connection lost after POST")
        if self.close_outcome == "filled":
            self.quantity -= request.quantity
        return [ExecutionReport(
            event_type=self.close_outcome, origin="broker",
            client_order_id=request.client_order_id, broker_order_id="EOD-SELL-1",
            broker_fill_id="EOD-SELL-FILL-1" if self.close_outcome == "filled" else None,
            symbol=request.symbol, side="sell", intent_type="close", quantity=request.quantity,
            filled_quantity=request.quantity if self.close_outcome == "filled" else Decimal("0"),
            fill_price=Decimal("5.6") if self.close_outcome == "filled" else None,
            metadata=dict(request.metadata),
        )]


def _eod_service(monkeypatch):
    broker = _EodBroker()
    service, factory, broker = _service(monkeypatch, broker=broker)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    asyncio.run(service.process_trade_intent(event))
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.side == "buy"))
        entry.status = "filled"
        session.add(VirtualPosition(
            strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol="CLRO", quantity=Decimal("2"), average_price=Decimal("5.53"),
        ))
        session.add(AccountPosition(
            broker_account_id=entry.broker_account_id,
            symbol="CLRO", quantity=Decimal("2"), average_price=Decimal("5.53"),
        ))
    broker.quantity = Decimal("2")
    broker.sequence.clear()
    clock = [OPEN.replace(hour=19, minute=55)]
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: clock[0])
    return service, factory, broker, clock


def test_eod_leaves_normal_exit_working_until_1555(monkeypatch) -> None:
    service, _factory, broker, clock = _eod_service(monkeypatch)
    clock[0] -= timedelta(seconds=1)
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.sequence == []
    assert len(broker.submitted) == 1


def test_eod_confirms_pair_release_then_rereads_quantity_and_closes_once(monkeypatch) -> None:
    service, factory, broker, _clock = _eod_service(monkeypatch)
    asyncio.run(service._orb_schwab_eod_close())
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.sequence == ["parent", "positions", "release", "positions", "sell"]
    assert broker.release_calls == [(ACCOUNT, "SCHWAB-ORB-1")]
    request = broker.submitted[-1]
    assert request.quantity == 2 and request.side == "sell" and request.order_type == "market"
    assert "bracket" not in request.metadata
    with factory() as session:
        position = session.scalar(select(VirtualPosition))
        assert position.quantity == 0
        assert len(session.scalars(select(Fill).where(Fill.side == "sell")).all()) == 1


def test_eod_uses_remaining_quantity_after_release(monkeypatch) -> None:
    service, _factory, broker, _clock = _eod_service(monkeypatch)
    broker.after_release_quantity = Decimal("1")
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.submitted[-1].quantity == 1


def test_eod_never_sells_when_exit_pair_release_is_unknown(monkeypatch) -> None:
    service, factory, broker, _clock = _eod_service(monkeypatch)
    broker.release_result = "unanswerable"
    asyncio.run(service._orb_schwab_eod_close())
    asyncio.run(service._orb_schwab_eod_close())
    assert len(broker.submitted) == 1
    assert len(broker.release_calls) == 1
    with factory() as session:
        incident = session.scalar(select(SystemIncident))
        assert incident.payload["reason"] == "sell_pair_release_unconfirmed"


def test_eod_attributes_child_fill_without_second_sell(monkeypatch) -> None:
    service, factory, broker, clock = _eod_service(monkeypatch)
    broker.release_result = "resolved_by_fill"
    broker.exit_detail = {
        "broker_order_id": "SCHWAB-ORB-CHILD-1", "quantity": Decimal("2"),
        "price": Decimal("5.81"), "filled_at": clock[0],
    }
    asyncio.run(service._orb_schwab_eod_close())
    assert len(broker.submitted) == 1
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 0
        assert session.scalar(select(SystemIncident)) is None


def test_eod_resolved_by_fill_without_exact_fill_stays_unknown(monkeypatch) -> None:
    service, factory, broker, _clock = _eod_service(monkeypatch)
    broker.release_result = "resolved_by_fill"
    asyncio.run(service._orb_schwab_eod_close())
    assert len(broker.submitted) == 1
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 2
        assert session.scalar(select(SystemIncident)).payload["reason"] == "child_fill_not_recorded"


def test_eod_unknown_post_is_never_retried_after_restart(monkeypatch) -> None:
    service, factory, broker, _clock = _eod_service(monkeypatch)
    broker.close_outcome = "unknown"
    asyncio.run(service._orb_schwab_eod_close())
    restarted = OmsRiskService(
        settings=service.settings, redis_client=_Redis(), session_factory=factory,
        broker_adapter=broker,
    )
    asyncio.run(restarted._orb_schwab_eod_close())
    assert [r.intent_type for r in broker.submitted] == ["open", "close"]
    with factory() as session:
        pending = session.scalar(select(BrokerOrder).where(BrokerOrder.side == "sell"))
        assert pending.status == "pending" and pending.broker_order_id is None
        assert len(session.scalars(select(SystemIncident)).all()) == 1


def test_eod_does_not_release_or_sell_when_another_strategy_owns_shares(monkeypatch) -> None:
    service, factory, broker, _clock = _eod_service(monkeypatch)
    with factory.begin() as session:
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == ACCOUNT))
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="v2", execution_mode="live")
        session.add(VirtualPosition(
            strategy_id=strategy.id, broker_account_id=account.id, symbol="CLRO",
            quantity=Decimal("1"), average_price=Decimal("5.5"),
        ))
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.release_calls == []
    assert len(broker.submitted) == 1


def test_eod_unknown_position_read_keeps_pair_intact(monkeypatch) -> None:
    service, factory, broker, _clock = _eod_service(monkeypatch)

    async def unreadable(_account):
        raise RuntimeError("position read unavailable")

    broker.list_account_positions = unreadable
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.release_calls == []
    assert len(broker.submitted) == 1
    with factory() as session:
        assert session.scalar(select(SystemIncident)) is not None


def test_eod_never_sends_rth_market_order_after_1600(monkeypatch) -> None:
    service, factory, broker, clock = _eod_service(monkeypatch)
    clock[0] = clock[0].replace(hour=20, minute=0)
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.release_calls == []
    assert len(broker.submitted) == 1
    with factory() as session:
        assert session.scalar(select(SystemIncident)).payload["reason"] == "position_unresolved_at_session_close"


def test_eod_rechecks_clock_after_release(monkeypatch) -> None:
    service, factory, broker, clock = _eod_service(monkeypatch)
    release = broker.release_native_oco_for_close

    async def slow_release(account, parent_id):
        result = await release(account, parent_id)
        clock[0] = clock[0].replace(hour=20, minute=0)
        return result

    broker.release_native_oco_for_close = slow_release
    asyncio.run(service._orb_schwab_eod_close())
    assert len(broker.submitted) == 1
    with factory() as session:
        assert session.scalar(select(SystemIncident)).payload["reason"] == "close_window_elapsed_after_release"


def test_eod_rechecks_clock_after_durable_attempt_write(monkeypatch) -> None:
    service, factory, broker, clock = _eod_service(monkeypatch)
    original = service.store.get_or_create_order

    def slow_write(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("side") == "sell":
            clock[0] = clock[0].replace(hour=20, minute=0)
        return result

    monkeypatch.setattr(service.store, "get_or_create_order", slow_write)
    asyncio.run(service._orb_schwab_eod_close())
    assert len(broker.submitted) == 1
    with factory() as session:
        assert session.scalar(select(SystemIncident)).payload["reason"] == "close_window_elapsed_before_submit"


def test_eod_is_inert_when_live_feature_disabled(monkeypatch) -> None:
    service, _factory, broker, _clock = _eod_service(monkeypatch)
    service.settings.orb_live_schwab_orders_enabled = False
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.sequence == []


def test_eod_sell_rejection_records_incident_without_resubmitting(monkeypatch) -> None:
    service, factory, broker, _clock = _eod_service(monkeypatch)
    broker.close_outcome = "rejected"
    asyncio.run(service._orb_schwab_eod_close())
    asyncio.run(service._orb_schwab_eod_close())
    assert [r.intent_type for r in broker.submitted] == ["open", "close"]
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 2
        assert session.scalar(select(SystemIncident)).payload["reason"] == "close_rejected_after_pair_release"


def test_eod_accepted_is_not_treated_as_fill_and_alerts_by_1559(monkeypatch) -> None:
    service, factory, broker, clock = _eod_service(monkeypatch)
    broker.close_outcome = "accepted"
    asyncio.run(service._orb_schwab_eod_close())
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 2
        assert session.scalar(select(SystemIncident)) is None
    clock[0] = clock[0].replace(minute=59)
    asyncio.run(service._orb_schwab_eod_close())
    assert [r.intent_type for r in broker.submitted] == ["open", "close"]
    with factory() as session:
        assert session.scalar(select(SystemIncident)) is not None


def test_eod_does_not_use_macd_to_block_a_close(monkeypatch) -> None:
    service, _factory, broker, _clock = _eod_service(monkeypatch)
    monkeypatch.setattr(
        "project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
        lambda *_args: (False, "negative", -0.1),
    )
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.submitted[-1].intent_type == "close"


def test_eod_1555_uses_eastern_time_in_winter(monkeypatch) -> None:
    service, _factory, broker, clock = _eod_service(monkeypatch)
    clock[0] = datetime(2026, 12, 1, 20, 54, 59, tzinfo=UTC)
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.sequence == []
    clock[0] += timedelta(seconds=1)
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.submitted[-1].intent_type == "close"


def test_oms_control_loop_runs_eod_close_without_strategy_process(monkeypatch) -> None:
    service, _factory, broker, _clock = _eod_service(monkeypatch)

    async def no_op(*_args, **_kwargs):
        return {}

    async def cadence():
        return 1.0

    for name in (
        "sync_broker_state", "_check_webull_uncovered_shares", "_window_flatten_armed_stops",
        "_orb_schwab_watchdog", "_v2_eod_oco_transition", "_retry_webull_eh_ladder_pending",
        "_v2_eod_cancel_and_reexit", "_v2_rth_edge_bracket", "_v2_overnight_flatten",
    ):
        monkeypatch.setattr(service, name, no_op)
    monkeypatch.setattr(service, "_broker_sync_interval_seconds", cadence)

    async def run_once():
        stop = asyncio.Event()

        async def read(*_args, **_kwargs):
            stop.set()
            return []

        service.redis.xread = read
        await asyncio.wait_for(service._run_control_loop(stop), timeout=5)

    asyncio.run(run_once())
    assert [r.intent_type for r in broker.submitted] == ["open", "close"]
