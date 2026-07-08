"""Runner for the ATR wait-3-break entry study (RESEARCH; not part of the CI gate).

    python -m project_mai_tai.backtest.v2_sweep_run 2026-07-06 2026-07-07 [--qty 10]

Enumerates the v2-qualified universe for each date (tracked[built Schwab bars] UNION traded, NO
exclusions), runs the wait-3-break entry across all 6 hard-stop buckets, tags each name by ATR%
volatility, and prints the per-name x per-buffer P&L matrix + totals + a volatility split so we can
read (1) which buffer works and (2) whether winners cluster volatile-vs-slow.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.v2_wait3break import (
    STOP_BUCKETS,
    atr_pct_rth,
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


def _run_name(src, sym, lo, hi, qty):
    sb = src.schwab_bars(sym, lo, hi)
    sq = src.schwab_quotes(sym, lo, hi)
    mq = src.quotes(sym, lo, hi)
    if len(sb) < 10 or len(sq) == 0 or len(mq) == 0:
        return {"skip": f"no-feed(sb={len(sb)},sq={len(sq)},mq={len(mq)})"}
    vol = atr_pct_rth(sb)
    per_bucket = {}
    setups = breaks = None
    trades_ref = None
    for name, mode in STOP_BUCKETS:
        trades, n_setups, n_breaks = simulate_wait3break(sb, sq, mq, qty=qty, stop_mode=mode)
        per_bucket[name] = {"pnl": sum(t.pnl for t in trades), "n": len(trades)}
        setups, breaks = n_setups, n_breaks
        if trades_ref is None:
            trades_ref = trades
    return {"vol": vol, "setups": setups, "breaks": breaks, "buckets": per_bucket}


def _fmt_pnl(v):
    return f"{v:+8.2f}"


def main():
    argv = sys.argv[1:]
    qty = 10
    for a in argv:
        if a.startswith("--qty="):
            qty = int(a.split("=", 1)[1])
    dates = [a for a in argv if a.count("-") == 2 and a[:1].isdigit()]   # YYYY-MM-DD positionals
    if not dates:
        print("usage: python -m project_mai_tai.backtest.v2_sweep_run YYYY-MM-DD [YYYY-MM-DD ...] [--qty=N]")
        return
    src = DbMarketDataSource(build_session_factory(get_settings()))

    all_rows = []       # (date, sym, vol, setups, breaks, {bucket: {pnl,n}})
    bucket_tot = {b: 0.0 for b in BUCKET_NAMES}
    for date in dates:
        y, m, d = (int(x) for x in date.split("-"))
        lo, hi = _window(y, m, d)
        syms = src.v2_qualified_symbols(lo, hi)
        print(f"\n{'='*118}\nDAY {date}  qualified v2 names: {len(syms)}  (qty={qty}; entry-feed=Schwab, exit=massive bid)\n{'='*118}")
        hdr = f"{'SYMBOL':<7}{'ATR%':>6} {'set':>4}{'brk':>4}  " + "".join(f"{b:>9}" for b in BUCKET_NAMES)
        print(hdr)
        print("-" * len(hdr))
        day_tot = {b: 0.0 for b in BUCKET_NAMES}
        traded_rows = 0
        for sym in syms:
            r = _run_name(src, sym, lo, hi, qty)
            if "skip" in r:
                print(f"{sym:<7}{'':>6} {'':>4}{'':>4}  SKIP {r['skip']}")
                continue
            if r["breaks"] == 0:
                vtxt = f"{r['vol']:.2f}" if r["vol"] is not None else "  -"
                print(f"{sym:<7}{vtxt:>6} {r['setups']:>4}{r['breaks']:>4}  (no break -> no entry)")
                all_rows.append((date, sym, r["vol"], r["setups"], r["breaks"], r["buckets"]))
                continue
            traded_rows += 1
            vtxt = f"{r['vol']:.2f}" if r["vol"] is not None else "  -"
            cells = ""
            for b in BUCKET_NAMES:
                pnl = r["buckets"][b]["pnl"]
                cells += _fmt_pnl(pnl).rjust(9)
                day_tot[b] += pnl
                bucket_tot[b] += pnl
            print(f"{sym:<7}{vtxt:>6} {r['setups']:>4}{r['breaks']:>4}  {cells}")
            all_rows.append((date, sym, r["vol"], r["setups"], r["breaks"], r["buckets"]))
        print("-" * len(hdr))
        tot_cells = "".join(_fmt_pnl(day_tot[b]).rjust(9) for b in BUCKET_NAMES)
        print(f"{'DAY TOT':<7}{'':>6} {'':>4}{'':>4}  {tot_cells}   (names with >=1 entry: {traded_rows})")

    # ---- combined totals ----
    print(f"\n{'='*118}\nCOMBINED (all days) — net P&L by hard-stop bucket\n{'='*118}")
    print("".join(f"{b:>10}" for b in BUCKET_NAMES))
    print("".join(f"{bucket_tot[b]:>+10.2f}" for b in BUCKET_NAMES))

    # ---- volatility split (test 'winners cluster volatile-vs-slow') ----
    voled = [row for row in all_rows if row[2] is not None and row[4] > 0]  # has vol + >=1 break
    if voled:
        vols = sorted(v for *_x, v, _s, _b, _bk in [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in voled])
        med = vols[len(vols) // 2]
        print(f"\n{'='*118}\nVOLATILITY SPLIT — median ATR% across traded name-days = {med:.2f}%  "
              f"(HI = >= median, 'volatile like CLRO'; LO = < median, 'slow')\n{'='*118}")
        for label, pred in (("HI-vol (volatile)", lambda v: v >= med), ("LO-vol (slow)", lambda v: v < med)):
            grp = [r for r in voled if pred(r[2])]
            print(f"\n{label}:  {len(grp)} name-days")
            split = {b: 0.0 for b in BUCKET_NAMES}
            for _dt, _sym, _v, _s, _bk, buckets in grp:
                for b in BUCKET_NAMES:
                    split[b] += buckets[b]["pnl"]
            print("  " + "".join(f"{b:>10}" for b in BUCKET_NAMES))
            print("  " + "".join(f"{split[b]:>+10.2f}" for b in BUCKET_NAMES))

    # ---- per-name detail sorted by volatility (clustering visual) ----
    print(f"\n{'='*118}\nPER NAME-DAY sorted by ATR% (best-bucket net) — clustering view\n{'='*118}")
    detail = [r for r in all_rows if r[2] is not None and r[4] > 0]
    detail.sort(key=lambda r: r[2], reverse=True)
    print(f"{'DATE':<11}{'SYMBOL':<7}{'ATR%':>6}  {'bestBucket':>12}{'bestNet':>10}   {'-1.5% net':>10}")
    for dt, sym, v, _s, _bk, buckets in detail:
        best_b = max(BUCKET_NAMES, key=lambda b: buckets[b]["pnl"])
        print(f"{dt:<11}{sym:<7}{v:>6.2f}  {best_b:>12}{buckets[best_b]['pnl']:>+10.2f}   {buckets['-1.5%']['pnl']:>+10.2f}")


if __name__ == "__main__":
    main()
