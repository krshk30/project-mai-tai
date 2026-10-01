from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
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
from project_mai_tai.orb_schwab_macd import MacdVerdict
from project_mai_tai.orb_schwab_order_route import (
    build_orb_schwab_cancel_intent,
    build_orb_schwab_exit_intent,
    build_orb_schwab_open_intent,
    build_orb_schwab_reprice_intent,
    orb_schwab_intent_refusal,
    publish_orb_schwab_intent,
)
from project_mai_tai.runtime_registry import strategy_registration_map
from project_mai_tai.settings import Settings

OPEN = datetime(2026, 9, 29, 13, 28, tzinfo=UTC)
ACCOUNT = "live:schwab_1m_v2"

# Broker-relevant fields captured from read-only Schwab /previewOrder calls on
# 2026-10-01. Account number and buying-power fields are omitted from the fixture.
ACCEPTED_PREVIEW = {
    "orderId": 0,
    "orderStrategy": {"status": "ACCEPTED", "orderType": "STOP_LIMIT"},
    "orderValidationResult": {"warns": [{
        "activityMessage": "With stop orders and triggered market orders, there is no guarantee that the execution price will be equal to or near the activation/trigger price.",
        "originalSeverity": "WARN",
    }]},
}
REJECTED_PREVIEW = {
    "orderId": 0,
    "orderStrategy": {"status": "REJECTED", "orderType": "STOP_LIMIT"},
    "orderValidationResult": {"rejects": [{
        "activityMessage": "The stop price must be above the current ask for buy stop orders and below the bid for sell stop orders.",
        "originalSeverity": "REJECT",
    }]},
}


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
        self, *, preview_accepted: bool = True, positions=None, parent_status: str = "accepted",
        preview_body=None, preview_status: int = 200,
    ) -> None:
        self.preview_accepted = preview_accepted
        self.preview_body = preview_body
        self.preview_status = preview_status
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
        if self.preview_body is not None:
            return self.preview_status, self.preview_body
        if self.preview_accepted:
            return 200, ACCEPTED_PREVIEW
        return 200, REJECTED_PREVIEW

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
            metadata={**request.metadata, "orb_replace_confirmation": "confirmed"},
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
        lambda *_args: (MacdVerdict.ALLOWED, "nonnegative", 0.1),
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


@pytest.mark.parametrize("body,status", [
    ({"orderStrategy": {"status": "REJECTED"}, "orderValidationResult": {}}, 200),
    (REJECTED_PREVIEW, 200),
    ({"orderStrategy": {"status": "ACCEPTED"}, "orderValidationResult": {"rejects": ["bad"]}}, 200),
    (ACCEPTED_PREVIEW, 500),
])
def test_orb_preview_requires_nested_acceptance_and_zero_rejects(monkeypatch, body, status):
    broker = _Broker(preview_body=body, preview_status=status)
    service, factory, _ = _service(monkeypatch, broker=broker)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    result = asyncio.run(service.process_trade_intent(event))
    assert result[0].payload.status == "rejected"
    assert broker.submitted == []
    with factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "CLRO"))
        assert intent.payload["orb_schwab_preview"]["body"] == body
        assert intent.payload["orb_schwab_preview"]["http_status"] == status


def test_orb_preview_accepts_nested_status_with_warns_and_no_rejects(monkeypatch):
    broker = _Broker(preview_body=ACCEPTED_PREVIEW)
    service, _factory, _ = _service(monkeypatch, broker=broker)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(event))[0].payload.status == "accepted"
    assert len(broker.submitted) == 1


def test_orb_reprice_preview_refusal_never_replaces_parent(monkeypatch):
    service, factory, broker = _service(monkeypatch)
    opened = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(opened))[0].payload.status == "accepted"
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN + timedelta(minutes=1))
    broker.preview_body = {
        "orderStrategy": {"status": "ACCEPTED"},
        "orderValidationResult": {"rejects": REJECTED_PREVIEW["orderValidationResult"]["rejects"]},
    }
    event = build_orb_schwab_reprice_intent(service.settings, "CLRO", Decimal("5.6"))
    result = asyncio.run(service.process_trade_intent(event))
    assert result[0].payload.reason == "orb_schwab_reprice_preview_not_accepted"
    assert broker.replaced == []
    with factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.reason == event.payload.reason))
        assert intent.payload["orb_schwab_preview"]["body"] == broker.preview_body


def test_orb_preview_timeout_refuses_and_preserves_error(monkeypatch):
    broker = _Broker()

    async def timeout(_request):
        raise TimeoutError("preview timed out")

    broker.preview_bracket_order = timeout
    service, factory, _ = _service(monkeypatch, broker=broker)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(event))[0].payload.status == "rejected"
    assert broker.submitted == []
    with factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "CLRO"))
        assert intent.payload["orb_schwab_preview"] == {
            "http_status": 0, "body": None, "error": "preview timed out",
        }


