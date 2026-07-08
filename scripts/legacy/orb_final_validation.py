"""FINAL full-build validation — the complete PR #403 config (tick entry + ungate-first-4min + first-bar
liquidity gate 100K/1.0%) across the last ~10 days. Per-trade detail for EVERY qualifying (gate-passing)
name: entry/exit/P&L, behavior tag, first-bar vol/spread. Exit comparison, TWO configs only:
  fixed2   the incumbent 2% trail
  atr0.25  breathing ATR-value trail k=0.25 (~1.5%) — the only ATR width that tied/edged 2% before
NEW question: last time k=0.25 was on the UNFILTERED set; now it's inside the full build WITH the
liquidity gate on. On LIQUID names only, does k=0.25 beat fixed-2%? total/median/win%/drop-top.

HONEST: same ~10-day sample we've squeezed hard — a VALIDATION run (build behaves + one fair look at
k=0.25 on liquid names), NOT new-sample proof. Read-only.
"""
from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.fill import QuoteBook, entry_fill, exit_fill
from project_mai_tai.backtest.orb_sim import _run_trail_exit
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import OrbTickEntry, atr_pct5

_ET = ZoneInfo("America/New_York")
GATE, UNGATE_MIN, GAP, TRAIL = 4.3, 4, 1.5, 2.0
LIQ_VOL, LIQ_SPR = 100000.0, 1.0
K = 0.25
FALLBACK = 2.0


def hh(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


def _firstbar(trades, quotes, so):
    t1 = [t for t in trades if so <= t.ts < so + timedelta(seconds=60)]
    q1 = [q for q in quotes if so <= q.ts < so + timedelta(seconds=60)]
    vol = sum(t.size for t in t1)
    spr = [(q.ask - q.bid) / ((q.ask + q.bid) / 2) * 100 for q in q1 if q.ask > 0 and q.bid > 0 and q.ask >= q.bid]
    return vol, (median(spr) if spr else 99.0)


def _atr_series(bars):
    ts, ap = [], []
    for i in range(len(bars)):
        ts.append(bars[i].timestamp + timedelta(seconds=60))
        ap.append(atr_pct5(bars[:i + 1]))
    return ts, ap


def _atr_exit(quotes, start, fill, fill_ts, series_ts, series_ap, book):
    def tp(ts):
        idx = bisect_right(series_ts, ts) - 1
        a = series_ap[idx] if idx >= 0 else None
        return K * a if a is not None else FALLBACK
    hwm, stop = fill, fill * (1 - tp(fill_ts) / 100)
    for i in range(start, len(quotes)):
        q = quotes[i]
        if q.bid <= 0:
            continue
        hwm = max(hwm, q.bid)
        stop = max(stop, hwm * (1 - tp(q.ts) / 100))
        if q.bid <= stop:
            return q.ts, (exit_fill(book, q.ts, latency_s=3.0) or q.bid), "ATR_TRAIL"
    return (quotes[-1].ts, quotes[-1].bid, "WINDOW_END") if quotes else (None, fill, "NO_Q")


def simulate(trades, quotes, bars, windows, win, exit_kind):
    book = QuoteBook(quotes)
    eng = OrbTickEntry(observe_open=win["observe_open"], session_open=win["session_open"], cutoff=win["cutoff"],
                       atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60, liq_min_volume=LIQ_VOL,
                       liq_max_spread_pct=LIQ_SPR)
    fb_close = win["session_open"] + timedelta(seconds=60)
    for q in quotes:
        if win["session_open"] <= q.ts < fb_close:
            eng.observe_quote(q.ts, q.bid, q.ask)
    sts, sap = _atr_series(bars) if exit_kind == "atr" else (None, None)
    bar_iter = iter(bars)
    nb = next(bar_iter, None)
    out, i, n = [], 0, len(trades)
    while i < n:
        t = trades[i]
        while nb is not None and nb.timestamp + timedelta(seconds=60) <= t.ts:
            eng.observe_bar(nb)
            nb = next(bar_iter, None)
        level = eng.observe_tick(t.ts, t.price, t.size)
        if level is not None and any(a <= t.ts <= b for a, b in windows):
            fill = entry_fill(book, t.ts, level, GAP)
            if fill is not None:
                fill_ts = t.ts + timedelta(seconds=3)
                start = bisect_left(book._ts, fill_ts)
                if exit_kind == "atr":
                    xts, xpx, xr = _atr_exit(quotes, start, fill, fill_ts, sts, sap, book)
                else:
                    xts, xpx, xr, _ = _run_trail_exit(quotes, start, fill, TRAIL, book, 3.0)
                out.append((t.ts, fill, xts, xpx, (xpx - fill) * 5, xr))
                while i < n and (xts is None or trades[i].ts <= xts):
                    eng.advance(trades[i].price)
                    i += 1
                continue
        i += 1
    return out


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def main():
    src = DbMarketDataSource(build_session_factory(get_settings()))
    wdir = "/home/trader/wt-atr-study/windows"
    dates = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
             "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]
    fx_nd, at_nd = [], []
    n_qual = n_skip = 0
    print("FULL PR #403 BUILD — per-trade, every gate-passing name (fixed-2% vs ATR k=0.25)\n")
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
            fbv, fbs = _firstbar(tr, q, so)
            if fbv < LIQ_VOL or fbs > LIQ_SPR:
                n_skip += 1
                continue                                       # not a qualifying (liquid) name
            n_qual += 1
            win = dict(observe_open=obs, session_open=so, cutoff=cut)
            a5 = atr_pct5(bars)
            cl = [b.close for b in bars if b.close > 0]
            er = abs(cl[-1] - cl[0]) / sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl))) if len(cl) > 1 and sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl))) > 0 else 0
            tag = "slow" if (a5 is None or a5 < GATE) else ("grinding" if er >= 0.10 else "volatile")
            fx = simulate(tr, q, bars, ewin, win, "fixed")
            at = simulate(tr, q, bars, ewin, win, "atr")
            fx_nd.append(sum(x[4] for x in fx))
            at_nd.append(sum(x[4] for x in at))
            print(f"{date} {sym:<6} [{tag}] ATR5%={a5:.1f} ER={er:.2f} | first-bar vol={fbv/1000:.0f}K spr={fbs:.2f}%"
                  f"  fixed={sum(x[4] for x in fx):+.2f} atr0.25={sum(x[4] for x in at):+.2f}")
            for lbl, trades in (("fixed", fx), ("atr25", at)):
                for x in trades:
                    print(f"    {lbl:<6} {hh(x[0])} {x[1]:.3f} -> {hh(x[2])} {x[3]:.3f} ({(x[3]/x[1]-1)*100:+.1f}%) pnl{x[4]:+.2f} {x[5]}")

    def stats(p):
        d1 = sum(p) - max(p, key=abs)
        w = sum(1 for x in p if x > 0.005) / len(p) * 100
        return f"total={sum(p):+.1f}  mean={sum(p)/len(p):+.2f}  median={statistics.median(p):+.2f}  win={w:.0f}%  drop-top={d1:+.1f}"

    print(f"\n{'='*88}\nAGGREGATE — {n_qual} qualifying (liquid) name-days ({n_skip} skipped by the gate)\n{'='*88}")
    print(f"  fixed-2%     {stats(fx_nd)}")
    print(f"  ATR k=0.25   {stats(at_nd)}")


if __name__ == "__main__":
    main()
