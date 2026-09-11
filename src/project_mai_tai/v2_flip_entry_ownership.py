"""Durable ownership for the flag-gated one-first-entry-per-ATR-flip lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Mapping
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.fanout_segment_store import current_session_anchor


SNAPSHOT_TYPE = "v2_flip_entry_ownership"
SCHEMA_VERSION = 1
ACTIVE_PHASES = frozenset(
    {
        "resting",
        "awaiting_fill",
        "provisional",
        "bound",
        "consumed",
        "awaiting_close",
        "unknown",
    }
)


@dataclass(frozen=True)
class FlipPositionLeg:
    account_name: str
    managed_row_id: str
    entry_time_ms: int
    quantity: int


@dataclass(frozen=True)
class FlipConfirmationClose:
    """A confirmed confirmation-exit fill for one exact managed-position row."""

    account_name: str
    managed_row_id: str
    fanout_slot_id: str


@dataclass(frozen=True)
class FlipPositionBook:
    observed_at_ms: int
    readable: bool
    legs_by_symbol: Mapping[str, tuple[FlipPositionLeg, ...]]
    confirmation_closes_by_symbol: Mapping[
        str, tuple[FlipConfirmationClose, ...]
    ] = field(default_factory=dict)
    terminal_unfilled_opportunities_by_symbol: Mapping[
        str, frozenset[int]
    ] = field(default_factory=dict)


@dataclass(frozen=True)
class FlipEntryOwnershipRecord:
    symbol: str
    opportunity_id: int
    phase: str
    flip_bar_ts: int
    provisional_started_ms: int
    fill_accounts: tuple[str, ...]
    position_ids: Mapping[str, str]
    position_entry_ms: Mapping[str, int]

    def as_payload(self, *, active: bool, reason: str) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "strategy_code": "schwab_1m_v2",
            "symbol": self.symbol,
            "opportunity_id": str(self.opportunity_id),
            "phase": self.phase,
            "flip_bar_ts": str(self.flip_bar_ts),
            "provisional_started_ms": str(self.provisional_started_ms),
            "fill_accounts": list(self.fill_accounts),
            "position_ids": dict(self.position_ids),
            "position_entry_ms": {
                account: str(value) for account, value in self.position_entry_ms.items()
            },
            "active": active,
            "reason": reason,
        }


class FlipEntryOwnershipStore:
    """Append-only store; no schema migration and no writes when the feature is off."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        ownership: FlipEntryOwnershipRecord,
        *,
        active: bool,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        observed_at = now or datetime.now(UTC)
        with self._session_factory() as session:
            session.add(
                DashboardSnapshot(
                    snapshot_type=SNAPSHOT_TYPE,
                    payload=ownership.as_payload(active=active, reason=reason),
                    created_at=observed_at,
                )
            )
            session.commit()

    def restore_active(
        self, *, now: datetime | None = None
    ) -> Mapping[str, FlipEntryOwnershipRecord]:
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

        latest: dict[str, FlipEntryOwnershipRecord | None] = {}
        for row in rows:
            if not isinstance(row.payload, dict):
                raise ValueError(f"unreadable flip ownership snapshot {row.id}")
            payload = row.payload
            symbol = str(payload.get("symbol", "")).strip().upper()
            if (
                not symbol
                or payload.get("strategy_code") != "schwab_1m_v2"
                or int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION
            ):
                raise ValueError(f"invalid flip ownership snapshot {row.id}")
            if not bool(payload.get("active", False)):
                latest[symbol] = None
                continue
            try:
                phase = str(payload.get("phase", ""))
                opportunity_id = int(str(payload.get("opportunity_id", "0")))
                flip_bar_ts = int(str(payload.get("flip_bar_ts", "0")))
                provisional_started_ms = int(str(payload.get("provisional_started_ms", "0")))
                raw_position_entry_ms = payload.get("position_entry_ms", {})
                if not isinstance(raw_position_entry_ms, dict):
                    raise ValueError("position_entry_ms must be an object")
                position_entry_ms = {
                    str(account): int(str(value))
                    for account, value in raw_position_entry_ms.items()
                }
                raw_position_ids = payload.get("position_ids", {})
                if not isinstance(raw_position_ids, dict):
                    raise ValueError("position_ids must be an object")
                position_ids = {
                    str(account): str(value)
                    for account, value in raw_position_ids.items()
                    if str(account) and str(value)
                }
                raw_fill_accounts = payload.get("fill_accounts", [])
                if not isinstance(raw_fill_accounts, list):
                    raise ValueError("fill_accounts must be a list")
                fill_accounts = tuple(sorted(str(value) for value in raw_fill_accounts if value))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid flip ownership snapshot {row.id}") from exc
            if phase not in ACTIVE_PHASES or opportunity_id <= 0:
                raise ValueError(f"invalid active flip ownership snapshot {row.id}")
            latest[symbol] = FlipEntryOwnershipRecord(
                symbol=symbol,
                opportunity_id=opportunity_id,
                phase=phase,
                flip_bar_ts=flip_bar_ts,
                provisional_started_ms=provisional_started_ms,
                fill_accounts=fill_accounts,
                position_ids=position_ids,
                position_entry_ms=position_entry_ms,
            )
        return {symbol: value for symbol, value in latest.items() if value is not None}