def test_oms_refuses_negative_macd_without_preview_or_order(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch)
    monkeypatch.setattr(
        "project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
        lambda *_args: (MacdVerdict.NEGATIVE, "negative", -0.1),
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


def test_oms_never_treats_pending_macd_as_truthy_permission(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch)
    monkeypatch.setattr(
        "project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
        lambda *_args: (MacdVerdict.BAR_NOT_YET, "missing_last_closed_schwab_bar", None),
    )
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    assert asyncio.run(service.process_trade_intent(event)) == []
    assert broker.submitted == []


def test_oms_watchdog_waits_for_late_bar_but_cancels_after_90_seconds(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    asyncio.run(service.process_trade_intent(event))
    monkeypatch.setattr(
        "project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
        lambda *_args: (MacdVerdict.BAR_NOT_YET, "missing_last_closed_schwab_bar", None),
    )
    clock = [OPEN + timedelta(minutes=2, seconds=1)]
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: clock[0])
    asyncio.run(service._orb_schwab_watchdog())
    assert [request.intent_type for request in broker.submitted] == ["open"]
    clock[0] = OPEN + timedelta(minutes=3, seconds=29)
    asyncio.run(service._orb_schwab_watchdog())
    assert [request.intent_type for request in broker.submitted] == ["open"]
    clock[0] = OPEN + timedelta(minutes=3, seconds=30)
    asyncio.run(service._orb_schwab_watchdog())
    assert [request.intent_type for request in broker.submitted] == ["open", "cancel"]


def test_oms_watchdog_computed_negative_cancels_without_waiting(monkeypatch) -> None:
    service, _factory_, broker = _service(monkeypatch)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"))
    asyncio.run(service.process_trade_intent(event))
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN + timedelta(minutes=2, seconds=1))
    monkeypatch.setattr(
        "project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
        lambda *_args: (MacdVerdict.NEGATIVE, "negative", -0.1),
    )
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
        lambda *_args: (MacdVerdict.NEGATIVE, "negative", -0.1),
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


def test_live_orb_watchdog_gets_one_second_control_loop_opportunity(monkeypatch) -> None:
    service, _factory_, _broker = _service(monkeypatch)
    calls = []

    async def no_op(*_args, **_kwargs):
        return {}

    async def watch():
        calls.append("watch")

    async def cadence():
        return 5.0

    for name in (
        "sync_broker_state", "_check_webull_uncovered_shares", "_window_flatten_armed_stops",
        "_orb_schwab_eod_close", "_v2_eod_oco_transition", "_retry_webull_eh_ladder_pending",
        "_v2_eod_cancel_and_reexit", "_v2_rth_edge_bracket", "_v2_overnight_flatten",
    ):
        monkeypatch.setattr(service, name, no_op)
    monkeypatch.setattr(service, "_orb_schwab_watchdog", watch)
    monkeypatch.setattr(service, "_broker_sync_interval_seconds", cadence)

    async def run_once():
        stop = asyncio.Event()

        async def read(_streams, *, block, count):
            calls.append((block, count))
            if len([call for call in calls if isinstance(call, tuple)]) == 2:
                stop.set()
            return []

        service.redis.xread = read
        await asyncio.wait_for(service._run_control_loop(stop), timeout=5)

    asyncio.run(run_once())
    assert calls[0] == (100, 50)
    assert calls[1] == "watch"
    assert calls[2] == (1000, 50)


def _strategy_exit_service(monkeypatch, *, atr=False):
    from project_mai_tai.orb_schwab_exits import CONTEXT_KEY, completed_bar_evidence
    from project_mai_tai.strategy_core.orb_intrabar import OrbBar

    service, factory, broker, clock = _eod_service(monkeypatch)
    opening = OPEN.replace(minute=30)
    fill_at = opening + timedelta(seconds=10)
    clock[0] = opening + timedelta(minutes=2, seconds=1)
    bars = [OrbBar(timestamp=opening - timedelta(minutes=8) + timedelta(minutes=i),
                   open=10, high=10.1, low=9.9 if i < 9 else 8.8,
                   close=(10.1 if atr and i == 8 else 10) if i < 9 else 8.9, volume=100) for i in range(10)]
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.side == "buy"))
        entry.submitted_at = OPEN
        fill = Fill(order_id=entry.id, strategy_id=entry.strategy_id,
                    broker_account_id=entry.broker_account_id, broker_fill_id="EXACT-BUY-FILL",
                    symbol="CLRO", side="buy", quantity=2, price=10.1, filled_at=fill_at,
                    payload={"metadata": {"orb_entry_fill_time_source": "execution_leg",
                                          "orb_entry_first_fill_at": fill_at.isoformat()}})
        session.add(fill)
        session.flush()
        context = completed_bar_evidence(str(fill.id), fill_at, clock[0], atr_bars=bars, atr_status="complete")
        entry.payload = {**entry.payload, CONTEXT_KEY: context}
        event = build_orb_schwab_exit_intent(service.settings, "CLRO", str(entry.id), str(fill.id), context["reason"])
    service._latest_quotes_by_symbol["CLRO"] = {"received_at": clock[0], "bid": 9.8}
    return service, factory, broker, clock, event


