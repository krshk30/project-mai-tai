"""Durable positive evidence that a v2 ATR segment already produced an entry.

The strategy permits one logical entry episode per ATR segment when reclaim is
disabled.  This store records only confirmed fills.  Absence is therefore not
interpreted as an unused segment after a restart; callers must treat a restored
active segment without a matching record as unknown and fail closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.fanout_segment_store import (
    STRATEGY_CODE,
    current_session_anchor,
)


SNAPSHOT_TYPE = "v2_segment_entry_consumed"


class V2SegmentConsumptionStore:
    """Append and restore fill-positive segment consumption evidence."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record_consumed(
        self,
        symbol: str,
        segment_id: int,
        slot_id: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> None:
        if segment_id <= 0:
            raise ValueError("segment_id must be positive")
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required")
        observed_at = now or datetime.now(UTC)
        anchor = current_session_anchor(observed_at)
        with self._session_factory() as session:
            session.add(
                DashboardSnapshot(
                    snapshot_type=SNAPSHOT_TYPE,
                    payload={
                        "schema_version": 1,
                        "strategy_code": STRATEGY_CODE,
                        "symbol": normalized_symbol,
                        "fanout_segment_id": str(segment_id),
                        "fanout_slot_id": str(slot_id),
                        "reason": str(reason),
                        "session_anchor": anchor.isoformat(),
                    },
                    created_at=observed_at,
                )
            )
            session.commit()

    def restore_consumed(
        self,
        active_segments: Mapping[str, int],
        *,
        now: datetime | None = None,
    ) -> Mapping[str, tuple[int, str]]:
        """Return positive fill evidence for the supplied active segments only."""

        active = {
            str(symbol).strip().upper(): int(segment_id)
            for symbol, segment_id in active_segments.items()
            if str(symbol).strip() and int(segment_id) > 0
        }
        if not active:
            return {}
        anchor = current_session_anchor(now)
        with self._session_factory() as session:
            rows = session.scalars(
                select(DashboardSnapshot)
                .where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                    DashboardSnapshot.created_at >= anchor,
                )
                .order_by(DashboardSnapshot.created_at, DashboardSnapshot.id)
            ).all()

        restored: dict[str, tuple[int, str]] = {}
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if payload.get("strategy_code") != STRATEGY_CODE:
                continue
            if str(payload.get("session_anchor", "")) != anchor.isoformat():
                continue
            symbol = str(payload.get("symbol", "")).strip().upper()
            try:
                segment_id = int(str(payload.get("fanout_segment_id", "0")))
            except ValueError:
                continue
            if active.get(symbol) != segment_id:
                continue
            restored[symbol] = (segment_id, str(payload.get("fanout_slot_id", "")))
        return restored


__all__ = ["SNAPSHOT_TYPE", "V2SegmentConsumptionStore"]
