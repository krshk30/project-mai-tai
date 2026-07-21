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

PROXIMITY_PCTS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
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
    exit_ts: int | None = None          # epoch ms, for per-trade forensics
    signal_prox_pct: float | None = None  # how close to the trail the signal bar closed
    trail_at_signal: float | None = None


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


# ------------------------------------------------- HONEST FILLS (quote-based, 2026-07-21)
#
# The bar-level `_walk_exit` above is an UPPER BOUND, not an estimate: every one of its
# idealizations points the same way.
#   entry at the next bar's OPEN (a trade print)  -> you actually pay the ASK
#   stop fills exactly at -5%                     -> market-on-touch; SOBR filled -13.2%
#   floor fills exactly at the floor              -> same, it walks the book
#   no spread, no latency                         -> measured spreads here are 0.20-0.89%
#
# That matters more than any parameter in this study: the surviving OOS signal is
# +0.353%/trade and the MEDIAN measured spread is ~0.5%. The signal is smaller than one
# spread crossing. This function measures the haircut instead of assuming it.
#
# Model: BUY at the observed ask at (signal bar close + latency). SELL on the observed bid:
# a floor/stop triggers when the bid TOUCHES the level and fills at the NEXT observed bid --
# which is what captures gap slip, the thing the bar-level walk cannot see.


def _quote_at_or_after(quotes, ts, start_idx=0):
    """First quote at/after ts. Returns (idx, quote) or (len, None). Quotes are ascending."""
    i = start_idx
    n = len(quotes)
    while i < n and quotes[i].ts < ts:
        i += 1
    return (i, quotes[i]) if i < n else (n, None)


def simulate_cell_honest(
    bars: list[OracleBar],
    rows: list[dict],
    quotes,
    *,
    symbol: str,
    day: str,
    threshold_pct: float,
    stop_pct: float = -5.0,
    floor_start_pct: float = 2.0,
    latency_s: float = 1.0,
    signal_filter=None,
) -> list[ProxTrade]:
    """Floor-ladder exit with quote-based fills. Same signals as `simulate_cell`; only the
    FILLS differ, so subtracting the two isolates the haircut."""
    from datetime import datetime, timezone

    out: list[ProxTrade] = []
    if not quotes:
        return out

    for idx in find_proximity_signals(rows, threshold_pct):
        if signal_filter is not None and not signal_filter(idx):
            continue
        # Decision is known at the signal bar's CLOSE = bar start + 60s, then latency.
        decide_ts = datetime.fromtimestamp(
            bars[idx].ts / 1000 + 60 + latency_s, tz=timezone.utc
        )
        qi, q = _quote_at_or_after(quotes, decide_ts)
        if q is None or q.ask <= 0:
            continue
        entry = float(q.ask)                     # pay the ask
        stop_lvl = entry * (1 + stop_pct / 100.0)
        hwm = entry
        floor_lvl = None
        exit_px = None
        exit_ts_ms = None
        reason = "EOD"

        # Bar index used only for the ATR flip check; quotes drive the fills.
        bar_j = idx + 1
        for k in range(qi + 1, len(quotes)):
            qq = quotes[k]
            bid = float(qq.bid)
            if bid <= 0:
                continue
            # Advance the bar cursor so FLIP is checked on the right bar.
            while bar_j + 1 < len(bars) and bars[bar_j + 1].ts <= qq.ts.timestamp() * 1000:
                bar_j += 1

            if floor_lvl is not None and bid <= floor_lvl:
                # Market-on-touch: fill at the NEXT observed bid, not the level. This is
                # where gap slip shows up.
                nxt = next((float(x.bid) for x in quotes[k + 1: k + 4] if x.bid > 0), bid)
                exit_px, reason = nxt, "FLOOR"
                exit_ts_ms = qq.ts.timestamp() * 1000
                break
            if floor_lvl is None and bid <= stop_lvl:
                nxt = next((float(x.bid) for x in quotes[k + 1: k + 4] if x.bid > 0), bid)
                exit_px, reason = nxt, "STOP"
                exit_ts_ms = qq.ts.timestamp() * 1000
                break

            hwm = max(hwm, bid)
            gain = (hwm - entry) / entry * 100.0
            if gain >= floor_start_pct:
                step = max(floor_start_pct, float(int(gain)))
                lvl = entry * (1 + step / 100.0)
                floor_lvl = lvl if floor_lvl is None else max(floor_lvl, lvl)

            if bar_j < len(rows) and rows[bar_j].get("flip") == "SELL":
                exit_px, reason = bid, "FLIP"
                exit_ts_ms = qq.ts.timestamp() * 1000
                break

        if exit_px is None:
            last = next((float(x.bid) for x in reversed(quotes) if x.bid > 0), entry)
            exit_px, reason = last, "EOD"

        _tr = rows[idx].get("trail")
        _cl = rows[idx].get("close")
        out.append(ProxTrade(
            symbol=symbol, day=day, entry_ts=bars[idx].ts, entry_price=entry,
            exit_price=exit_px, pnl_pct=(exit_px - entry) / entry * 100.0,
            reason=reason, proximity_pct=threshold_pct, fill_mode="honest",
            exit_ts=int(exit_ts_ms) if exit_ts_ms else None,
            signal_prox_pct=((_tr - _cl) / _cl * 100.0) if (_tr and _cl) else None,
            trail_at_signal=_tr,
        ))
    return out


