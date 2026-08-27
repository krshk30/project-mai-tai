"""C3 controls for the post-exit stale-held retry bound.

These drive the real managed-exit read/evaluate/emit path. A confirmed SELL fill is the positive
classifier; broker-position freshness, not elapsed time or quote count, decides whether another
attempt is allowed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerOrder,
    Fill,
    OmsManagedPosition,
    TradeIntent,
)
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

ACCT = "live:orb"
SYMBOL = "YYGH"


class _FakeRedis:
    async def xadd(self, *args, **kwargs):
        return b"1-1"


def _session_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in ("market_trade_ticks", "market_quote_ticks")
    ]
    Base.metadata.create_all(engine, tables=tables)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _service(sf: sessionmaker, *, bound_seconds: float = 245.0) -> OmsRiskService:
    service = OmsRiskService(
        Settings(
            oms_v2_exit_management_enabled=True,
            oms_v2_exit_close_on_fill_enabled=True,
            oms_post_exit_stale_held_max_age_seconds=bound_seconds,
        ),
        redis_client=_FakeRedis(),
        session_factory=sf,
        broker_adapter=SimulatedBrokerAdapter(),
    )
    with sf() as session:
        service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        service.store.ensure_broker_account(
            session, ACCT, provider="webull", environment="test"
        )
        session.commit()
    return service


def _arm(service: OmsRiskService, sf: sessionmaker, *, entry_at: datetime) -> None:
    with sf() as session:
        row = service.store.create_managed_position(
            session,
            strategy_code="schwab_1m_v2",
            broker_account_name=ACCT,
            symbol=SYMBOL,
            entry_price=Decimal("2.00"),
            quantity=1,
            entry_path="MACD Cross",
        )
        row.entry_time = entry_at
        session.commit()
    service._managed_v2_symbols.add((ACCT, SYMBOL))


def _seed_sell_fill_and_position(
    service: OmsRiskService,
    sf: sessionmaker,
    *,
    fill_at: datetime,
    position_as_of: datetime,
    quantity: Decimal = Decimal("1"),
    order_type: str = "oco_exit",
) -> None:
    with sf() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = service.store.ensure_broker_account(
            session, ACCT, provider="webull", environment="test"
        )
        order = BrokerOrder(
            intent_id=None,
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id=f"c3-prior-{uuid4().hex}",
            broker_order_id=f"c3-prior-broker-{uuid4().hex}",
            symbol=SYMBOL,
            side="sell",
            order_type=order_type,
            time_in_force="day",
            quantity=Decimal("1"),
            status="filled",
            payload={"metadata": {"oms_v2_managed_exit": "true"}},
            submitted_at=fill_at,
        )
        session.add(order)
        session.flush()
        session.add(
            Fill(
                order_id=order.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                broker_fill_id=f"c3-fill-{uuid4().hex}",
                symbol=SYMBOL,
                side="sell",
                quantity=Decimal("1"),
                price=Decimal("1.95"),
                filled_at=fill_at,
                payload={"source": "test-confirmed-exit"},
            )
        )
        session.add(
            AccountPosition(
                broker_account_id=account.id,
                symbol=SYMBOL,
                quantity=quantity,
                average_price=Decimal("2.00") if quantity else Decimal("0"),
                market_value=Decimal("2.00") if quantity else Decimal("0"),
                source_updated_at=position_as_of,
            )
        )
        session.commit()


def _set_position_snapshot(
    service: OmsRiskService,
    sf: sessionmaker,
    *,
    as_of: datetime,
    quantity: Decimal,
) -> None:
    with sf() as session:
        account = service.store.ensure_broker_account(
            session, ACCT, provider="webull", environment="test"
        )
        position = session.scalar(
            select(AccountPosition).where(
                AccountPosition.broker_account_id == account.id,
                AccountPosition.symbol == SYMBOL,
            )
        )
        assert position is not None
        position.quantity = quantity
        position.source_updated_at = as_of
        session.commit()


def _quote(service: OmsRiskService) -> None:
    service._latest_quotes_by_symbol[SYMBOL] = {
        "bid": 1.90,
        "ask": 1.91,
        "received_at": datetime.now(UTC),
    }


def _sell_intents(sf: sessionmaker) -> list[TradeIntent]:
    with sf() as session:
        return list(
            session.scalars(
                select(TradeIntent).where(
                    TradeIntent.symbol == SYMBOL,
                    TradeIntent.side == "sell",
                )
            ).all()
        )


def _managed_row(sf: sessionmaker) -> OmsManagedPosition:
    with sf() as session:
        row = session.scalar(
            select(OmsManagedPosition).where(OmsManagedPosition.symbol == SYMBOL)
        )
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_stale_snapshot_waits_then_newer_held_retries_and_fresh_flat_closes() -> None:
    """Known-positive: inside the measured window, evidence advances the retry to success."""
    sf = _session_factory()
    service = _service(sf)
    now = datetime.now(UTC).replace(microsecond=0)
    fill_at = now - timedelta(seconds=20)
    _arm(service, sf, entry_at=fill_at - timedelta(minutes=5))
    _seed_sell_fill_and_position(
        service,
        sf,
        fill_at=fill_at,
        position_as_of=fill_at - timedelta(seconds=1),
    )
    _quote(service)

    # The old HELD snapshot cannot fund a sell, no matter how many quotes arrive.
    await service._evaluate_v2_managed_exit(ACCT, SYMBOL)
    await service._evaluate_v2_managed_exit(ACCT, SYMBOL)
    assert _sell_intents(sf) == []

    # A genuinely newer HELD generation permits exactly one bounded attempt. The simulated
    # adapter fills it; that is the "retry then succeeds" control through the real emit path.
    _set_position_snapshot(
        service, sf, as_of=fill_at + timedelta(seconds=5), quantity=Decimal("1")
    )
    await service._evaluate_v2_managed_exit(ACCT, SYMBOL)
    assert len(_sell_intents(sf)) == 1
    assert _managed_row(sf).status == "closed"


@pytest.mark.asyncio
async def test_outside_measured_bound_stops_reports_and_retains_owned_row() -> None:
    """Negative polarity: age stops retries; it never turns into permission to sell."""
    sf = _session_factory()
    service = _service(sf, bound_seconds=245.0)
    now = datetime.now(UTC).replace(microsecond=0)
    fill_at = now - timedelta(seconds=246)
    _arm(service, sf, entry_at=fill_at - timedelta(minutes=5))
    _seed_sell_fill_and_position(
        service,
        sf,
        fill_at=fill_at,
        position_as_of=fill_at + timedelta(seconds=5),
    )
    _quote(service)
    service.logger = Mock()

    await service._evaluate_v2_managed_exit(ACCT, SYMBOL)
    await service._evaluate_v2_managed_exit(ACCT, SYMBOL)

    assert _sell_intents(sf) == []
    assert _managed_row(sf).status == "open"  # ownership/protection is preserved
    assert service.logger.log.call_count == 1
    assert "outcome=%s" in service.logger.log.call_args.args[1]
    assert "bound_exceeded_stop_and_report" in service.logger.log.call_args.args


@pytest.mark.asyncio
async def test_without_preceding_sell_fill_no_position_is_not_settlement_lag() -> None:
    """Classifier polarity: no preceding SELL fill means C3 must not intercept the exit."""
    sf = _session_factory()
    service = _service(sf)
    now = datetime.now(UTC).replace(microsecond=0)
    _arm(service, sf, entry_at=now - timedelta(minutes=5))
    _quote(service)

    await service._evaluate_v2_managed_exit(ACCT, SYMBOL)

    assert len(_sell_intents(sf)) == 1
    assert _managed_row(sf).status == "closed"


@pytest.mark.asyncio
async def test_partial_scale_sell_fill_is_not_misclassified_as_full_exit() -> None:
    """A partial sell is not evidence that the venue already sold the whole position."""
    sf = _session_factory()
    service = _service(sf)
    now = datetime.now(UTC).replace(microsecond=0)
    fill_at = now - timedelta(seconds=20)
    _arm(service, sf, entry_at=fill_at - timedelta(minutes=5))
    _seed_sell_fill_and_position(
        service,
        sf,
        fill_at=fill_at,
        position_as_of=fill_at - timedelta(seconds=1),
        order_type="limit",  # no close intent and not a native OCO child
    )
    _quote(service)

    await service._evaluate_v2_managed_exit(ACCT, SYMBOL)

    assert len(_sell_intents(sf)) == 1
    assert _managed_row(sf).status == "closed"


@pytest.mark.asyncio
async def test_fresh_flat_after_sell_fill_closes_without_another_sell() -> None:
    sf = _session_factory()
    service = _service(sf)
    now = datetime.now(UTC).replace(microsecond=0)
    fill_at = now - timedelta(seconds=10)
    _arm(service, sf, entry_at=fill_at - timedelta(minutes=5))
    _seed_sell_fill_and_position(
        service,
        sf,
        fill_at=fill_at,
        position_as_of=fill_at + timedelta(seconds=5),
        quantity=Decimal("0"),
    )
    _quote(service)

    await service._evaluate_v2_managed_exit(ACCT, SYMBOL)

    assert _sell_intents(sf) == []
    assert _managed_row(sf).status == "closed"
