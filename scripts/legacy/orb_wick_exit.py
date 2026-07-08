"""INTRABAR-WICK sensitivity of the 2% exit, on LIQUID (post-gate) names. Worry: the tick-driven 2%
trail fires on an intrabar wick-DOWN and scratches out right before a same-candle recovery + run (CLRO
dip-then-rip). Compare, same entries (PR config incl. 100K/1.0% liquidity gate):
  tick      current — 2% trail evaluated every quote tick (fires on intrabar wicks)
  barclose  2% trail evaluated ONLY at each 1-min bar close (rides through intrabar wicks)
  grace_N   tick 2% trail but NO exit for the first N seconds after entry (entry-candle wick can't stop)

KEY: on dip-then-rip names (CLRO) does tick scratch before the run, and do barclose/grace catch more of
the +run? COST: on dip-and-KEEP-dipping reversals (SDOT) do barclose/grace give back more? Robust: drop-CLRO.
"""
from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.fill import QuoteBook, entry_fill, exit_fill
from project_mai_tai.backtest.orb_sim import _run_trail_exit
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import OrbTickEntry

_ET = ZoneInfo("America/New_York")
GATE, UNGATE_MIN, GAP, TRAIL = 4.3, 4, 1.5, 2.0
LIQ_VOL, LIQ_SPR = 100000.0, 1.0
GRACES = [30, 60]


