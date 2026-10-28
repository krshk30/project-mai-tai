"""Additive FIRSTBAR planning; no live hooks, execution, storage or provider IO.

A candidate is NOT an allocated fill. The execution adapter must preserve share
conservation across accounts/models and report its modeled stop-fill policy.
Reference-study neighbor lookahead is not an entry permission in this planner.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .execution_eligibility import execution_status
from .types import Boundary, EpisodeInput, SourceRef, TradePrint


FIRSTBAR_CONTRACT_VERSION = "ATRSIM_FIRSTBAR_V1"
MINUTE_NS = 60_000_000_000


class FirstBarMode(str, Enum):
    FIRSTBAR = "FIRSTBAR"
    FIRSTBAR_X3 = "FIRSTBAR_X3"


@dataclass(frozen=True)
class CompletedFlipBar:
    symbol: str
    start_ns: int
    available_ns: int
    high: Decimal
    close: Decimal
    line_at_close: Decimal | None
    atr_state_at_close: str | None
    complete: bool | None
    source: SourceRef


@dataclass(frozen=True)
class FirstBarEntryPlan:
    version: str
    mode: FirstBarMode
    decision: str
    trigger: Decimal | None
    eligible_after_ns: int
    end_ns: int
    candidate: TradePrint | None
    requested_quantity: int | None
    bar_source: SourceRef
    issues: tuple[str, ...]


def plan_firstbar_entry(
    episode: EpisodeInput, bar: CompletedFlipBar, *, mode: FirstBarMode,
    entry_cutoff: Boundary,
) -> FirstBarEntryPlan:
    """First eligible later-bar print >= completed flip high+.01, no fill claim.

    Caller supplies the actual session's 15:45 cutoff and causal completed-bar
    evidence. UNKNOWN coverage never becomes a skipped flip or a missing trade.
    FIRSTBAR and FIRSTBAR_X3 share entry planning; only the exit policy differs.
    """
    if not isinstance(mode, FirstBarMode):
        raise ValueError("explicit_firstbar_mode_required")
    if bar.symbol != episode.identity.symbol:
        raise ValueError("flip_bar_symbol_mismatch")
    if bar.available_ns < bar.start_ns + MINUTE_NS:
        raise ValueError("completed_flip_bar_availability_required")
    if episode.buy_flip.bar_start_ns is not None and episode.buy_flip.bar_start_ns != bar.start_ns:
        raise ValueError("flip_bar_identity_mismatch")
    if any(not value.is_finite() or value <= 0 for value in (bar.high, bar.close)):
        raise ValueError("positive_finite_flip_bar_prices_required")
    if bar.close > bar.high:
        raise ValueError("flip_close_above_high")
    if entry_cutoff.available_ns > episode.session_end.available_ns:
        raise ValueError("entry_cutoff_outside_session")
    after = max(bar.available_ns, episode.buy_flip.available_ns)
    end = min(entry_cutoff.available_ns, episode.session_end.available_ns,
              episode.sell_flip.available_ns if episode.sell_flip is not None else episode.session_end.available_ns)
    trigger = bar.high + Decimal("0.01")
    issues = []

    def result(decision, candidate=None):
        return FirstBarEntryPlan(FIRSTBAR_CONTRACT_VERSION, mode, decision, trigger,
            after, end, candidate, episode.quantity, bar.source, tuple(dict.fromkeys(issues)))

    if episode.watched is not True or episode.watched_source is None:
        issues.append("UNKNOWN_OR_NOT_WATCHED")
    if bar.complete is not True:
        issues.append("UNKNOWN_COMPLETED_FLIP_BAR")
    if bar.line_at_close is None or not bar.line_at_close.is_finite() or bar.line_at_close <= 0:
        issues.append("UNKNOWN_FLIP_CLOSE_LINE")
    if (type(episode.quantity) is not int or episode.quantity <= 0
            or episode.sizing_source is None):
        issues.append("UNKNOWN_QUANTITY_OR_SIZING")
    if issues:
        return result("UNKNOWN")
    if bar.close < bar.line_at_close:
        return result("SKIPPED_FALSE_FLIP")
    if bar.atr_state_at_close != "long":
        issues.append("UNKNOWN_OR_CONTRADICTORY_FLIP_CLOSE_STATE")
        return result("UNKNOWN")
    if end <= after:
        return result("SKIPPED_ENTRY_WINDOW_ENDED")
    for label, coverage in (("FLIP", episode.flip_coverage), ("TAPE", episode.tape_coverage)):
        if coverage.complete is not True or coverage.start_ns > bar.start_ns or coverage.end_ns < end:
            issues.append("UNKNOWN_" + label + "_WINDOW")
        issues.extend(coverage.gaps)
    if issues:
        return result("UNKNOWN")
    seen, prior = set(), None
    for trade in episode.trades:
        key = (trade.key.event_ns, trade.key.sequence)
        if trade.symbol != episode.identity.symbol or trade.key.raw_id in seen or (prior is not None and key <= prior):
            raise ValueError("unique_causal_symbol_tape_required")
        if not trade.price.is_finite() or trade.price <= 0 or type(trade.size) is not int or trade.size < 0:
            raise ValueError("valid_print_price_size_required")
        seen.add(trade.key.raw_id)
        prior = key
    for trade in episode.trades:
        if not after <= trade.key.event_ns < end or trade.price < trigger:
            continue
        eligible, _ = execution_status(trade)
        if eligible is None:
            issues.append("UNKNOWN_EXECUTION_FIRSTBAR_ENTRY_ELIGIBILITY")
            continue
        if eligible is True:
            return result("ENTRY_CANDIDATE" if not issues else "UNKNOWN", trade)
    if issues:
        return result("UNKNOWN")
    return result("SKIPPED_NO_BREAK_BEFORE_BOUNDARY")
