"""ALERTS1: durable per-account receipt of the last COMPLETE, SUCCESSFUL broker positions read.

Produced ONLY by the OMS broker-sync pass (``OmsRiskService.sync_broker_positions``) for
accounts whose ``list_account_positions`` call returned (the adapters raise on a failed or
incomplete read, and such an account is excluded from ``fetched``) AND only after that pass's
snapshot save committed. It is then handed to ``PositionsReadReceiptWriter``: a bounded queue
(put_nowait, drop-and-log when full) drained by one ``asyncio.to_thread`` worker on its own
small pool (statement_timeout 1000 ms, lock_timeout 250 ms). The sync pass never awaits it and
never shares its transaction; a write failure is logged once and not retried (the next pass
offers a newer receipt). A failed read produces nothing, so the previous receipt is retained
and ages. Fills never touch it.

``read_at`` is the time of the real broker (wire) read. When an adapter served a cached snapshot
(Webull throttle / 429 backoff) it is the time that cached snapshot was actually read, so a cache
can never make an account look fresh.

Storage: one ``dashboard_snapshots`` row per account (snapshot_type below), upserted. No
migration. Readers: the reconciler's ALERTS1 incident auto-resolve evidence fence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.db.session import build_engine, build_session_factory

SNAPSHOT_TYPE = "oms_positions_read_receipt"
QUEUE_LIMIT = 8

logger = logging.getLogger("oms-risk")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def record_receipts(
    session: Session, receipts: Iterable[tuple[str, datetime, int]], *, recorded_at: datetime
) -> int:
    """Upsert one receipt per (account_name, read_at, position_count). One SELECT per call."""
    receipts = list(receipts)
    if not receipts:
        return 0
    existing: dict[str, DashboardSnapshot] = {}
    for row in session.scalars(
        select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)
    ).all():
        name = str((row.payload or {}).get("broker_account_name") or "")
        if name and name not in existing:
            existing[name] = row
    for account_name, read_at, position_count in receipts:
        payload = {
            "broker_account_name": account_name,
            "read_at": _aware(read_at).isoformat(),
            "position_count": int(position_count),
            "recorded_at": _aware(recorded_at).isoformat(),
            "writer": "oms.sync_broker_positions",
        }
        row = existing.get(account_name)
        if row is None:
            session.add(
                DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE, payload=payload, created_at=recorded_at)
            )
        else:
            row.payload = payload
            row.created_at = recorded_at
    return len(receipts)


def load_receipts(session: Session) -> dict[str, dict[str, Any]]:
    """Return {account_name: {"read_at": aware datetime, ...payload}} (latest row per account)."""
    receipts: dict[str, dict[str, Any]] = {}
    for row in session.scalars(
        select(DashboardSnapshot)
        .where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)
        .order_by(DashboardSnapshot.created_at.desc())
    ).all():
        payload = dict(row.payload or {})
        name = str(payload.get("broker_account_name") or "")
        if not name or name in receipts:
            continue
        try:
            payload["read_at"] = _aware(datetime.fromisoformat(str(payload["read_at"])))
        except (KeyError, ValueError):
            payload["read_at"] = None
        receipts[name] = payload
    return receipts


def receipt_session_factory(settings) -> sessionmaker[Session]:
    """Independent small pool: the OMS trading/sync sessions never wait for this writer."""
    if not str(settings.database_url).startswith("postgresql"):
        return build_session_factory(settings)
    return sessionmaker(
        bind=build_engine(
            settings.database_url,
            connect_timeout_s=2,
            statement_timeout_ms=1000,
            lock_timeout_ms=250,
            pool_timeout_s=1,
        ),
        expire_on_commit=False,
    )


class PositionsReadReceiptWriter:
    """Bounded, fire-and-forget receipt writer. ``offer`` never blocks and never raises."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None):
        self.session_factory = session_factory
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)

    def offer(self, receipts: list[tuple[str, datetime, int]]) -> bool:
        if not receipts:
            return False
        try:
            self.queue.put_nowait(list(receipts))
            return True
        except asyncio.QueueFull:
            logger.warning(
                "[POSITIONS-READ-RECEIPT] dropped reason=queue_full accounts=%s",
                ",".join(name for name, _, _ in receipts),
            )
            return False

    def _persist(self, batches: list[list[tuple[str, datetime, int]]]) -> None:
        latest: dict[str, tuple[str, datetime, int]] = {}
        for batch in batches:
            for receipt in batch:
                previous = latest.get(receipt[0])
                if previous is None or receipt[1] >= previous[1]:
                    latest[receipt[0]] = receipt
        with self.session_factory() as session:
            record_receipts(session, latest.values(), recorded_at=datetime.now(UTC))
            session.commit()

    async def run(self) -> None:
        while True:
            first = await self.queue.get()
            batches = [first]
            while not self.queue.empty():
                batches.append(self.queue.get_nowait())
            try:
                # One physical worker at a time, even when the database is slow.
                await asyncio.to_thread(self._persist, batches)
            except Exception as exc:  # noqa: BLE001 - evidence only; logged, never retried
                logger.warning(
                    "[POSITIONS-READ-RECEIPT] write_failed batches=%s error=%s",
                    len(batches),
                    type(exc).__name__,
                )
            finally:
                for _ in batches:
                    self.queue.task_done()
