"""ALERTS1: durable per-account receipt of the last COMPLETE, SUCCESSFUL broker positions read.

Written ONLY by the OMS broker-sync pass (``OmsRiskService.sync_broker_positions`` ->
``_persist``), off the event loop, inside the same transaction that applies the snapshot, and
ONLY for accounts whose ``list_account_positions`` call returned (the adapters raise on a failed
or incomplete read, and such an account is excluded from ``fetched``). A failed read writes
nothing, so the previous receipt is retained and ages. Fills never touch it.

``read_at`` is the time of the real broker (wire) read. When an adapter served a cached snapshot
(Webull throttle / 429 backoff) it is the time that cached snapshot was actually read, so a cache
can never make an account look fresh.

Storage: one ``dashboard_snapshots`` row per account (snapshot_type below), upserted. No
migration. Readers: the reconciler's ALERTS1 incident auto-resolve evidence fence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from project_mai_tai.db.models import DashboardSnapshot

SNAPSHOT_TYPE = "oms_positions_read_receipt"


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
