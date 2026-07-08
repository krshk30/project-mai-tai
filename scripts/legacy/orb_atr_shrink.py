"""Exit = ATR-VALUE CONTRACTION (momentum fade), NOT the flip line and NOT waiting 40 min. Track the
live period-5 ATR VALUE (dollars, the plotted indicator); it rises while the move is strong. Exit when
it turns down from its peak by drop_frac (so we leave as volatility contracts, not on a price line).

Part A0: VTAK 07-08 per-minute detail (price + ATR value + %, mark the peak & where each shrink fires)
     — shows WHY the flip held 40 min and where an ATR-shrink exit would have left instead.
Part A: VTAK per-trade — fixed-2% vs flip vs ATR-shrink(drop%).
Part B: 17 grinding name-days robust — does ATR-shrink fix the flip's give-back names (JEM/SHPH)?
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
DROPS = [0.05, 0.10, 0.15, 0.20]      # ATR contraction from peak that triggers the exit


def hh(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


def _obars(bars):
    return [OBar(int(b.timestamp.timestamp() * 1000), b.open, b.high, b.low, b.close, int(b.volume)) for b in bars]


def _atr_value_series(bars):
    """(bar_close_ts, period-5 Wilders ATR VALUE in $, close) — the plotted ATR value, causal."""
    rows = compute_atr_trail(_obars(bars), period=5, factor=1.0)     # factor 1 -> loss == raw ATR value
    return [(bars[i].timestamp + timedelta(seconds=60), rows[i]["loss"], bars[i].close) for i in range(len(bars))]


def _flip_rows(bars):
    rows = compute_atr_trail(_obars(bars), period=5, factor=3.5)
    return [(bars[i].timestamp + timedelta(seconds=60), rows[i]["state"]) for i in range(len(bars))]


def _bid_at(quotes, qts, ts):
    j = bisect_right(qts, ts) - 1
    return quotes[j].bid if j >= 0 else None


def _run_flip_exit(quotes, qts, book, fill, fill_ts, frows):
    for close_ts, state in frows:
        if close_ts <= fill_ts or state != "short":
            continue
        return close_ts, (exit_fill(book, close_ts, latency_s=3.0) or _bid_at(quotes, qts, close_ts) or fill), "ATR_FLIP"
    return (quotes[-1].ts, quotes[-1].bid, "WINDOW_END") if quotes else (None, fill, "NO_Q")


def _run_shrink_exit(quotes, qts, book, fill, fill_ts, vseries, drop):
    peak = None
    for close_ts, val, _ in vseries:
        if close_ts <= fill_ts or val is None:
            continue
        if peak is None or val >= peak:
            peak = val
        elif val < peak * (1 - drop):
            return close_ts, (exit_fill(book, close_ts, latency_s=3.0) or _bid_at(quotes, qts, close_ts) or fill), "ATR_SHRINK"
    return (quotes[-1].ts, quotes[-1].bid, "WINDOW_END") if quotes else (None, fill, "NO_Q")


def simulate(trades, quotes, bars, *, exit_kind, trail_pct=2.0, drop=0.10, windows=None,
             observe_open, session_open, cutoff):
    book = QuoteBook(quotes)
    qts = [q.ts for q in quotes]
    eng = OrbTickEntry(observe_open=observe_open, session_open=session_open, cutoff=cutoff,
                       atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60)
    frows = _flip_rows(bars) if exit_kind == "flip" else None
    vseries = _atr_value_series(bars) if exit_kind == "shrink" else None
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
                elif exit_kind == "shrink":
                    xts, xpx, xr = _run_shrink_exit(quotes, qts, book, fill, fill_ts, vseries, drop)
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
    tr, q, bars, win = _load(src, "VTAK", 2026, 7, 8)

    print("=" * 96 + "\nA0 — VTAK 07-08 the 40-min hold: per-minute price + ATR VALUE (why it held; where shrink fires)\n" + "=" * 96)
    vseries = _atr_value_series(bars)
    peak = None
    fired = {d: None for d in DROPS}
    print(f"  {'min':<7}{'close':>8}{'ATR$':>9}{'ATR%':>8}{'peak$':>9}  note")
    for close_ts, val, close in vseries:
        if close_ts < _et(2026, 7, 8, 9, 34) or val is None:
            continue
        note = ""
        if peak is None or val >= peak:
            peak, note = val, "ATR rising / new peak" if (peak is None or val > peak) else ""
        for d in DROPS:
            if fired[d] is None and peak and val < peak * (1 - d):
                fired[d] = close_ts
                note += f"  <-- shrink{int(d*100)}% EXIT (ATR {val:.4f} < {peak*(1-d):.4f})"
        atrpct = val / close * 100 if close else 0
        print(f"  {hh(close_ts)[:5]:<7}{close:>8.4f}{val:>9.4f}{atrpct:>7.2f}%{peak:>9.4f}  {note}")

    print("\n" + "=" * 96 + "\nA — VTAK 07-08 per-trade: fixed-2% vs flip vs ATR-shrink\n" + "=" * 96)
    configs = [("fixed", {}, "fixed-2%"), ("flip", {}, "3.5x flip")] + \
              [("shrink", {"drop": d}, f"ATR-shrink {int(d*100)}%") for d in DROPS]
    for kind, kw, lbl in configs:
        r = simulate(tr, q, bars, exit_kind=kind, **kw, **win)
        hold = sum(x[6] for x in r) / len(r) if r else 0
        print(f"\n  {lbl}: {len(r)} trades, net {sum(x[4] for x in r):+.2f}, avg hold {hold:.0f}s")
        for x in r:
            print(f"     {hh(x[0])} {x[1]:.4f} -> {hh(x[2])} {x[3]:.4f} pnl{x[4]:+.2f} hold{x[6]:>5.0f}s {x[5]}")

    print("\n" + "=" * 96 + "\nB — 17 GRINDING name-days (robust): does ATR-shrink fix the flip give-back names?\n" + "=" * 96)
    wdir = "/home/trader/wt-atr-study/windows"
    dates = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
             "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]
    cols = {"fixed": [], "flip": [], **{f"shrink{d}": [] for d in DROPS}}
    holds = {k: [] for k in cols}
    rows_detail = []
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
            t2, q2, b2, w2 = ld
            a5 = atr_pct5(b2)
            cl = [b.close for b in b2 if b.close > 0]
            netm = abs(cl[-1] - cl[0]) if len(cl) > 1 else 0
            path = sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl)))
            er = netm / path if path > 0 else 0
            if a5 is None or a5 < GATE or er < ER_HI:
                continue
            res = {}
            res["fixed"] = simulate(t2, q2, b2, exit_kind="fixed", trail_pct=2.0, windows=ewin, **w2)
            res["flip"] = simulate(t2, q2, b2, exit_kind="flip", windows=ewin, **w2)
            for d in DROPS:
                res[f"shrink{d}"] = simulate(t2, q2, b2, exit_kind="shrink", drop=d, windows=ewin, **w2)
            for k, r in res.items():
                cols[k].append(sum(x[4] for x in r))
                holds[k].extend(x[6] for x in r)
            rows_detail.append((date, sym, {k: sum(x[4] for x in r) for k, r in res.items()}))
    print(f"  grinding name-days = {len(cols['fixed'])}")
    print(f"  {'config':<16}{'total':>8}{'mean':>7}{'median':>8}{'win%':>6}{'drop-1':>9}{'avgHold':>9}")
    for k, lbl in [("fixed", "fixed-2%"), ("flip", "3.5x flip")] + [(f"shrink{d}", f"shrink{int(d*100)}%") for d in DROPS]:
        ah = sum(holds[k]) / len(holds[k]) if holds[k] else 0
        print(f"  {lbl:<16}{_stats(cols[k])}{ah:>8.0f}s")
    print(f"\n  give-back names (flip's worst) — does shrink10% rescue them?")
    print(f"  {'date':<11}{'sym':<7}{'fixed':>8}{'flip':>8}{'shrink10':>10}")
    for date, sym, r in rows_detail:
        if r["flip"] < -1.0 or sym in ("VTAK", "CELZ", "CLRO"):
            print(f"  {date:<11}{sym:<7}{r['fixed']:>+8.2f}{r['flip']:>+8.2f}{r['shrink0.1']:>+10.2f}")


if __name__ == "__main__":
    main()