# ------------------------------------------------- RESTING-LIMIT ENTRY (2026-07-21)
#
# WHY. The proximity rule fires on the bar that REACHES the trail -- and that bar is often
# violent (ADVB 12:47 moved +4.83% in one minute; 14:07 moved +2.75%). Filling at that bar's
# close means buying AFTER the move: a chase. The operator's diagnosis, and the tape agrees.
#
# THE FIX UNDER TEST. Do not react to the bar. Rest a BUY LIMIT at trail*(1-X%) and let price
# come to you. Filled ONLY if the market actually trades there (ask <= level), at the level.
# The resting order is re-priced every bar as the trail moves -- i.e. broker-side replace,
# the same primitive the OCO bracket work proved on 07-20.
#
# THE COST THIS MUST ACCOUNT FOR. A resting order that never fills is not free: the setups it
# misses include the ones that CROSSED (the winners). So every unfilled short segment is
# classified, and the misses are reported alongside the fills. A resting-entry backtest that
# only counts fills is a lie by omission.
#
# WINDOW. Entries are gated to the LIVE v2 window (07:00-16:30 ET). The prior study had no
# window filter and 10 of 15 trades on 2026-07-20 fired outside it -- unfillable in production.

ENTRY_WINDOW_START_ET = (7, 0)
ENTRY_WINDOW_END_ET = (16, 30)


def _in_entry_window(ts_ms: int) -> bool:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    t = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone(ZoneInfo("America/New_York"))
    mins = t.hour * 60 + t.minute
    return (ENTRY_WINDOW_START_ET[0] * 60 + ENTRY_WINDOW_START_ET[1]) <= mins <= \
           (ENTRY_WINDOW_END_ET[0] * 60 + ENTRY_WINDOW_END_ET[1])