@pytest.mark.parametrize("atr", [False, True])
def test_strategy_exit_uses_same_protected_single_close_path_as_eod(monkeypatch, atr):
    service, factory, broker, clock, event = _strategy_exit_service(monkeypatch, atr=atr)
    monkeypatch.setattr("project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
                        lambda *_args: (MacdVerdict.NEGATIVE, "negative", -0.1))
    asyncio.run(service.process_trade_intent(event))
    asyncio.run(service.process_trade_intent(event))
    clock[0] = clock[0].replace(hour=19, minute=55)
    asyncio.run(service._orb_schwab_eod_close())
    assert broker.sequence == ["parent", "positions", "release", "positions", "sell"]
    assert [r.intent_type for r in broker.submitted] == ["open", "close"]
    assert broker.submitted[-1].reason == event.payload.reason
    assert broker.submitted[-1].metadata["orb_schwab_eod"] == "false"
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 0
        assert len(session.scalars(select(Fill).where(Fill.side == "sell")).all()) == 1


@pytest.mark.parametrize("problem", ["no_bid", "stale", "before_decision"])
def test_strategy_exit_waits_for_fresh_post_decision_bid_without_taking_latch(monkeypatch, problem):
    # An old quote must still be AFTER the decision to isolate quote age from
    # the independent post-decision guard.
    service, factory, broker, clock, event = _strategy_exit_service(monkeypatch, atr=problem != "stale")
    quote = service._latest_quotes_by_symbol["CLRO"]
    if problem == "no_bid":
        quote["bid"] = 0
    else:
        quote["received_at"] = clock[0] - timedelta(seconds=6 if problem == "stale" else 2)
    asyncio.run(service.process_trade_intent(event))
    assert broker.sequence == []
    with factory() as session:
        assert "orb_schwab_eod_close" not in session.scalar(select(BrokerOrder)).payload
    service._latest_quotes_by_symbol["CLRO"] = {"received_at": clock[0], "bid": 9.8}
    asyncio.run(service.process_trade_intent(event))
    assert broker.sequence[-1] == "sell"


@pytest.mark.parametrize("problem", ["reason", "fill_time", "source", "wrong_fill"])
def test_strategy_exit_recomputes_evidence_and_rejects_spoofed_request(monkeypatch, problem):
    from uuid import uuid4
    from project_mai_tai.orb_schwab_exits import ATR_REASON, CONTEXT_KEY

    service, factory, broker, _clock, event = _strategy_exit_service(monkeypatch)
    if problem == "reason":
        event.payload.reason = ATR_REASON
    elif problem == "wrong_fill":
        event.payload.metadata["entry_fill_id"] = str(uuid4())
    else:
        with factory.begin() as session:
            entry = session.scalar(select(BrokerOrder))
            context = dict(entry.payload[CONTEXT_KEY])
            context["body_source" if problem == "source" else "fill_at"] = "wrong"
            entry.payload = {**entry.payload, CONTEXT_KEY: context}
    asyncio.run(service.process_trade_intent(event))
    assert broker.sequence == [] and len(broker.submitted) == 1


def test_strategy_exit_partial_parent_keeps_protection_until_buy_is_fully_filled(monkeypatch):
    service, factory, broker, _clock, event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        session.scalar(select(BrokerOrder)).status = "partially_filled"
    asyncio.run(service.process_trade_intent(event))
    assert broker.sequence == []
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        assert "orb_schwab_eod_close" not in entry.payload
        entry.status = "filled"
    asyncio.run(service.process_trade_intent(event))
    assert broker.sequence[-1] == "sell"


def test_strategy_exit_unknown_post_is_not_retried_by_new_oms_or_eod(monkeypatch):
    service, factory, broker, clock, event = _strategy_exit_service(monkeypatch)
    broker.close_outcome = "unknown"
    asyncio.run(service.process_trade_intent(event))
    restarted = OmsRiskService(settings=service.settings, redis_client=_Redis(),
                               session_factory=factory, broker_adapter=broker)
    restarted._latest_quotes_by_symbol = service._latest_quotes_by_symbol
    asyncio.run(restarted.process_trade_intent(event))
    clock[0] = clock[0].replace(hour=19, minute=55)
    asyncio.run(restarted._orb_schwab_eod_close())
    assert [r.intent_type for r in broker.submitted] == ["open", "close"]
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 2
        assert session.scalar(select(SystemIncident)) is not None


@pytest.mark.parametrize("resolved", [False, True])
def test_strategy_exit_never_sells_without_confirmed_pair_release(monkeypatch, resolved):
    service, factory, broker, clock, event = _strategy_exit_service(monkeypatch)
    broker.release_result = "resolved_by_fill" if resolved else "unanswerable"
    if resolved:
        broker.exit_detail = {"broker_order_id": "CHILD", "quantity": Decimal("2"),
                             "price": Decimal("5.81"), "filled_at": clock[0]}
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.submitted) == 1
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == (0 if resolved else 2)


