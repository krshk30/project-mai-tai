"""Durable, evidence-positive Webull fan-out outcome transport.

The v2 strategy owns the latch; the OMS owns most outcome evidence.  This module is the
append-only seam between them.  Every record is keyed by the shared Section 82 segment/slot
identity.  Missing records never masquerade as venue truth: the strategy's existing provisional
grace chooses the deliberately visible failure direction (release/possible duplicate), while a
positive submitted/working/fill record can hold the latch without consulting virtual positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import logging
from threading import Lock
from typing import Callable, Iterable, Mapping
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import DashboardSnapshot


OUTCOME_SNAPSHOT_TYPE = "v2_fanout_outcome"
CURSOR_SNAPSHOT_TYPE = "v2_fanout_outcome_cursor"
EASTERN = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)
_recorded_at_lock = Lock()
_last_recorded_at: datetime | None = None

PROVISIONAL_OUTCOMES = frozenset({"queued", "could_not_tell", "rejected_unclassified"})
POSITIVE_HOLD_OUTCOMES = frozenset({"submitted", "working", "partially_filled"})
TERMINAL_RELEASE_OUTCOMES = frozenset(
    {
        "dropped_no_emitter",
        "dropped_ineligible",
        "dropped_routing",
        "dropped_risk",
        "dropped_collision",
        "dropped_dedup",
        "rejected_client_abort",
        "rejected_venue",
        "cancelled",
        "expired",
        "strategy_release",
    }
)


def _next_recorded_at() -> datetime:
    """Return a process-monotonic microsecond timestamp for causal replay order.

    ``datetime.now`` can repeat at the platform clock's resolution.  Queue and an immediate local
    terminal can therefore otherwise share ``created_at`` and replay in random UUID order.
    """

    global _last_recorded_at
    with _recorded_at_lock:
        current = datetime.now(UTC)
        if _last_recorded_at is not None and current <= _last_recorded_at:
            current = _last_recorded_at + timedelta(microseconds=1)
        _last_recorded_at = current
        return current


def session_anchor(now: datetime | None = None) -> datetime:
    """Return the current 04:00 ET session boundary in UTC."""

    current = (now or datetime.now(UTC)).astimezone(EASTERN)
    anchor = datetime.combine(current.date(), time(4, 0), tzinfo=EASTERN)
    if current < anchor:
        anchor -= timedelta(days=1)
    return anchor.astimezone(UTC)


@dataclass(frozen=True)
class FanoutOutcome:
    record_id: UUID
    created_at: datetime
    symbol: str
    segment_id: int
    slot: str
    slot_id: str
    attempt_id: str
    outcome: str
    evidence_id: str
    reason: str = ""
    event_source: str = "unknown"
    broker_account_name: str = ""


def identity_from_metadata(metadata: Mapping[str, object] | None) -> dict[str, str] | None:
    md = metadata or {}
    if str(md.get("fanout_leg", "")).strip().lower() != "webull":
        return None
    segment = str(md.get("fanout_segment_id", "")).strip()
    slot = str(md.get("fanout_slot", "")).strip().lower()
    slot_id = str(md.get("fanout_slot_id", "")).strip()
    if not segment or not slot or not slot_id:
        return None
    try:
        if int(segment) <= 0:
            return None
    except ValueError:
        return None
    return {
        "fanout_segment_id": segment,
        "fanout_slot": slot,
        "fanout_slot_id": slot_id,
        "fanout_attempt_id": str(md.get("fanout_attempt_id", "")).strip(),
    }


def broker_outcome(event_type: str, event_source: str) -> str:
    """Map one durable OMS event without inferring a missing provenance."""

    event = str(event_type or "").strip().lower()
    source = str(event_source or "unknown").strip().lower()
    if event == "filled":
        return "filled"
    if event in {"partially_filled", "partial_fill"}:
        return "partially_filled"
    if event in {"accepted", "submitted", "pending", "pending_new", "pending_cancel"}:
        return "submitted"
    if event in {"working", "open", "new"}:
        return "working"
    if event in {"cancelled", "canceled"}:
        return "cancelled"
    if event == "expired":
        return "expired"
    if event == "rejected":
        if source == "client":
            return "rejected_client_abort"
        if source == "broker":
            return "rejected_venue"
        return "rejected_unclassified"
    return "could_not_tell"


def append_outcome(
    session: Session,
    *,
    metadata: Mapping[str, object] | None,
    symbol: str,
    outcome: str,
    evidence_id: str,
    attempt_id: str = "",
    reason: str = "",
    event_source: str = "unknown",
    broker_account_name: str = "",
) -> DashboardSnapshot | None:
    """Append one attributable Webull outcome in the caller's transaction."""

    identity = identity_from_metadata(metadata)
    if identity is None:
        return None
    normalized_attempt = attempt_id or identity["fanout_attempt_id"]
    row = DashboardSnapshot(
        snapshot_type=OUTCOME_SNAPSHOT_TYPE,
        created_at=_next_recorded_at(),
        payload={
            "symbol": str(symbol).upper(),
            "fanout_segment_id": identity["fanout_segment_id"],
            "fanout_slot": identity["fanout_slot"],
            "fanout_slot_id": identity["fanout_slot_id"],
            "fanout_attempt_id": normalized_attempt,
            "outcome": str(outcome),
            "evidence_id": str(evidence_id),
            "reason": str(reason or ""),
            "event_source": str(event_source or "unknown"),
            "broker_account_name": str(broker_account_name or ""),
        },
    )
    session.add(row)
    session.flush()
    return row


