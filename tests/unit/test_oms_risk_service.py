from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AccountPosition, BrokerOrder, Fill, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def aclose(self) -> None:
        return None


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
