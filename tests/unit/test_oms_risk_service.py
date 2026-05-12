from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    Fill,
    SchwabIneligibleToday,
    Strategy,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.events import (
    QuoteTickEvent,
    QuoteTickPayload,
    TradeIntentEvent,
    TradeIntentPayload,
    TradeTickEvent,
    TradeTickPayload,
)
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.runtime_registry import configured_broker_account_registrations, strategy_registration_map
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.time_utils import session_day_eastern_str


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


class FakeSequentialOrderSyncBrokerAdapter:
    def __init__(self, reports: list[ExecutionReport | None]) -> None:
        self.reports = reports
        self.fetch_calls = 0

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
        del request
        report = self.reports[min(self.fetch_calls, len(self.reports) - 1)]
        self.fetch_calls += 1
        return report

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


class FakeStopRejectedFallbackBrokerAdapter:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str, str, Decimal]] = []
        self.quantity = Decimal("10")

    async def submit_order(self, request):
        self.submitted.append((request.intent_type, request.side, request.symbol, request.quantity))
        if request.intent_type == "open":
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason="child stop rejected at/below stop",
                    metadata=dict(request.metadata),
                )
            ]
        self.quantity = Decimal("0")
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-fallback",
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
                broker_order_id="ord-fallback",
                broker_fill_id="fill-fallback",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                filled_quantity=request.quantity,
                fill_price=Decimal("2.40"),
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        if self.quantity <= 0:
            return []
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=self.quantity,
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


class FakeWorkingOrderRefreshBrokerAdapter:
    def __init__(
        self,
        *,
        fetch_event_type: str = "accepted",
        filled_quantity: Decimal = Decimal("0"),
        fill_price: Decimal | None = None,
        ask_price: float = 1.23,
        bid_price: float = 1.21,
    ) -> None:
        self.fetch_event_type = fetch_event_type
        self.filled_quantity = filled_quantity
        self.fill_price = fill_price
        self.ask_price = ask_price
        self.bid_price = bid_price
        self.submit_requests = []

    async def submit_order(self, request):
        self.submit_requests.append(request)
        if request.intent_type == "cancel":
            return [
                ExecutionReport(
                    event_type="cancelled",
                    client_order_id=request.client_order_id,
                    broker_order_id=str(request.metadata.get("broker_order_id", "ord-123")),
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]

        broker_order_id = f"ord-{len([item for item in self.submit_requests if item.intent_type != 'cancel'])}"
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        return ExecutionReport(
            event_type=self.fetch_event_type,  # type: ignore[arg-type]
            client_order_id=request.client_order_id,
            broker_order_id=str(request.metadata.get("broker_order_id", "ord-123")),
            symbol=request.symbol,
            side=request.side,
            intent_type=request.intent_type,
            quantity=request.quantity,
            filled_quantity=self.filled_quantity,
            fill_price=self.fill_price,
            reason=request.reason,
            metadata=dict(request.metadata),
        )

    async def fetch_quotes(self, symbols):
        return {
            str(symbol).upper(): {
                "ask_price": self.ask_price,
                "bid_price": self.bid_price,
                "last_price": (self.ask_price + self.bid_price) / 2,
            }
            for symbol in symbols
        }

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


class FakeTickDrivenHardStopBrokerAdapter:
    def __init__(self, *, open_fill_price: Decimal = Decimal("4.00")) -> None:
        self.open_fill_price = open_fill_price
        self.submit_requests = []
        self.position_quantity = Decimal("0")

    async def submit_order(self, request):
        self.submit_requests.append(request)
        if request.intent_type == "open":
            self.position_quantity += request.quantity
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
                    fill_price=self.open_fill_price,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
            ]
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-close",
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
        if self.position_quantity <= 0:
            return []
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=self.position_quantity,
                average_price=self.open_fill_price,
                market_value=None,
                as_of=None,
            )
        ]