def test_strategy_exit_flag_off_is_inert(monkeypatch):
    service, _factory, broker, _clock, event = _strategy_exit_service(monkeypatch)
    service.settings.orb_live_schwab_orders_enabled = False
    asyncio.run(service.process_trade_intent(event))
    assert broker.sequence == []


def test_old_trip_cannot_close_new_position_and_producer_selects_only_latest(monkeypatch):
    from project_mai_tai.orb_schwab_exits import open_entries

    service, factory, broker, clock, event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        next_entry = BrokerOrder(strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
                                 intent_id=entry.intent_id, client_order_id="NEXT-TRIP", symbol="CLRO",
                                 side="buy", order_type="stop_limit", time_in_force="day",
                                 quantity=2, status="filled", submitted_at=clock[0])
        session.add(next_entry)
        session.flush()
        next_id = str(next_entry.id)
    rows = open_entries(factory, ACCOUNT)
    assert len(rows) == 1 and rows[0]["entry_id"] == next_id and rows[0]["fill_id"] is None
    asyncio.run(service.process_trade_intent(event))
    assert broker.sequence == []


def test_live_producer_persists_fill_evidence_then_publishes_deterministic_exit(monkeypatch):
    from project_mai_tai.orb_schwab_exits import CONTEXT_KEY
    from project_mai_tai.services.orb_schwab_app import OrbSchwabService

    oms, factory, broker, clock, event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        context = entry.payload[CONTEXT_KEY]
    redis = _Redis()
    producer = OrbSchwabService(settings=oms.settings, redis_client=redis, session_factory=factory)
    monkeypatch.setattr(producer, "_processing_time", lambda: clock[0])
    # The durable fill/body context also survives an ORB producer restart.
    asyncio.run(producer._process_strategy_exits(clock[0]))
    assert producer._exit_held_symbols == {"CLRO"}
    assert producer._last_gateway_symbols == ["CLRO"]  # held, even absent from scanner
    published = [row for row in redis.events if row["event_type"] == "trade_intent"]
    assert len(published) == 1
    emitted = TradeIntentEvent.model_validate(published[0])
    assert emitted.event_id == event.event_id
    assert emitted.payload.reason == context["reason"]
    assert len(broker.submitted) == 1  # producer does not execute broker orders
    asyncio.run(oms.process_trade_intent(emitted))
    assert broker.sequence[-1] == "sell"


def test_live_producer_missing_execution_time_never_fabricates_exit(monkeypatch):
    from project_mai_tai.services.orb_schwab_app import OrbSchwabService
    from project_mai_tai.orb_schwab_exits import CONTEXT_KEY

    oms, factory, broker, clock, _event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        session.scalar(select(Fill)).payload = {}
        entry = session.scalar(select(BrokerOrder))
        entry.payload = {key: value for key, value in entry.payload.items() if key != CONTEXT_KEY}
    redis = _Redis()
    producer = OrbSchwabService(settings=oms.settings, redis_client=redis, session_factory=factory)
    monkeypatch.setattr(producer, "_processing_time", lambda: clock[0])
    asyncio.run(producer._process_strategy_exits(clock[0]))
    assert all(row["event_type"] != "trade_intent" for row in redis.events)
    assert len(broker.submitted) == 1
    with factory() as session:
        incident = session.scalar(select(SystemIncident))
        assert incident.payload["native_protection"] == "not_cancelled"


@pytest.mark.parametrize("atr", [False, True])
def test_live_producer_builds_signal_from_real_fill_and_persisted_schwab_bars(monkeypatch, atr):
    from project_mai_tai.db.models import StrategyBarHistory
    from project_mai_tai.orb_schwab_exits import CONTEXT_KEY, decode_bar
    from project_mai_tai.services.orb_schwab_app import OrbSchwabService

    oms, factory, _broker, clock, event = _strategy_exit_service(monkeypatch, atr=atr)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        context = entry.payload[CONTEXT_KEY]
        entry.payload = {key: value for key, value in entry.payload.items() if key != CONTEXT_KEY}
        for row in context["atr_bars"]:
            bar = decode_bar(row)
            session.add(StrategyBarHistory(strategy_code="schwab_1m_v2", symbol="CLRO", interval_secs=60,
                                          bar_time=bar.timestamp, open_price=bar.open, high_price=bar.high,
                                          low_price=bar.low, close_price=bar.close, volume=int(bar.volume), source="live"))
    redis = _Redis()
    producer = OrbSchwabService(settings=oms.settings, redis_client=redis, session_factory=factory)
    monkeypatch.setattr(producer, "_processing_time", lambda: clock[0])
    asyncio.run(producer._process_strategy_exits(clock[0]))
    with factory() as session:
        saved = session.scalar(select(BrokerOrder)).payload[CONTEXT_KEY]
        assert saved["reason"] == event.payload.reason
        assert saved["fill_id"] == context["fill_id"]
    published = [row for row in redis.events if row["event_type"] == "trade_intent"]
    assert len(published) == 1 and published[0]["payload"]["reason"] == event.payload.reason


