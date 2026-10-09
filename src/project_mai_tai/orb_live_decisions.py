"""Bounded, observational ORB Live tape; never an order or indicator input."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.db.session import build_engine, build_session_factory

logger = logging.getLogger("orb-schwab")
SNAPSHOT_TYPE = "orb_schwab_live_decision"
VISIBLE_LIMIT = 50
QUEUE_LIMIT = 128
_ET = ZoneInfo("America/New_York")


def decision_session_factory(settings):
    if not str(settings.database_url).startswith("postgresql"):
        return build_session_factory(settings)
    # Independent observer pool: trading queries never wait for this writer.
    return sessionmaker(bind=build_engine(
        settings.database_url, connect_timeout_s=2, statement_timeout_ms=1000,
        lock_timeout_ms=250, pool_timeout_s=1,
    ), expire_on_commit=False)


def read_decisions(session, start: datetime, end: datetime) -> list[dict]:
    rows = session.scalars(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
        DashboardSnapshot.created_at >= start,
        DashboardSnapshot.created_at < end,
    ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
        .limit(VISIBLE_LIMIT)).all()
    return [dict(row.payload) for row in rows]


class LiveDecisionTape:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)

    def offer(self, row: dict) -> None:
        try:
            self.queue.put_nowait(dict(row))
        except asyncio.QueueFull:
            logger.warning("[ORB-LIVE-TAPE] dropped reason=queue_full symbol=%s", row["symbol"])

    def _persist(self, batch: list[dict]) -> None:
        with self.session_factory() as session:
            newest = max(datetime.fromisoformat(row["evaluated_at"]) for row in batch)
            start = newest.astimezone(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
            start = start.astimezone(UTC)
            for row in batch:
                session.add(DashboardSnapshot(
                    snapshot_type=SNAPSHOT_TYPE, payload=row,
                    created_at=datetime.fromisoformat(row["evaluated_at"]),
                ))
            session.flush()
            excess = list(session.scalars(select(DashboardSnapshot.id).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.created_at >= start,
                DashboardSnapshot.created_at < start + timedelta(days=1),
            ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc())
                .offset(VISIBLE_LIMIT)).all())
            if excess:
                session.execute(delete(DashboardSnapshot).where(DashboardSnapshot.id.in_(excess)))
            session.execute(delete(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.created_at < start - timedelta(days=7),
            ))
            session.commit()

    async def run(self) -> None:
        while True:
            first = await self.queue.get()
            batch = [first]
            while len(batch) < VISIBLE_LIMIT and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            try:
                # One physical worker at a time, even when a database is slow.
                await asyncio.to_thread(self._persist, batch)
            except Exception as exc:
                logger.warning("[ORB-LIVE-TAPE] write_failed count=%s error=%s",
                               len(batch), type(exc).__name__)
            finally:
                for _ in batch:
                    self.queue.task_done()