class FakeNativeStopGuardBrokerAdapter:
    def __init__(self, *, open_fill_price: Decimal = Decimal("4.00")) -> None:
        self.open_fill_price = open_fill_price
        self.submit_requests = []
        self.position_quantity = Decimal("0")

    async def submit_order(self, request):
        self.submit_requests.append(request)
        if request.intent_type == "open":
            self.position_quantity += request.quantity
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
                    fill_price=self.open_fill_price,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
            ]
        if request.intent_type == "cancel":
            return [
                ExecutionReport(
                    event_type="cancelled",
                    client_order_id=request.client_order_id,
                    broker_order_id=str(request.metadata.get("broker_order_id", "ord-stop")),
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]
        if str(request.metadata.get("native_stop_guard", "")).lower() == "true":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id=f"ord-stop-{len(self.submit_requests)}",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]
        self.position_quantity = max(Decimal("0"), self.position_quantity - request.quantity)
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=f"ord-sell-{len(self.submit_requests)}",
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
                broker_order_id=f"ord-sell-{len(self.submit_requests)}",
                broker_fill_id=f"fill-sell-{len(self.submit_requests)}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                filled_quantity=request.quantity,
                fill_price=Decimal("4.20"),
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        if self.position_quantity <= 0:
            return []
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=self.position_quantity,
                average_price=self.open_fill_price,
                market_value=None,
                as_of=None,
            )
        ]


async def _noop_sync_broker_state(*, account_names=None):
    del account_names
    return {"accounts": 0, "positions": 0, "orders": 0, "terminal_orders": 0}


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


class FakeRejectSchwabIneligibleBrokerAdapter:
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
                reason="Opening transactions for this security must be placed with a broker. Contact us",
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


def test_runtime_registry_can_route_macd_30s_to_schwab_only() -> None:
    settings = Settings(
        oms_adapter="simulated",
        strategy_macd_30s_broker_provider="schwab",
        strategy_macd_1m_enabled=True,
    )

    registrations = strategy_registration_map(settings)
    broker_accounts = {item.name: item for item in configured_broker_account_registrations(settings)}

    assert registrations["macd_30s"].execution_mode == "live"
    assert registrations["macd_30s"].metadata["provider"] == "schwab"
    assert registrations["macd_1m"].execution_mode == "shadow"
    assert broker_accounts[settings.strategy_macd_30s_account_name].provider == "schwab"
    assert broker_accounts[settings.strategy_macd_1m_account_name].provider == "alpaca"


def test_oms_service_builds_routing_adapter_for_mixed_brokers() -> None:
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            strategy_macd_30s_broker_provider="schwab",
            strategy_macd_1m_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
    )

    assert isinstance(service.broker_adapter, RoutingBrokerAdapter)


def test_runtime_registry_can_route_tos_to_schwab_only() -> None:
    settings = Settings(
        oms_adapter="simulated",
        strategy_macd_1m_enabled=True,
        strategy_tos_enabled=True,
        strategy_tos_broker_provider="schwab",
    )

    registrations = strategy_registration_map(settings)
    broker_accounts = {item.name: item for item in configured_broker_account_registrations(settings)}

    assert registrations["tos"].execution_mode == "live"
    assert registrations["tos"].metadata["provider"] == "schwab"
    assert registrations["macd_1m"].execution_mode == "shadow"
    assert broker_accounts[settings.strategy_tos_account_name].provider == "schwab"
    assert broker_accounts[settings.strategy_macd_1m_account_name].provider == "alpaca"


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
async def test_oms_service_caches_schwab_ineligible_symbol_for_session_day() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeRejectSchwabIneligibleBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            strategy_macd_30s_broker_provider="schwab",
        ),
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
                symbol="AEHL",
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
                symbol="AEHL",
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
    assert "placed with a broker" in (first[0].payload.reason or "")
    assert second[0].payload.status == "rejected"
    assert second[0].payload.reason == "schwab_ineligible_cached"

    with session_factory() as session:
        entries = session.scalars(select(SchwabIneligibleToday)).all()
        assert len(entries) == 1
        assert entries[0].symbol == "AEHL"
        assert entries[0].session_date == session_day_eastern_str()
        assert entries[0].hit_count == 1


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
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

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
async def test_oms_service_sync_publishes_terminal_order_event_for_strategy_runtime() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeSequentialOrderSyncBrokerAdapter(
        reports=[
            ExecutionReport(
                event_type="partially_filled",
                client_order_id="macd_1m-MASK-open-abc123",
                broker_order_id="ord-123",
                broker_fill_id="fill-92",
                symbol="MASK",
                side="buy",
                intent_type="open",
                quantity=Decimal("100"),
                filled_quantity=Decimal("92"),
                fill_price=Decimal("2.43"),
                reason="ENTRY_P2_VWAP_BREAKOUT",
                metadata={},
            ),
            ExecutionReport(
                event_type="filled",
                client_order_id="macd_1m-MASK-open-abc123",
                broker_order_id="ord-123",
                broker_fill_id="fill-100",
                symbol="MASK",
                side="buy",
                intent_type="open",
                quantity=Decimal("100"),
                filled_quantity=Decimal("100"),
                fill_price=Decimal("2.43"),
                reason="ENTRY_P2_VWAP_BREAKOUT",
                metadata={},
            ),
        ]
    )
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_1m",
                broker_account_name="paper:macd_1m",
                symbol="MASK",
                side="buy",
                quantity=Decimal("100"),
                intent_type="open",
                reason="ENTRY_P2_VWAP_BREAKOUT",
                metadata={},
            ),
        )
    )

    first_summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])
    second_summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])

    assert first_summary == {"orders": 1, "terminal_orders": 0}
    assert second_summary == {"orders": 1, "terminal_orders": 1}
    order_events = [payload for stream, payload in redis.entries if stream == "test:order-events"]
    assert [item["payload"]["status"] for item in order_events] == ["accepted", "partially_filled", "filled"]


