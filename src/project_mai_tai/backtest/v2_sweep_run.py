"""Runner for the ATR wait-3-break entry study (RESEARCH; not part of the CI gate).

    python -m project_mai_tai.backtest.v2_sweep_run 2026-07-06 2026-07-07 [--qty 10]

Enumerates the v2-qualified universe for each date (tracked[built Schwab bars] UNION traded, NO
exclusions), runs the wait-3-break entry across all 6 hard-stop buckets, tags each name by ATR%
volatility, and prints the per-name x per-buffer P&L matrix + totals + a volatility split so we can
read (1) which buffer works and (2) whether winners cluster volatile-vs-slow.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.backtest.v2_wait3break import (
    STOP_BUCKETS,
    atr_pct_rth,
    bars_from_trades,
    simulate_wait3break,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
BUCKET_NAMES = [name for name, _ in STOP_BUCKETS]


def _window(y, m, d):
    lo = datetime(y, m, d, 4, 0, tzinfo=_ET).astimezone(timezone.utc)
    hi = datetime(y, m, d, 20, 0, tzinfo=_ET).astimezone(timezone.utc)
    return lo, hi


def _run_name(src, sym, lo, hi, qty, feed, windows):
    mq = src.quotes(sym, lo, hi)                    # massive bid/ask (exit feed always)
    if feed == "massive":
        bars = bars_from_trades(src.trades(sym, lo, hi))
        entry_q = mq                               # break-detect + fill on the dense massive feed
    else:                                          # schwab-fidelity feed
        bars = src.schwab_bars(sym, lo, hi)
        entry_q = src.schwab_quotes(sym, lo, hi)
    if len(bars) < 10 or len(entry_q) == 0 or len(mq) == 0:
        return {"skip": f"no-feed(bars={len(bars)},eq={len(entry_q)},mq={len(mq)})", "windows": windows}
    vol = atr_pct_rth(bars)
    per_bucket = {}
    setups = breaks = brk_in = strict = None
    for name, mode in STOP_BUCKETS:
        trades, n_setups, n_breaks, n_brk_in, n_strict = simulate_wait3break(
            bars, entry_q, mq, qty=qty, stop_mode=mode, windows=windows)
        per_bucket[name] = {"pnl": sum(t.pnl for t in trades), "n": len(trades)}
        setups, breaks, brk_in, strict = n_setups, n_breaks, n_brk_in, n_strict
    return {"vol": vol, "setups": setups, "breaks": breaks, "brk_in": brk_in,
            "strict": strict, "buckets": per_bucket, "windows": windows}


def _et(dt):
    return dt.astimezone(_ET).strftime("%H:%M:%S")


def _fmt_windows(windows):
    """(summary_str, detail_str) for a list of (start,end) UTC confirmed intervals."""
    if not windows:
        return "0 windows (NEVER confirmed)", ""
    durs = [(b - a).total_seconds() / 60 for a, b in windows]
    total, longest = sum(durs), max(durs)
    summary = f"{len(windows)} win, total {total:.1f}m, longest {longest:.1f}m"
    detail = "  ".join(f"{_et(a)}-{_et(b)}({d:.1f}m)" for (a, b), d in zip(windows, durs))
    return summary, detail


def main():
    argv = sys.argv[1:]
    qty, feed, wdir, jsonp = 10, "massive", None, None
    for a in argv:
        if a.startswith("--qty="):
            qty = int(a.split("=", 1)[1])
        elif a.startswith("--feed="):
            feed = a.split("=", 1)[1]
        elif a.startswith("--windows-dir="):
            wdir = a.split("=", 1)[1]
        elif a.startswith("--json="):
            jsonp = a.split("=", 1)[1]
    dates = [a for a in argv if a.count("-") == 2 and a[:1].isdigit()]
    if not dates:
        print("usage: v2_sweep_run YYYY-MM-DD [...] [--qty=N] [--feed=massive|schwab] "
              "[--windows-dir=DIR] [--json=OUT]")
        return
    print(f"FEED={feed}  WINDOWS={'CONFIRMED-ONLY (' + wdir + ')' if wdir else 'WHOLE-SESSION (no restriction)'}  qty={qty}")
    src = DbMarketDataSource(build_session_factory(get_settings()))

    rows = []            # structured per name-day
    bucket_tot = {b: 0.0 for b in BUCKET_NAMES}
    for date in dates:
        y, m, d = (int(x) for x in date.split("-"))
        lo, hi = _window(y, m, d)
        wins_by_sym = load_windows(f"{wdir}/windows_{date}.json") if wdir else {}
        syms = src.v2_qualified_symbols(lo, hi)
        print(f"\n{'='*120}\nDAY {date} — {len(syms)} qualified v2 names (confirmed-window restricted; ET times)\n{'='*120}")
        day_tot = {b: 0.0 for b in BUCKET_NAMES}
        traded = 0
        for sym in syms:
            wins = wins_by_sym.get(sym, []) if wdir else None
            r = _run_name(src, sym, lo, hi, qty, feed, wins)
            wsum, wdet = _fmt_windows(wins if wdir else [])
            if "skip" in r:
                print(f"\n{sym:<6} SKIP {r['skip']}   [{wsum}]")
                continue
            vtxt = f"{r['vol']:.2f}%" if r["vol"] is not None else "  -  "
            print(f"\n{sym:<6} ATR%={vtxt:<7} setups={r['setups']:<2} breaks={r['breaks']:<2} "
                  f"in-window={r['brk_in']:<2} strict={r['strict']:<2}   [{wsum}]")
            if wdet:
                print(f"       windows: {wdet}")
            if r["brk_in"] == 0:
                why = ("no ATR flip/setup" if r["setups"] == 0 else
                       "breaks but ALL outside confirmed windows" if r["breaks"] > 0 else
                       "3-candle high never broken")
                print(f"       -> NO tradeable entry ({why})")
            else:
                cells = "  ".join(f"{b}:{r['buckets'][b]['pnl']:+.2f}(n{r['buckets'][b]['n']})" for b in BUCKET_NAMES)
                print(f"       {cells}")
                traded += 1
                for b in BUCKET_NAMES:
                    day_tot[b] += r["buckets"][b]["pnl"]
                    bucket_tot[b] += r["buckets"][b]["pnl"]
            rows.append({"date": date, "sym": sym, "vol": r["vol"], "setups": r["setups"],
                         "breaks": r["breaks"], "brk_in": r["brk_in"], "strict": r["strict"],
                         "buckets": r["buckets"],
                         "windows": [[a.isoformat(), b.isoformat()] for a, b in (wins or [])]})
        print(f"\n  DAY {date} TOTAL (names w/ entry: {traded}):  " +
              "  ".join(f"{b}:{day_tot[b]:+.2f}" for b in BUCKET_NAMES))

    print(f"\n{'='*120}\nCOMBINED — net P&L by hard-stop bucket (confirmed-window restricted)\n{'='*120}")
    print("  ".join(f"{b}:{bucket_tot[b]:+.2f}" for b in BUCKET_NAMES))

    voled = [r for r in rows if r["vol"] is not None and r["brk_in"] > 0]
    if voled:
        med = sorted(r["vol"] for r in voled)[len(voled) // 2]
        print(f"\nVOLATILITY SPLIT — median ATR% (traded name-days) = {med:.2f}%")
        for label, pred in (("HI-vol", lambda v: v >= med), ("LO-vol", lambda v: v < med)):
            grp = [r for r in voled if pred(r["vol"])]
            split = {b: sum(r["buckets"][b]["pnl"] for r in grp) for b in BUCKET_NAMES}
            print(f"  {label} ({len(grp)} nd): " + "  ".join(f"{b}:{split[b]:+.2f}" for b in BUCKET_NAMES))

    if jsonp:
        with open(jsonp, "w") as fh:
            json.dump({"feed": feed, "qty": qty, "dates": dates, "confirmed_only": bool(wdir),
                       "buckets": BUCKET_NAMES, "combined": bucket_tot, "rows": rows}, fh, indent=1)
        print(f"\n[json dumped -> {jsonp}]")


if __name__ == "__main__":
    main()
