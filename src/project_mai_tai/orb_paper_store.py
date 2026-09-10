from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import OrbPaperEvent

ORB_PAPER_ACCOUNT_NAME = "paper:orb"
ORB_PAPER_ATR_BAR_EVENT_TYPE = "PAPER_ATR_BAR"
ORB_PAPER_ENTRY_GATE_EVENT_TYPE = "PAPER_ENTRY_GATE"
ORB_PAPER_EVENT_TYPE = "PAPER_ENTRY_DECISION"
ORB_PAPER_EXIT_EVENT_TYPE = "PAPER_EXIT_DECISION"
ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE = "PAPER_LEVEL_FINALIZED"
ORB_PAPER_ORDER_ADJUSTED_EVENT_TYPE = "PAPER_ORDER_ADJUSTED"
ORB_PAPER_ORDER_PLACED_EVENT_TYPE = "PAPER_ORDER_PLACED"
ORB_PAPER_ORDER_UNANSWERABLE_EVENT_TYPE = "PAPER_ORDER_UNANSWERABLE"


@dataclass(frozen=True)
class OrbPaperDecision:
    """Durable paper evidence, deliberately not a broker order or broker fill."""

    event_key: str
    session_date: date
    symbol: str
    observed_at: datetime
    entry_price: Decimal
    quantity: Decimal
    attempt: int
    mode: str
    detail: dict[str, object] = field(default_factory=dict)
    event_type: str = ORB_PAPER_EVENT_TYPE


class OrbPaperStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def append(self, decision: OrbPaperDecision) -> bool:
        """Append once by decision-time identity; return whether a row was inserted."""
        with self.session_factory() as session:
            exists = session.scalar(
                select(OrbPaperEvent.id).where(OrbPaperEvent.event_key == decision.event_key)
            )
            if exists is not None:
                return False
            session.add(
                OrbPaperEvent(
                    event_key=decision.event_key,
                    event_type=decision.event_type,
                    session_date=decision.session_date,
                    symbol=decision.symbol,
                    observed_at=decision.observed_at,
                    entry_price=decision.entry_price,
                    quantity=decision.quantity,
                    attempt=decision.attempt,
                    mode=decision.mode,
                    payload=dict(decision.detail),
                )
            )
            session.commit()
        return True

    def load_lifecycle(self) -> list[OrbPaperDecision]:
        """Load the small append-only lifecycle tape for restart reconstruction."""
        with self.session_factory() as session:
            rows = session.scalars(
                select(OrbPaperEvent)
                .where(
                    OrbPaperEvent.event_type.in_(
                        (
                            ORB_PAPER_ATR_BAR_EVENT_TYPE,
                            ORB_PAPER_EVENT_TYPE,
                            ORB_PAPER_EXIT_EVENT_TYPE,
                        )
                    )
                )
                .order_by(OrbPaperEvent.observed_at, OrbPaperEvent.created_at)
            ).all()
        return [
            OrbPaperDecision(
                event_key=row.event_key,
                event_type=row.event_type,
                session_date=row.session_date,
                symbol=row.symbol,
                observed_at=row.observed_at,
                entry_price=row.entry_price,
                quantity=row.quantity,
                attempt=row.attempt,
                mode=row.mode,
                detail=dict(row.payload or {}),
            )
            for row in rows
        ]
