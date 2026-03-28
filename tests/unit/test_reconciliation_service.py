from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    ReconciliationFinding,
    ReconciliationRun,
    Strategy,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.settings import Settings


class FakeRedis:
    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        del stream, fields
        return "1-0"

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


def seed_reconciliation_state(session_factory: sessionmaker[Session]) -> None:
    stale_time = datetime.now(UTC) - timedelta(minutes=10)

    with session_factory() as session:
        strategy_one = Strategy(code="macd_30s", name="MACD 30S", execution_mode="paper", metadata_json={})
        strategy_two = Strategy(code="runner", name="Runner", execution_mode="paper", metadata_json={})
        account = BrokerAccount(name="paper:shared", provider="alpaca", environment="development")
        session.add_all([strategy_one, strategy_two, account])
        session.flush()

        session.add_all(
            [
                VirtualPosition(
                    strategy_id=strategy_one.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("10"),
                    average_price=Decimal("2.50"),
                    realized_pnl=Decimal("0"),
                    opened_at=stale_time,
                ),
                VirtualPosition(
                    strategy_id=strategy_two.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("5"),
                    average_price=Decimal("2.70"),
                    realized_pnl=Decimal("0"),
                    opened_at=stale_time,
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("12"),
                    average_price=Decimal("2.55"),
                    market_value=Decimal("30.60"),
                    source_updated_at=stale_time,
                ),
            ]
        )

        intent = TradeIntent(
            strategy_id=strategy_one.id,
            broker_account_id=account.id,
            symbol="UGRO",
            side="buy",
            intent_type="open",
            quantity=Decimal("10"),
            reason="ENTRY_P1_MACD_CROSS",
            status="pending",
            payload={},
            updated_at=stale_time,
        )
        session.add(intent)
        session.flush()

        session.add(
            BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy_one.id,
                broker_account_id=account.id,
                client_order_id="macd_30s-UGRO-open-stale",
                broker_order_id="broker-order-stale",
                symbol="UGRO",
                side="buy",
                order_type="market",
                time_in_force="day",
                quantity=Decimal("10"),
                status="accepted",
                payload={},
                submitted_at=stale_time,
                updated_at=stale_time,
            )
        )
        session.commit()


def test_reconciler_creates_run_findings_and_incidents() -> None:
    session_factory = build_test_session_factory()
    seed_reconciliation_state(session_factory)
    service = ReconciliationService(
        settings=Settings(
            redis_stream_prefix="test",
            reconciliation_stuck_order_seconds=60,
            reconciliation_stuck_intent_seconds=60,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    result = service.run_reconciliation_cycle()

    assert result["status"] == "completed"
    assert result["summary"]["total_findings"] == 3
    assert result["summary"]["critical_findings"] == 0
    assert result["summary"]["warning_findings"] == 3
    assert result["summary"]["cutover_confidence"] == 70

    with session_factory() as session:
        run = session.scalar(select(ReconciliationRun))
        findings = session.scalars(select(ReconciliationFinding)).all()
        incidents = session.scalars(select(SystemIncident)).all()

        assert run is not None
        assert run.summary["total_findings"] == 3
        assert {finding.finding_type for finding in findings} == {
            "position_quantity_mismatch",
            "stuck_order",
            "stuck_intent",
        }
        assert len(incidents) == 3
        assert all(incident.status == "open" for incident in incidents)


def test_reconciler_closes_incidents_when_findings_resolve() -> None:
    session_factory = build_test_session_factory()
    seed_reconciliation_state(session_factory)
    service = ReconciliationService(
        settings=Settings(
            redis_stream_prefix="test",
            reconciliation_stuck_order_seconds=60,
            reconciliation_stuck_intent_seconds=60,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    first = service.run_reconciliation_cycle()
    assert first["summary"]["total_findings"] == 3

    with session_factory() as session:
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == "paper:shared"))
        assert account is not None
        account_position = session.scalar(select(AccountPosition).where(AccountPosition.broker_account_id == account.id))
        intent = session.scalar(select(TradeIntent).where(TradeIntent.broker_account_id == account.id))
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.broker_account_id == account.id))
        assert account_position is not None
        assert intent is not None
        assert order is not None

        account_position.quantity = Decimal("15")
        account_position.average_price = Decimal("2.56666667")
        intent.status = "filled"
        order.status = "filled"
        session.commit()

    second = service.run_reconciliation_cycle()
    assert second["summary"]["total_findings"] == 0
    assert second["summary"]["cutover_confidence"] == 100

    with session_factory() as session:
        incidents = session.scalars(select(SystemIncident).order_by(SystemIncident.opened_at)).all()
        assert incidents
        assert all(incident.status == "closed" for incident in incidents)
