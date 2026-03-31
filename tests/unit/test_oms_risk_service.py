from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Fill, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []
        self.values: dict[str, str] = {}

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        del ex
        self.values[key] = value
        return True

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


class FakePendingExitBrokerAdapter:
    async def submit_order(self, request):
        if request.intent_type == "open":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
                ExecutionReport(
                    event_type="filled",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    broker_fill_id="fill-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    filled_quantity=request.quantity,
                    fill_price=Decimal("2.55"),
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
            ]
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-exit",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=Decimal("10"),
                average_price=Decimal("2.55"),
                market_value=None,
                as_of=None,
            )
        ]


class FakeAcceptedOnlyBrokerAdapter:
    async def submit_order(self, request):
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=f"ord-{request.client_order_id}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


class FakeRejectNotTradableBrokerAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def submit_order(self, request):
        self.requests.append(request)
        return [
            ExecutionReport(
                event_type="rejected",
                client_order_id=request.client_order_id,
                broker_order_id=None,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason='asset "JCSE" is not tradable',
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

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
async def test_oms_service_blocks_not_tradable_symbol_for_rest_of_session() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeRejectNotTradableBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    first = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="JCSE",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={},
            ),
        )
    )
    second = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="JCSE",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={},
            ),
        )
    )

    assert len(adapter.requests) == 1
    assert first[0].payload.status == "rejected"
    assert 'tradable' in (first[0].payload.reason or '')
    assert second[0].payload.status == "rejected"
    assert second[0].payload.reason == "broker_symbol_not_tradable_for_session"

    with session_factory() as session:
        intents = session.scalars(select(TradeIntent).order_by(TradeIntent.created_at.asc())).all()
        assert len(intents) == 2
        assert all(intent.status == "rejected" for intent in intents)


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
async def test_oms_service_sync_clears_virtual_positions_without_broker_backing() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    with session_factory() as session:
        strategy = service.store.ensure_strategy(session, "macd_30s")
        account = service.store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="alpaca",
            environment="paper",
        )
        session.add(
            VirtualPosition(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                symbol="ASTC",
                quantity=Decimal("10"),
                average_price=Decimal("5.36"),
                realized_pnl=Decimal("0"),
            )
        )
        session.commit()

    sync_summary = await service.sync_broker_positions(account_names=["paper:macd_30s"])
    assert sync_summary == {"accounts": 1, "positions": 0}

    with session_factory() as session:
        virtual_position = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "ASTC"))
        assert virtual_position is not None
        assert virtual_position.quantity == Decimal("0")
        assert virtual_position.average_price == Decimal("0")


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


@pytest.mark.asyncio
async def test_oms_service_rejects_exit_when_broker_has_no_position() -> None:
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
        account_position.quantity = Decimal("0")
        session.commit()

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={"reference_price": "2.40"},
            ),
        )
    )

    assert len(events) == 1
    assert events[0].payload.status == "rejected"
    assert events[0].payload.reason == "no broker position available to sell"


@pytest.mark.asyncio
async def test_oms_service_rejects_duplicate_exit_in_flight() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakePendingExitBrokerAdapter(),
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

    first = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={"reference_price": "2.40"},
            ),
        )
    )
    assert first[0].payload.status == "accepted"

    duplicate = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={"reference_price": "2.39"},
            ),
        )
    )

    assert len(duplicate) == 1
    assert duplicate[0].payload.status == "rejected"
    assert duplicate[0].payload.reason == "duplicate_exit_in_flight"


