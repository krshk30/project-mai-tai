"""ORB bar-close trade simulator (Components 3+4) — re-entry/counting + trail exit.

Turns the running-high breaks (Component 2) into TRADES: enter (honest fill) -> hold ->
3% trail exit -> re-enter when flat. Two counting policies:
  - capped=True  (LIVE behavior): emit on each gap-ok break, attempts++ (incl. an emit the
    OMS then abandons), cap at _ENTRY_ATTEMPT_CAP=2 — reproduces the real broker fills
    (KIDZ: attempt1 abandon, attempt2 fill, 09:53 suppressed → 1 trade).
  - capped=False (EDGE study): no cap; enter on every gap-ok break when flat — reproduces
    the operator's 15-name-day table (CELZ BC ~5 trades).

The trail mirrors the live OMS (`_ratcheted_trailing_stop`, oms/service.py:2343): HWM seeded
at the FILL price (NOT the bar high — the fake-win-bug guard), bid-only ratchet, stop only
rises, trigger when bid <= stop. Exit fills at the bid ~3s after the trigger (honest).
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta

from project_mai_tai.backtest.fill import BAR_SECS, BROKER_LATENCY_S, QuoteBook, entry_fill, exit_fill
from project_mai_tai.backtest.orb_entry import RunningHighTracker
from project_mai_tai.strategy_core.orb_tick_entry import OrbTickEntry

ENTRY_ATTEMPT_CAP = 2  # live orb_app.py:141

# MEASURED Webull decision->fill latency band (real ORB fills, RTH, outliers excluded — the
# AZI off-hours plumbing test 302/601s and a JEM afternoon-hold 17576s are not fill latencies).
# Liquidity-dependent: liquid ~1-3s (IVF 0.9, KIDZ 3.0), thin ~10-14s (JEM 10.2, CANF 10.9,
# SDOT 13.5). Latency is NOT a point — every result is reported across this band. Schwab (v2)
# gets its OWN measured band before v2 backtests are trusted (do NOT reuse Webull's).
WEBULL_LATENCY_BAND_S = (3.0, 6.0, 10.0, 14.0)


def _ratcheted_trailing_stop(stop_price, hwm, observed, trail_pct):
    """Mirror of OmsRiskService._ratcheted_trailing_stop (oms/service.py:2343-2351).
    Stop only ever rises; HWM advances to a new observed high. Inert if trail_pct<=0."""
    if trail_pct <= 0 or observed <= hwm:
        return stop_price, hwm
    candidate = observed * (1.0 - trail_pct / 100.0)
    return (candidate if candidate > stop_price else stop_price), observed


@dataclass(frozen=True)
class Trade:
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime | None
    exit_price: float | None
    qty: int
    pnl: float
    exit_reason: str
    level: float | None = None   # the running-high that was broken (informational, for validation)


def _run_trail_exit(quotes, start_idx, fill_price, trail_pct, book, latency_s, hard_stop_pct=None):
    """Walk the bid path from the fill forward; return (exit_ts, exit_price, reason, next_idx).
    HWM seeded at fill_price (the fake-win guard). Stop = fill*(1-trail%); ratchet on bid.
    The exit is a stop that fires at the trigger and fills `latency_s` later at the then-bid
    (honest exit slippage — the broker's real decision->fill time).

    hard_stop_pct (research): a FIXED loss floor beneath the trail. The effective trigger stop is
    max(trailing_stop, fill*(1-hard%)), so an immediate fade exits at the hard floor while a runner
    (trail ratcheted above the floor) is unaffected. None = live behavior (byte-identical)."""
    hwm = fill_price
    stop = fill_price * (1.0 - trail_pct / 100.0)
    hard_floor = fill_price * (1.0 - hard_stop_pct / 100.0) if hard_stop_pct else None
    i = start_idx
    n = len(quotes)
    while i < n:
        q = quotes[i]
        stop, hwm = _ratcheted_trailing_stop(stop, hwm, q.bid, trail_pct)
        eff = stop if hard_floor is None else max(stop, hard_floor)
        if q.bid <= eff:
            xfill = exit_fill(book, q.ts, latency_s=latency_s)
            reason = "HARD_STOP" if (hard_floor is not None and eff == hard_floor and stop < hard_floor) else "TRAIL_STOP"
            return q.ts, (xfill if xfill is not None else q.bid), reason, i
        i += 1
    # window ended with no stop hit -> exit at the last bid (WINDOW_END)
    if quotes:
        last = quotes[-1]
        return last.ts, last.bid, "WINDOW_END", n
    return None, None, "NO_QUOTES", n


def _ts_in_windows(ts, windows):
    """True if ts falls inside any (start,end) confirmed interval. None = no restriction."""
    return windows is None or any(a <= ts <= b for a, b in windows)


def simulate_bar_close(bars, quotes, *, gap_cap_pct, trail_pct, qty,
                       observe_open, session_open, cutoff, capped,
                       latency_s=BROKER_LATENCY_S["webull"], hard_stop_pct=None, entry_windows=None):
    """Return list[Trade]. `quotes` must be sorted by ts. `latency_s` is the PER-BROKER
    decision->fill latency (ORB=Webull ~3s; v2 must pass Schwab's measured value).
    hard_stop_pct (research): fixed loss floor beneath the trail (None = live).
    entry_windows (research): only enter while the name is scanner-CONFIRMED (None = no gate)."""
    book = QuoteBook(quotes)
    tracker = RunningHighTracker(
        observe_open=observe_open, session_open=session_open, cutoff=cutoff, gap_cap_pct=gap_cap_pct
    )
    trades: list[Trade] = []
    attempts = 0
    flat_after: datetime | None = None   # can only (re)enter once decision_ts >= this
    for bar in bars:
        brk = tracker.on_bar(bar)
        if brk is None or not brk.gap_ok:
            continue
        decision_ts = bar.timestamp + timedelta(seconds=BAR_SECS)   # bar close
        if not _ts_in_windows(decision_ts, entry_windows):
            continue  # name not scanner-confirmed at the breakout -> untradeable
        if flat_after is not None and decision_ts < flat_after:
            continue  # still holding a prior position
        if capped and attempts >= ENTRY_ATTEMPT_CAP:
            continue  # live 2-attempt cap reached
        attempts += 1  # EMIT (matches live: attempts++ on emit, incl. one the OMS abandons)
        fill = entry_fill(book, decision_ts, brk.level, gap_cap_pct)
        if fill is None:
            continue  # ASK_PAST_GAP_CAP abandon — attempt consumed, stay flat
        fill_ts = decision_ts + timedelta(seconds=latency_s)   # fill completes latency later
        # exit walk from the first quote at/after the fill
        start = bisect_left(book._ts, fill_ts)
        xts, xprice, xreason, _ = _run_trail_exit(quotes, start, fill, trail_pct, book, latency_s, hard_stop_pct)
        pnl = (xprice - fill) * qty if xprice is not None else 0.0
        trades.append(Trade(fill_ts, fill, xts, xprice, qty, pnl, xreason, brk.level))
        flat_after = xts
    return trades


def simulate_intrabar(trades, quotes, *, gap_cap_pct, trail_pct, qty,
                      observe_open, session_open, cutoff, capped,
                      latency_s=BROKER_LATENCY_S["webull"], hard_stop_pct=None, entry_windows=None):
    """INTRABAR mode — the strategy the operator actually wants, now honestly testable.

    CONTINUOUS running-high: the level advances every TRADE TICK (`running_high = max(rh,
    price)`), so a stale bar-lagged level can NEVER be re-crossed — this is the real CELZ-bug
    fix (the phantom 93 came from per-tick evaluation against a level that only updated at bar
    close). A break = while FLAT, a tick makes a NEW session high (price > running_high);
    entry is intrabar (not waiting for bar close), filled honestly (ask at the break tick +
    per-broker latency). Trail exit identical to bar-close. After an exit, running_high sits at
    the peak reached during the hold, so a re-entry needs a genuinely higher high.

    `trades` and `quotes` sorted by ts. capped=True applies the live 2-attempt cap (upper-bound
    reclaim); capped=False = thesis (all genuine new-high breaks)."""
    book = QuoteBook(quotes)
    running_high: float | None = None
    out: list[Trade] = []
    attempts = 0
    i, n = 0, len(trades)
    while i < n:
        t = trades[i]
        if t.ts < observe_open:
            i += 1
            continue
        if running_high is None:
            running_high = t.price
            i += 1
            continue
        in_window = session_open <= t.ts <= cutoff and _ts_in_windows(t.ts, entry_windows)
        if in_window and t.price > running_high and not (capped and attempts >= ENTRY_ATTEMPT_CAP):
            level = running_high                       # the prior high being broken
            attempts += 1                              # EMIT (intrabar break)
            fill = entry_fill(book, t.ts, level, gap_cap_pct)   # ask at the break tick (placement)
            if fill is not None:
                fill_ts = t.ts + timedelta(seconds=latency_s)
                start = bisect_left(book._ts, fill_ts)
                xts, xprice, xreason, _ = _run_trail_exit(quotes, start, fill, trail_pct, book, latency_s, hard_stop_pct)
                pnl = (xprice - fill) * qty if xprice is not None else 0.0
                out.append(Trade(fill_ts, fill, xts, xprice, qty, pnl, xreason, level))
                # advance running_high through the hold; resume after the exit (can't re-enter
                # while holding). running_high sitting at the hold's peak => re-entry needs a new high.
                while i < n and (xts is None or trades[i].ts <= xts):
                    running_high = max(running_high, trades[i].price)
                    i += 1
                continue
            # abandon (ASK_PAST_GAP_CAP): stay flat, advance rh past this tick
        running_high = max(running_high, t.price)
        i += 1
    return out


def simulate_orb_tick_entry(trades, quotes, *, gap_cap_pct, trail_pct, qty,
                            observe_open, session_open, cutoff, capped,
                            latency_s=BROKER_LATENCY_S["webull"], hard_stop_pct=None,
                            entry_windows=None, atr_gate_pct=None, bars=None):
    """Backtest driver that runs the PRODUCTION `OrbTickEntry` engine for the entry decision, so the
    back-test validates the real code path (the same engine the live orb_app.py tick handler uses).
    Exit = the same `_run_trail_exit` 2% ratcheting trail. `bars` (closed 1-min OrbBars) feed the
    causal high-ATR gate. With atr_gate_pct=None and bars=None this is TRADE-IDENTICAL to
    `simulate_intrabar` (parity-pinned by tests/backtest/test_orb_tick_entry.py)."""
    book = QuoteBook(quotes)
    engine = OrbTickEntry(observe_open=observe_open, session_open=session_open,
                          cutoff=cutoff, atr_gate_pct=atr_gate_pct)
    bar_iter = iter(bars or [])
    next_bar = next(bar_iter, None)
    out: list[Trade] = []
    attempts = 0
    i, n = 0, len(trades)
    while i < n:
        t = trades[i]
        # causal: feed only bars that have CLOSED at/before this tick
        while next_bar is not None and next_bar.timestamp + timedelta(seconds=BAR_SECS) <= t.ts:
            engine.observe_bar(next_bar)
            next_bar = next(bar_iter, None)
        level = engine.observe_tick(t.ts, t.price)
        if (level is not None and (entry_windows is None or any(a <= t.ts <= b for a, b in entry_windows))
                and not (capped and attempts >= ENTRY_ATTEMPT_CAP)):
            attempts += 1
            fill = entry_fill(book, t.ts, level, gap_cap_pct)
            if fill is not None:
                fill_ts = t.ts + timedelta(seconds=latency_s)
                start = bisect_left(book._ts, fill_ts)
                xts, xprice, xreason, _ = _run_trail_exit(quotes, start, fill, trail_pct, book, latency_s, hard_stop_pct)
                pnl = (xprice - fill) * qty if xprice is not None else 0.0
                out.append(Trade(fill_ts, fill, xts, xprice, qty, pnl, xreason, level))
                while i < n and (xts is None or trades[i].ts <= xts):
                    engine.advance(trades[i].price)
                    i += 1
                continue
        i += 1
    return out


def simulate_intrabar_v2(trades, quotes, *, gap_cap_pct, trail_pct, qty,
                         observe_open, session_open, cutoff, capped,
                         latency_s=BROKER_LATENCY_S["webull"]):
    """INDEPENDENT 2nd intrabar implementation for the PARITY CHECK (design principle #5).

    Deliberately a DIFFERENT structure from simulate_intrabar: one MERGED time-ordered event
    stream through an explicit FLAT/HOLDING state machine (vs impl-1's trade-index walk with
    hold-skip). Same modeled behavior; if the two agree, that's strong evidence neither has a
    silent bug — intrabar's substitute for the missing real-fill anchor. NOT for production
    runs (rebuilds/sorts the merged stream); parity only."""
    book = QuoteBook(quotes)
    # merged stream: trades tagged 0, quotes tagged 1 -> stable sort puts a trade before a
    # quote at the same ts (matches impl-1: running_high/break updates before the exit walk).
    events = [(t.ts, 0, t.price, 0.0) for t in trades] + [(q.ts, 1, q.bid, q.ask) for q in quotes]
    events.sort(key=lambda e: (e[0], e[1]))
    running_high: float | None = None
    holding = False
    attempts = 0
    out: list[Trade] = []
    fill_price = 0.0
    fill_ts = None
    stop = hwm = 0.0
    last_bid = None
    for ts, kind, x, y in events:
        if ts < observe_open:
            continue
        if kind == 0:  # trade
            price = x
            if running_high is None:
                running_high = price
                continue
            if (not holding and session_open <= ts <= cutoff and price > running_high
                    and not (capped and attempts >= ENTRY_ATTEMPT_CAP)):
                level = running_high
                attempts += 1
                fp = entry_fill(book, ts, level, gap_cap_pct)
                if fp is not None:
                    holding = True
                    fill_price = fp
                    fill_ts = ts + timedelta(seconds=latency_s)
                    stop = fp * (1.0 - trail_pct / 100.0)
                    hwm = fp
            running_high = max(running_high, price)
        else:  # quote
            last_bid = x
            if holding and fill_ts is not None and ts >= fill_ts:
                stop, hwm = _ratcheted_trailing_stop(stop, hwm, x, trail_pct)
                if x <= stop:
                    xfill = exit_fill(book, ts, latency_s=latency_s)
                    xprice = xfill if xfill is not None else x
                    out.append(Trade(fill_ts, fill_price, ts, xprice, qty, (xprice - fill_price) * qty, "TRAIL_STOP"))
                    holding = False
    if holding and last_bid is not None:  # window ended holding -> exit at last bid
        out.append(Trade(fill_ts, fill_price, events[-1][0], last_bid, qty, (last_bid - fill_price) * qty, "WINDOW_END"))
    return out


def simulate_latency_band(bars, quotes, *, latencies=WEBULL_LATENCY_BAND_S, **kwargs):
    """Run the bar-close simulator across a measured per-broker latency band. Returns
    {latency_s: [Trade, ...]}. A single-latency P&L is fragile; the band bounds the honest
    answer (SDOT swings -$0.6@3s .. -$5.9@14s). `kwargs` are simulate_bar_close's args."""
    return {lat: simulate_bar_close(bars, quotes, latency_s=lat, **kwargs) for lat in latencies}
