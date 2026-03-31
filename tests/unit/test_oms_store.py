from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.store import OmsStore


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_record_fill_if_needed_uses_incremental_quantity_for_cumulative_reports() -> None:
    session_factory = build_test_session_factory()
    store = OmsStore()
    with session_factory() as session:
        strategy = store.ensure_strategy(session, "macd_30s", name="30s")
        account = store.ensure_broker_account(session, "paper:test", provider="alpaca", environment="test")
        intent = store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=account,
            event=TradeIntentEvent(
                source_service="test",
                payload=TradeIntentPayload(
                    strategy_code="macd_30s",
                    broker_account_name="paper:test",
                    symbol="ELAB",
                    side="buy",
                    quantity=Decimal("100"),
                    intent_type="open",
                    reason="ENTRY",
                    metadata={},
                ),
            ),
        )
        order = store.get_or_create_order(
            session,
            intent=intent,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="order-1",
            symbol="ELAB",
            side="buy",
            quantity=Decimal("100"),
            metadata={},
            broker_order_id="broker-1",
            status="accepted",
        )

        partial_report = ExecutionReport(
            event_type="partially_filled",
            client_order_id="order-1",
            broker_order_id="broker-1",
            broker_fill_id="fill-partial",
            symbol="ELAB",
            side="buy",
            intent_type="open",
            quantity=Decimal("100"),
            filled_quantity=Decimal("19"),
            fill_price=Decimal("3.95"),
            reason="",
            metadata={},
            reported_at=datetime(2026, 3, 31, 12, 46, 46, tzinfo=UTC),
        )
        final_report = ExecutionReport(
            event_type="filled",
            client_order_id="order-1",
            broker_order_id="broker-1",
            broker_fill_id="fill-final",
            symbol="ELAB",
            side="buy",
            intent_type="open",
            quantity=Decimal("100"),
            filled_quantity=Decimal("100"),
            fill_price=Decimal("3.95"),
            reason="",
            metadata={},
            reported_at=datetime(2026, 3, 31, 12, 46, 49, tzinfo=UTC),
        )

        fill_one = store.record_fill_if_needed(
            session,
            order=order,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            report=partial_report,
            payload={},
        )
        fill_two = store.record_fill_if_needed(
            session,
            order=order,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            report=final_report,
            payload={},
        )

        assert fill_one is not None
        assert fill_two is not None
        assert fill_one.quantity == Decimal("19")
        assert fill_two.quantity == Decimal("81")