@pytest.mark.asyncio
async def test_oms_service_sync_skips_duplicate_partial_without_new_fill_progress() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeOrderSyncBrokerAdapter(
        ExecutionReport(
            event_type="partially_filled",
            client_order_id="macd_1m-MASK-open-abc123",
            broker_order_id="ord-123",
            broker_fill_id="fill-92",
            symbol="MASK",
            side="buy",
            intent_type="open",
            quantity=Decimal("100"),
            filled_quantity=Decimal("92"),
            fill_price=Decimal("2.43"),
            reason="ENTRY_P2_VWAP_BREAKOUT",
            metadata={},
        )
    )
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_1m",
                broker_account_name="paper:macd_1m",
                symbol="MASK",
                side="buy",
                quantity=Decimal("100"),
                intent_type="open",
                reason="ENTRY_P2_VWAP_BREAKOUT",
                metadata={},
            ),
        )
    )

    first_summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])
    second_summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])

    assert first_summary == {"orders": 1, "terminal_orders": 0}
    assert second_summary == {"orders": 0, "terminal_orders": 0}
    order_events = [payload for stream, payload in redis.entries if stream == "test:order-events"]
    assert [item["payload"]["status"] for item in order_events] == ["accepted", "partially_filled"]


@pytest.mark.asyncio
async def test_oms_service_refreshes_stale_working_limit_buy_order() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

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
                metadata={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "1.15",
                    "reference_price": "1.15",
                    "price_source": "ask",
                },
            ),
        )
    )

    with session_factory() as session:
        stored_order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id.like("macd_1m-BFRG-open-%")))
        assert stored_order is not None
        stale_time = datetime.now(UTC) - timedelta(seconds=10)
        stored_order.updated_at = stale_time
        stored_order.submitted_at = stale_time
        session.commit()

    summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])
    assert summary == {"orders": 1, "terminal_orders": 1}

    with session_factory() as session:
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "BFRG").order_by(BrokerOrder.client_order_id)
        ).all()
        stored_intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "BFRG"))

        assert len(orders) == 2
        assert orders[0].status == "cancelled"
        assert orders[1].status == "accepted"
        assert orders[1].payload["limit_price"] == "1.23"
        assert orders[1].payload["watchdog_replaces_client_order_id"] == orders[0].client_order_id
        assert stored_intent is not None
        assert stored_intent.status == "submitted"

    order_events = [payload for stream, payload in redis.entries if stream == "test:order-events"]
    assert [item["payload"]["status"] for item in order_events] == ["accepted", "accepted"]
    assert all(item["payload"]["status"] != "cancelled" for item in order_events)
    assert [request.intent_type for request in adapter.submit_requests] == ["open", "cancel", "open"]


