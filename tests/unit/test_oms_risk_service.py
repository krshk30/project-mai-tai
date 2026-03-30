from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AccountPosition, BrokerOrder, Fill, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def aclose(self) -> None:
        return None


class FakeCancelBrokerAdapter:
    def __init__(self, *, cancel_event_type: str = "cancelled") -> None:
        self.cancel_event_type = cancel_event_type

    async def submit_order(self, request):
        if request.intent_type == "open":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-123",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]
        return [
            ExecutionReport(
                event_type=self.cancel_event_type,  # type: ignore[arg-type]
                client_order_id=request.client_order_id,
                broker_order_id="ord-123",
                symbol=request.symbol,
                side=request.side,
                intent_type="cancel",
                quantity=request.quantity,
                reason=request.reason or "USER_CANCEL",
                metadata=dict(request.metadata),
            )
        ]

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []

    async def fetch_order_update(self, request):
        del request
        return None


class FakeOrderSyncBrokerAdapter:
    def __init__(self, report: ExecutionReport | None = None) -> None:
        self.report = report

    async def submit_order(self, request):
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-123",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        assert request.metadata["broker_order_id"] == "ord-123"
        return self.report

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_oms_service_persists_filled_intent_and_positions() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "path": "P1_MACD_CROSS",
                    "reference_price": "2.55",
                },
            ),
        )
    )

    assert [event.payload.status for event in events] == ["accepted", "filled"]
    assert [stream for stream, _payload in redis.entries] == ["test:order-events", "test:order-events"]

    with session_factory() as session:
        stored_intent = session.scalar(select(TradeIntent))
        stored_order = session.scalar(select(BrokerOrder))
        stored_fill = session.scalar(select(Fill))
        virtual_position = session.scalar(select(VirtualPosition))
        account_position = session.scalar(select(AccountPosition))

        assert stored_intent is not None
        assert stored_intent.status == "filled"
        assert stored_order is not None
        assert stored_order.status == "filled"
        assert stored_fill is not None
        assert stored_fill.price == Decimal("2.55")
        assert virtual_position is not None
        assert virtual_position.quantity == Decimal("10")
        assert virtual_position.average_price == Decimal("2.55")
        assert account_position is not None
        assert account_position.quantity == Decimal("10")


@pytest.mark.asyncio
async def test_oms_service_rejects_non_positive_quantity() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("0"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    assert len(events) == 1
    assert events[0].payload.status == "rejected"
    with session_factory() as session:
        stored_intent = session.scalar(select(TradeIntent))
        assert stored_intent is not None
        assert stored_intent.status == "rejected"


@pytest.mark.asyncio
async def test_oms_service_syncs_account_positions_from_broker_truth() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition))
        assert account_position is not None
        account_position.quantity = Decimal("3")
        account_position.average_price = Decimal("1.11")
        session.commit()

    sync_summary = await service.sync_broker_positions(account_names=["paper:macd_30s"])
    assert sync_summary == {"accounts": 1, "positions": 1}

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition))
        assert account_position is not None
        assert account_position.quantity == Decimal("10")
        assert account_position.average_price == Decimal("2.55")


@pytest.mark.asyncio
async def test_oms_service_cancels_open_order_using_existing_order_identity() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeCancelBrokerAdapter(),
    )

    open_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )
    cancel_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("0"),
                intent_type="cancel",
                reason="USER_CANCEL",
                metadata={},
            ),
        )
    )

    assert [event.payload.status for event in open_events] == ["accepted"]
    assert [event.payload.status for event in cancel_events] == ["cancelled"]
    assert cancel_events[0].payload.client_order_id == open_events[0].payload.client_order_id
    assert cancel_events[0].payload.quantity == Decimal("10")

    with session_factory() as session:
        stored_order = session.scalar(select(BrokerOrder))
        intents = session.scalars(select(TradeIntent).order_by(TradeIntent.created_at)).all()

        assert stored_order is not None
        assert stored_order.status == "cancelled"
        assert [intent.intent_type for intent in intents] == ["open", "cancel"]
        assert intents[0].status == "submitted"
        assert intents[1].status == "cancelled"


@pytest.mark.asyncio
async def test_oms_service_keeps_open_order_status_when_cancel_is_rejected() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeCancelBrokerAdapter(cancel_event_type="rejected"),
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )
    cancel_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("0"),
                intent_type="cancel",
                reason="USER_CANCEL",
                metadata={},
            ),
        )
    )

    assert [event.payload.status for event in cancel_events] == ["rejected"]

    with session_factory() as session:
        stored_order = session.scalar(select(BrokerOrder))
        cancel_intent = session.scalars(
            select(TradeIntent).where(TradeIntent.intent_type == "cancel")
        ).one()

        assert stored_order is not None
        assert stored_order.status == "accepted"
        assert cancel_intent.status == "rejected"


@pytest.mark.asyncio
async def test_oms_service_syncs_open_order_status_from_broker() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeOrderSyncBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_1m",
                broker_account_name="paper:macd_1m",
                symbol="BFRG",
                side="buy",
                quantity=Decimal("100"),
                intent_type="open",
                reason="ENTRY_P3_MACD_SURGE",
                metadata={"reference_price": "1.15"},
            ),
        )
    )

    adapter.report = ExecutionReport(
        event_type="cancelled",
        client_order_id="macd_1m-BFRG-open-abc123",
        broker_order_id="ord-123",
        symbol="BFRG",
        side="buy",
        intent_type="open",
        quantity=Decimal("100"),
        reason="ENTRY_P3_MACD_SURGE",
        metadata={},
    )

    summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])
    assert summary == {"orders": 1, "terminal_orders": 1}

    with session_factory() as session:
        stored_order = session.scalar(select(BrokerOrder))
        stored_intent = session.scalar(select(TradeIntent))

        assert stored_order is not None
        assert stored_order.status == "cancelled"
        assert stored_intent is not None
        assert stored_intent.status == "cancelled"
