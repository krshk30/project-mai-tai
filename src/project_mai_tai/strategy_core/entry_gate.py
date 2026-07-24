"""Shared, pure entry emit-gate for schwab_1m_v2.

The ONE implementation of the entry-side emit chokepoint — the entry-window gate
and the extended-hours order routing — called by BOTH the live bot
(``services/schwab_1m_v2_bot.py::_maybe_emit``) and the backtest replay
(``backtest/replay.py``).

Extracted behavior-identically from the bot on 2026-07-24 (Decision 1 of
``docs/backtest-replay-engine-design.md``). The live bot's two helper methods
(``_within_entry_window`` / ``_apply_extended_hours_routing``) now delegate here,
so the replay runs the SAME gate the live code runs — no drift by construction.

Purity: these functions perform no I/O, no DB, no broker calls, and are
deterministic given (draft, now, settings, quote). ``route_extended_hours``
DOES mutate ``draft.metadata`` (that is its whole purpose, exactly as the inline
bot code did) and emits the one legacy skip-warning through an injected logger so
the log line stays byte-identical when called from the bot.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from project_mai_tai.strategy_core.order_routing import (
    _format_limit_price,
    extended_hours_session,
    order_routing_metadata,
)
from project_mai_tai.strategy_core.time_utils import is_fillable_et_session

logger = logging.getLogger(__name__)

# Canonical operator entry-window defaults (ET). SINGLE source of truth — the bot
# imports these so its module-level V2_ENTRY_WINDOW_* names alias exactly one value.
# 07:00–16:00 ET: Phase B start 07:00 (operator 2026-07-24), Phase A end 16:00
# (2026-07-24 — no new entries after the RTH close). Overridable per-setting.
ENTRY_WINDOW_START_HOUR_ET = 7
ENTRY_WINDOW_START_MINUTE_ET = 0
ENTRY_WINDOW_END_HOUR_ET = 16
ENTRY_WINDOW_END_MINUTE_ET = 0


def resolve_entry_window(settings: Any) -> tuple[int, int, int, int]:
    """``(start_hour, start_minute, end_hour, end_minute)`` resolved from Settings with the
    canonical defaults — the exact getattr chain the bot used inline, in one place so the
    check, the bot's log message, and the replay can never disagree on the bounds."""
    return (
        int(getattr(settings, "strategy_schwab_1m_v2_entry_window_start_hour_et", ENTRY_WINDOW_START_HOUR_ET)),
        int(getattr(settings, "strategy_schwab_1m_v2_entry_window_start_minute_et", ENTRY_WINDOW_START_MINUTE_ET)),
        int(getattr(settings, "strategy_schwab_1m_v2_entry_window_end_hour_et", ENTRY_WINDOW_END_HOUR_ET)),
        int(getattr(settings, "strategy_schwab_1m_v2_entry_window_end_minute_et", ENTRY_WINDOW_END_MINUTE_ET)),
    )


def within_entry_window(now: datetime, settings: Any) -> bool:
    """True iff ``now`` falls in the operator entry window: a weekday, non-holiday ET day
    inside ``[start, end)`` (minute granularity). Byte-identical to the bot's former
    ``_within_entry_window`` body (``is_fillable_et_session`` on the resolved bounds)."""
    start_hour, start_minute, end_hour, end_minute = resolve_entry_window(settings)
    return is_fillable_et_session(
        now,
        start_hour,
        end_hour,
        start_minute=start_minute,
        end_minute=end_minute,
    )


def route_extended_hours(
    draft: Any,
    now: datetime,
    quote_lookup: Callable[[str], Any],
    *,
    session_fn: Callable[[datetime], str | None] = extended_hours_session,
    log: logging.Logger = logger,
) -> bool:
    """Extended-hours entry routing — byte-identical extraction of the bot's former
    ``_apply_extended_hours_routing`` body.

    In extended hours, merge ``order_routing_metadata`` (session=AM/PM + order_type=limit +
    limit_price=ask) onto an ``open`` draft so it can fill pre/post-market — the limit price is
    the live ask; if there is no ask quote in extended hours we skip the entry (legacy block).
    RTH is byte-identical (``order_routing_metadata`` returns ``{}`` when the session is regular,
    so the draft is untouched). Returns False only to skip the emit.

    ``quote_lookup(symbol_upper)`` is called ONLY in the EH branch (preserving the bot's exact
    evaluation order); the bot passes ``self._last_quote_by_symbol.get``. ``session_fn`` is
    injected so the bot passes its OWN module binding of ``extended_hours_session`` — keeping the
    existing ``monkeypatch(schwab_1m_v2_bot.extended_hours_session)`` seam live. ``log`` is
    injected so the skip-warning logs under the caller's logger (byte-identical for the bot).
    """
    if getattr(draft, "intent_type", "") != "open":
        return True
    if session_fn(now) is None:
        return True  # regular session — unchanged (market/NORMAL)
    symbol = str(getattr(draft, "symbol", "")).upper()
    side = str(getattr(draft, "side", "buy"))
    quote = quote_lookup(symbol)
    quote_field = "ask_price" if side == "buy" else "bid_price"
    price = _format_limit_price(getattr(quote, quote_field, None)) if quote is not None else None
    if price is None:
        log.warning(
            "schwab_1m_v2 skipping extended-hours %s entry for %s — no %s quote "
            "(mirrors legacy _resolve_routed_price block)",
            side, symbol, quote_field,
        )
        return False
    draft.metadata.update(order_routing_metadata(price=price, side=side, now=now))
    return True


@dataclass(frozen=True)
class EntryGateDecision:
    """Outcome of ``gate_open_intent``. ``emit`` True => send ``draft`` (EH-routed in place).
    ``drop_reason`` is "" on emit, else one of ``entry_window`` / ``atr_only`` / ``eh_no_quote``."""

    emit: bool
    draft: Any
    drop_reason: str


def gate_open_intent(
    draft: Any,
    now: datetime,
    settings: Any,
    quote_lookup: Callable[[str], Any],
    *,
    session_fn: Callable[[datetime], str | None] = extended_hours_session,
    log: logging.Logger = logger,
) -> EntryGateDecision:
    """The full open-intent emit-gate composed from the shared primitives, mirroring the live
    bot's ``_maybe_emit`` open-intent path: (1) entry-window gate, (2) ATR-only belt (drop an
    ``open`` whose reason lacks ``"ATR Flip"`` when ``atr_only_mode``), (3) extended-hours routing.

    The REPLAY runs every strategy-returned draft through this. The bot composes the same
    primitives inline (so its per-method monkeypatch seams survive); a characterization test pins
    this function's outcome against the real ``_maybe_emit`` for RTH / pre-market EH / drop cases.
    """
    if getattr(draft, "intent_type", "") == "open" and not within_entry_window(now, settings):
        return EntryGateDecision(False, draft, "entry_window")
    if bool(getattr(settings, "strategy_schwab_1m_v2_atr_only_mode", False)):
        reason = str(getattr(draft, "reason", ""))
        if getattr(draft, "intent_type", "") == "open" and "ATR Flip" not in reason:
            return EntryGateDecision(False, draft, "atr_only")
    if not route_extended_hours(draft, now, quote_lookup, session_fn=session_fn, log=log):
        return EntryGateDecision(False, draft, "eh_no_quote")
    return EntryGateDecision(True, draft, "")