@pytest.mark.asyncio
async def test_oms_service_refreshes_remaining_quantity_for_stale_sell_order() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter(
        fetch_event_type="partially_filled",
        filled_quantity=Decimal("4"),
        fill_price=Decimal("2.50"),
        bid_price=2.41,
    )
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    store = OmsStore()
    with session_factory() as session:
        strategy = store.ensure_strategy(session, "macd_30s", name="MACD 30s", execution_mode="paper", metadata_json={})
        account = store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="schwab",
            environment="development",
        )
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="UGRO",
            side="sell",
            intent_type="close",
            quantity=Decimal("10"),
            reason="HARD_STOP",
            status="submitted",
            payload={"metadata": {"order_type": "limit"}},
        )
        session.add(intent)
        session.flush()
        stale_time = datetime.now(UTC) - timedelta(seconds=10)
        session.add(
            BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id="macd_30s-UGRO-close-abc123",
                broker_order_id="ord-123",
                symbol="UGRO",
                side="sell",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal("10"),
                status="accepted",
                payload={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "2.40",
                    "reference_price": "2.40",
                    "price_source": "bid",
                },
                submitted_at=stale_time,
                updated_at=stale_time,
            )
        )
        session.commit()

    summary = await service.sync_broker_orders(account_names=["paper:macd_30s"])
    assert summary == {"orders": 2, "terminal_orders": 1}

    with session_factory() as session:
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "UGRO").order_by(BrokerOrder.client_order_id)
        ).all()
        fills = session.scalars(select(Fill).where(Fill.symbol == "UGRO")).all()
        stored_intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "UGRO"))

        assert len(orders) == 2
        assert orders[0].status == "cancelled"
        assert orders[1].status == "accepted"
        assert orders[1].quantity == Decimal("6")
        assert orders[1].payload["limit_price"] == "2.41"
        assert len(fills) == 1
        assert fills[0].quantity == Decimal("4")
        assert stored_intent is not None
        assert stored_intent.status == "submitted"

    order_events = [payload for stream, payload in redis.entries if stream == "test:order-events"]
    assert [item["payload"]["status"] for item in order_events] == ["partially_filled", "accepted"]
    assert all(item["payload"]["status"] != "cancelled" for item in order_events)
    assert [request.intent_type for request in adapter.submit_requests] == ["cancel", "close"]


@pytest.mark.asyncio
async def test_oms_service_refreshes_stop_guard_sell_order_with_wider_panic_limit() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter(
        fetch_event_type="accepted",
        bid_price=2.41,
    )
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
            oms_stop_guard_refresh_stage_1_seconds=0.5,
            oms_stop_guard_refresh_stage_2_seconds=1.0,
            oms_stop_guard_refresh_stage_3_seconds=2.0,
            oms_stop_guard_refresh_stage_1_buffer_pct=3.0,
            oms_stop_guard_refresh_stage_2_buffer_pct=5.0,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    store = OmsStore()
    with session_factory() as session:
        strategy = store.ensure_strategy(session, "macd_30s", name="MACD 30s", execution_mode="paper", metadata_json={})
        account = store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="schwab",
            environment="development",
        )
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="UGRO",
            side="sell",
            intent_type="close",
            quantity=Decimal("10"),
            reason="HARD_STOP",
            status="submitted",
            payload={"metadata": {"order_type": "limit"}},
        )
        session.add(intent)
        session.flush()
        stale_time = datetime.now(UTC) - timedelta(seconds=10)
        session.add(
            BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id="macd_30s-UGRO-close-stop0",
                broker_order_id="ord-stop0",
                symbol="UGRO",
                side="sell",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal("10"),
                status="accepted",
                payload={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "2.35",
                    "reference_price": "2.35",
                    "price_source": "bid",
                    "stop_guard": "true",
                    "panic_buffer_pct": "1.5",
                },
                submitted_at=stale_time,
                updated_at=stale_time,
            )
        )
        session.commit()

    summary = await service.sync_broker_orders(account_names=["paper:macd_30s"])
    assert summary == {"orders": 1, "terminal_orders": 1}

    with session_factory() as session:
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "UGRO").order_by(BrokerOrder.client_order_id)
        ).all()

        assert len(orders) == 2
        assert orders[0].status == "cancelled"
        assert orders[1].status == "accepted"
        assert orders[1].payload["limit_price"] == "2.34"
        assert orders[1].payload["panic_buffer_pct"] == "3.0"
        assert orders[1].payload["stop_guard_refresh_stage"] == "1"