@pytest.mark.parametrize("child", ["TARGET", "STOP"])
def test_native_child_filled_inside_break_bar_means_no_body_sell(monkeypatch, child):
    service, factory, broker, _clock, event = _strategy_exit_service(monkeypatch)
    broker.quantity = Decimal("0")  # local position has not yet seen the broker sell
    broker.exit_detail = {"broker_order_id": child, "quantity": Decimal("2"),
                         "price": Decimal("10.5") if child == "TARGET" else Decimal("9.2"),
                         "filled_at": OPEN.replace(minute=30, second=40)}
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.submitted) == 1 and broker.release_calls == []
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 0
        sell = session.scalar(select(Fill).where(Fill.side == "sell"))
        assert sell is not None and sell.price == broker.exit_detail["price"]


def test_break_close_only_reconciles_the_current_parent_not_an_old_trip(monkeypatch):
    service, factory, broker, _clock, event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        session.add(BrokerOrder(strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            intent_id=entry.intent_id, client_order_id="OLD-TRIP", broker_order_id="OLD-PARENT",
            symbol="CLRO", side="buy", order_type="stop_limit", time_in_force="day", quantity=2,
            status="filled", submitted_at=OPEN - timedelta(days=1)))
    parents = []
    async def child_detail(_account, _symbol, _client_id, *, entry_broker_order_id):
        parents.append(entry_broker_order_id)
        if entry_broker_order_id == "OLD-PARENT":
            return {"broker_order_id": "OLD-TARGET", "quantity": Decimal("2"),
                    "price": Decimal("5.81"), "filled_at": OPEN - timedelta(days=1)}
        return None
    broker.fetch_oco_exit_fill = child_detail
    asyncio.run(service.process_trade_intent(event))
    assert parents and set(parents) == {"SCHWAB-ORB-1"}
    assert len(broker.submitted) == 2 and broker.submitted[-1].reason == event.payload.reason


@pytest.mark.parametrize("close,sells", [(108.98, True), (109, False)])
def test_held_at_break_close_449_sells_450_keeps_native_pair(monkeypatch, close, sells):
    from project_mai_tai.orb_schwab_exits import CONTEXT_KEY
    service, factory, broker, _clock, event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        context = dict(entry.payload[CONTEXT_KEY])
        context["body"] = {**context["body"], "open": 100, "high": 120, "low": 100, "close": close}
        context["atr_status"] = "unknown"  # isolate the body, not a later SELL flip
        entry.payload = {**entry.payload, CONTEXT_KEY: context}
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.submitted) == 1 + int(sells)
    assert len(broker.release_calls) == int(sells)


@pytest.mark.parametrize("seconds", [3.5, 90])
def test_normal_break_bar_write_latency_is_pending_not_an_incident(monkeypatch, seconds):
    from project_mai_tai.orb_schwab_exits import CONTEXT_KEY
    from project_mai_tai.services.orb_schwab_app import OrbSchwabService
    oms, factory, broker, clock, _event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        entry.payload = {key: value for key, value in entry.payload.items() if key != CONTEXT_KEY}
    producer = OrbSchwabService(settings=oms.settings, redis_client=_Redis(), session_factory=factory)
    clock[0] = OPEN.replace(minute=31) + timedelta(seconds=seconds)
    monkeypatch.setattr(producer, "_processing_time", lambda: clock[0])
    asyncio.run(producer._process_strategy_exits(clock[0]))
    with factory() as session:
        assert session.scalar(select(SystemIncident)) is None
        assert session.scalar(select(BrokerOrder)).payload[CONTEXT_KEY]["body_status"] == "pending_break_bar"
    assert broker.release_calls == []
    assert not any(row["event_type"] == "trade_intent" for row in producer.redis.events)


def test_missing_completed_break_bar_emits_incident_not_exit(monkeypatch):
    from project_mai_tai.orb_schwab_exits import CONTEXT_KEY
    from project_mai_tai.services.orb_schwab_app import OrbSchwabService

    oms, factory, broker, clock, _event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        entry.payload = {key: value for key, value in entry.payload.items() if key != CONTEXT_KEY}
    producer = OrbSchwabService(settings=oms.settings, redis_client=_Redis(), session_factory=factory)
    monkeypatch.setattr(producer, "_processing_time", lambda: clock[0])
    clock[0] = OPEN.replace(minute=32, second=31)
    asyncio.run(producer._process_strategy_exits(clock[0]))
    # A fresh producer reloads the persisted context, but must not page again.
    producer = OrbSchwabService(settings=oms.settings, redis_client=producer.redis, session_factory=factory)
    clock[0] += timedelta(seconds=2)
    asyncio.run(producer._process_strategy_exits(clock[0]))
    assert not any(row["event_type"] == "trade_intent" for row in producer.redis.events)
    assert broker.release_calls == [] and len(broker.submitted) == 1
    with factory() as session:
        incident = session.scalar(select(SystemIncident))
        assert incident.payload["reason"] == "missing_completed_schwab_break_bar"
        assert incident.payload["native_protection"] == "not_cancelled"
        assert len(session.scalars(select(SystemIncident)).all()) == 1


