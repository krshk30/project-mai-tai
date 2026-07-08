"""Re-validate the ATR-value CONTRACTION (shrink-from-peak) exit inside the FULL PR build (tick + ungate
+ liquidity gate 100K/1.0%) on the LIQUID post-gate name-days — does the gate change the verdict vs the
unfiltered run (where shrink underperformed)? Compare fixed-2% vs ATR k=0.25 vs shrink 5/10/15% on the
same 32 liquid name-days. total/median/win/drop-top. Read-only; same squeezed 10-day sample.
"""
from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import Bar as OBar
from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.fill import QuoteBook, entry_fill, exit_fill
from project_mai_tai.backtest.orb_sim import _run_trail_exit
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import OrbTickEntry, atr_pct5

_ET = ZoneInfo("America/New_York")
GATE, UNGATE_MIN, GAP, TRAIL, K, FB = 4.3, 4, 1.5, 2.0, 0.25, 2.0
LIQ_VOL, LIQ_SPR = 100000.0, 1.0
DROPS = [0.05, 0.10, 0.15]
CONFIGS = [("fixed2", "fixed", 0), ("atr0.25", "atr", 0)] + [(f"shrink{int(d*100)}", "shrink", d) for d in DROPS]


def _atr_pct_series(bars):
    ts, ap = [], []
    for i in range(len(bars)):
        ts.append(bars[i].timestamp + timedelta(seconds=60))
        ap.append(atr_pct5(bars[:i + 1]))
    return ts, ap


def _atr_val_series(bars):
    ob = [OBar(int(b.timestamp.timestamp() * 1000), b.open, b.high, b.low, b.close, int(b.volume)) for b in bars]
    rows = compute_atr_trail(ob, period=5, factor=1.0)
    return [b.timestamp + timedelta(seconds=60) for b in bars], [rows[i]["loss"] for i in range(len(bars))]


def _atr_exit(quotes, start, fill, fill_ts, sts, sap, book):
    def tp(ts):
        idx = bisect_right(sts, ts) - 1
        a = sap[idx] if idx >= 0 else None
        return K * a if a is not None else FB
    hwm, stop = fill, fill * (1 - tp(fill_ts) / 100)
    for i in range(start, len(quotes)):
        q = quotes[i]
        if q.bid <= 0:
            continue
        hwm = max(hwm, q.bid)
        stop = max(stop, hwm * (1 - tp(q.ts) / 100))
        if q.bid <= stop:
            return q.ts, (exit_fill(book, q.ts, latency_s=3.0) or q.bid), "ATR"
    return (quotes[-1].ts, quotes[-1].bid, "WEND") if quotes else (None, fill, "NOQ")


def _shrink_exit(quotes, qts, book, fill, fill_ts, vts, vval, drop):
    peak = None
    for k in range(len(vts)):
        ct, val = vts[k], vval[k]
        if ct <= fill_ts or val is None:
            continue
        if peak is None or val >= peak:
            peak = val
        elif val < peak * (1 - drop):
            j = bisect_right(qts, ct) - 1
            bid = quotes[j].bid if j >= 0 else fill
            return ct, (exit_fill(book, ct, latency_s=3.0) or bid), "SHRINK"
    return (quotes[-1].ts, quotes[-1].bid, "WEND") if quotes else (None, fill, "NOQ")