@pytest.mark.asyncio
async def test_oms_service_builds_second_stage_stop_guard_refresh_with_five_percent_buffer() -> None:
    adapter = FakeWorkingOrderRefreshBrokerAdapter(bid_price=2.41)
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_stop_guard_refresh_stage_1_buffer_pct=3.0,
            oms_stop_guard_refresh_stage_2_buffer_pct=5.0,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
        broker_adapter=adapter,
    )

    order = BrokerOrder(
        strategy_id=None,  # type: ignore[arg-type]
        broker_account_id=None,  # type: ignore[arg-type]
        client_order_id="macd_30s-UGRO-close-stop1",
        broker_order_id="ord-stop1",
        symbol="UGRO",
        side="sell",
        order_type="limit",
        time_in_force="day",
        quantity=Decimal("10"),
        status="accepted",
        payload={
            "order_type": "limit",
            "time_in_force": "day",
            "limit_price": "2.34",
            "reference_price": "2.34",
            "price_source": "bid",
            "stop_guard": "true",
            "panic_buffer_pct": "3.0",
            "stop_guard_refresh_stage": "1",
        },
    )

    refreshed = await service._build_refreshed_order_metadata(
        broker_account_name="paper:macd_30s",
        order=order,
    )

    assert refreshed is not None
    assert refreshed["limit_price"] == "2.29"
    assert refreshed["panic_buffer_pct"] == "5.0"
    assert refreshed["stop_guard_refresh_stage"] == "2"


@pytest.mark.asyncio
async def test_oms_service_uses_catastrophic_after_hours_stop_guard_refresh_when_quote_is_far_below_stop() -> None:
    adapter = FakeWorkingOrderRefreshBrokerAdapter(bid_price=2.10, ask_price=2.12)
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_after_hours_stop_guard_catastrophic_gap_pct=1.5,
            oms_after_hours_stop_guard_catastrophic_panic_buffer_pct=8.0,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
        broker_adapter=adapter,
    )

    order = BrokerOrder(
        strategy_id=None,  # type: ignore[arg-type]
        broker_account_id=None,  # type: ignore[arg-type]
        client_order_id="macd_30s-UGRO-close-stop-cat",
        broker_order_id="ord-stop-cat",
        symbol="UGRO",
        side="sell",
        order_type="limit",
        time_in_force="day",
        quantity=Decimal("10"),
        status="accepted",
        payload={
            "order_type": "limit",
            "time_in_force": "day",
            "limit_price": "2.29",
            "reference_price": "2.29",
            "price_source": "bid",
            "stop_guard": "true",
            "panic_buffer_pct": "1.0",
            "stop_price": "2.35",
            "session": "AM",
            "extended_hours": "true",
        },
    )

    refreshed = await service._build_refreshed_order_metadata(
        broker_account_name="paper:macd_30s",
        order=order,
    )

    assert refreshed is not None
    assert refreshed["limit_price"] == "1.93"
    assert refreshed["reference_price"] == "1.93"
    assert refreshed["panic_buffer_pct"] == "8.0"
    assert refreshed["catastrophic_stop_guard"] == "true"
    assert refreshed["stop_guard_refresh_stage"] == "2"
    assert refreshed["watchdog_refresh_reason"] == "catastrophic_gap"


@pytest.mark.asyncio
async def test_oms_service_uses_fast_broker_sync_interval_when_stop_guard_order_is_active() -> None:
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_broker_sync_interval_seconds=5,
            oms_stop_guard_refresh_stage_1_seconds=0.5,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    store = OmsStore()
    with session_factory() as session:
        strategy = store.ensure_strategy(session, "macd_30s", name="MACD 30s", execution_mode="paper", metadata_json={})
        account = store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="schwab",
            environment="development",
        )
        session.add(
            BrokerOrder(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id="macd_30s-UGRO-close-live",
                broker_order_id="ord-live",
                symbol="UGRO",
                side="sell",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal("10"),
                status="accepted",
                payload={
                    "order_type": "limit",
                    "stop_guard": "true",
                    "panic_buffer_pct": "1.5",
                },
                submitted_at=datetime.now(UTC),
            )
        )
        session.commit()

    assert await service._broker_sync_interval_seconds() == 0.5