def test_break_bar_arrives_at_3_5_seconds_body_decided_without_incident(monkeypatch):
    from project_mai_tai.db.models import StrategyBarHistory
    from project_mai_tai.orb_schwab_exits import BODY_REASON, CONTEXT_KEY
    from project_mai_tai.services.orb_schwab_app import OrbSchwabService

    oms, factory, broker, clock, _event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        rows = entry.payload[CONTEXT_KEY]["atr_bars"][:-1]
        entry.payload = {key: value for key, value in entry.payload.items() if key != CONTEXT_KEY}
    producer = OrbSchwabService(settings=oms.settings, redis_client=_Redis(), session_factory=factory)
    monkeypatch.setattr(producer, "_processing_time", lambda: clock[0])
    clock[0] = OPEN.replace(minute=31)
    asyncio.run(producer._process_strategy_exits(clock[0]))
    assert not any(row["event_type"] == "trade_intent" for row in producer.redis.events)
    with factory.begin() as session:
        for row in rows:
            session.add(StrategyBarHistory(strategy_code="schwab_1m_v2", symbol="CLRO", interval_secs=60,
                bar_time=datetime.fromisoformat(row["at"]), open_price=row["open"], high_price=row["high"],
                low_price=row["low"], close_price=row["close"], volume=int(row["volume"]), source="live"))
    clock[0] += timedelta(seconds=3.5)
    asyncio.run(producer._process_strategy_exits(clock[0]))
    with factory() as session:
        assert session.scalar(select(SystemIncident)) is None
        context = session.scalar(select(BrokerOrder)).payload[CONTEXT_KEY]
        assert context["body_status"] == "complete" and context["reason"] == BODY_REASON
    intents = [row for row in producer.redis.events if row["event_type"] == "trade_intent"]
    assert len(intents) == 1 and intents[0]["payload"]["reason"] == BODY_REASON
    # A proposed close is not a sale. The OMS still owns child-fill reconciliation.
    assert broker.release_calls == [] and len(broker.submitted) == 1


def test_unreadable_atr_is_not_hidden_by_pending_body(monkeypatch):
    from project_mai_tai.orb_schwab_exits import completed_bar_evidence, save_context

    _oms, factory, _broker, _clock, _event = _strategy_exit_service(monkeypatch)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        entry.payload = {}
        entry_id = str(entry.id)
        fill = session.scalar(select(Fill))
        context = completed_bar_evidence(str(fill.id), OPEN.replace(minute=30, second=10),
            OPEN.replace(minute=31, second=1), atr_bars=[], atr_status="schwab_bar_read_unavailable")
    save_context(factory, entry_id, context)
    with factory() as session:
        assert session.scalar(select(SystemIncident)).payload["reason"] == "schwab_bar_read_unavailable"


def test_missing_later_atr_bar_pages_once_at_91_seconds_after_reload(monkeypatch):
    from project_mai_tai.orb_schwab_exits import CONTEXT_KEY, completed_bar_evidence, decode_bar, save_context

    _oms, factory, broker, _clock, _event = _strategy_exit_service(monkeypatch, atr=True)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder))
        initial = entry.payload[CONTEXT_KEY]
        bars = [decode_bar(row) for row in initial["atr_bars"][:-1]]
        entry.payload = {}
        entry_id = str(entry.id)
    close = OPEN.replace(minute=32)
    for seconds in (3.5, 61, 90, 91, 93):
        with factory() as session:
            prior = session.scalar(select(BrokerOrder)).payload.get(CONTEXT_KEY)
        now = close + timedelta(seconds=seconds)
        context = completed_bar_evidence(initial["fill_id"], datetime.fromisoformat(initial["fill_at"]),
            now, atr_bars=bars, atr_status="missing_last_closed_schwab_bar", prior=prior)
        save_context(factory, entry_id, context)
        assert context["reason"] is None
        with factory() as session:
            incidents = session.scalars(select(SystemIncident)).all()
            assert len(incidents) == int(seconds > 90)
            if incidents:
                assert incidents[0].payload["reason"] == "missing_completed_schwab_atr_bar"
                assert OPEN.replace(minute=31).isoformat() in incidents[0].payload["atr_overdue_minutes"]
    assert broker.release_calls == [] and len(broker.submitted) == 1


def _v2_open():
    return TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT, symbol="CLRO",
        side="buy", quantity=Decimal("2"), intent_type="open", reason="ATR_ENTRY",
        metadata={"order_type": "stop_limit", "stop_price": "5.53", "limit_price": "5.56"},
    ))


