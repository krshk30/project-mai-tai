"""ATR-VALUE (breathing) trailing-stop test — stop = HWM*(1 - k*ATR%_causal(t)/100), ratcheting, with
the period-5 ATR% recomputed as new bars close DURING the hold (so the stop breathes wider when ATR
rises, tighter when it contracts). NOT the ATR flip; a normal trailing pullback with ATR-scaled width.

Different from the earlier 'ATR-adaptive' sweep bucket (which was a STATIC per-name trail_pct = ATR5%,
no multiplier, no breathing). Here we sweep the multiplier k finely and check GRINDING names.

Part A: VTAK 07-08 per-trade — fixed-2% vs ATR-trail at each k.
Part B: grinding name-days (gated 10-day sample) — does any k beat fixed-2%? (median/win, robust).
"""
from __future__ import annotations

import statistics
import sys
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.fill import QuoteBook, entry_fill, exit_fill
from project_mai_tai.backtest.orb_sim import _run_trail_exit
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import OrbTickEntry, atr_pct5

_ET = ZoneInfo("America/New_York")
GATE, UNGATE_MIN, GAP, ER_HI = 4.3, 4, 1.5, 0.10
KS = [0.25, 0.33, 0.5, 0.75, 1.0, 1.5]
FALLBACK = 2.0        # trail% used before the causal ATR is computable (early window)


