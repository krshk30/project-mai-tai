"""Exit = the PLOTTED 3.5x ATR TRAILING-STOP LINE (period-5, factor-3.5 — the SAME ATRTrailingStop
line that defines the entry flip). Hold until a bar CLOSES below the line (state flips long->short),
i.e. ride the whole grind to the flip. Far wider than the k=0.25-1.5 breathing sweep; the "ride the
grind" thesis. Compare to fixed-2%: hold length, exit count, and NET (does riding pay more than the
extra give-back on reversals?).

Part A: VTAK 07-08 per-trade (hold seconds + reason).  Part B: 17 grinding name-days, robust (drop-1).
"""
from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
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
GATE, UNGATE_MIN, GAP, ER_HI = 4.3, 4, 1.5, 0.10


def hh(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


def _flip_rows(bars):
    ob = [OBar(int(b.timestamp.timestamp() * 1000), b.open, b.high, b.low, b.close, int(b.volume)) for b in bars]
    rows = compute_atr_trail(ob, period=5, factor=3.5)
    return [(bars[i].timestamp + timedelta(seconds=60), rows[i]["state"], rows[i]["trail"]) for i in range(len(bars))]


def _run_flip_exit(quotes, qts, book, fill, fill_ts, flip_rows):
    for close_ts, state, _ in flip_rows:
        if close_ts <= fill_ts or state != "short":
            continue
        xf = exit_fill(book, close_ts, latency_s=3.0)
        if xf is None:
            j = bisect_right(qts, close_ts) - 1
            xf = quotes[j].bid if j >= 0 else fill
        return close_ts, xf, "ATR_FLIP"
    return (quotes[-1].ts, quotes[-1].bid, "WINDOW_END") if quotes else (None, fill, "NO_Q")


def simulate(trades, quotes, bars, *, exit_kind, trail_pct=2.0, windows=None,
             observe_open, session_open, cutoff):
    book = QuoteBook(quotes)
    qts = [q.ts for q in quotes]
    eng = OrbTickEntry(observe_open=observe_open, session_open=session_open, cutoff=cutoff,
                       atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60)
    frows = _flip_rows(bars) if exit_kind == "flip" else None
    bar_iter = iter(bars)
    nb = next(bar_iter, None)
    out, i, n = [], 0, len(trades)
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
                if exit_kind == "flip":
                    xts, xpx, xr = _run_flip_exit(quotes, qts, book, fill, fill_ts, frows)
                else:
                    start = bisect_left(book._ts, fill_ts)
                    xts, xpx, xr, _ = _run_trail_exit(quotes, start, fill, trail_pct, book, 3.0)
                hold = (xts - t.ts).total_seconds() if xts else 0
                out.append((t.ts, fill, xts, xpx, (xpx - fill) * 5, xr, hold))
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


def _stats(p):
    if not p:
        return "  (none)"
    d1 = sum(p) - max(p, key=abs)
    w = sum(1 for x in p if x > 0.005) / len(p) * 100
    return f"{sum(p):>+8.1f}{sum(p)/len(p):>+7.2f}{statistics.median(p):>+8.2f}{w:>5.0f}%{d1:>+9.1f}"


def main():
    src = DbMarketDataSource(build_session_factory(get_settings()))
    print("=" * 96 + "\nPART A — VTAK 2026-07-08: fixed-2% vs 3.5x ATR-flip-line exit\n" + "=" * 96)
    tr, q, bars, win = _load(src, "VTAK", 2026, 7, 8)
    for kind, lbl in [("fixed", "fixed-2%"), ("flip", "3.5x ATR-flip line")]:
        r = simulate(tr, q, bars, exit_kind=kind, **win)
        hold = sum(x[6] for x in r) / len(r) if r else 0
        print(f"\n  {lbl}: {len(r)} trades, net {sum(x[4] for x in r):+.2f}, avg hold {hold:.0f}s")
        for x in r:
            print(f"     {hh(x[0])} {x[1]:.4f} -> {hh(x[2])} {x[3]:.4f} pnl{x[4]:+.2f} hold{x[6]:>5.0f}s {x[5]}")

    print("\n" + "=" * 96 + "\nPART B — 17 GRINDING name-days: fixed-2% vs 3.5x ATR-flip (robust)\n" + "=" * 96)
    wdir = "/home/trader/wt-atr-study/windows"
    dates = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
             "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]
    fx, fl, names, holds = [], [], [], {"fixed": [], "flip": []}
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
            tr, q, bars, win = ld
            a5 = atr_pct5(bars)
            closes = [b.close for b in bars if b.close > 0]
            net = abs(closes[-1] - closes[0]) if len(closes) > 1 else 0
            path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
            er = net / path if path > 0 else 0
            if a5 is None or a5 < GATE or er < ER_HI:
                continue
            rf = simulate(tr, q, bars, exit_kind="fixed", trail_pct=2.0, windows=ewin, **win)
            rl = simulate(tr, q, bars, exit_kind="flip", windows=ewin, **win)
            fx.append(sum(x[4] for x in rf))
            fl.append(sum(x[4] for x in rl))
            names.append((date, sym, len(rf), len(rl),
                          (sum(x[6] for x in rf) / len(rf)) if rf else 0,
                          (sum(x[6] for x in rl) / len(rl)) if rl else 0))
    print(f"  grinding name-days = {len(fx)}")
    print(f"  {'config':<20}{'total':>8}{'mean':>7}{'median':>8}{'win%':>6}{'drop-1':>9}")
    print(f"  {'fixed-2%':<20}{_stats(fx)}")
    print(f"  {'3.5x ATR-flip':<20}{_stats(fl)}")
    avg_tr_fx = sum(x[2] for x in names) / len(names)
    avg_tr_fl = sum(x[3] for x in names) / len(names)
    avg_h_fx = sum(x[4] for x in names) / len(names)
    avg_h_fl = sum(x[5] for x in names) / len(names)
    print(f"\n  avg trades/name: fixed {avg_tr_fx:.1f} vs flip {avg_tr_fl:.1f}   "
          f"avg hold: fixed {avg_h_fx:.0f}s vs flip {avg_h_fl:.0f}s")
    print(f"\n  {'date':<11}{'sym':<7}{'fx_n':>5}{'fl_n':>5}{'fx_pnl':>8}{'fl_pnl':>8}{'fl-fx':>8}")
    for (date, sym, fxn, fln, _, _), a, b in zip(names, fx, fl):
        print(f"  {date:<11}{sym:<7}{fxn:>5}{fln:>5}{a:>+8.2f}{b:>+8.2f}{b-a:>+8.2f}")


if __name__ == "__main__":
    main()