def simulate_resting_entry(
    bars: list[OracleBar],
    rows: list[dict],
    quotes,
    *,
    symbol: str,
    day: str,
    offset_pct: float,
    stop_pct: float = -5.0,
    floor_start_pct: float = 2.0,
) -> tuple[list[ProxTrade], dict]:
    """Resting BUY LIMIT at trail*(1-offset_pct%), one fill per short segment.

    Returns (trades, accounting) where accounting counts what the resting order MISSED:
      missed_cross    -- segment ended in a BUY flip we never filled = a potential winner missed
      avoided_no_cross-- segment ended with no cross and no fill = a loser correctly avoided
    """
    trades: list[ProxTrade] = []
    acct = {"segments": 0, "filled": 0, "missed_cross": 0, "avoided_no_cross": 0,
            "out_of_window": 0}
    if not quotes:
        return trades, acct

    qi = 0
    seg_open = False
    seg_filled = False
    i = 0
    n = len(bars)
    while i < n:
        r = rows[i]
        state = r.get("state")
        if state == "short":
            if not seg_open:
                seg_open, seg_filled = True, False
                acct["segments"] += 1
            trail = r.get("trail")
            if trail and not seg_filled:
                if not _in_entry_window(bars[i].ts):
                    acct["out_of_window"] += 1
                else:
                    level = float(trail) * (1.0 - offset_pct / 100.0)
                    bar_end_ms = bars[i].ts + 60000
                    while qi < len(quotes) and quotes[qi].ts.timestamp() * 1000 < bars[i].ts:
                        qi += 1
                    k = qi
                    # BUY STOP, not a buy limit. The trail sits ABOVE price while short, so
                    # trail*(1-X%) is usually ABOVE the ask -- a buy LIMIT there would be
                    # marketable and fill instantly at a worse price (it did: -9.3% stop fills
                    # and a 2.9% win rate, the tell). A buy STOP triggers as price RISES to the
                    # level, which is the "get in during the run-up" behaviour we want, and it
                    # is the same primitive the OCO work proved live (trigger must be > market
                    # at placement -- STOP_PRICE_MUST_BE_GREATER_THAN_MARKET).
                    armed = False
                    while k < len(quotes) and quotes[k].ts.timestamp() * 1000 < bar_end_ms:
                        ask_now = float(quotes[k].ask)
                        if ask_now <= 0:
                            k += 1
                            continue
                        if not armed:
                            # Only a level ABOVE the market can rest as a stop.
                            if ask_now < level:
                                armed = True
                            else:
                                break   # already through it: no valid resting placement
                            k += 1
                            continue
                        if ask_now >= level:
                            # Stop triggers -> becomes a market buy: pay the observed ask,
                            # which is >= our level. That gap IS the slippage, not assumed away.
                            entry = ask_now
                            exit_px, reason, exit_ts = _honest_exit_walk(
                                bars, rows, quotes, k, i, entry,
                                stop_pct=stop_pct, floor_start_pct=floor_start_pct)
                            trades.append(ProxTrade(
                                symbol=symbol, day=day, entry_ts=bars[i].ts,
                                entry_price=entry, exit_price=exit_px,
                                pnl_pct=(exit_px - entry) / entry * 100.0,
                                reason=reason, proximity_pct=offset_pct,
                                fill_mode="resting", exit_ts=exit_ts,
                                trail_at_signal=float(trail)))
                            seg_filled = True
                            acct["filled"] += 1
                            break
                        k += 1
        else:
            if seg_open and not seg_filled:
                # Segment ended unfilled. A BUY flip means the cross happened without us.
                if r.get("flip") == "BUY":
                    acct["missed_cross"] += 1
                else:
                    acct["avoided_no_cross"] += 1
            seg_open = False
        i += 1
    if seg_open and not seg_filled:
        acct["avoided_no_cross"] += 1
    return trades, acct


def _honest_exit_walk(bars, rows, quotes, k0, bar_idx, entry, *, stop_pct, floor_start_pct):
    """Shared honest exit: floor ladder on the observed bid, market-on-touch fills."""
    stop_lvl = entry * (1 + stop_pct / 100.0)
    hwm = entry
    floor_lvl = None
    bar_j = bar_idx
    for k in range(k0 + 1, len(quotes)):
        qq = quotes[k]
        bid = float(qq.bid)
        if bid <= 0:
            continue
        while bar_j + 1 < len(bars) and bars[bar_j + 1].ts <= qq.ts.timestamp() * 1000:
            bar_j += 1
        if floor_lvl is not None and bid <= floor_lvl:
            nxt = next((float(x.bid) for x in quotes[k + 1:k + 4] if x.bid > 0), bid)
            return nxt, "FLOOR", int(qq.ts.timestamp() * 1000)
        if floor_lvl is None and bid <= stop_lvl:
            nxt = next((float(x.bid) for x in quotes[k + 1:k + 4] if x.bid > 0), bid)
            return nxt, "STOP", int(qq.ts.timestamp() * 1000)
        hwm = max(hwm, bid)
        gain = (hwm - entry) / entry * 100.0
        if gain >= floor_start_pct:
            step = max(floor_start_pct, float(int(gain)))
            lvl = entry * (1 + step / 100.0)
            floor_lvl = lvl if floor_lvl is None else max(floor_lvl, lvl)
        if bar_j < len(rows) and rows[bar_j].get("flip") == "SELL":
            return bid, "FLIP", int(qq.ts.timestamp() * 1000)
    last = next((float(x.bid) for x in reversed(quotes) if x.bid > 0), entry)
    return last, "EOD", None


