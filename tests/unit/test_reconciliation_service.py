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
    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del stream, fields, kwargs
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


def test_reconciler_can_ignore_position_mismatch_for_specific_account_symbols() -> None:
    session_factory = build_test_session_factory()
    seed_reconciliation_state(session_factory)
    service = ReconciliationService(
        settings=Settings(
            redis_stream_prefix="test",
            reconciliation_stuck_order_seconds=60,
            reconciliation_stuck_intent_seconds=60,
            reconciliation_ignored_position_mismatches="paper:shared:UGRO",
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    result = service.run_reconciliation_cycle()

    assert result["summary"]["total_findings"] == 2
    assert result["summary"]["critical_findings"] == 0
    assert result["summary"]["warning_findings"] == 2

    with session_factory() as session:
        findings = session.scalars(select(ReconciliationFinding)).all()
        incidents = session.scalars(select(SystemIncident)).all()

        assert {finding.finding_type for finding in findings} == {
            "stuck_order",
            "stuck_intent",
        }
        assert len(incidents) == 2


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ⛔⭐⭐ THE VIRTUAL-POSITION FALSE ZERO — reconciler must not page for a position the OMS tracks.
#
# Live instance 2026-08-14 (WETO, live:orb): the OMS filled 1 @ 8.005; `[VIRTUAL-CLEAR]` zeroed the
# virtual row 0.7s later, INSIDE the ~15s Webull settle window, and nothing restored it when the
# broker became visible. `oms_managed_positions` read status=open/current_quantity=1 the whole time.
# Comparing only against the virtual row produced a CRITICAL page for a correctly-tracked position.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _reconcile_position_findings(*, virtual_qty, managed_qty, broker_qty):
    """Seed one symbol on one account with the three quantities, return its position findings."""
    from project_mai_tai.db.models import OmsManagedPosition

    session_factory = build_test_session_factory()
    now = datetime.now(UTC)
    with session_factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live", metadata_json={})
        account = BrokerAccount(name="live:orb", provider="webull", environment="production")
        session.add_all([strategy, account])
        session.flush()
        if virtual_qty:
            session.add(VirtualPosition(
                strategy_id=strategy.id, broker_account_id=account.id, symbol="WETO",
                quantity=Decimal(str(virtual_qty)), average_price=Decimal("8.005"),
                realized_pnl=Decimal("0"), opened_at=now,
            ))
        if managed_qty:
            session.add(OmsManagedPosition(
                strategy_code="schwab_1m_v2", broker_account_name="live:orb", symbol="WETO",
                entry_price=Decimal("8.005"), original_quantity=managed_qty,
                current_quantity=managed_qty, entry_time=now,
                current_profit_pct=Decimal("0"), peak_profit_pct=Decimal("0"), status="open",
            ))
        if broker_qty:
            session.add(AccountPosition(
                broker_account_id=account.id, symbol="WETO", quantity=Decimal(str(broker_qty)),
                average_price=Decimal("8.005"), market_value=Decimal("8.005"), source_updated_at=now,
            ))
        session.commit()

    service = ReconciliationService(
        settings=Settings(), redis_client=FakeRedis(), session_factory=session_factory
    )
    with session_factory() as session:
        return [f for f in service._build_position_findings(session)
                if f.finding_type == "position_quantity_mismatch"]


def test_managed_position_covers_a_false_zero_virtual_row() -> None:
    """THE 08-14 WETO CASE: broker 1, virtual 0, managed 1 -> NOT a drift."""
    findings = _reconcile_position_findings(virtual_qty=0, managed_qty=1, broker_qty=1)
    assert findings == [], f"the OMS tracks this position; it must not page. got {findings}"


def test_unowned_broker_position_still_pages() -> None:
    """⭐ THE CONTROL. A position NOTHING of ours knows about must still be reported — otherwise the
    fix has simply blinded the reconciler, which is worse than the false page it removes."""
    findings = _reconcile_position_findings(virtual_qty=0, managed_qty=0, broker_qty=1)
    assert len(findings) == 1
    assert findings[0].severity == "critical"


def test_stale_open_managed_row_against_a_flat_broker_still_pages() -> None:
    """A managed row left open on a position the broker no longer shows is the PHANTOM-ROW defect;
    taking the max must not swallow it."""
    findings = _reconcile_position_findings(virtual_qty=0, managed_qty=1, broker_qty=0)
    assert len(findings) == 1
    assert findings[0].severity == "critical"


def test_real_quantity_disagreement_still_pages_and_reports_both_books() -> None:
    findings = _reconcile_position_findings(virtual_qty=1, managed_qty=1, broker_qty=5)
    assert len(findings) == 1
    payload = findings[0].payload
    assert Decimal(payload["virtual_quantity"]) == 1
    assert Decimal(payload["managed_quantity"]) == 1
    assert Decimal(payload["our_quantity"]) == 1
    assert Decimal(payload["account_quantity"]) == 5


def test_tracked_position_with_a_real_disagreement_is_WARNING_not_critical() -> None:
    """⛔⭐ SEVERITY MUST READ *OUR BOOKS*, NOT THE VIRTUAL ROW ALONE.

    broker 5 / virtual 0 / managed 2 is a genuine drift on a position we DO own. `critical` is
    reserved for "one side is zero" — i.e. nobody owns it, or we think we hold something the broker
    has never heard of. Judging that off the virtual row alone calls an owned, tracked position
    `critical` purely because the virtual row false-zeroed, which is the severity inversion that
    makes the loudest alarms the ones we understand least.
    """
    findings = _reconcile_position_findings(virtual_qty=0, managed_qty=2, broker_qty=5)
    assert len(findings) == 1
    assert findings[0].severity == "warning", (
        "an owned position whose quantities merely disagree is a warning; "
        f"got {findings[0].severity} — severity is reading the virtual row again"
    )
    assert Decimal(findings[0].payload["our_quantity"]) == 2
