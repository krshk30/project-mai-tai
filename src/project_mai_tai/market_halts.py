"""Shared print-gap halt classification for replay and live paper decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

HALT_MIN_PRINT_GAP = timedelta(seconds=285)
HALT_MIN_QUOTE_UPDATES = 2


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class HaltWindow:
    last_print_at: datetime
    reopen_print_at: datetime
    quote_updates: int


def halt_is_confirmed(
    *, last_print_at: datetime, through_at: datetime, quote_updates: int
) -> bool:
    """Apply the one halt definition used by historical and live consumers."""
    return (
        _utc(through_at) - _utc(last_print_at) >= HALT_MIN_PRINT_GAP
        and quote_updates >= HALT_MIN_QUOTE_UPDATES
    )


def confirmed_halt_window(
    *, last_print_at: datetime, reopen_print_at: datetime, quote_updates: int
) -> HaltWindow | None:
    if not halt_is_confirmed(
        last_print_at=last_print_at,
        through_at=reopen_print_at,
        quote_updates=quote_updates,
    ):
        return None
    return HaltWindow(_utc(last_print_at), _utc(reopen_print_at), quote_updates)


def timestamp_is_halted(at: datetime | None, halts: list[HaltWindow]) -> bool:
    return at is not None and any(
        halt.last_print_at < _utc(at) < halt.reopen_print_at for halt in halts
    )


def window_contains_halt(start: datetime, end: datetime, halts: list[HaltWindow]) -> bool:
    start_utc = _utc(start)
    end_utc = _utc(end)
    return any(
        halt.reopen_print_at > start_utc and halt.last_print_at < end_utc for halt in halts
    )


@dataclass(frozen=True)
class HaltQuoteObservation:
    state: Literal["UNKNOWN", "SUSPECTED", "CONFIRMED"]
    newly_confirmed: bool
    last_print_at: datetime | None
    quote_updates: int


class LiveHaltTracker:
    """Classify a print gap incrementally without pretending its end is known."""

    def __init__(self) -> None:
        self.last_print_at: datetime | None = None
        self.quote_updates = 0
        self.confirmed = False

    def observe_quote(self, observed_at: datetime) -> HaltQuoteObservation:
        at = _utc(observed_at)
        if self.last_print_at is None or at <= self.last_print_at:
            return HaltQuoteObservation("UNKNOWN", False, self.last_print_at, self.quote_updates)
        self.quote_updates += 1
        was_confirmed = self.confirmed
        self.confirmed = halt_is_confirmed(
            last_print_at=self.last_print_at,
            through_at=at,
            quote_updates=self.quote_updates,
        )
        return HaltQuoteObservation(
            "CONFIRMED" if self.confirmed else "SUSPECTED",
            self.confirmed and not was_confirmed,
            self.last_print_at,
            self.quote_updates,
        )

    def observe_print(self, observed_at: datetime) -> HaltWindow | None:
        at = _utc(observed_at)
        if self.last_print_at is not None and at <= self.last_print_at:
            return None
        window = (
            confirmed_halt_window(
                last_print_at=self.last_print_at,
                reopen_print_at=at,
                quote_updates=self.quote_updates,
            )
            if self.last_print_at is not None
            else None
        )
        self.last_print_at = at
        self.quote_updates = 0
        self.confirmed = False
        return window