def hh(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


def _barclose_exit(quotes, qts, book, fill, fill_ts, bar_closes):
    hwm, stop = fill, fill * (1 - TRAIL / 100)
    for bc in bar_closes:
        if bc <= fill_ts:
            continue
        j = bisect_right(qts, bc) - 1
        if j < 0 or quotes[j].bid <= 0:
            continue
        bid = quotes[j].bid
        hwm = max(hwm, bid)
        stop = max(stop, hwm * (1 - TRAIL / 100))
        if bid <= stop:
            return bc, (exit_fill(book, bc, latency_s=3.0) or bid), "BARCLOSE"
    return (quotes[-1].ts, quotes[-1].bid, "WINDOW_END") if quotes else (None, fill, "NO_Q")


def _grace_exit(quotes, start, fill, fill_ts, book, grace):
    hwm, stop = fill, fill * (1 - TRAIL / 100)
    cutoff = fill_ts + timedelta(seconds=grace)
    i, n = start, len(quotes)
    while i < n:
        q = quotes[i]
        if q.bid > 0:
            hwm = max(hwm, q.bid)
            stop = max(stop, hwm * (1 - TRAIL / 100))
            if q.ts >= cutoff and q.bid <= stop:
                return q.ts, (exit_fill(book, q.ts, latency_s=3.0) or q.bid), "GRACE"
        i += 1
    return (quotes[-1].ts, quotes[-1].bid, "WINDOW_END") if quotes else (None, fill, "NO_Q")


def simulate(trades, quotes, bars, *, exit_kind, grace=0, windows=None, observe_open, session_open, cutoff):
    book = QuoteBook(quotes)
    qts = [q.ts for q in quotes]
    bar_closes = [b.timestamp + timedelta(seconds=60) for b in bars]
    eng = OrbTickEntry(observe_open=observe_open, session_open=session_open, cutoff=cutoff,
                       atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60,
                       liq_min_volume=LIQ_VOL, liq_max_spread_pct=LIQ_SPR)
    fb_close = session_open + timedelta(seconds=60)
    for q in quotes:
        if session_open <= q.ts < fb_close:
            eng.observe_quote(q.ts, q.bid, q.ask)
    bar_iter = iter(bars)
    nb = next(bar_iter, None)
    out, i, n = [], 0, len(trades)
    while i < n:
        t = trades[i]
        while nb is not None and nb.timestamp + timedelta(seconds=60) <= t.ts:
            eng.observe_bar(nb)
            nb = next(bar_iter, None)
        level = eng.observe_tick(t.ts, t.price, t.size)
        inwin = windows is None or any(a <= t.ts <= b for a, b in windows)
        if level is not None and inwin:
            fill = entry_fill(book, t.ts, level, GAP)
            if fill is not None:
                fill_ts = t.ts + timedelta(seconds=3)
                start = bisect_left(book._ts, fill_ts)
                if exit_kind == "tick":
                    xts, xpx, xr, _ = _run_trail_exit(quotes, start, fill, TRAIL, book, 3.0)
                elif exit_kind == "barclose":
                    xts, xpx, xr = _barclose_exit(quotes, qts, book, fill, fill_ts, bar_closes)
                else:
                    xts, xpx, xr = _grace_exit(quotes, start, fill, fill_ts, book, grace)
                out.append((t.ts, fill, xts, xpx, (xpx - fill) * 5, xr))
                while i < n and (xts is None or trades[i].ts <= xts):
                    eng.advance(trades[i].price)
                    i += 1
                continue
        i += 1
    return out


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def _load(src, sym, y, mo, d):
    obs, so, cut, end = _et(y, mo, d, 9, 25), _et(y, mo, d, 9, 30), _et(y, mo, d, 10, 0), _et(y, mo, d, 10, 10)
    tr, q = src.trades(sym, obs, end), src.quotes(sym, obs, end)
    if len(tr) < 500 or len(q) < 50:
        return None
    return tr, q, build_bars(tr, so), dict(observe_open=obs, session_open=so, cutoff=cut)


CONFIGS = [("tick", 0), ("barclose", 0)] + [("grace", g) for g in GRACES]


def _run_all(tr, q, bars, win, windows=None):
    return {f"{k}{g or ''}": simulate(tr, q, bars, exit_kind=k, grace=g, windows=windows, **win) for k, g in CONFIGS}


def detail(src, sym, date):
    y, mo, d = (int(x) for x in date.split("-"))
    ld = _load(src, sym, y, mo, d)
    print(f"\n{'='*80}\n{sym} {date} per-trade under each exit\n{'='*80}")
    if ld is None:
        print("  no data")
        return
    tr, q, bars, win = ld
    for tag, trades in _run_all(*ld[:3], win).items():
        print(f"  {tag:<10} {len(trades)} trades, net {sum(x[4] for x in trades):+.2f}")
        for x in trades:
            print(f"     {hh(x[0])} {x[1]:.4f} -> {hh(x[2])} {x[3]:.4f} ({(x[3]/x[1]-1)*100:+.1f}%) pnl{x[4]:+.2f} {x[5]}")


def main():
    src = DbMarketDataSource(build_session_factory(get_settings()))
    for sym, date in [("CLRO", "2026-07-06"), ("CLRO", "2026-07-07"), ("SDOT", "2026-06-26")]:
        detail(src, sym, date)

    print(f"\n{'='*80}\nAGGREGATE — liquid (post-gate) names, all 10 days\n{'='*80}")
    wdir = "/home/trader/wt-atr-study/windows"
    dates = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
             "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]
    cols = {f"{k}{g or ''}": [] for k, g in CONFIGS}
    clro = {f"{k}{g or ''}": 0.0 for k, g in CONFIGS}
    for date in dates:
        y, mo, dd = (int(x) for x in date.split("-"))
        obs, end = _et(y, mo, dd, 9, 25), _et(y, mo, dd, 10, 10)
        wins = load_windows(f"{wdir}/windows_{date}.json")
        for sym in src.orb_qualified_symbols(obs, end, min_trades=500):
            ewin = wins.get(sym, [])
            if not ewin:
                continue
            ld = _load(src, sym, y, mo, dd)
            if ld is None:
                continue
            res = _run_all(*ld[:3], ld[3], windows=ewin)
            if not any(res.values()):
                continue
            for tag, trades in res.items():
                pnl = sum(x[4] for x in trades)
                cols[tag].append(pnl)
                if sym == "CLRO":
                    clro[tag] += pnl
    print(f"  {'exit':<12}{'total':>8}{'mean':>7}{'median':>8}{'win%':>6}{'drop-CLRO':>11}")
    for tag in cols:
        p = cols[tag]
        w = sum(1 for x in p if x > 0.005) / len(p) * 100 if p else 0
        print(f"  {tag:<12}{sum(p):>+8.1f}{sum(p)/len(p):>+7.2f}{statistics.median(p):>+8.2f}{w:>5.0f}%{sum(p)-clro[tag]:>+11.1f}")
    print(f"  (CLRO contribution per exit: " + ", ".join(f"{t}={clro[t]:+.1f}" for t in cols) + ")")


if __name__ == "__main__":
    main()
