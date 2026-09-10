"""One-shot confirmation-exit timing shared by the live and paper paths."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping
from uuid import UUID


ONE_MINUTE_MS = 60_000


def confirmation_bar_start_ms(fill_at: datetime, bar_count: int) -> int:
    """Return the start of the Nth full minute bar after the fill's containing bar."""
    if fill_at.tzinfo is None:
        raise ValueError("confirmation fill time must be timezone-aware")
    if bar_count < 1:
        raise ValueError("confirmation bar count must be at least one")
    fill_ms = int(fill_at.astimezone(UTC).timestamp() * 1000)
    containing_bar_ms = (fill_ms // ONE_MINUTE_MS) * ONE_MINUTE_MS
    return containing_bar_ms + bar_count * ONE_MINUTE_MS


def is_first_slot_resting(metadata: Mapping[str, object]) -> bool:
    """Use only durable entry stamps; reason strings and arm aliases are not evidence."""
    slot = str(metadata.get("cw_entry_slot", "")).strip().lower()
    variant = str(metadata.get("atr_variant", "")).strip().lower()
    if slot != "first":
        return False
    if variant == "cw-v2-resting":
        return str(metadata.get("resting_entry", "")).strip().lower() == "true"
    return (
        variant == "cw-v2-fanout"
        and str(metadata.get("fanout_leg", "")).strip().lower() == "webull"
        and str(metadata.get("fanout_slot", "")).strip().lower() == "resting"
    )


@dataclass(frozen=True)
class ConfirmationEntry:
    order_id: UUID
    fill_id: UUID
    broker_fill_id: str
    broker_order_id: str
    broker_account_name: str
    symbol: str
    filled_at: datetime
    evaluation_bar_start_ms: int
    confirmation_bars: int
    config_id: UUID | None
    config_effective_at: datetime
    fanout_slot_id: str = ""


@dataclass(frozen=True)
class ConfirmationEvaluation:
    entry: ConfirmationEntry
    bar_start_ms: int
    atr_state: str

    @property
    def should_exit(self) -> bool:
        return self.atr_state != "long"


@dataclass(frozen=True)
class ConfirmationDiscoveryCensus:
    matured: int
    evaluated: int
    missing_opportunities: tuple[str, ...]


def confirmation_discovery_census(
    *,
    entries: Iterable[ConfirmationEntry],
    evaluated_slot_ids: set[str],
    last_live_bar_ms: Mapping[str, int],
) -> ConfirmationDiscoveryCensus:
    """Compare matured logical opportunities with their durable decisions."""
    logical_entries: dict[str, ConfirmationEntry] = {}
    for entry in entries:
        if entry.fanout_slot_id:
            logical_entries.setdefault(entry.fanout_slot_id, entry)
    matured = [
        entry
        for entry in logical_entries.values()
        if last_live_bar_ms.get(entry.symbol.upper(), 0) >= entry.evaluation_bar_start_ms
    ]
    missing = tuple(
        f"{entry.symbol.upper()}:{entry.fanout_slot_id}"
        for entry in matured
        if entry.fanout_slot_id not in evaluated_slot_ids
    )
    return ConfirmationDiscoveryCensus(
        matured=len(matured),
        evaluated=len(matured) - len(missing),
        missing_opportunities=missing,
    )


class ConfirmationExitTracker:
    """Event-loop-owned one-shot registry keyed by the authoritative entry order."""

    def __init__(self) -> None:
        self._pending: dict[str, ConfirmationEntry] = {}
        self._seen: set[str] = set()

    @staticmethod
    def _identity(entry: ConfirmationEntry) -> str:
        return entry.fanout_slot_id or str(entry.order_id)

    def add(
        self,
        entry: ConfirmationEntry,
        *,
        preferred_account_name: str | None = None,
    ) -> bool:
        identity = self._identity(entry)
        if identity in self._seen:
            existing = self._pending.get(identity)
            if (
                existing is not None
                and preferred_account_name
                and entry.broker_account_name == preferred_account_name
                and existing.broker_account_name != preferred_account_name
            ):
                self._pending[identity] = entry
            return False
        self._seen.add(identity)
        self._pending[identity] = entry
        return True

    def evaluate_bar(
        self, *, symbol: str, bar_start_ms: int, atr_state: str | None
    ) -> list[ConfirmationEvaluation]:
        normalized = symbol.upper()
        evaluations: list[ConfirmationEvaluation] = []
        for identity, entry in list(self._pending.items()):
            if entry.symbol.upper() != normalized or entry.evaluation_bar_start_ms != bar_start_ms:
                continue
            self._pending.pop(identity, None)
            evaluations.append(
                ConfirmationEvaluation(
                    entry=entry,
                    bar_start_ms=bar_start_ms,
                    atr_state=str(atr_state or "unknown").lower(),
                )
            )
        return evaluations

    def expire_before(self, *, symbol: str, bar_start_ms: int) -> list[ConfirmationEntry]:
        normalized = symbol.upper()
        expired: list[ConfirmationEntry] = []
        for identity, entry in list(self._pending.items()):
            if (
                entry.symbol.upper() == normalized
                and entry.evaluation_bar_start_ms < bar_start_ms
            ):
                expired.append(entry)
                self._pending.pop(identity, None)
        return expired

    def discard(self, order_id: UUID) -> ConfirmationEntry | None:
        for identity, entry in list(self._pending.items()):
            if entry.order_id == order_id:
                return self._pending.pop(identity)
        return None

    @property
    def pending_count(self) -> int:
        return len(self._pending)