@pytest.mark.asyncio
async def test_oms_service_applies_after_hours_stop_guard_overrides_when_arming_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.oms.service.utcnow",
        lambda: datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
    )
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_after_hours_stop_guard_quote_max_age_ms=1000,
            oms_after_hours_stop_guard_initial_panic_buffer_pct=1.0,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
        broker_adapter=FakeAcceptedOnlyBrokerAdapter(),
    )

    service._update_hard_stop_registry_from_fill(
        strategy_code="schwab_1m",
        broker_account_name="live:schwab_1m",
        symbol="UGRO",
        side="buy",
        intent_type="open",
        quantity=Decimal("10"),
        price=Decimal("4.00"),
        metadata={
            "stop_guard_enabled": "true",
            "stop_loss_pct": "1.5",
            "stop_guard_quote_max_age_ms": "2000",
            "stop_guard_initial_panic_buffer_pct": "0.5",
            "session": "PM",
            "extended_hours": "true",
        },
    )

    stop = service._armed_hard_stops[("schwab_1m", "live:schwab_1m", "UGRO")]
    assert stop.quote_max_age_ms == 1000
    assert stop.initial_panic_buffer_pct == 1.0


@pytest.mark.asyncio
async def test_oms_service_arms_hard_stop_from_open_fill_and_triggers_close_on_quote_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.oms.service.utcnow",
        lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeTickDrivenHardStopBrokerAdapter(open_fill_price=Decimal("4.00"))
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
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "reference_price": "4.00",
                    "stop_guard_enabled": "true",
                    "stop_loss_pct": "1.5",
                    "stop_guard_quote_max_age_ms": "2000",
                    "stop_guard_initial_panic_buffer_pct": "0.5",
                },
            ),
        )
    )

    key = ("macd_30s", "paper:macd_30s", "UGRO")
    assert key in service._armed_hard_stops
    assert service._armed_hard_stops[key].stop_price == Decimal("3.940")

    await service._handle_stream_message(
        {
            "data": QuoteTickEvent(
                source_service="market-data",
                payload=QuoteTickPayload(
                    symbol="UGRO",
                    bid_price=Decimal("3.93"),
                    ask_price=Decimal("3.95"),
                ),
            ).model_dump_json()
        }
    )

    assert [request.intent_type for request in adapter.submit_requests] == ["open", "close"]
    close_request = adapter.submit_requests[-1]
    assert close_request.reason == "HARD_STOP"
    assert close_request.metadata["stop_guard"] == "true"
    assert close_request.metadata["stop_trigger_source"] == "bid"
    assert close_request.metadata["limit_price"] == "3.91"
    assert close_request.metadata["price_source"] == "bid"
    assert service._armed_hard_stops[key].close_in_flight is True


@pytest.mark.asyncio
async def test_oms_service_uses_trade_trigger_when_fresh_bid_has_not_breached_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 3, 31, 11, 0, tzinfo=UTC)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: fixed_now)
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeTickDrivenHardStopBrokerAdapter(open_fill_price=Decimal("4.00"))
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
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "reference_price": "4.00",
                    "stop_guard_enabled": "true",
                    "stop_loss_pct": "1.5",
                    "stop_guard_quote_max_age_ms": "2000",
                    "stop_guard_initial_panic_buffer_pct": "0.5",
                },
            ),
        )
    )

    key = ("macd_30s", "paper:macd_30s", "UGRO")
    assert key in service._armed_hard_stops

    await service._handle_stream_message(
        {
            "data": QuoteTickEvent(
                source_service="market-data",
                payload=QuoteTickPayload(
                    symbol="UGRO",
                    bid_price=Decimal("3.95"),
                    ask_price=Decimal("3.97"),
                ),
            ).model_dump_json()
        }
    )

    await service._handle_stream_message(
        {
            "data": TradeTickEvent(
                source_service="market-data",
                payload=TradeTickPayload(
                    symbol="UGRO",
                    price=Decimal("3.93"),
                    size=100,
                ),
            ).model_dump_json()
        }
    )

    assert [request.intent_type for request in adapter.submit_requests] == ["open", "close"]
    close_request = adapter.submit_requests[-1]
    assert close_request.reason == "HARD_STOP"
    assert close_request.metadata["stop_trigger_source"] == "last"
    assert close_request.metadata["stop_trigger_price"] == "3.93"
    assert close_request.metadata["price_source"] == "last"
    assert service._armed_hard_stops[key].close_in_flight is True


