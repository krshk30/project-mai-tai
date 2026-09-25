"""WETO 2026-09-24: a resolved Webull stop must not vanish from the ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import (
    Base,
    BrokerAccount,
    BrokerOrder,
    Fill,
    OmsManagedPosition,
    Strategy,
    SystemIncident,
    TradeIntent,
)
from project_mai_tai.oms.service import _EXIT_FETCH_FAILED, OmsRiskService
from project_mai_tai.settings import Settings


ACCOUNT = "live:orb"
SYMBOL = "WETO"
ENTRY_COID = "schwab_1m_v2-WETO-open-0ddb71bbd36b"
PROTECT_BASE = "schwab_1m_v2-WETO-protect-c00b7c927bb4"
ENTRY_ORDER_ID = "0FG4L6J7C27B3P8JSP9JRGGTQ8"


class _Redis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"


def _service_with_weto():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=_Redis(),
        session_factory=sessions,
    )
    with sessions() as session:
        strategy = Strategy(code="schwab_1m_v2", name="V2", execution_mode="live")
        account = BrokerAccount(name=ACCOUNT, provider="webull", environment="live")
        session.add_all([strategy, account])
        session.flush()
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol=SYMBOL,
            side="buy",
            intent_type="open",
            quantity=Decimal("1"),
            reason="ATR Flip",
            status="filled",
            payload={},
        )
        session.add(intent)
        session.flush()
        session.add(
            BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=ENTRY_COID,
                broker_order_id=ENTRY_ORDER_ID,
                symbol=SYMBOL,
                side="buy",
                order_type="market",
                time_in_force="day",
                quantity=Decimal("1"),
                status="filled",
                payload={
                    "fanout_leg": "webull",
                    "native_oco_bracket": "false",
                    "webull_protect_base_client_order_id": PROTECT_BASE,
                },
            )
        )
        row = OmsManagedPosition(
            strategy_code="schwab_1m_v2",
            broker_account_name=ACCOUNT,
            symbol=SYMBOL,
            entry_price=Decimal("2.1298"),
            original_quantity=1,
            current_quantity=1,
            entry_path="ATR Flip",
            entry_time=datetime(2026, 9, 24, 13, 36, 50, tzinfo=UTC),
            status="open",
            config_name="make_v2_variant",
        )
        session.add(row)
        session.commit()
        row_id = str(row.id)
    return service, sessions, row_id


@pytest.mark.asyncio
async def test_weto_unanswered_child_fetch_never_closes_row_and_pages_once() -> None:
    service, sessions, row_id = _service_with_weto()

    async def failed_fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    service._fetch_oco_exit_detail = failed_fetch
    for _ in range(service._MAX_EXIT_FETCH_DEFERRALS + 2):
        assert await service._close_resolved_oco_managed_row(
            ACCOUNT, SYMBOL, expected_row_id=row_id
        ) is False
    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        incidents = session.scalars(select(SystemIncident)).all()
    assert row is not None and row.status == "open" and row.current_quantity == 1
    assert len(incidents) == 1
    assert incidents[0].payload["symbol"] == SYMBOL
    assert incidents[0].payload["managed_row_id"] == row_id


@pytest.mark.asyncio
async def test_weto_child_sell_fill_is_durable_before_managed_row_closes() -> None:
    service, sessions, row_id = _service_with_weto()
    child_id = "WETO-STOP-CHILD-CONTROL"
    detail = {
        "symbol": SYMBOL,
        "quantity": Decimal("1"),
        "price": Decimal("1.95"),
        "filled_at": datetime(2026, 9, 24, 13, 43, 44, tzinfo=UTC),
        "broker_order_id": child_id,
    }

    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, detail=detail, expected_row_id=row_id
    ) is True
    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        fills = session.scalars(select(Fill).where(Fill.symbol == SYMBOL, Fill.side == "sell")).all()
        incidents = session.scalars(select(SystemIncident)).all()
    assert row is not None and row.status == "closed" and row.current_quantity == 0
    assert len(fills) == 1
    assert fills[0].broker_fill_id == f"{child_id}:1"
    assert fills[0].price == Decimal("1.95")
    assert incidents == []


@pytest.mark.asyncio
async def test_missing_weto_child_order_id_cannot_close_or_create_fill() -> None:
    service, sessions, row_id = _service_with_weto()
    detail = {
        "symbol": SYMBOL,
        "quantity": Decimal("1"),
        "price": Decimal("1.95"),
        "filled_at": datetime(2026, 9, 24, 13, 43, 44, tzinfo=UTC),
    }

    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, detail=detail, expected_row_id=row_id
    ) is False
    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        fills = session.scalars(select(Fill).where(Fill.symbol == SYMBOL, Fill.side == "sell")).all()
        incidents = session.scalars(select(SystemIncident)).all()
    assert row is not None and row.status == "open"
    assert fills == []
    assert len(incidents) == 1


@pytest.mark.asyncio
async def test_weto_resolved_answer_without_durable_fill_keeps_row_open() -> None:
    service, sessions, row_id = _service_with_weto()
    service._persist_oco_exit_fill = lambda *_args, **_kwargs: False

    assert await service._close_resolved_oco_managed_row(
        ACCOUNT,
        SYMBOL,
        expected_row_id=row_id,
        detail={
            "symbol": SYMBOL,
            "quantity": Decimal("1"),
            "price": Decimal("1.95"),
            "filled_at": datetime(2026, 9, 24, 13, 43, 44, tzinfo=UTC),
            "broker_order_id": "WETO-STOP-CHILD-CONTROL",
        },
    ) is False
    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        fills = session.scalars(select(Fill).where(Fill.symbol == SYMBOL, Fill.side == "sell")).all()
        incidents = session.scalars(select(SystemIncident)).all()
    assert row is not None and row.status == "open" and row.current_quantity == 1
    assert fills == []
    assert len(incidents) == 1
    assert incidents[0].payload["reason"] == "child_fill_not_durable"


@pytest.mark.asyncio
async def test_weto_child_fill_closes_prior_unrecorded_incident() -> None:
    service, sessions, row_id = _service_with_weto()

    async def failed_fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    service._fetch_oco_exit_detail = failed_fetch
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, expected_row_id=row_id
    ) is False
    with sessions() as session:
        before = session.scalars(select(SystemIncident)).all()
    assert len(before) == 1 and before[0].status == "open"

    assert await service._close_resolved_oco_managed_row(
        ACCOUNT,
        SYMBOL,
        expected_row_id=row_id,
        detail={
            "symbol": SYMBOL,
            "quantity": Decimal("1"),
            "price": Decimal("1.95"),
            "filled_at": datetime(2026, 9, 24, 13, 43, 44, tzinfo=UTC),
            "broker_order_id": "WETO-STOP-CHILD-CONTROL",
        },
    ) is True
    with sessions() as session:
        after = session.scalars(select(SystemIncident)).all()
    assert len(after) == 1 and after[0].status == "closed"


@pytest.mark.asyncio
async def test_weto_answer_without_entry_order_keeps_row_open_and_pages() -> None:
    service, sessions, row_id = _service_with_weto()
    with sessions() as session:
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == ENTRY_COID))
        assert entry is not None
        entry.status = "cancelled"
        session.commit()

    assert await service._close_resolved_oco_managed_row(
        ACCOUNT,
        SYMBOL,
        expected_row_id=row_id,
        detail={
            "symbol": SYMBOL,
            "quantity": Decimal("1"),
            "price": Decimal("1.95"),
            "filled_at": datetime(2026, 9, 24, 13, 43, 44, tzinfo=UTC),
            "broker_order_id": "WETO-STOP-CHILD-CONTROL",
        },
    ) is False
    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        fills = session.scalars(select(Fill).where(Fill.symbol == SYMBOL, Fill.side == "sell")).all()
        incidents = session.scalars(select(SystemIncident)).all()
    assert row is not None and row.status == "open"
    assert fills == []
    assert len(incidents) == 1


@pytest.mark.asyncio
async def test_weto_already_recorded_child_accepts_equivalent_decimal_scale() -> None:
    service, sessions, row_id = _service_with_weto()
    child_id = "WETO-STOP-CHILD-CONTROL"
    base_detail = {
        "symbol": SYMBOL,
        "quantity": Decimal("1"),
        "price": Decimal("1.95"),
        "filled_at": datetime(2026, 9, 24, 13, 43, 44, tzinfo=UTC),
        "broker_order_id": child_id,
    }
    with sessions() as session:
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == ENTRY_COID))
        assert entry is not None
        assert service._persist_oco_exit_fill(session, ACCOUNT, SYMBOL, entry, base_detail)
        session.commit()

    assert await service._close_resolved_oco_managed_row(
        ACCOUNT,
        SYMBOL,
        detail={**base_detail, "quantity": Decimal("1.00000000")},
        expected_row_id=row_id,
    ) is True
    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        fills = session.scalars(select(Fill).where(Fill.symbol == SYMBOL, Fill.side == "sell")).all()
    assert row is not None and row.status == "closed"
    assert len(fills) == 1


@pytest.mark.asyncio
async def test_weto_existing_fill_with_wrong_broker_order_id_keeps_row_open() -> None:
    service, sessions, row_id = _service_with_weto()
    child_id = "WETO-STOP-CHILD-CONTROL"
    detail = {
        "symbol": SYMBOL,
        "quantity": Decimal("1"),
        "price": Decimal("1.95"),
        "filled_at": datetime(2026, 9, 24, 13, 43, 44, tzinfo=UTC),
        "broker_order_id": child_id,
    }
    with sessions() as session:
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == ENTRY_COID))
        assert entry is not None
        assert service._persist_oco_exit_fill(session, ACCOUNT, SYMBOL, entry, detail)
        child_order = session.scalar(
            select(BrokerOrder).where(BrokerOrder.client_order_id.contains("-ocoexit-"))
        )
        assert child_order is not None
        child_order.broker_order_id = "OTHER-BROKER-ORDER"
        session.commit()

    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, detail=detail, expected_row_id=row_id
    ) is False
    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        incidents = session.scalars(select(SystemIncident)).all()
    assert row is not None and row.status == "open"
    assert len(incidents) == 1
    assert incidents[0].payload["reason"] == "child_fill_not_durable"