def append_outcome_isolated(session: Session, **kwargs: object) -> DashboardSnapshot | None:
    """Append under a savepoint so observability can never abort a money-path transaction."""

    try:
        with session.begin_nested():
            return append_outcome(session, **kwargs)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - the outer trade/fill transaction must remain usable
        logger.exception(
            "[V2-FANOUT-OUTCOME-DROPPED] durable=0 could_not_tell=1 "
            "failure_direction=release_after_grace — outer money-path transaction continues"
        )
        return None


class FanoutOutcomeJournal:
    """Append, replay, and durably cursor Section 82 outcomes."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory
        self._cursor_at: datetime | None = None
        self._cursor_record_id: UUID | None = None

    def record(
        self,
        *,
        metadata: Mapping[str, object] | None,
        symbol: str,
        outcome: str,
        evidence_id: str,
        attempt_id: str = "",
        reason: str = "",
        event_source: str = "client",
        broker_account_name: str = "",
    ) -> bool:
        with self.session_factory() as session:
            row = append_outcome(
                session,
                metadata=metadata,
                symbol=symbol,
                outcome=outcome,
                evidence_id=evidence_id,
                attempt_id=attempt_id,
                reason=reason,
                event_source=event_source,
                broker_account_name=broker_account_name,
            )
            session.commit()
            return row is not None

    def bootstrap(
        self,
        apply: Callable[[FanoutOutcome], object],
        *,
        active_segments: Mapping[str, int],
        now: datetime | None = None,
    ) -> int:
        """Rebuild current active claims before market-data tasks can emit."""

        rows = self._fetch(after=None, after_id=None, anchor=session_anchor(now))
        active = {str(k).upper(): int(v) for k, v in active_segments.items() if int(v) > 0}
        relevant = [row for row in rows if active.get(row.symbol) == row.segment_id]
        for row in relevant:
            apply(row)
        self._advance_cursor(rows)
        return len(relevant)

    def read_pending(self) -> list[FanoutOutcome]:
        """Read committed rows after the in-process cursor without applying them.

        The bot performs this blocking query in a worker thread, then applies the returned
        records on its event-loop thread.  Keeping strategy mutation out of the worker is
        load-bearing: quote/bar callbacks mutate the same ``SymbolState`` objects.
        """

        anchor = session_anchor()
        return self._fetch(
            after=self._cursor_at,
            after_id=self._cursor_record_id,
            anchor=anchor,
        )

    def advance(self, rows: Iterable[FanoutOutcome]) -> int:
        """Durably advance after every returned row has been applied."""

        rows_list = list(rows)
        if not rows_list:
            return 0
        self._advance_cursor(rows_list)
        return len(rows_list)

    def poll(self, apply: Callable[[FanoutOutcome], object]) -> int:
        """Synchronous convenience path used only when the caller owns the state thread."""

        rows = self.read_pending()
        for row in rows:
            apply(row)
        self._advance_cursor(rows)
        return len(rows)

    def _fetch(
        self,
        *,
        after: datetime | None,
        after_id: UUID | None,
        anchor: datetime,
    ) -> list[FanoutOutcome]:
        with self.session_factory() as session:
            stmt = (
                select(DashboardSnapshot)
                .where(DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE)
                .where(DashboardSnapshot.created_at >= anchor)
                .order_by(DashboardSnapshot.created_at, DashboardSnapshot.id)
            )
            if after is not None:
                stmt = stmt.where(
                    or_(
                        DashboardSnapshot.created_at > after,
                        and_(
                            DashboardSnapshot.created_at == after,
                            DashboardSnapshot.id > after_id,
                        ),
                    )
                    if after_id is not None
                    else DashboardSnapshot.created_at > after
                )
            snapshots = session.scalars(stmt).all()
        out: list[FanoutOutcome] = []
        for snapshot in snapshots:
            payload = snapshot.payload or {}
            try:
                segment_id = int(str(payload.get("fanout_segment_id", "0")))
            except ValueError:
                continue
            symbol = str(payload.get("symbol", "")).upper()
            slot_id = str(payload.get("fanout_slot_id", ""))
            if not symbol or segment_id <= 0 or not slot_id:
                continue
            out.append(
                FanoutOutcome(
                    record_id=snapshot.id,
                    created_at=snapshot.created_at,
                    symbol=symbol,
                    segment_id=segment_id,
                    slot=str(payload.get("fanout_slot", "")),
                    slot_id=slot_id,
                    attempt_id=str(payload.get("fanout_attempt_id", "")),
                    outcome=str(payload.get("outcome", "could_not_tell")),
                    evidence_id=str(payload.get("evidence_id", snapshot.id)),
                    reason=str(payload.get("reason", "")),
                    event_source=str(payload.get("event_source", "unknown")),
                    broker_account_name=str(payload.get("broker_account_name", "")),
                )
            )
        return out

    def _advance_cursor(self, rows: Iterable[FanoutOutcome]) -> None:
        rows_list = list(rows)
        if not rows_list:
            return
        last = rows_list[-1]
        with self.session_factory() as session:
            session.add(
                DashboardSnapshot(
                    snapshot_type=CURSOR_SNAPSHOT_TYPE,
                    payload={
                        "last_created_at": last.created_at.isoformat(),
                        "last_record_id": str(last.record_id),
                    },
                )
            )
            session.commit()
        self._cursor_at = last.created_at
        self._cursor_record_id = last.record_id

    @staticmethod
    def local_attempt_id(metadata: Mapping[str, object] | None) -> str:
        identity = identity_from_metadata(metadata)
        if identity and identity["fanout_attempt_id"]:
            return identity["fanout_attempt_id"]
        return f"local-{uuid4()}"


__all__ = [
    "CURSOR_SNAPSHOT_TYPE",
    "OUTCOME_SNAPSHOT_TYPE",
    "POSITIVE_HOLD_OUTCOMES",
    "PROVISIONAL_OUTCOMES",
    "TERMINAL_RELEASE_OUTCOMES",
    "FanoutOutcome",
    "FanoutOutcomeJournal",
    "append_outcome",
    "append_outcome_isolated",
    "broker_outcome",
    "identity_from_metadata",
    "session_anchor",
]