def hh(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


def _atr_series(bars):
    """(close_ts, atr%) after each bar closes — the causal ATR% available from that minute on."""
    ts, ap = [], []
    for i in range(len(bars)):
        ts.append(bars[i].timestamp + timedelta(seconds=60))
        ap.append(atr_pct5(bars[:i + 1]))
    return ts, ap


def _run_atr_trail_exit(quotes, start, fill, fill_ts, book, k, series_ts, series_ap):
    def trail_pct(ts):
        idx = bisect_right(series_ts, ts) - 1
        a = series_ap[idx] if idx >= 0 else None
        return k * a if a is not None else FALLBACK

    hwm = fill
    stop = fill * (1 - trail_pct(fill_ts) / 100)
    i, n = start, len(quotes)
    while i < n:
        q = quotes[i]
        if q.bid > 0:
            tp = trail_pct(q.ts)
            hwm = max(hwm, q.bid)
            cand = hwm * (1 - tp / 100)
            stop = max(stop, cand)
            if q.bid <= stop:
                xf = exit_fill(book, q.ts, latency_s=3.0)
                return q.ts, (xf if xf is not None else q.bid), "ATR_TRAIL"
        i += 1
    return (quotes[-1].ts, quotes[-1].bid, "WINDOW_END") if quotes else (None, None, "NO_QUOTES")


def simulate(trades, quotes, bars, *, exit_kind, k=None, trail_pct=2.0, windows=None,
             observe_open, session_open, cutoff):
    """entries = OrbTickEntry (gate 4.3 + ungate 4min); exit = fixed trail OR ATR-value trail."""
    book = QuoteBook(quotes)
    eng = OrbTickEntry(observe_open=observe_open, session_open=session_open, cutoff=cutoff,
                       atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60)
    series_ts, series_ap = _atr_series(bars) if exit_kind == "atr" else (None, None)
    bar_iter = iter(bars)
    nb = next(bar_iter, None)
    out = []
    i, n = 0, len(trades)
    while i < n:
        t = trades[i]
        while nb is not None and nb.timestamp + timedelta(seconds=60) <= t.ts:
            eng.observe_bar(nb)
            nb = next(bar_iter, None)
        level = eng.observe_tick(t.ts, t.price)
        inwin = windows is None or any(a <= t.ts <= b for a, b in windows)
        if level is not None and inwin:
            fill = entry_fill(book, t.ts, level, GAP)
            if fill is not None:
                fill_ts = t.ts + timedelta(seconds=3)
                start = bisect_left(book._ts, fill_ts)
                if exit_kind == "atr":
                    xts, xpx, xr = _run_atr_trail_exit(quotes, start, fill, fill_ts, book, k, series_ts, series_ap)
                else:
                    xts, xpx, xr, _ = _run_trail_exit(quotes, start, fill, trail_pct, book, 3.0)
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


def main():
    src = DbMarketDataSource(build_session_factory(get_settings()))

    print("=" * 96 + "\nPART A — VTAK 2026-07-08: fixed-2% vs breathing ATR-value trail (per k)\n" + "=" * 96)
    d = _load(src, "VTAK", 2026, 7, 8)
    tr, q, bars, win = d
    fx = simulate(tr, q, bars, exit_kind="fixed", trail_pct=2.0, **win)
    print(f"  fixed-2%: {len(fx)} trades, net {sum(x[4] for x in fx):+.2f}")
    for x in fx:
        print(f"     {hh(x[0])} {x[1]:.4f} -> {hh(x[2])} {x[3]:.4f} pnl{x[4]:+.2f} {x[5]}")
    for k in KS:
        at = simulate(tr, q, bars, exit_kind="atr", k=k, **win)
        print(f"\n  ATR k={k} (~{k*5.95:.1f}% at VTAK's ~5.95% ATR): {len(at)} trades, net {sum(x[4] for x in at):+.2f}")
        for x in at:
            print(f"     {hh(x[0])} {x[1]:.4f} -> {hh(x[2])} {x[3]:.4f} pnl{x[4]:+.2f} {x[5]}")

    print("\n" + "=" * 96 + "\nPART B — GRINDING name-days (gated 10-day sample): does any k beat fixed-2%?\n" + "=" * 96)
    wdir = "/home/trader/wt-atr-study/windows"
    dates = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
             "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]
    per = {"fixed2": []}
    for k in KS:
        per[f"atr{k}"] = []
    for date in dates:
        y, mo, dd = (int(x) for x in date.split("-"))
        obs, so, cut, end = _et(y, mo, dd, 9, 25), _et(y, mo, dd, 9, 30), _et(y, mo, dd, 10, 0), _et(y, mo, dd, 10, 10)
        wins = load_windows(f"{wdir}/windows_{date}.json")
        for sym in src.orb_qualified_symbols(obs, end, min_trades=500):
            ewin = wins.get(sym, [])
            if not ewin:
                continue
            ld = _load(src, sym, y, mo, dd)
            if ld is None:
                continue
            tr, q, bars, win = ld
            a5 = atr_pct5(bars)
            closes = [b.close for b in bars if b.close > 0]
            net = abs(closes[-1] - closes[0]) if len(closes) > 1 else 0
            path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
            er = net / path if path > 0 else 0
            if a5 is None or a5 < GATE or er < ER_HI:      # grinding only
                continue
            per["fixed2"].append(sum(x[4] for x in simulate(tr, q, bars, exit_kind="fixed", trail_pct=2.0, windows=ewin, **win)))
            for k in KS:
                per[f"atr{k}"].append(sum(x[4] for x in simulate(tr, q, bars, exit_kind="atr", k=k, windows=ewin, **win)))
    n = len(per["fixed2"])
    print(f"  grinding name-days = {n}")
    print(f"  {'config':<12}{'total':>8}{'mean':>7}{'median':>8}{'win%':>6}{'drop-top':>10}")
    for cfg in ["fixed2"] + [f"atr{k}" for k in KS]:
        p = per[cfg]
        if not p:
            continue
        dt = sum(p) - max(p, key=abs)
        w = sum(1 for x in p if x > 0.005) / len(p) * 100
        print(f"  {cfg:<12}{sum(p):>+8.1f}{sum(p)/len(p):>+7.2f}{statistics.median(p):>+8.2f}{w:>5.0f}%{dt:>+10.1f}")


if __name__ == "__main__":
    main()
