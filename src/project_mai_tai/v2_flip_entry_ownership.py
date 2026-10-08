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
RETRY_BUDGET_SNAPSHOT_TYPE = "v2_retry_one_budget"
SCHEMA_VERSION = 1
RETRY_BUDGET_SCHEMA_VERSION = 2
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
class FlipPositionClose:
    """A durable terminal close attributed to one exact managed-position row."""

    account_name: str
    managed_row_id: str
    exit_reason: str


@dataclass(frozen=True)
class FlipPositionBook:
    observed_at_ms: int
    readable: bool
    legs_by_symbol: Mapping[str, tuple[FlipPositionLeg, ...]]
    confirmation_closes_by_symbol: Mapping[
        str, tuple[FlipConfirmationClose, ...]
    ] = field(default_factory=dict)
    closes_by_symbol: Mapping[str, tuple[FlipPositionClose, ...]] = field(
        default_factory=dict
    )
    terminal_unfilled_opportunities_by_symbol: Mapping[
        str, frozenset[int]
    ] = field(default_factory=dict)
    entry_classifications: Mapping[str, tuple[dict, ...]] = field(default_factory=dict)
    filled_opportunities: Mapping[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    closed_entry_rows: frozenset[tuple[str, str]] = field(default_factory=frozenset)


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
    retry_segment_id: int = 0
    retry_closes_at_place: int = 0

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
            "retry_segment_id": str(self.retry_segment_id),
            "retry_closes_at_place": self.retry_closes_at_place,
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

    def record_retry_budget(
        self,
        symbol: str,
        segment_id: int,
        closes_in_segment: int,
        *,
        now: datetime | None = None,
    ) -> None:
        if segment_id <= 0 or closes_in_segment < 0:
            raise ValueError("retry budget requires a known segment and nonnegative count")
        observed_at = now or datetime.now(UTC)
        with self._session_factory() as session:
            session.add(
                DashboardSnapshot(
                    snapshot_type=RETRY_BUDGET_SNAPSHOT_TYPE,
                    payload={
                        "schema_version": RETRY_BUDGET_SCHEMA_VERSION,
                        "strategy_code": "schwab_1m_v2",
                        "symbol": str(symbol).strip().upper(),
                        "segment_id": str(segment_id),
                        "closes_in_segment": int(closes_in_segment),
                    },
                    created_at=observed_at,
                )
            )
            session.commit()

    def restore_retry_budgets(
        self, *, now: datetime | None = None
    ) -> Mapping[str, tuple[int, int]]:
        anchor = current_session_anchor(now)
        with self._session_factory() as session:
            rows = session.scalars(
                select(DashboardSnapshot)
                .where(
                    DashboardSnapshot.snapshot_type == RETRY_BUDGET_SNAPSHOT_TYPE,
                    DashboardSnapshot.created_at >= anchor,
                )
                .order_by(DashboardSnapshot.created_at, DashboardSnapshot.id)
            ).all()

        latest: dict[str, tuple[int, int]] = {}
        for row in rows:
            if not isinstance(row.payload, dict):
                raise ValueError(f"unreadable retry budget snapshot {row.id}")
            payload = row.payload
            symbol = str(payload.get("symbol", "")).strip().upper()
            if (
                not symbol
                or payload.get("strategy_code") != "schwab_1m_v2"
                or int(payload.get("schema_version", 0) or 0)
                not in {SCHEMA_VERSION, RETRY_BUDGET_SCHEMA_VERSION}
            ):
                raise ValueError(f"invalid retry budget snapshot {row.id}")
            try:
                version = int(payload["schema_version"])
                # A v1 daily counter has no provable SELL-cycle identity. Hold until the next
                # live SELL rather than transferring its allowance into an unknown segment.
                segment_id = (
                    int(str(payload["segment_id"]))
                    if version == RETRY_BUDGET_SCHEMA_VERSION
                    else 0
                )
                count = int(
                    payload["closes_in_segment"]
                    if version == RETRY_BUDGET_SCHEMA_VERSION
                    else payload["closes_today"]
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid retry budget snapshot {row.id}") from exc
            except KeyError as exc:
                raise ValueError(f"invalid retry budget snapshot {row.id}") from exc
            if count < 0 or (version == RETRY_BUDGET_SCHEMA_VERSION and segment_id <= 0):
                raise ValueError(f"invalid retry budget snapshot {row.id}")
            # Late closes from an older cycle may be journaled after a newer SELL. The
            # active budget is the greatest SELL timestamp, not the last row written.
            previous = latest.get(symbol)
            if previous is None or segment_id > previous[0] or (
                segment_id == previous[0] and count >= previous[1]
            ):
                latest[symbol] = (segment_id, count)
        return latest

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
                retry_segment_id = int(str(payload.get("retry_segment_id", "0")))
                retry_closes_at_place = int(payload.get("retry_closes_at_place", 0))
                if retry_closes_at_place < 0:
                    raise ValueError("retry_closes_at_place must be nonnegative")
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
                retry_segment_id=retry_segment_id,
                retry_closes_at_place=retry_closes_at_place,
            )
        return {symbol: value for symbol, value in latest.items() if value is not None}
