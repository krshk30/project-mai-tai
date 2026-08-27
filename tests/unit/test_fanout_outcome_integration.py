from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import Base, DashboardSnapshot
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.fanout_outcome_consumer import OUTCOME_SNAPSHOT_TYPE
from project_mai_tai.oms.store import OmsStore


IDENTITY = {
    "fanout_leg": "webull",
    "fanout_segment_id": "1787846400000",
    "fanout_slot": "reclaim",
    "fanout_slot_id": "f2722c29-8dbf-57e4-aa90-7f326b54e474",
    "fanout_attempt_id": "attempt-1",
}


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _event(*, account: str, metadata: dict[str, str]) -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=account,
            symbol="YYGH",
            side="buy",
            quantity=Decimal("1"),
            intent_type="open",
            reason="test",
            metadata=metadata,
        ),
    )


def _cancel_event(*, account: str, metadata: dict[str, str]) -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=account,
            symbol="YYGH",
            side="buy",
            quantity=Decimal("1"),
            intent_type="cancel",
            reason="test cancel",
            metadata=metadata,
        ),
    )


def _outcomes(session) -> list[str]:  # type: ignore[no-untyped-def]
    rows = session.scalars(
        select(DashboardSnapshot)
        .where(DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE)
        .order_by(DashboardSnapshot.created_at, DashboardSnapshot.id)
    ).all()
    return [str((row.payload or {}).get("outcome", "")) for row in rows]


def test_oms_transaction_records_queue_and_explicit_pre_submit_terminal() -> None:
    factory = _factory()
    store = OmsStore()
    with factory() as session:
        strategy = store.ensure_strategy(session, "schwab_1m_v2")
        account = store.ensure_broker_account(
            session,
            "live:orb",
            provider="webull",
            environment="live",
        )
        intent = store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=account,
            event=_event(account=account.name, metadata=IDENTITY),
        )
        assert store.record_fanout_pre_submit_outcome(
            session,
            intent=intent,
            outcome="dropped_collision",
            reason="fanout_webull_collision_managed",
            broker_account_name=account.name,
        )
        session.commit()

        assert _outcomes(session) == ["queued", "dropped_collision"]


def test_broker_event_uses_account_provider_and_preserves_reading_a_scope() -> None:
    factory = _factory()
    store = OmsStore()
    with factory() as session:
        strategy = store.ensure_strategy(session, "schwab_1m_v2")
        webull = store.ensure_broker_account(
            session,
            "live:orb",
            provider="webull",
            environment="live",
        )
        event = _event(account=webull.name, metadata=IDENTITY)
        intent = store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=webull,
            event=event,
        )
        order = store.get_or_create_order(
            session,
            intent=intent,
            strategy_id=strategy.id,
            broker_account_id=webull.id,
            client_order_id="attempt-1",
            symbol="YYGH",
            side="buy",
            quantity=Decimal("1"),
            metadata=IDENTITY,
        )
        report = ExecutionReport(
            event_type="filled",
            client_order_id="attempt-1",
            symbol="YYGH",
            filled_quantity=Decimal("1"),
            fill_price=Decimal("1.25"),
            origin="broker",
        )
        # Simulate an SDK report that echoes only the shared identity, not fanout_leg. The provider
        # is the venue boundary; a missing optional label must not make the Webull fill invisible.
        store.append_order_event(
            session,
            order=order,
            report=report,
            payload={
                "metadata": {
                    key: value for key, value in IDENTITY.items() if key != "fanout_leg"
                }
            },
        )
        session.commit()
        assert _outcomes(session) == ["queued", "filled"]

        schwab = store.ensure_broker_account(
            session,
            "live:schwab_1m_v2",
            provider="schwab",
            environment="live",
        )
        primary_metadata = {key: value for key, value in IDENTITY.items() if key != "fanout_leg"}
        primary_event = _event(account=schwab.name, metadata=primary_metadata)
        primary_intent = store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=schwab,
            event=primary_event,
        )
        primary_order = store.get_or_create_order(
            session,
            intent=primary_intent,
            strategy_id=strategy.id,
            broker_account_id=schwab.id,
            client_order_id="primary-1",
            symbol="YYGH",
            side="buy",
            quantity=Decimal("1"),
            metadata=primary_metadata,
        )
        store.append_order_event(
            session,
            order=primary_order,
            report=ExecutionReport(
                event_type="filled",
                client_order_id="primary-1",
                symbol="YYGH",
                filled_quantity=Decimal("1"),
                fill_price=Decimal("1.24"),
                origin="broker",
            ),
            payload={"metadata": primary_metadata},
        )
        session.commit()

        # One intended Schwab fill plus one Webull fill is the Reading-A 2x, never a duplicate.
        assert _outcomes(session) == ["queued", "filled"]


def test_cancel_intent_is_not_misclassified_as_a_new_or_dropped_buy_attempt() -> None:
    factory = _factory()
    store = OmsStore()
    with factory() as session:
        strategy = store.ensure_strategy(session, "schwab_1m_v2")
        webull = store.ensure_broker_account(
            session,
            "live:orb",
            provider="webull",
            environment="live",
        )
        intent = store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=webull,
            event=_cancel_event(account=webull.name, metadata=IDENTITY),
        )
        assert not store.record_fanout_pre_submit_outcome(
            session,
            intent=intent,
            outcome="dropped_risk",
            reason="forced_cancel_risk_reject",
            broker_account_name=webull.name,
        )
        session.commit()

        assert _outcomes(session) == []