@pytest.mark.parametrize("enabled", [False, True])
def test_v2_own_add_not_blocked_by_orb_broker_read_and_flag_off_has_no_lock(monkeypatch, enabled):
    from contextlib import contextmanager
    from types import SimpleNamespace

    service, factory, broker = _service(monkeypatch, enabled=enabled)
    # Main already reconciles accounts AFTER dispatch. Isolate new entry-path
    # dependencies, without changing that existing post-submit reconciliation.
    service._reconcile_after_intent = lambda *_args: asyncio.sleep(0)
    reads, locks = [], []
    async def unavailable(_account):
        reads.append(_account)
        raise RuntimeError("a v2 add must not acquire this new ORB dependency")
    broker.list_account_positions = unavailable
    with factory.begin() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2")
        account = service.store.ensure_broker_account(session, ACCOUNT, provider="schwab", environment="live")
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                                    symbol="CLRO", quantity=2, average_price=5.5))
    class Proxy:
        def __init__(self, session):
            self.session = session
        def __getattr__(self, name):
            return getattr(self.session, name)
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        def execute(self, statement, params=None, **kwargs):
            if "pg_advisory_xact_lock" in str(statement):
                locks.append(params)
                return None
            return self.session.execute(statement, params, **kwargs)
    @contextmanager
    def spy_factory():
        with factory() as session:
            yield Proxy(session)
    service.session_factory = spy_factory
    result = asyncio.run(service.process_trade_intent(_v2_open()))
    assert reads == []
    assert len(locks) == int(enabled)
    assert [row.payload.status for row in result] == ["accepted"]
    assert len(broker.submitted) == 1 and broker.submitted[0].strategy_code == "schwab_1m_v2"


def test_flag_off_watchdog_does_no_query_and_emits_no_intent(monkeypatch):
    service, _factory_, broker = _service(monkeypatch, enabled=False)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN.replace(hour=14))
    def forbidden():
        pytest.fail("flag-off watchdog queried its database")
    service.session_factory = forbidden
    asyncio.run(service._orb_schwab_watchdog())
    assert broker.submitted == [] and service.redis.events == []


def test_atr_open_does_not_consult_orb_macd_or_deferred_window(monkeypatch):
    service, _factory_, broker = _service(monkeypatch, enabled=False)
    monkeypatch.setattr(
        "project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
        lambda *_args: pytest.fail("ATR order path consulted ORB-only MACD gate"),
    )
    result = asyncio.run(service.process_trade_intent(_v2_open()))
    assert [row.payload.status for row in result] == ["accepted"]
    assert len(broker.submitted) == 1
    assert broker.submitted[0].strategy_code == "schwab_1m_v2"


def test_flag_off_v2_skips_orb_db_collision_even_with_orb_owned_position(monkeypatch):
    service, factory, broker = _service(monkeypatch, enabled=False)
    service._reconcile_after_intent = lambda *_args: asyncio.sleep(0)
    with factory.begin() as session:
        strategy = service.store.ensure_strategy(session, "orb_schwab")
        account = service.store.ensure_broker_account(session, ACCOUNT, provider="schwab", environment="live")
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                                    symbol="CLRO", quantity=2, average_price=5.5))
    calls = []
    original = service._orb_schwab_collision_reason
    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_orb_schwab_collision_reason", spy)
    result = asyncio.run(service.process_trade_intent(_v2_open()))
    assert calls == []
    assert [row.payload.status for row in result] == ["accepted"]
    assert len(broker.submitted) == 1 and broker.submitted[0].strategy_code == "schwab_1m_v2"


@pytest.mark.parametrize(
    "seconds,deferred,allowed",
    [(14, False, True), (15, False, False), (15, True, True),
     (89, True, True), (90, True, False)],
)
def test_new_open_deadline_is_exactly_092930_only_if_macd_waited(seconds, deferred, allowed):
    event = build_orb_schwab_open_intent(
        _settings(), "CLRO", Decimal("5.53"),
        deferred_macd_bar_close=OPEN if deferred else None,
    )
    assert (orb_schwab_intent_refusal(event, _settings(), OPEN + timedelta(seconds=seconds)) is None) is allowed


def test_deferred_open_proof_must_name_this_sessions_0928_bar():
    event = build_orb_schwab_open_intent(
        _settings(), "CLRO", Decimal("5.53"),
        deferred_macd_bar_close=OPEN + timedelta(days=1),
    )
    assert orb_schwab_intent_refusal(event, _settings(), OPEN + timedelta(seconds=89)) == (
        "orb_schwab_invalid_deferred_macd_proof"
    )


def test_wrong_source_is_refused_before_any_io(monkeypatch):
    service, factory, broker = _service(monkeypatch)
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.53"))
    event.source_service = "orb"
    assert orb_schwab_intent_refusal(event, service.settings, OPEN) == "orb_schwab_wrong_source"
    assert asyncio.run(service.process_trade_intent(event)) == []
    assert broker.previewed == [] and broker.submitted == []
    with factory() as session:
        assert session.scalar(select(TradeIntent)) is None


