from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy import distinct, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import MomentumPaperEvent
from project_mai_tai.momentum_paper.models import MomentumTapeRecord


TERMINAL_EVENT_TYPES = frozenset({"FINAL", "NO_FILL", "UNANSWERABLE"})


class MomentumPaperStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def append_many(self, records: Iterable[MomentumTapeRecord]) -> int:
        pending = list({record.event_key: record for record in records}.values())
        if not pending:
            return 0
        keys = [record.event_key for record in pending]
        with self.session_factory() as session:
            existing = set(
                session.scalars(
                    select(MomentumPaperEvent.event_key).where(
                        MomentumPaperEvent.event_key.in_(keys)
                    )
                ).all()
            )
            new_records = [record for record in pending if record.event_key not in existing]
            session.add_all(
                [
                    MomentumPaperEvent(
                        event_key=record.event_key,
                        logical_id=record.logical_id,
                        strategy_code=record.strategy_code,
                        event_type=record.event_type,
                        session_date=record.session_date,
                        symbol=record.symbol,
                        observed_at=record.observed_at,
                        payload=dict(record.payload),
                    )
                    for record in new_records
                ]
            )
            session.commit()
        return len(new_records)

    def load_session(self, session_date: date) -> list[MomentumTapeRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(MomentumPaperEvent)
                .where(MomentumPaperEvent.session_date == session_date)
                .order_by(MomentumPaperEvent.observed_at, MomentumPaperEvent.created_at)
            ).all()
        return [self._record(row) for row in rows]

    def load_completed_since(self, start_date: date) -> list[dict[str, object]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(MomentumPaperEvent)
                .where(
                    MomentumPaperEvent.session_date >= start_date,
                    MomentumPaperEvent.event_type.in_(TERMINAL_EVENT_TYPES),
                )
                .order_by(MomentumPaperEvent.observed_at)
            ).all()
        return [dict(row.payload or {}) for row in rows]

    def load_closed_session_dates(self, *, limit: int = 20) -> list[date]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(distinct(MomentumPaperEvent.session_date))
                .where(MomentumPaperEvent.event_type == "SESSION_CLOSED")
                .order_by(MomentumPaperEvent.session_date.desc())
                .limit(limit)
            ).all()
        return list(rows)

    @staticmethod
    def incomplete_logical_ids(rows: Iterable[MomentumTapeRecord]) -> set[str]:
        detected = {row.logical_id for row in rows if row.event_type == "DETECTED"}
        terminal = {row.logical_id for row in rows if row.event_type in TERMINAL_EVENT_TYPES}
        return detected - terminal

    @staticmethod
    def latest_session_ready(
        rows: Iterable[MomentumTapeRecord],
    ) -> MomentumTapeRecord | None:
        matches = [row for row in rows if row.event_type == "SESSION_READY"]
        return matches[-1] if matches else None

    @staticmethod
    def _record(row: MomentumPaperEvent) -> MomentumTapeRecord:
        return MomentumTapeRecord(
            event_key=row.event_key,
            logical_id=row.logical_id,
            strategy_code=row.strategy_code,
            event_type=row.event_type,
            session_date=row.session_date,
            symbol=row.symbol,
            observed_at=row.observed_at,
            payload=dict(row.payload or {}),
        )


def session_record(
    *,
    session_date: date,
    observed_at: datetime,
    prior_close_date: date,
    prior_closes: dict[str, str],
    condition_snapshot: dict[str, object],
) -> MomentumTapeRecord:
    return MomentumTapeRecord(
        event_key=f"momentum-paper:{session_date.isoformat()}:SESSION_READY",
        logical_id=f"momentum-paper:{session_date.isoformat()}",
        strategy_code="momentum_paper",
        event_type="SESSION_READY",
        session_date=session_date,
        symbol="*",
        observed_at=observed_at,
        payload={
            "prior_close_date": prior_close_date.isoformat(),
            "prior_closes": prior_closes,
            "condition_snapshot": condition_snapshot,
        },
    )


def session_closed_record(
    *, session_date: date, observed_at: datetime, excluded_prints: int
) -> MomentumTapeRecord:
    return MomentumTapeRecord(
        event_key=f"momentum-paper:{session_date.isoformat()}:SESSION_CLOSED",
        logical_id=f"momentum-paper:{session_date.isoformat()}",
        strategy_code="momentum_paper",
        event_type="SESSION_CLOSED",
        session_date=session_date,
        symbol="*",
        observed_at=observed_at,
        payload={"excluded_prints": excluded_prints},
    )
