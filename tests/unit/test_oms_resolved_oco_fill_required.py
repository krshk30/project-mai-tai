"""WETO 2026-09-24: a resolved Webull stop must not vanish from the ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select, text
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
from project_mai_tai.oms.service import _EXIT_FETCH_FAILED, OmsRiskService, utcnow
from project_mai_tai.settings import Settings


ACCOUNT = "live:orb"
SYMBOL = "WETO"
ENTRY_COID = "schwab_1m_v2-WETO-open-0ddb71bbd36b"
PROTECT_BASE = "schwab_1m_v2-WETO-protect-c00b7c927bb4"
ENTRY_ORDER_ID = "0FG4L6J7C27B3P8JSP9JRGGTQ8"
STOP_CHILD_ORDER_ID = "KHJ4FMKM17PS9GA1P43E0VQRS9"
STOP_FILLED_AT = datetime(2026, 9, 24, 13, 43, 43, 473000, tzinfo=UTC)


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
        settings=Settings(
            redis_stream_prefix="test", oms_adapter="simulated",
            oms_native_oco_stand_down_enabled=True,
            oms_native_oco_resolve_flat_reconcile_enabled=True,
            oms_record_native_oco_exit_fills_enabled=True,
        ),
        redis_client=_Redis(),
        session_factory=sessions,
    )
    async def no_child(*_args, **_kwargs):
        return None

    service.broker_adapter.fetch_oco_exit_fill = no_child
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
        session.flush()
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == ENTRY_COID))
        row = OmsManagedPosition(
            entry_order_id=entry.id,
            entry_client_order_id=entry.client_order_id,
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
    service._managed_v2_symbols.add((ACCOUNT, SYMBOL))
    service._native_oco_resolving[(ACCOUNT, SYMBOL)] = utcnow() - timedelta(seconds=120)
    fetches = 0

    async def failed_fetch(*_args, **_kwargs):
        nonlocal fetches
        fetches += 1
        return _EXIT_FETCH_FAILED

    service._fetch_oco_exit_detail = failed_fetch
    for attempt in range(5):
        assert await service._close_resolved_oco_managed_row(
            ACCOUNT, SYMBOL, expected_row_id=row_id
        ) is False
        assert service._native_oco_stand_down_active(ACCOUNT, SYMBOL) is True
        with sessions() as session:
            incidents = session.scalars(select(SystemIncident)).all()
        assert len(incidents) == (1 if attempt == 4 else 0)
    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        incidents = session.scalars(select(SystemIncident)).all()
    assert row is not None and row.status == "open" and row.current_quantity == 1
    assert len(incidents) == 1
    assert incidents[0].payload["symbol"] == SYMBOL
    assert incidents[0].payload["managed_row_id"] == row_id
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, expected_row_id=row_id
    ) is False
    assert fetches == 5
    with sessions() as session:
        assert len(session.scalars(select(SystemIncident)).all()) == 1


@pytest.mark.asyncio
async def test_unscoped_schwab_refresh_read_failure_holds_the_open_row() -> None:
    service, sessions, row_id = _service_with_weto()
    schwab_acct = "live:schwab_1m_v2"
    with sessions() as session:
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == ACCOUNT))
        row = session.get(OmsManagedPosition, UUID(row_id))
        assert account is not None and row is not None
        account.name = schwab_acct
        row.broker_account_name = schwab_acct
        session.commit()
    service._managed_v2_symbols.add((schwab_acct, SYMBOL))
    service._native_oco_resolving[(schwab_acct, SYMBOL)] = utcnow() - timedelta(seconds=120)

    async def failed_fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    async def armed(_acct, _symbols):
        return {"working": [], "filled": True, "unsafe": False}

    async def resolved(_acct, _symbols):
        return {SYMBOL}

    service._fetch_oco_exit_detail = failed_fetch
    service.settings.oms_native_oco_stand_down_enabled = True
    service.settings.oms_native_oco_resolve_flat_reconcile_enabled = True
    service.broker_adapter.fetch_exit_legs_for_entry = armed
    service.broker_adapter.fetch_oco_resolved_by_fill_symbols = resolved

    await service._refresh_native_oco_armed_state([schwab_acct])

    service.settings.oms_v2_exit_management_enabled = True
    service._latest_quotes_by_symbol[SYMBOL] = {
        "bid": 1.50, "ask": 1.51, "received_at": utcnow(),
    }
    await service._evaluate_v2_managed_exit(schwab_acct, SYMBOL)

    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        sells = session.scalars(select(TradeIntent).where(TradeIntent.side == "sell")).all()
    assert row is not None and row.status == "open"
    assert sells == []
    assert (schwab_acct, SYMBOL) in service._native_oco_resolving
    assert service._oco_exit_fill_pending[(schwab_acct, SYMBOL)].row_id == row_id
    assert service._native_oco_stand_down_active(schwab_acct, SYMBOL) is True


@pytest.mark.asyncio
async def test_pending_child_fill_hold_survives_eod_handover_until_attributed() -> None:
    service, _sessions, row_id = _service_with_weto()
    service._managed_v2_symbols.add((ACCOUNT, SYMBOL))

    async def failed_fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    service._fetch_oco_exit_detail = failed_fetch
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, expected_row_id=row_id
    ) is False
    service._v2_eod_oco_transitioned.add((service._session_day_et(), ACCOUNT, SYMBOL))

    assert service._native_oco_stand_down_active(ACCOUNT, SYMBOL) is True


@pytest.mark.asyncio
async def test_pending_child_fill_blocks_the_real_hard_stop_ladder() -> None:
    service, sessions, row_id = _service_with_weto()
    service.settings.oms_v2_exit_management_enabled = True
    service._managed_v2_symbols.add((ACCOUNT, SYMBOL))

    async def failed_fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    service._fetch_oco_exit_detail = failed_fetch
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, expected_row_id=row_id
    ) is False
    service._latest_quotes_by_symbol[SYMBOL] = {
        "bid": 1.50,
        "ask": 1.51,
        "received_at": utcnow(),
    }

    await service._evaluate_v2_managed_exit(ACCOUNT, SYMBOL)

    with sessions() as session:
        sells = session.scalars(select(TradeIntent).where(TradeIntent.side == "sell")).all()
        row = session.get(OmsManagedPosition, UUID(row_id))
    assert sells == []
    assert row is not None and row.status == "open" and row.current_quantity == 1


@pytest.mark.asyncio
async def test_reject_flat_backstop_preserves_known_pending_child_writer() -> None:
    service, sessions, row_id = _service_with_weto()

    async def failed_fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    async def flat_state(*_args, **_kwargs):
        return "flat"

    async def confirmed_flat(*_args, **_kwargs):
        return True

    service._fetch_oco_exit_detail = failed_fetch
    service._broker_symbol_position_state = flat_state
    service._broker_symbol_is_flat = confirmed_flat
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, expected_row_id=row_id
    ) is False

    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        assert row is not None
        for _ in range(service._V2_EXIT_RECONCILE_AFTER_FAILURES - 1):
            assert await service._v2_close_reconcile_flat(session, ACCOUNT, SYMBOL, row) is False
        assert await service._v2_close_reconcile_flat(session, ACCOUNT, SYMBOL, row) is False
        session.commit()

    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        assert row is not None and row.status == "open" and row.current_quantity > 0
        assert session.scalars(select(Fill).where(Fill.symbol == SYMBOL, Fill.side == "sell")).all() == []
        assert service._oco_exit_fill_pending[(ACCOUNT, SYMBOL)].row_id == row_id


@pytest.mark.asyncio
async def test_pending_child_fill_retries_after_eod_removes_resolution_grace() -> None:
    service, _sessions, row_id = _service_with_weto()
    service._managed_v2_symbols.add((ACCOUNT, SYMBOL))
    fetches = 0

    async def failed_fetch(*_args, **_kwargs):
        nonlocal fetches
        fetches += 1
        return _EXIT_FETCH_FAILED

    async def armed(_acct, _symbols):
        return {"working": [], "filled": False, "unsafe": False}

    async def no_longer_in_recent_fills(_acct, _symbols):
        return set()

    service._fetch_oco_exit_detail = failed_fetch
    service.settings.oms_native_oco_stand_down_enabled = True
    service.settings.oms_native_oco_resolve_flat_reconcile_enabled = True
    service.broker_adapter.fetch_exit_legs_for_entry = armed
    service.broker_adapter.fetch_oco_resolved_by_fill_symbols = no_longer_in_recent_fills
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, expected_row_id=row_id
    ) is False
    service._native_oco_resolving.pop((ACCOUNT, SYMBOL), None)
    service._oco_exit_fill_pending[(ACCOUNT, SYMBOL)].last_attempt -= timedelta(seconds=31)

    await service._refresh_native_oco_armed_state([ACCOUNT])

    assert fetches == 2
    assert service._native_oco_stand_down_active(ACCOUNT, SYMBOL) is True


@pytest.mark.asyncio
async def test_pending_child_fill_pages_after_two_minutes_even_if_broker_unavailable() -> None:
    service, sessions, row_id = _service_with_weto()
    service._managed_v2_symbols.add((ACCOUNT, SYMBOL))

    async def failed_fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    service._fetch_oco_exit_detail = failed_fetch
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, expected_row_id=row_id
    ) is False
    service._oco_exit_fill_pending[(ACCOUNT, SYMBOL)].first_seen -= timedelta(seconds=121)

    await service._refresh_native_oco_armed_state([ACCOUNT])

    with sessions() as session:
        incidents = session.scalars(select(SystemIncident)).all()
    assert len(incidents) == 1
    assert incidents[0].payload["reason"] == "child_fill_unrecorded_after_retry_window"
    assert service._native_oco_stand_down_active(ACCOUNT, SYMBOL) is True


@pytest.mark.asyncio
async def test_previous_oco_fill_cannot_hold_a_replacement_row() -> None:
    service, sessions, old_row_id = _service_with_weto()
    with sessions() as session:
        # Production's index is partial (open rows only); SQLite renders it as unconditional.
        session.execute(text("DROP INDEX uq_oms_managed_positions_open_symbol"))
        old_row = session.get(OmsManagedPosition, UUID(old_row_id))
        assert old_row is not None
        old_row.status = "closed"
        old_row.current_quantity = 0
        replacement = OmsManagedPosition(
            strategy_code="schwab_1m_v2",
            broker_account_name=ACCOUNT,
            symbol=SYMBOL,
            entry_price=Decimal("2.50"),
            original_quantity=1,
            current_quantity=1,
            entry_path="ATR Flip",
            entry_time=datetime(2026, 9, 24, 14, 10, tzinfo=UTC),
            status="open",
            config_name="make_v2_variant",
        )
        session.add(replacement)
        session.commit()
    service._managed_v2_symbols.add((ACCOUNT, SYMBOL))

    async def failed_fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    service._fetch_oco_exit_detail = failed_fetch
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, expected_row_id=old_row_id
    ) is False

    assert service._native_oco_stand_down_active(ACCOUNT, SYMBOL) is False


@pytest.mark.asyncio
async def test_weto_child_sell_fill_is_durable_before_managed_row_closes() -> None:
    service, sessions, row_id = _service_with_weto()
    child_id = STOP_CHILD_ORDER_ID
    detail = {
        "symbol": SYMBOL,
        "entry_broker_order_id": ENTRY_ORDER_ID,
        "exit_base_client_order_id": PROTECT_BASE,
        "quantity": Decimal("1"),
        "price": Decimal("1.96"),
        "filled_at": STOP_FILLED_AT,
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
    assert fills[0].price == Decimal("1.96")
    assert fills[0].filled_at == STOP_FILLED_AT.replace(tzinfo=None)
    assert incidents == []


@pytest.mark.asyncio
async def test_missing_weto_child_order_id_cannot_close_or_create_fill() -> None:
    service, sessions, row_id = _service_with_weto()
    detail = {
        "symbol": SYMBOL,
        "entry_broker_order_id": ENTRY_ORDER_ID,
        "exit_base_client_order_id": PROTECT_BASE,
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
            "entry_broker_order_id": ENTRY_ORDER_ID,
            "exit_base_client_order_id": PROTECT_BASE,
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
    assert incidents[0].payload["reason"] == "child_attribution_refused"


@pytest.mark.asyncio
async def test_weto_child_fill_closes_prior_unrecorded_incident() -> None:
    service, sessions, row_id = _service_with_weto()

    async def failed_fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    service._fetch_oco_exit_detail = failed_fetch
    for _ in range(5):
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
            "entry_broker_order_id": ENTRY_ORDER_ID,
            "exit_base_client_order_id": PROTECT_BASE,
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
async def test_weto_answer_without_entry_order_keeps_row_open_as_ownership_mismatch() -> None:
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
            "entry_broker_order_id": ENTRY_ORDER_ID,
            "exit_base_client_order_id": PROTECT_BASE,
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
    assert incidents == []


@pytest.mark.asyncio
async def test_weto_already_recorded_child_accepts_equivalent_decimal_scale() -> None:
    service, sessions, row_id = _service_with_weto()
    child_id = "WETO-STOP-CHILD-CONTROL"
    base_detail = {
        "symbol": SYMBOL,
        "entry_broker_order_id": ENTRY_ORDER_ID,
        "exit_base_client_order_id": PROTECT_BASE,
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
        "entry_broker_order_id": ENTRY_ORDER_ID,
        "exit_base_client_order_id": PROTECT_BASE,
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
    assert incidents[0].payload["reason"] == "child_attribution_refused"