def simulate(tr, q, bars, ewin, win, kind, drop):
    book = QuoteBook(q)
    qts = [x.ts for x in q]
    eng = OrbTickEntry(observe_open=win["observe_open"], session_open=win["session_open"], cutoff=win["cutoff"],
                       atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60, liq_min_volume=LIQ_VOL,
                       liq_max_spread_pct=LIQ_SPR)
    fb_close = win["session_open"] + timedelta(seconds=60)
    for x in q:
        if win["session_open"] <= x.ts < fb_close:
            eng.observe_quote(x.ts, x.bid, x.ask)
    sts = sap = vts = vval = None
    if kind == "atr":
        sts, sap = _atr_pct_series(bars)
    elif kind == "shrink":
        vts, vval = _atr_val_series(bars)
    bar_iter = iter(bars)
    nb = next(bar_iter, None)
    out, i, n = [], 0, len(tr)
    while i < n:
        t = tr[i]
        while nb is not None and nb.timestamp + timedelta(seconds=60) <= t.ts:
            eng.observe_bar(nb)
            nb = next(bar_iter, None)
        level = eng.observe_tick(t.ts, t.price, t.size)
        if level is not None and any(a <= t.ts <= b for a, b in ewin):
            fill = entry_fill(book, t.ts, level, GAP)
            if fill is not None:
                fill_ts = t.ts + timedelta(seconds=3)
                start = bisect_left(book._ts, fill_ts)
                if kind == "atr":
                    xts, xpx, xr = _atr_exit(q, start, fill, fill_ts, sts, sap, book)
                elif kind == "shrink":
                    xts, xpx, xr = _shrink_exit(q, qts, book, fill, fill_ts, vts, vval, drop)
                else:
                    xts, xpx, xr, _ = _run_trail_exit(q, start, fill, TRAIL, book, 3.0)
                out.append((t.ts, fill, xts, xpx, (xpx - fill) * 5, xr))
                while i < n and (xts is None or tr[i].ts <= xts):
                    eng.advance(tr[i].price)
                    i += 1
                continue
        i += 1
    return out


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def _fb(tr, q, so):
    t1 = [t for t in tr if so <= t.ts < so + timedelta(seconds=60)]
    q1 = [x for x in q if so <= x.ts < so + timedelta(seconds=60)]
    vol = sum(t.size for t in t1)
    spr = [(x.ask - x.bid) / ((x.ask + x.bid) / 2) * 100 for x in q1 if x.ask > 0 and x.bid > 0 and x.ask >= x.bid]
    return vol, (median(spr) if spr else 99.0)


def main():
    src = DbMarketDataSource(build_session_factory(get_settings()))
    wdir = "/home/trader/wt-atr-study/windows"
    dates = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
             "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]
    per = {tag: [] for tag, _, _ in CONFIGS}
    rows = []
    for date in dates:
        y, mo, dd = (int(x) for x in date.split("-"))
        obs, so, cut, end = _et(y, mo, dd, 9, 25), _et(y, mo, dd, 9, 30), _et(y, mo, dd, 10, 0), _et(y, mo, dd, 10, 10)
        wins = load_windows(f"{wdir}/windows_{date}.json")
        for sym in src.orb_qualified_symbols(obs, end, min_trades=500):
            ewin = wins.get(sym, [])
            if not ewin:
                continue
            tr, q = src.trades(sym, obs, end), src.quotes(sym, obs, end)
            if len(tr) < 500 or len(q) < 50:
                continue
            bars = build_bars(tr, so)
            vol, spr = _fb(tr, q, so)
            if vol < LIQ_VOL or spr > LIQ_SPR:
                continue
            win = dict(observe_open=obs, session_open=so, cutoff=cut)
            r = {}
            for tag, kind, drop in CONFIGS:
                r[tag] = sum(x[4] for x in simulate(tr, q, bars, ewin, win, kind, drop))
                per[tag].append(r[tag])
            rows.append((date, sym, r))

    def stats(p):
        d1 = sum(p) - max(p, key=abs)
        w = sum(1 for x in p if x > 0.005) / len(p) * 100
        return f"total={sum(p):>+6.1f}  mean={sum(p)/len(p):>+5.2f}  median={statistics.median(p):>+5.2f}  win={w:>3.0f}%  drop-top={d1:>+6.1f}"

    n = len(per["fixed2"])
    print(f"AGGREGATE — {n} liquid (post-gate) name-days\n")
    for tag, _, _ in CONFIGS:
        print(f"  {tag:<10} {stats(per[tag])}")
    print("\nper-name (fixed2 | shrink10 | shrink15) where they differ:")
    for date, sym, r in rows:
        if abs(r['shrink10'] - r['fixed2']) > 0.001 or abs(r['shrink15'] - r['fixed2']) > 0.001:
            print(f"  {date} {sym:<6} fixed {r['fixed2']:+.2f} | shrink10 {r['shrink10']:+.2f} | shrink15 {r['shrink15']:+.2f}")


if __name__ == "__main__":
    main()
