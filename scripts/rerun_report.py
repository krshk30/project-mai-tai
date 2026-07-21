#!/usr/bin/env python3
"""Post-backfill re-run report on SCHWAB REST bars. R&D, read-only.

Delivery order is fixed and deliberate:
  GATE 0  coverage + bar-correctness. If the corpus is thin or the bars do not reproduce what
          the bot saw, NOTHING below it is worth reading -- so it is reported first and can
          fail the whole run.
  TABLE   per-study HELD / FLIPPED against the Polygon numbers from 2026-07-21.
  VERDICT the two headlines: does any entry survive, and do the exit-side findings hold.

Bars: /var/lib/project-mai-tai/schwab_rest_bars (production truth).
Quotes: market_capture_quotes (Polygon NBBO) -- point-in-time reads, unaffected by the
aggregation/recursion defect that poisoned the bar-derived signals.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean, median, pstdev
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.proximity_sweep import (
    _in_entry_window,
    simulate_cell_honest,
    simulate_limit_pullback_entry,
    simulate_resting_entry,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

ET = ZoneInfo("America/New_York")
BARS = Path("/var/lib/project-mai-tai/schwab_rest_bars")
STOP = -5.0

# Polygon-bar reference numbers produced 2026-07-21 (in-window, honest fills unless noted).
REF = {
    "chase_2.0_floor2":      (+0.134, 54, 68.5),
    "pullback_1.0_floor2":   (+0.565, 36, 75.0),
    "buystop_2.0_floor2":    (-1.961, 69, 55.1),
    "chase_1.5_floor2":      (-0.588, 102, 54.9),
    "chase_2.5_floor2":      (-0.820, 128, 53.9),
    "chase_2.0_floor3":      (-1.138, 118, 49.2),
}


def st(p):
    if not p:
        return None
    m = fmean(p)
    sd = pstdev(p) if len(p) > 1 else 0.0
    h = 1.96 * sd / (len(p) ** 0.5) if len(p) > 1 else 0.0
    return {"n": len(p), "mean": m, "median": median(p),
            "win": 100.0 * len([x for x in p if x > 0]) / len(p),
            "lo": m - h, "hi": m + h, "excl0": (m - h > 0) or (m + h < 0)}


def load_day(day_dir: Path) -> dict[str, list[Bar]]:
    out = {}
    for f in sorted(day_dir.glob("*.json")):
        try:
            rows = json.loads(f.read_text())
        except Exception:
            continue
        bars = [Bar(ts=int(c["datetime"]), open=float(c["open"]), high=float(c["high"]),
                    low=float(c["low"]), close=float(c["close"]), volume=int(c["volume"]))
                for c in rows]
        bars = [b for b in bars if b.close > 0]
        if len(bars) >= 80:
            out[f.stem] = bars
    return out


def main() -> int:
    settings = get_settings()
    sf = build_session_factory(settings)
    src = DbMarketDataSource(sf)

    print("=" * 92)
    print("SCHWAB-BAR RE-RUN REPORT —", datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"))
    print("=" * 92)

    # ---------------------------------------------------------------- GATE 0
    print("\n" + "#" * 92)
    print("# GATE 0 — COVERAGE + BAR CORRECTNESS  (if this fails, ignore everything below)")
    print("#" * 92)
    if not BARS.exists():
        print("  FAIL: no corpus at", BARS)
        return 1
    days = sorted([d for d in BARS.iterdir() if d.is_dir()])
    corpus = {d.name: load_day(d) for d in days}
    tot_sd = sum(len(v) for v in corpus.values())
    tot_bars = sum(len(b) for v in corpus.values() for b in v.values())
    print(f"\n  COVERAGE: {len(days)} days, {tot_sd} symbol-days (>=80 bars), {tot_bars} bars")
    for d in days:
        n = len(corpus[d.name])
        bb = sum(len(b) for b in corpus[d.name].values())
        print(f"    {d.name}  symbol-days={n:>3}  bars={bb:>6}")
    print(f"\n  vs the sparse strategy_bar_history re-run earlier today: 11 usable symbol-days")

    # bar correctness: REST vs what the bot logged, where both exist
    print("\n  BAR CORRECTNESS (REST vs the bot's own strategy_bar_history, same minute):")
    ohlc_match = ohlc_tot = 0
    close_bps = []
    with sf() as s:
        rows = s.execute(text("""
            SELECT symbol, bar_time, open_price, high_price, low_price, close_price
            FROM strategy_bar_history
            WHERE strategy_code='schwab_1m_v2' AND bar_time > now() - interval '10 days'
        """)).all()
    bot = {(r[0], int(r[1].timestamp() * 1000)): (float(r[2]), float(r[3]), float(r[4]), float(r[5]))
           for r in rows}
    for dname, syms in corpus.items():
        for sym, bars in syms.items():
            for b in bars:
                k = (sym, b.ts)
                if k in bot:
                    o, h, l_, c = bot[k]
                    ohlc_tot += 1
                    if abs(b.close - c) < 1e-6 and abs(b.high - h) < 1e-6 and abs(b.low - l_) < 1e-6:
                        ohlc_match += 1
                    if c:
                        close_bps.append(abs(b.close - c) / c * 10000.0)
    if ohlc_tot:
        print(f"    overlapping bars={ohlc_tot}  exact OHLC match={100*ohlc_match/ohlc_tot:.2f}%"
              f"  median |close err|={median(close_bps):.2f} bps")
        gate0 = (100 * ohlc_match / ohlc_tot) > 95.0 and tot_sd >= 30
    else:
        print("    no overlap to compare (bot log empty for this range)")
        gate0 = tot_sd >= 30
    print(f"\n  >>> GATE 0: {'PASS' if gate0 else 'FAIL'}"
          f"   (needs >95% exact OHLC match and >=30 symbol-days)")
    if not gate0:
        print("  Stopping: the corpus cannot support the studies below.")
        return 1

    # ---------------------------------------------------------------- studies
    print("\n" + "#" * 92)
    print("# PER-STUDY: SCHWAB BARS vs THE POLYGON NUMBERS")
    print("#" * 92)

    res = defaultdict(list)
    acct = defaultdict(lambda: defaultdict(int))
    for dname, syms in corpus.items():
        d0 = datetime.fromisoformat(dname).replace(tzinfo=ET)
        for sym, bars in syms.items():
            quotes = src.quotes(sym,
                                datetime.fromtimestamp(bars[0].ts / 1000, timezone.utc),
                                datetime.fromtimestamp(bars[-1].ts / 1000 + 900, timezone.utc))
            if not quotes:
                continue
            atr = compute_atr_trail(bars)
            for prox, floor, key in ((2.0, 2.0, "chase_2.0_floor2"), (1.5, 2.0, "chase_1.5_floor2"),
                                     (2.5, 2.0, "chase_2.5_floor2"), (2.0, 3.0, "chase_2.0_floor3")):
                for t in simulate_cell_honest(bars, atr, quotes, symbol=sym, day=dname,
                                              threshold_pct=prox, stop_pct=STOP,
                                              floor_start_pct=floor, latency_s=1.0):
                    if _in_entry_window(t.entry_ts):
                        res[key].append(t.pnl_pct)
            tr, ac = simulate_limit_pullback_entry(bars, atr, quotes, symbol=sym, day=dname,
                                                   proximity_pct=2.0, pullback_pct=1.0,
                                                   stop_pct=STOP, floor_start_pct=2.0)
            res["pullback_1.0_floor2"].extend(t.pnl_pct for t in tr)
            for k2, v in ac.items():
                acct["pullback"][k2] += v
            tr2, ac2 = simulate_resting_entry(bars, atr, quotes, symbol=sym, day=dname,
                                              offset_pct=2.0, stop_pct=STOP, floor_start_pct=2.0)
            res["buystop_2.0_floor2"].extend(t.pnl_pct for t in tr2)

    hdr = f"  {'study':<24}{'POLY mean':>11}{'POLY n':>8}{'SCHWAB mean':>13}{'SCHWAB n':>10}{'win%':>7}{'CI excl 0':>11}  VERDICT"
    print("\n" + hdr)
    print("  " + "-" * (len(hdr) - 2))
    verdicts = {}
    for key, (pm, pn, _pw) in REF.items():
        s2 = st(res.get(key, []))
        if not s2:
            print(f"  {key:<24}{pm:>+11.3f}{pn:>8}{'NO TRADES':>13}")
            verdicts[key] = "NO DATA"
            continue
        same_sign = (pm > 0) == (s2["mean"] > 0)
        v = "HELD" if same_sign else "FLIPPED"
        verdicts[key] = v
        print(f"  {key:<24}{pm:>+11.3f}{pn:>8}{s2['mean']:>+13.3f}{s2['n']:>10}"
              f"{s2['win']:>7.1f}{str(s2['excl0']):>11}  {v}")

    # orderings
    print("\n  ORDERINGS:")
    c2 = st(res.get("chase_2.0_floor2", []))
    pb = st(res.get("pullback_1.0_floor2", []))
    bs = st(res.get("buystop_2.0_floor2", []))
    f3 = st(res.get("chase_2.0_floor3", []))
    if c2 and pb:
        poly = "pullback > chase"
        now = "pullback > chase" if pb["mean"] > c2["mean"] else "chase > pullback"
        print(f"    pullback vs chase : POLY={poly:<18} SCHWAB={now:<18} "
              f"{'HELD' if poly == now else 'FLIPPED'}")
    if c2 and bs:
        print(f"    buy-stop is worst : POLY=yes                SCHWAB="
              f"{'yes' if bs['mean'] < c2['mean'] else 'NO'}")
    if c2 and f3:
        print(f"    floor2 vs floor3  : POLY=floor2 better      SCHWAB="
              f"{'floor2 better' if c2['mean'] > f3['mean'] else 'floor3 better'}")

    a = acct["pullback"]
    if a:
        inw = a["signals"] - a["out_of_window"]
        print(f"\n  pullback fill accounting: signals={a['signals']} in-window={inw} "
              f"filled={a['filled']} missed_cross={a['missed_cross']}")

    # ---------------------------------------------------------------- verdicts
    print("\n" + "#" * 92)
    print("# THE TWO HEADLINE VERDICTS")
    print("#" * 92)
    best_k, best = None, None
    for k in ("chase_2.0_floor2", "pullback_1.0_floor2", "chase_1.5_floor2", "chase_2.5_floor2"):
        s2 = st(res.get(k, []))
        if s2 and (best is None or s2["mean"] > best["mean"]):
            best_k, best = k, s2
    print("\n  1. DOES ANY ENTRY SURVIVE ON PRODUCTION BARS?")
    if not best:
        print("     NO DATA")
    else:
        print(f"     best cell = {best_k}: mean {best['mean']:+.3f}% n={best['n']} "
              f"win {best['win']:.1f}% CI[{best['lo']:+.2f},{best['hi']:+.2f}] excl0={best['excl0']}")
        if best["mean"] > 0 and best["excl0"]:
            print("     >>> A POSITIVE CELL WITH CI EXCLUDING ZERO. Still needs a walk-forward "
                  "before it means anything -- that is what killed the last one.")
        elif best["mean"] > 0:
            print("     >>> positive but CI spans zero => NOT an edge. Consistent with every "
                  "entry variant tested this month.")
        else:
            print("     >>> NEGATIVE on production bars. The entry stays dead.")

    print("\n  2. DO THE EXIT-SIDE FINDINGS HOLD?")
    held = [k for k, v in verdicts.items() if v == "HELD"]
    flip = [k for k, v in verdicts.items() if v == "FLIPPED"]
    print(f"     HELD: {len(held)}/{len(verdicts)}   FLIPPED: {len(flip)}")
    if flip:
        print(f"     flipped: {', '.join(flip)}")
    print("     (exit mechanics -- honest-fill haircut, floor-vs-target, buy-stop slippage -- are")
    print("      quote/fill driven and were expected to survive the bar-source change.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
