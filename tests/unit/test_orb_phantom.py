"""ORB paper decisions never masquerade as broker fills or positions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import OrbPaperEvent
from project_mai_tai.orb_paper_store import OrbPaperDecision, OrbPaperStore
from project_mai_tai.services.orb_app import OrbService, _PendingPaperEntry, _SymbolState
from project_mai_tai.settings import Settings

AT = datetime(2026, 9, 4, 13, 31, 26, tzinfo=UTC)


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _decision() -> OrbPaperDecision:
    return OrbPaperDecision(
        event_key="orb-paper:2026-09-04:DAIC:1:1",
        session_date=AT.date(),
        symbol="DAIC",
        observed_at=AT,
        entry_price=Decimal("3.21"),
        quantity=Decimal("5"),
        attempt=1,
        mode="running_high_breakout",
        detail={"reason": "ORB_OPEN"},
    )


def test_can_enter_is_bounded_by_paper_write_and_attempt_cap() -> None:
    service = OrbService(settings=Settings(), redis_client=MagicMock())
    state = _SymbolState()
    assert service._can_enter(state) is True
    state.pending = True
    assert service._can_enter(state) is False
    state.pending = False
    state.attempts = 2
    assert service._can_enter(state) is False


def test_paper_store_appends_once_without_order_or_account_identity() -> None:
    factory = _session_factory()
    store = OrbPaperStore(factory)

    assert store.append(_decision()) is True
    assert store.append(_decision()) is False

    with factory() as session:
        row = session.scalar(select(OrbPaperEvent))
        assert row is not None
        assert row.event_type == "PAPER_ENTRY_DECISION"
        assert row.symbol == "DAIC"
        assert row.entry_price == Decimal("3.21000000")
        assert session.scalar(select(func.count()).select_from(OrbPaperEvent)) == 1
    assert {column.name for column in OrbPaperEvent.__table__.columns}.isdisjoint(
        {"broker_account_id", "broker_order_id", "trade_intent_id", "fill_id"}
    )


def test_non_paper_output_is_refused_at_service_boundary() -> None:
    with pytest.raises(RuntimeError, match="broker-disconnected"):
        OrbService._require_paper_decision(object())


def test_failed_paper_write_is_retained_without_fallback() -> None:
    class _FailingStore:
        def append(self, decision: OrbPaperDecision) -> bool:
            del decision
            raise RuntimeError("database unavailable")

    service = OrbService(
        settings=Settings(),
        redis_client=MagicMock(),
        paper_store=_FailingStore(),  # type: ignore[arg-type]
    )
    service._running_high_mode = False
    service._reclaim_mode = False
    service._states["DAIC"] = _SymbolState(attempts=1, pending=True)
    pending = _PendingPaperEntry("DAIC", 3.21, AT, 1)
    service._pending_paper_entries = [pending]

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(service._record_pending_paper_entries())

    assert service._pending_paper_entries == [pending]
    assert service._states["DAIC"].pending is True
