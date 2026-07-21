"""ATR-PROXIMITY anticipatory entry sweep (R&D, operator request 2026-07-21).

THE RULE UNDER TEST. Today's CW entry waits for the ATR trail to flip long, then 3 bars, then
a break of the 3-bar high -- it buys CONFIRMATION. This buys ANTICIPATION: while the trail is
still SHORT (the purple dots sit ABOVE price), enter when a bar CLOSES within X% below the
trail. One entry per short-segment. Exits unchanged (the v2 ladder, tape-level bid fills).

    proximity = (trail - close) / close      # close is BELOW the trail, pre-cross
    signal    = state == "short" and 0 <= proximity <= X

X sweep = 0.5% / 1.0% / 1.5% (operator-chosen).

TWO FILL VARIANTS, and the gap between them is the point:
  same_bar  = fill at the SIGNAL BAR'S CLOSE. Optimistic: the condition is only KNOWN once
              that bar closes, so filling at that same print is the idealized-fill flavour
              that inflated the ORB "+11.2". Treat as an UPPER BOUND, never the headline.
  next_open = fill at the NEXT bar's open. The honest one. Headline.
The next_open-minus-same_bar gap answers the operator's "does it go below if it doesn't
break?" -- negative gap = waiting a bar is a discount, positive = a tax.

Universe/window: scanner_confirmed_events CONFIRM -> (FADE | RETENTION_DROP), so entries are
only taken while the scanner actually had the name confirmed -- the same gating as the 07-17
studies, which is what makes this comparable to them.

Reporting discipline (non-negotiable, see the percentages-not-dollars rule): per-trade %,
MEDIAN-FIRST, drop-one BY NAME, cells-searched declared. Never a bare dollar total.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from project_mai_tai.backtest.atr_oracle import Bar as OracleBar
from project_mai_tai.backtest.atr_oracle import compute_atr_trail

PROXIMITY_PCTS = (0.5, 1.0, 1.5)
FILL_MODES = ("same_bar", "next_open")


@dataclass
class ProxTrade:
    symbol: str
    day: str
    entry_ts: int
    entry_price: float
    exit_price: float
    pnl_pct: float
    reason: str
    proximity_pct: float
    fill_mode: str


@dataclass
class Cell:
    """One (threshold, fill_mode) cell of the sweep."""
    proximity_pct: float
    fill_mode: str
    trades: list[ProxTrade] = field(default_factory=list)

    @property
    def pcts(self) -> list[float]:
        return [t.pnl_pct for t in self.trades]

    def summary(self) -> dict:
        p = self.pcts
        if not p:
            return {"n": 0}
        wins = [x for x in p if x > 0]
        med = statistics.median(p)
        mean = statistics.fmean(p)
        sd = statistics.pstdev(p) if len(p) > 1 else 0.0
        # 95% CI on the MEAN (normal approx). Reported so a positive mean with a
        # CI straddling zero cannot be read as an edge.
        half = 1.96 * sd / (len(p) ** 0.5) if len(p) > 1 else 0.0
        return {
            "n": len(p),
            "names": len({t.symbol for t in self.trades}),
            "median_pct": med,
            "mean_pct": mean,
            "win_rate": 100.0 * len(wins) / len(p),
            "ci_lo": mean - half,
            "ci_hi": mean + half,
            "ci_excludes_zero": (mean - half > 0) or (mean + half < 0),
        }

    def drop_one_by_name(self) -> list[tuple[str, float, float]]:
        """Recompute median/mean with each NAME removed. A conclusion that flips when one
        symbol leaves is a story about that symbol, not about the rule."""
        names = sorted({t.symbol for t in self.trades})
        out = []
        for nm in names:
            rest = [t.pnl_pct for t in self.trades if t.symbol != nm]
            if rest:
                out.append((nm, statistics.median(rest), statistics.fmean(rest)))
        return out


def find_proximity_signals(rows: list[dict], threshold_pct: float) -> list[int]:
    """Bar indices where the rule fires. ONE per short-segment.

    A segment is a contiguous run of state == 'short'. It is consumed by the first bar that
    satisfies proximity; a new segment starts at the next SELL flip (long -> short).
    """
    signals: list[int] = []
    segment_claimed = False
    prev_state = None
    for i, r in enumerate(rows):
        state, trail, close = r.get("state"), r.get("trail"), r.get("close")
        if state != prev_state:
            # Entering a fresh short segment releases the claim.
            if state == "short":
                segment_claimed = False
            prev_state = state
        if state != "short" or trail is None or not close:
            continue
        if segment_claimed:
            continue
        prox = (trail - close) / close * 100.0
        if 0.0 <= prox <= threshold_pct:
            signals.append(i)
            segment_claimed = True
    return signals


def _walk_exit(
    bars: list[OracleBar], rows: list[dict], entry_idx: int, entry: float,
    *, exit_mode: str, target_pct: float, stop_pct: float, trail_pct: float,
    floor_start_pct: float = 2.0,
) -> tuple[float, str]:
    """Bar-level exit walk. Returns (exit_price, reason).

    exit_mode:
      target      -- hard take-profit at +target_pct (the incumbent CW geometry)
      floor_ladder-- NO hard target: once the high reaches +2%, a floor is set at the whole
                     percent reached (2,3,4...) and ratchets up 1% at a time. Exit when the
                     bar's LOW falls back to the floor. Lets a runner run; the floor locks
                     what it already gave.
      trail2      -- NO hard target: trail `trail_pct` below the high-water mark once in
                     profit. Exit when the LOW touches the trail.

    In ALL modes the -5% initial stop stays live until the floor/trail takes over, and
    stop-before-target precedence holds within a bar (pessimistic: a bar that spans both
    books the loss -- assuming the good fill is the easiest way to fake an edge here).
    """
    stop = entry * (1 + stop_pct / 100.0)
    target = entry * (1 + target_pct / 100.0)
    hwm = entry
    floor_level: float | None = None

    for j in range(entry_idx + 1, len(bars)):
        b = bars[j]
        low, high, close = float(b.low), float(b.high), float(b.close)

        # 1. Protective levels first (pessimistic precedence).
        if floor_level is not None and low <= floor_level:
            return floor_level, "FLOOR"
        if floor_level is None and low <= stop:
            return stop, "STOP"

        # 2. Take-profit / ratchet.
        if exit_mode == "target":
            if high >= target:
                return target, "TARGET"
        elif exit_mode == "floor_ladder":
            hwm = max(hwm, high)
            gain_pct = (hwm - entry) / entry * 100.0
            if gain_pct >= floor_start_pct:
                # First floor at floor_start_pct; thereafter ratchet 1% at a time.
                step = max(floor_start_pct, float(int(gain_pct)))
                lvl = entry * (1 + step / 100.0)
                floor_level = lvl if floor_level is None else max(floor_level, lvl)
        elif exit_mode == "trail2":
            hwm = max(hwm, high)
            if hwm > entry:
                lvl = hwm * (1 - trail_pct / 100.0)
                if lvl > entry * (1 + stop_pct / 100.0):
                    floor_level = lvl if floor_level is None else max(floor_level, lvl)

        # 3. ATR flip to short ends it.
        if rows[j].get("flip") == "SELL":
            return close, "FLIP"

    return float(bars[-1].close), "EOD"


def simulate_cell(
    bars: list[OracleBar],
    rows: list[dict],
    *,
    symbol: str,
    day: str,
    threshold_pct: float,
    fill_mode: str,
    target_pct: float = 2.0,
    stop_pct: float = -5.0,
    exit_mode: str = "target",
    trail_pct: float = 2.0,
    floor_start_pct: float = 2.0,
    signal_filter=None,
) -> list[ProxTrade]:
    """Bar-level exit walk: +2% target / -5% stop / ATR flip-to-short, first-touch wins.

    Intrabar precedence: if a bar's LOW breaches the stop AND its HIGH reaches the target,
    the STOP is taken. Pessimistic by construction -- the alternative (assuming the good
    fill) is the single easiest way to manufacture a fake edge in a bar-level walk.
    """
    trades: list[ProxTrade] = []
    for idx in find_proximity_signals(rows, threshold_pct):
        # The confirmation filter is evaluated on the SIGNAL bar (what we'd know at decision
        # time), never on the fill bar -- using the fill bar would be lookahead.
        if signal_filter is not None and not signal_filter(idx):
            continue
        if fill_mode == "same_bar":
            entry_idx, entry = idx, float(bars[idx].close)
        else:
            if idx + 1 >= len(bars):
                continue
            entry_idx, entry = idx + 1, float(bars[idx + 1].open)
        if entry <= 0:
            continue

        exit_price, reason = _walk_exit(
            bars, rows, entry_idx, entry, exit_mode=exit_mode,
            target_pct=target_pct, stop_pct=stop_pct, trail_pct=trail_pct,
            floor_start_pct=floor_start_pct,
        )

        trades.append(ProxTrade(
            symbol=symbol, day=day, entry_ts=bars[entry_idx].ts,
            entry_price=entry, exit_price=exit_price,
            pnl_pct=(exit_price - entry) / entry * 100.0,
            reason=reason, proximity_pct=threshold_pct, fill_mode=fill_mode,
        ))
    return trades


def to_oracle_bars(orb_bars) -> list[OracleBar]:
    """OrbBar (live aggregator output) -> OracleBar (what compute_atr_trail eats).

    OrbBar carries `timestamp` as a datetime; the oracle wants bar-start epoch ms.
    """
    return [
        OracleBar(
            ts=int(b.timestamp.timestamp() * 1000),
            open=float(b.open), high=float(b.high), low=float(b.low),
            close=float(b.close), volume=int(b.volume),
        )
        for b in orb_bars
    ]


def confirmed_windows(session, days: int) -> list[tuple[str, datetime, datetime]]:
    """(symbol, confirm_at, drop_at) from scanner_confirmed_events.

    A CONFIRM with no later FADE/RETENTION_DROP that day is held to the session end -- the
    name never faded, so the window is genuinely open, not missing data.
    """
    from sqlalchemy import text

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(text("""
        SELECT symbol, event_type, event_at
        FROM scanner_confirmed_events
        WHERE event_at >= :since
        ORDER BY symbol, event_at
    """), {"since": since}).all()

    out: list[tuple[str, datetime, datetime]] = []
    open_confirm: dict[str, datetime] = {}
    for symbol, event_type, event_at in rows:
        if event_type == "CONFIRM":
            open_confirm.setdefault(symbol, event_at)
        elif symbol in open_confirm:
            out.append((symbol, open_confirm.pop(symbol), event_at))
    for symbol, confirm_at in open_confirm.items():
        out.append((symbol, confirm_at, confirm_at + timedelta(hours=8)))
    return out
