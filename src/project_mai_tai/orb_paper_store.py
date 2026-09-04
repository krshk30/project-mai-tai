from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import OrbPaperEvent

ORB_PAPER_ACCOUNT_NAME = "paper:orb"
ORB_PAPER_EVENT_TYPE = "PAPER_ENTRY_DECISION"


@dataclass(frozen=True)
class OrbPaperDecision:
    """A durable observation, deliberately not an order or a claimed fill."""

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
