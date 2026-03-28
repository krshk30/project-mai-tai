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
    ReconciliationFinding,
    ReconciliationRun,
    Strategy,
    SystemIncident,
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
    StrategyBotStatePayload,
    StrategyStateSnapshotEvent,
    StrategyStateSnapshotPayload,
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


class FakeLegacyClient:
    async def fetch_snapshot(self) -> dict[str, object]:
        return {
            "enabled": True,
            "connected": True,
            "fetched_at": "2026-03-28T14:00:00+00:00",
            "scanner": {
                "confirmed_symbols": ["UGRO", "SBET"],
                "count": 2,
            },
            "bots": {
                "macd_30s": {
                    "status": "running (dry run)",
                    "watched_tickers": ["UGRO", "SBET"],
                    "positions": [{"symbol": "UGRO", "quantity": 10.0}],
                    "recent_actions": [{"symbol": "UGRO", "action": "BUY"}],
                    "daily_pnl": 0,
                },
                "macd_1m": {
                    "status": "running (dry run)",
                    "watched_tickers": [],
                    "positions": [],
                    "recent_actions": [],
                    "daily_pnl": 0,
                },
                "tos": {
                    "status": "running (dry run)",
                    "watched_tickers": ["UGRO"],
                    "positions": [],
                    "recent_actions": [],
                    "daily_pnl": 0,
                },
                "runner": {
                    "status": "running (NO ALPACA)",
                    "watched_tickers": ["SBET"],
                    "positions": [],
                    "recent_actions": [],
                    "daily_pnl": 0,
                },
            },
            "errors": [],
        }


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
        reconciliation_run = ReconciliationRun(
            broker_account_id=account.id,
            status="completed",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            summary={
                "cutover_confidence": 90,
                "total_findings": 1,
                "critical_findings": 0,
                "warning_findings": 1,
            },
        )
        session.add(reconciliation_run)
        session.flush()

        reconciliation_finding = ReconciliationFinding(
            reconciliation_run_id=reconciliation_run.id,
            order_id=order.id,
            severity="warning",
            finding_type="stuck_order",
            symbol="UGRO",
            payload={
                "title": "Order stuck in accepted for UGRO",
                "fingerprint": "stuck-order:test",
            },
        )
        incident = SystemIncident(
            service_name="reconciler",
            severity="warning",
            title="Order stuck in accepted for UGRO",
            status="open",
            payload={"fingerprint": "stuck-order:test"},
            opened_at=datetime.now(UTC),
        )
        session.add_all(
            [
                fill,
                virtual_position,
                account_position,
                reconciliation_finding,
                incident,
            ]
        )
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
    strategy_state = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(
            watchlist=["UGRO"],
            top_confirmed=[{"ticker": "UGRO", "rank_score": 72}],
            bots=[
                StrategyBotStatePayload(
                    strategy_code="macd_30s",
                    account_name="paper:macd_30s",
                    watchlist=["UGRO"],
                    positions=[{"ticker": "UGRO", "quantity": 10}],
                    pending_open_symbols=[],
                    pending_close_symbols=[],
                    pending_scale_levels=[],
                ),
                StrategyBotStatePayload(
                    strategy_code="macd_1m",
                    account_name="paper:macd_1m",
                    watchlist=["UGRO"],
                    positions=[],
                    pending_open_symbols=[],
                    pending_close_symbols=[],
                    pending_scale_levels=[],
                ),
                StrategyBotStatePayload(
                    strategy_code="tos",
                    account_name="paper:tos_runner_shared",
                    watchlist=["UGRO"],
                    positions=[],
                    pending_open_symbols=[],
                    pending_close_symbols=[],
                    pending_scale_levels=[],
                ),
            ],
        ),
    )
    return {
        f"{prefix}:heartbeats": [("1-0", {"data": heartbeat.model_dump_json()})],
        f"{prefix}:snapshot-batches": [("1-0", {"data": snapshot.model_dump_json()})],
        f"{prefix}:market-data-subscriptions": [("1-0", {"data": subscription.model_dump_json()})],
        f"{prefix}:strategy-state": [("1-0", {"data": strategy_state.model_dump_json()})],
    }


def test_control_plane_overview_and_dashboard_render() -> None:
    settings = Settings(redis_stream_prefix="test")
    session_factory = build_test_session_factory()
    seed_database(session_factory)
    redis = FakeRedis(make_streams(settings.redis_stream_prefix))

    app = build_app(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis,
        legacy_client=FakeLegacyClient(),
    )

    with TestClient(app) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        body = overview.json()
        assert body["counts"]["open_virtual_positions"] == 1
        assert body["virtual_positions"][0]["symbol"] == "UGRO"
        assert body["services"][0]["service_name"] == "strategy-engine"
        assert body["market_data"]["active_subscription_symbols"] == 1
        assert body["scanner"]["top_confirmed"][0]["ticker"] == "UGRO"
        assert body["scanner"]["legacy_confirmed_symbols"] == ["UGRO", "SBET"]
        assert body["bots"][0]["strategy_code"] == "macd_30s"
        assert body["bots"][0]["watchlist"] == ["UGRO"]
        assert body["reconciliation"]["latest_run"]["summary"]["cutover_confidence"] == 90
        assert body["reconciliation"]["findings"][0]["finding_type"] == "stuck_order"
        assert body["legacy_shadow"]["divergence"]["status"] == "drifted"
        assert body["legacy_shadow"]["divergence"]["confirmed_only_in_legacy"] == ["SBET"]
        assert body["strategy_runtime"]["watchlist"] == ["UGRO"]

        scanner = client.get("/api/scanner")
        assert scanner.status_code == 200
        scanner_body = scanner.json()
        assert scanner_body["scanner"]["watchlist"] == ["UGRO"]
        assert scanner_body["scanner"]["top_confirmed"][0]["rank_score"] == 72.0

        bots = client.get("/api/bots")
        assert bots.status_code == 200
        bots_body = bots.json()
        assert bots_body["bots"][0]["display_name"] == "MACD Bot"
        assert bots_body["bots"][0]["recent_intents"][0]["symbol"] == "UGRO"

        reconciliation = client.get("/api/reconciliation")
        assert reconciliation.status_code == 200
        reconciliation_body = reconciliation.json()
        assert reconciliation_body["reconciliation"]["findings"][0]["title"] == "Order stuck in accepted for UGRO"

        shadow = client.get("/api/shadow")
        assert shadow.status_code == 200
        shadow_body = shadow.json()
        assert shadow_body["legacy_shadow"]["divergence"]["strategies"]["runner"]["new_present"] is False

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Project Mai Tai Operator View" in dashboard.text
        assert "Scanner Pipeline" in dashboard.text
        assert "Confirmed Candidates" in dashboard.text
        assert "Bot Deck" in dashboard.text
        assert "Legacy-style bot visibility for 30s, 1m, TOS, and Runner." in dashboard.text
        assert "UGRO" in dashboard.text
        assert "MACD Bot" in dashboard.text
        assert "Virtual Positions" in dashboard.text
        assert "Cutover Confidence" in dashboard.text
        assert "Order stuck in accepted for UGRO" in dashboard.text
        assert "Legacy Shadow" in dashboard.text
        assert "SBET" in dashboard.text
