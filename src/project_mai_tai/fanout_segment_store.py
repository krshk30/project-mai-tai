"""Durable observation store for the active v2 fan-out segment identity.

The strategy state machine is process-local.  A restart must not manufacture a
new cross-venue identity for an opportunity that was already armed, so the bot
records every bind/release transition in ``dashboard_snapshots``.  This module
does not decide whether an entry, latch, slot, or venue action is allowed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import DashboardSnapshot


SNAPSHOT_TYPE = "v2_fanout_segment_identity"
STRATEGY_CODE = "schwab_1m_v2"
EASTERN_TZ = ZoneInfo("America/New_York")
SESSION_ANCHOR_HOUR_ET = 4


def current_session_anchor(now: datetime | None = None) -> datetime:
    """Return the current 04:00 ET strategy-session anchor as a UTC instant."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current_et = current.astimezone(EASTERN_TZ)
    anchor_et = current_et.replace(
        hour=SESSION_ANCHOR_HOUR_ET,
        minute=0,
        second=0,
        microsecond=0,
    )
    if current_et < anchor_et:
        anchor_et -= timedelta(days=1)
    return anchor_et.astimezone(UTC)


class FanoutSegmentIdentityStore:
    """Append and restore identity transitions without influencing strategy policy."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        symbol: str,
        segment_id: int,
        active: bool,
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
                        "active": bool(active),
                        "reason": str(reason),
                        "session_anchor": anchor.isoformat(),
                    },
                    created_at=observed_at,
                )
            )
            session.commit()

    def restore_active(self, *, now: datetime | None = None) -> Mapping[str, int]:
        """Return the latest active identity per symbol in the current session."""

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

        latest: dict[str, int | None] = {}
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if payload.get("strategy_code") != STRATEGY_CODE:
                continue
            if str(payload.get("session_anchor", "")) != anchor.isoformat():
                continue
            symbol = str(payload.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            if not bool(payload.get("active", False)):
                latest[symbol] = None
                continue
            try:
                segment_id = int(str(payload.get("fanout_segment_id", "0")))
            except ValueError:
                continue
            latest[symbol] = segment_id if segment_id > 0 else None
        return {
            symbol: segment_id
            for symbol, segment_id in latest.items()
            if segment_id is not None
        }