@pytest.mark.asyncio
async def test_oms_service_shared_account_exit_uses_strategy_virtual_quantity() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeAcceptedOnlyBrokerAdapter(),
    )

    with session_factory() as session:
        tos = service.store.ensure_strategy(session, "tos")
        runner = service.store.ensure_strategy(session, "runner")
        account = service.store.ensure_broker_account(
            session,
            "paper:tos_runner_shared",
            provider="alpaca",
            environment="paper",
        )
        session.add_all(
            [
                VirtualPosition(
                    strategy_id=tos.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("60"),
                    average_price=Decimal("2.00"),
                    realized_pnl=Decimal("0"),
                ),
                VirtualPosition(
                    strategy_id=runner.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("40"),
                    average_price=Decimal("2.10"),
                    realized_pnl=Decimal("0"),
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("100"),
                    average_price=Decimal("2.04"),
                    market_value=Decimal("204.00"),
                ),
            ]
        )
        session.commit()

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="runner",
                broker_account_name="paper:tos_runner_shared",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("100"),
                intent_type="close",
                reason="TRAIL_STOP_10%",
                metadata={},
            ),
        )
    )

    assert len(events) == 1
    assert events[0].payload.status == "accepted"
    assert events[0].payload.quantity == Decimal("40")


@pytest.mark.asyncio
async def test_oms_service_shared_account_exit_respects_pending_exit_reservations() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeAcceptedOnlyBrokerAdapter(),
    )

    with session_factory() as session:
        tos = service.store.ensure_strategy(session, "tos")
        runner = service.store.ensure_strategy(session, "runner")
        account = service.store.ensure_broker_account(
            session,
            "paper:tos_runner_shared",
            provider="alpaca",
            environment="paper",
        )
        existing_intent = TradeIntent(
            strategy_id=tos.id,
            broker_account_id=account.id,
            symbol="UGRO",
            side="sell",
            intent_type="close",
            quantity=Decimal("60"),
            reason="TRAIL_STOP_10%",
            status="submitted",
            payload={},
        )
        session.add(existing_intent)
        session.flush()
        session.add(
            BrokerOrder(
                intent_id=existing_intent.id,
                strategy_id=tos.id,
                broker_account_id=account.id,
                client_order_id="tos-exit-1",
                broker_order_id="ord-existing",
                symbol="UGRO",
                side="sell",
                order_type="market",
                time_in_force="day",
                quantity=Decimal("60"),
                status="accepted",
                payload={},
            )
        )
        session.add_all(
            [
                VirtualPosition(
                    strategy_id=runner.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("50"),
                    average_price=Decimal("2.10"),
                    realized_pnl=Decimal("0"),
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("100"),
                    average_price=Decimal("2.04"),
                    market_value=Decimal("204.00"),
                ),
            ]
        )
        session.commit()

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="runner",
                broker_account_name="paper:tos_runner_shared",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("50"),
                intent_type="close",
                reason="TRAIL_STOP_10%",
                metadata={},
            ),
        )
    )

    assert len(events) == 1
    assert events[0].payload.status == "accepted"
    assert events[0].payload.quantity == Decimal("40")


def test_store_clears_virtual_positions_without_broker_backing() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        strategy = Strategy(code="macd_30s", name="MACD 30S", execution_mode="paper", metadata_json={})
        account = BrokerAccount(name="paper:macd_30s", provider="alpaca", environment="development")
        session.add_all([strategy, account])
        session.flush()
        session.add_all(
            [
                VirtualPosition(
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("10"),
                    average_price=Decimal("2.50"),
                    realized_pnl=Decimal("0"),
                ),
                VirtualPosition(
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    symbol="MESA",
                    quantity=Decimal("5"),
                    average_price=Decimal("1.25"),
                    realized_pnl=Decimal("0"),
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol="MESA",
                    quantity=Decimal("5"),
                    average_price=Decimal("1.25"),
                    market_value=Decimal("6.25"),
                ),
            ]
        )
        session.commit()

    store = OmsStore()
    with session_factory() as session:
        cleared = store.clear_virtual_positions_without_account_backing(session)
        session.commit()

    assert cleared == 1
    with session_factory() as session:
        positions = {position.symbol: position for position in session.scalars(select(VirtualPosition)).all()}
        assert positions["UGRO"].quantity == Decimal("0")
        assert positions["UGRO"].average_price == Decimal("0")
        assert positions["MESA"].quantity == Decimal("5")