@pytest.mark.asyncio
async def test_oms_service_arms_native_stop_guard_in_regular_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.oms.service.utcnow",
        lambda: datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
    )
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeNativeStopGuardBrokerAdapter(open_fill_price=Decimal("4.00"))
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
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "reference_price": "4.00",
                    "stop_guard_enabled": "true",
                    "stop_loss_pct": "1.5",
                    "stop_guard_quote_max_age_ms": "2000",
                    "stop_guard_initial_panic_buffer_pct": "0.5",
                },
            ),
        )
    )

    assert [request.intent_type for request in adapter.submit_requests] == ["open", "close"]
    native_stop_request = adapter.submit_requests[-1]
    assert native_stop_request.reason == service.NATIVE_STOP_GUARD_REASON
    assert native_stop_request.metadata["native_stop_guard"] == "true"
    assert native_stop_request.metadata["order_type"] == "STOP"
    assert native_stop_request.metadata["stop_price"] == "3.94"

    await service._handle_stream_message(
        {
            "data": QuoteTickEvent(
                source_service="market-data",
                payload=QuoteTickPayload(
                    symbol="UGRO",
                    bid_price=Decimal("3.93"),
                    ask_price=Decimal("3.95"),
                ),
            ).model_dump_json()
        }
    )

    assert [request.intent_type for request in adapter.submit_requests] == ["open", "close"]


@pytest.mark.asyncio
async def test_oms_service_cancels_and_rearms_native_stop_guard_around_regular_hours_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.oms.service.utcnow",
        lambda: datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
    )
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeNativeStopGuardBrokerAdapter(open_fill_price=Decimal("4.00"))
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
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "reference_price": "4.00",
                    "stop_guard_enabled": "true",
                    "stop_loss_pct": "1.5",
                    "stop_guard_quote_max_age_ms": "2000",
                    "stop_guard_initial_panic_buffer_pct": "0.5",
                },
            ),
        )
    )

    sell_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("4"),
                intent_type="scale",
                reason="SCALE_1",
                metadata={"reference_price": "4.20"},
            ),
        )
    )

    request_flow = [(request.intent_type, request.reason) for request in adapter.submit_requests]
    assert request_flow == [
        ("open", "ENTRY_P1_MACD_CROSS"),
        ("close", service.NATIVE_STOP_GUARD_REASON),
        ("cancel", "NATIVE_STOP_GUARD_CANCEL"),
        ("scale", "SCALE_1"),
        ("close", service.NATIVE_STOP_GUARD_REASON),
    ]
    rearmed_stop_request = adapter.submit_requests[-1]
    assert rearmed_stop_request.metadata["native_stop_guard"] == "true"
    assert rearmed_stop_request.quantity == Decimal("6")
    assert any(event.payload.reason == "NATIVE_STOP_GUARD_CANCEL" for event in sell_events)

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "UGRO"))
        assert account_position is not None
        assert account_position.quantity == Decimal("6")


@pytest.mark.asyncio
async def test_oms_service_refreshes_broker_positions_before_rejecting_exit() -> None:
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

    assert [event.payload.status for event in events] == ["accepted", "filled"]

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "UGRO"))
        assert account_position is not None
        assert account_position.quantity == Decimal("0")


@pytest.mark.asyncio
async def test_oms_service_still_rejects_exit_when_broker_refresh_confirms_no_position() -> None:
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

    service.broker_adapter.seed_account_positions("paper:macd_30s", {})  # type: ignore[attr-defined]

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
async def test_oms_service_submits_market_fallback_after_stop_rejection() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    broker_adapter = FakeStopRejectedFallbackBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=broker_adapter,
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
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    assert [event.payload.status for event in events] == ["rejected", "accepted", "filled"]
    assert events[1].payload.intent_type == "close"
    assert events[2].payload.reason == "STOP_REJECTED_FALLBACK"
    assert broker_adapter.submitted == [
        ("open", "buy", "UGRO", Decimal("10")),
        ("close", "sell", "UGRO", Decimal("10")),
    ]

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "UGRO"))
        assert account_position is not None
        assert account_position.quantity == Decimal("0")


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