def test_cancel_with_accepted_status_but_nonzero_filled_quantity_is_refused(monkeypatch):
    from dataclasses import replace
    service, _factory_, broker = _service(monkeypatch)
    asyncio.run(service.process_trade_intent(build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.53"))))
    original = broker.fetch_order_update
    async def partial(request):
        return replace(await original(request), filled_quantity=Decimal("1"))
    broker.fetch_order_update = partial
    result = asyncio.run(service.process_trade_intent(build_orb_schwab_cancel_intent(service.settings, "CLRO")))
    assert result[0].payload.reason == "orb_schwab_cancel_entry_not_unfilled_working"
    assert len(broker.submitted) == 1


def test_reprice_to_lower_level_is_refused(monkeypatch):
    service, _factory_, broker = _service(monkeypatch)
    asyncio.run(service.process_trade_intent(build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.53"))))
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN + timedelta(minutes=1))
    result = asyncio.run(service.process_trade_intent(build_orb_schwab_reprice_intent(service.settings, "CLRO", Decimal("5.52"))))
    assert result[0].payload.reason == "orb_schwab_reprice_not_higher"
    assert broker.replaced == [] and len(broker.submitted) == 1


def test_put_accepted_confirmation_unreadable_persists_new_id_and_reconciles_under_hold(monkeypatch):
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter

    service, factory, broker = _service(monkeypatch)
    asyncio.run(service.process_trade_intent(build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.53"))))
    adapter = SchwabBrokerAdapter(Settings(
        oms_adapter="schwab", schwab_access_token="test", schwab_account_hash="test-hash",
        strategy_schwab_1m_v2_account_name=ACCOUNT, strategy_schwab_1m_v2_broker_provider="schwab",
        strategy_schwab_1m_v2_go_live_enabled=True, orb_live_schwab_orders_enabled=True,
    ))
    puts = []
    async def fetch(_account, broker_id):
        if broker_id == "SCHWAB-ORB-1":
            return {"status": "WORKING", "orderType": "STOP_LIMIT", "quantity": 2, "filledQuantity": 0}
        raise RuntimeError("confirmation GET unavailable after accepted PUT")
    async def put(method, path, body):
        puts.append((method, path))
        return 201, {"Location": "/trader/v1/accounts/test-hash/orders/NEW-PARENT"}, {}
    monkeypatch.setattr(adapter, "_fetch_order", fetch)
    monkeypatch.setattr(adapter, "_authorized_request_json", put)
    broker.replace_bracket_order = adapter.replace_bracket_order
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN + timedelta(minutes=1))
    result = asyncio.run(service.process_trade_intent(build_orb_schwab_reprice_intent(service.settings, "CLRO", Decimal("5.6"))))
    assert result[0].payload.reason == "orb_schwab_reprice_broker_outcome_unknown"
    with factory() as session:
        parent = session.scalar(select(BrokerOrder))
        assert parent.broker_order_id == "NEW-PARENT"
        assert parent.payload["orb_replace_hold"]["new_id"] == "NEW-PARENT"
        assert session.scalar(select(SystemIncident)).payload["source"] == "orb_schwab_replace_unknown"
        assert session.scalar(select(Fill)) is None

    reads = []
    async def reconcile(request):
        reads.append((request.strategy_code, request.metadata["broker_order_id"]))
        return ExecutionReport(event_type="partially_filled", client_order_id=request.client_order_id,
            broker_order_id="NEW-PARENT", broker_fill_id="ACTUAL-NEW-FILL", symbol="CLRO",
            side="buy", intent_type="open", quantity=Decimal("2"), filled_quantity=Decimal("1"),
            fill_price=Decimal("5.6"), metadata={"orb_entry_fill_time_source": "execution_leg",
                                               "orb_entry_first_fill_at": OPEN.replace(minute=30).isoformat()})
    broker.fetch_order_update = reconcile
    asyncio.run(service.sync_broker_orders(account_names=[ACCOUNT]))
    assert reads == [("orb_schwab", "NEW-PARENT")]
    with factory() as session:
        assert session.scalar(select(BrokerOrder)).payload["orb_replace_hold"]["new_id"] == "NEW-PARENT"
        assert session.scalar(select(Fill)).broker_fill_id == "ACTUAL-NEW-FILL"
        assert session.scalar(select(VirtualPosition)).quantity == 1
    restarted = OmsRiskService(settings=service.settings, redis_client=_Redis(),
                               session_factory=factory, broker_adapter=broker)
    for event in (build_orb_schwab_cancel_intent(service.settings, "CLRO"),
                  build_orb_schwab_reprice_intent(service.settings, "CLRO", Decimal("5.7"))):
        result = asyncio.run(restarted.process_trade_intent(event))
        assert result[0].payload.reason == "orb_schwab_replace_hold_reconcile_required"
    assert len(puts) == 1 and len(broker.submitted) == 1
