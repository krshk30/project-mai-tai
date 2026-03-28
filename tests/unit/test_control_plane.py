from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    Fill,
    Strategy,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    SnapshotBatchEvent,
    SnapshotBatchPayload,
)
from project_mai_tai.services.control_plane import build_app
from project_mai_tai.settings import Settings


class FakeRedis:
    def __init__(self, streams: dict[str, list[tuple[str, dict[str, str]]]]) -> None:
        self.streams = streams

    async def xrevrange(self, stream: str, count: int | None = None, **kwargs):
        del kwargs
        entries = self.streams.get(stream, [])
        if count is not None:
            entries = entries[:count]
        return entries

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


def seed_database(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        strategy = Strategy(code="macd_30s", name="MACD 30S", execution_mode="paper", metadata_json={})
        account = BrokerAccount(name="paper:macd_30s", provider="alpaca", environment="development")
        session.add_all([strategy, account])
        session.flush()

        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="UGRO",
            side="buy",
            intent_type="open",
            quantity=Decimal("10"),
            reason="ENTRY_P1_MACD_CROSS",
            status="filled",
            payload={},
        )
        session.add(intent)
        session.flush()

        order = BrokerOrder(
            intent_id=intent.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="macd_30s-UGRO-open-abc123",
            broker_order_id="sim-order-abc123",
            symbol="UGRO",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("10"),
            status="filled",
            payload={},
            submitted_at=datetime.now(UTC),
        )
        session.add(order)
        session.flush()

        fill = Fill(
            order_id=order.id,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            broker_fill_id="sim-order-abc123-fill-1",
            symbol="UGRO",
            side="buy",
            quantity=Decimal("10"),
            price=Decimal("2.55"),
            filled_at=datetime.now(UTC),
            payload={},
        )
        virtual_position = VirtualPosition(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="UGRO",
            quantity=Decimal("10"),
            average_price=Decimal("2.55"),
            realized_pnl=Decimal("0"),
            opened_at=datetime.now(UTC),
        )
        account_position = AccountPosition(
            broker_account_id=account.id,
            symbol="UGRO",
            quantity=Decimal("10"),
            average_price=Decimal("2.55"),
            market_value=Decimal("25.5"),
            source_updated_at=datetime.now(UTC),
        )
        session.add_all([fill, virtual_position, account_position])
        session.commit()


def make_streams(prefix: str) -> dict[str, list[tuple[str, dict[str, str]]]]:
    heartbeat = HeartbeatEvent(
        source_service="strategy-engine",
        payload=HeartbeatPayload(
            service_name="strategy-engine",
            instance_name="test-instance",
            status="healthy",
            details={"watchlist_size": "1"},
        ),
    )
    snapshot = SnapshotBatchEvent(
        source_service="market-data-gateway",
        payload=SnapshotBatchPayload(snapshots=[], reference_data=[]),
    )
    subscription = MarketDataSubscriptionEvent(
        source_service="strategy-engine",
        payload=MarketDataSubscriptionPayload(
            consumer_name="strategy-engine",
            mode="replace",
            symbols=["UGRO"],
        ),
    )
    return {
        f"{prefix}:heartbeats": [("1-0", {"data": heartbeat.model_dump_json()})],
        f"{prefix}:snapshot-batches": [("1-0", {"data": snapshot.model_dump_json()})],
        f"{prefix}:market-data-subscriptions": [("1-0", {"data": subscription.model_dump_json()})],
    }


def test_control_plane_overview_and_dashboard_render() -> None:
    settings = Settings(redis_stream_prefix="test")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(settings=settings, session_factory=session_factory, redis_client=redis)

    with TestClient(app) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        body = overview.json()
        assert body["counts"]["open_virtual_positions"] == 1
        assert body["virtual_positions"][0]["symbol"] == "UGRO"
        assert body["services"][0]["service_name"] == "strategy-engine"
        assert body["market_data"]["active_subscription_symbols"] == 1

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Project Mai Tai Operator View" in dashboard.text
        assert "UGRO" in dashboard.text
        assert "Virtual Positions" in dashboard.text
