"""Honest fill model — the anti-optimism component (Option A, operator-confirmed).

Fills at the ASK ~latency after the decision (never assumes price improvement); charges
the real spread (the ask IS the entry price, the bid IS the exit price). The real broker
fill is reported ALONGSIDE separately — the delta is the price-improvement signal, surfaced.
Reads market_capture_quotes (NBBO). Stdlib only.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta

# Fill latency is PER-BROKER, not global — it is the broker's decision->fill time and must be
# VALIDATED against that broker's real fills, never assumed. ORB posts to Webull; v2/ATR posts
# to Schwab (TOS) — two brokers, two latencies. Reusing Webull's ~3s for a Schwab backtest
# would be a guess. Each strategy passes its own broker latency; the engine never hard-codes one.
#   webull (ORB): 3.0s — VALIDATED vs KIDZ 07-06 (submit->fill +2.97s). A point estimate; variable.
#   schwab (v2/ATR): MEASURE decision->fill on real Schwab v2 fills when the engine extends to v2.
BROKER_LATENCY_S: dict[str, float | None] = {"webull": 3.0, "schwab": None}
DEFAULT_LATENCY_S = 3.0   # Webull default (ORB); callers should pass the correct per-broker value
BAR_SECS = 60             # 1-min decision bars; a bar labeled T closes ~T+60s
DEFAULT_TICK = 0.01


class QuoteBook:
    """Prevailing-NBBO lookup: the last quote at-or-before a time."""

    def __init__(self, quotes) -> None:
        self._q = list(quotes)
        self._ts = [q.ts for q in self._q]

    def at(self, t: datetime):
        i = bisect_right(self._ts, t) - 1
        return self._q[i] if i >= 0 else None


def entry_fill(book: QuoteBook, decision_ts: datetime, level: float, gap_cap_pct: float) -> float | None:
    """Honest entry fill: pay the ASK at PLACEMENT (the bar-close decision). The live entry is
    a marketable limit min(ask+tick, level*(1+cap)) placed at the decision; it commits to the
    ask at that instant and completes ~3s later — Option A takes the committed ask, never a
    better price the 3s move might have offered (no improvement) and never a resting-limit
    re-price. Returns None (ABANDON) when that ask is past the gap-cap — the live
    ASK_PAST_GAP_CAP (the limit, bounded by level*(1+cap), can't reach an ask above it)."""
    q = book.at(decision_ts)
    if q is None:
        return None
    if q.ask > level * (1.0 + gap_cap_pct / 100.0):
        return None  # ASK_PAST_GAP_CAP
    return q.ask


def exit_fill(book: QuoteBook, trigger_ts: datetime, *, latency_s: float = DEFAULT_LATENCY_S) -> float | None:
    """Honest exit fill: sell at the BID at trigger+latency (spread paid on the exit too)."""
    q = book.at(trigger_ts + timedelta(seconds=latency_s))
    return q.bid if q is not None else None
