"""Shared print-gap halt classification for replay and live paper decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

HALT_MIN_PRINT_GAP = timedelta(seconds=285)
HALT_MIN_QUOTE_UPDATES = 2

# ET extended session, 04:00-20:00 on weekdays.
_ET = ZoneInfo("America/New_York")
_SESSION_OPEN_MIN = 4 * 60
_SESSION_CLOSE_MIN = 20 * 60


def _in_extended_session(at: datetime) -> bool:
    et = _utc(at).astimezone(_ET)
    if et.weekday() >= 5:
        return False
    return _SESSION_OPEN_MIN <= et.hour * 60 + et.minute < _SESSION_CLOSE_MIN


def session_is_continuous(a: datetime, b: datetime) -> bool:
    """Did the market stay OPEN across the whole span from `a` to `b`?

    ⛔⭐⭐ WHY THIS EXISTS (2026-09-09). `halt_is_confirmed` asks only "has enough time passed with
    quotes still arriving?" — it has no session term. Overnight, quotes keep flowing while no
    trading occurs, so an ~8-hour MARKET CLOSURE satisfies the rule exactly as a halt does. Live
    v2 confirmed a halt on SUNE at 03:59 ET against a last print of 19:59:58 ET the previous
    evening. Nothing was held and no decision was gated, but it paged the operator as the first
    real halt ever seen, which spent a once-only alarm on an artefact.

    ⛔ A time-of-day test is NOT sufficient. The gap accumulates while the market is shut and then
    confirms on the FIRST quote of the new session, which can arrive at 04:01 ET — inside any "is
    it session hours now" window. Both ends must lie in one continuous session.

    ⭐ The precedent already existed elsewhere: strategy_engine_app's symbol-health monitor refuses
    to call a flat symbol halted after trading hours (test_schwab_after_hours_stale_halt.py). That
    guard was simply never applied to this module.
    """
    a_utc, b_utc = _utc(a), _utc(b)
    if not (_in_extended_session(a_utc) and _in_extended_session(b_utc)):
        return False
    return a_utc.astimezone(_ET).date() == b_utc.astimezone(_ET).date()


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
    """Classify a print gap incrementally without pretending its end is known.

    ⛔⭐⭐ `require_continuous_session` DEFAULTS TO FALSE ON PURPOSE. This module's own docstring
    calls it "the one halt definition used by historical and live consumers", and the other
    consumers are research surfaces — `paper_exit.py`, `scripts/actual_resting_operator_rule.py`,
    `scripts/orb_exit_ladder_comparison.py`, `scripts/orb_raw_price_walk.py`. Turning the guard on
    for all of them in one step would silently move historical halt windows and every result
    derived from them, including the live PEX1 measurement. Default-off keeps every existing caller
    byte-identical; the LIVE detector opts in. Widening it to the research path is a separate,
    MEASURED decision, not a side effect of this fix.
    """

    def __init__(self, *, require_continuous_session: bool = False) -> None:
        self.last_print_at: datetime | None = None
        self.quote_updates = 0
        self.confirmed = False
        self.require_continuous_session = require_continuous_session

    def observe_quote(self, observed_at: datetime) -> HaltQuoteObservation:
        at = _utc(observed_at)
        if self.last_print_at is None or at <= self.last_print_at:
            return HaltQuoteObservation("UNKNOWN", False, self.last_print_at, self.quote_updates)
        if self.require_continuous_session and not session_is_continuous(self.last_print_at, at):
            # ⛔ The gap spans a market closure, so the prior print cannot support a halt judgement
            # at all — this is UNKNOWN for the same reason "no usable prior print" is: we have
            # nothing to judge, rather than something we judged to be fine. Deliberately does NOT
            # touch `quote_updates` or `confirmed`: an overnight gap must not accumulate evidence
            # that then confirms the moment the new session opens.
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
