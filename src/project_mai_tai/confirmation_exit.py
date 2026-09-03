"""One-shot confirmation-exit timing shared by the live and paper paths."""

from __future__ import annotations

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
    resting = str(metadata.get("resting_entry", "")).strip().lower()
    return slot == "first" and variant == "cw-v2-resting" and resting == "true"


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


@dataclass(frozen=True)
class ConfirmationEvaluation:
    entry: ConfirmationEntry
    bar_start_ms: int
    atr_state: str

    @property
    def should_exit(self) -> bool:
        return self.atr_state != "long"


class ConfirmationExitTracker:
    """Event-loop-owned one-shot registry keyed by the authoritative entry order."""

    def __init__(self) -> None:
        self._pending: dict[UUID, ConfirmationEntry] = {}
        self._seen: set[UUID] = set()

    def add(self, entry: ConfirmationEntry) -> bool:
        if entry.order_id in self._seen:
            return False
        self._seen.add(entry.order_id)
        self._pending[entry.order_id] = entry
        return True

    def evaluate_bar(
        self, *, symbol: str, bar_start_ms: int, atr_state: str | None
    ) -> list[ConfirmationEvaluation]:
        normalized = symbol.upper()
        evaluations: list[ConfirmationEvaluation] = []
        for order_id, entry in list(self._pending.items()):
            if entry.symbol.upper() != normalized or entry.evaluation_bar_start_ms != bar_start_ms:
                continue
            self._pending.pop(order_id, None)
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
        for order_id, entry in list(self._pending.items()):
            if (
                entry.symbol.upper() == normalized
                and entry.evaluation_bar_start_ms < bar_start_ms
            ):
                expired.append(entry)
                self._pending.pop(order_id, None)
        return expired

    def discard(self, order_id: UUID) -> ConfirmationEntry | None:
        return self._pending.pop(order_id, None)

    @property
    def pending_count(self) -> int:
        return len(self._pending)