def simulate_limit_pullback_entry(
    bars: list[OracleBar],
    rows: list[dict],
    quotes,
    *,
    symbol: str,
    day: str,
    proximity_pct: float = 2.0,
    pullback_pct: float = 1.0,
    stop_pct: float = -5.0,
    floor_start_pct: float = 2.0,
    max_wait_bars: int = 30,
) -> tuple[list[ProxTrade], dict]:
    """CONCEPT 3 -- same signal as the chase, better price demanded.

    Identical trigger to `simulate_cell_honest` (proximity to the trail), but instead of
    buying at the signal bar's close we rest a BUY LIMIT at close*(1-pullback_pct%) and trade
    ONLY if the market comes down to us. This is what "buy it 1-2% lower" actually requires:
    a limit BELOW the market, not the buy-stop of concept 2 (which paid a WORSE price and lost
    ~2pp) .

    The cost is explicit and must be counted: price may never pull back, and the setups that
    run straight up are exactly the winners. Every unfilled signal is classified by whether it
    went on to cross.

    Order rests until the short segment ends (a BUY flip = the cross happened without us) or
    `max_wait_bars`, whichever comes first -- a limit left resting forever is not a strategy.
    """
    trades: list[ProxTrade] = []
    acct = {"signals": 0, "filled": 0, "missed_cross": 0, "no_fill_no_cross": 0,
            "out_of_window": 0}
    if not quotes:
        return trades, acct

    for idx in find_proximity_signals(rows, proximity_pct):
        acct["signals"] += 1
        if not _in_entry_window(bars[idx].ts):
            acct["out_of_window"] += 1
            continue
        close_i = rows[idx].get("close")
        if not close_i:
            continue
        level = float(close_i) * (1.0 - pullback_pct / 100.0)

        # Rest from the signal bar's close until the segment ends or the wait expires.
        start_ms = bars[idx].ts + 60000
        end_bar = min(len(bars) - 1, idx + max_wait_bars)
        for j in range(idx + 1, end_bar + 1):
            if rows[j].get("flip") == "BUY":       # crossed without us
                end_bar = j
                break
        end_ms = bars[end_bar].ts + 60000

        k, _q = _quote_at_or_after(quotes, __import__("datetime").datetime.fromtimestamp(
            start_ms / 1000, __import__("datetime").timezone.utc))
        filled = False
        while k < len(quotes) and quotes[k].ts.timestamp() * 1000 <= end_ms:
            ask = float(quotes[k].ask)
            if 0 < ask <= level:
                # Passive limit: we get OUR price (or better). This is the whole point.
                entry = level
                exit_px, reason, exit_ts = _honest_exit_walk(
                    bars, rows, quotes, k, idx, entry,
                    stop_pct=stop_pct, floor_start_pct=floor_start_pct)
                trades.append(ProxTrade(
                    symbol=symbol, day=day, entry_ts=bars[idx].ts, entry_price=entry,
                    exit_price=exit_px, pnl_pct=(exit_px - entry) / entry * 100.0,
                    reason=reason, proximity_pct=proximity_pct, fill_mode="limit_pullback",
                    exit_ts=exit_ts, trail_at_signal=rows[idx].get("trail")))
                acct["filled"] += 1
                filled = True
                break
            k += 1

        if not filled:
            crossed = any(rows[j].get("flip") == "BUY" for j in range(idx + 1, end_bar + 1))
            if crossed:
                acct["missed_cross"] += 1
            else:
                acct["no_fill_no_cross"] += 1
    return trades, acct
