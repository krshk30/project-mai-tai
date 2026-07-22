#!/usr/bin/env python3
"""Resting-limit entry vs the chase entry. R&D, read-only.

Resting BUY LIMIT at trail*(1-X%), filled only if the ask actually reaches it, at the level.
Live window 07:00-16:30 ET applied to BOTH arms so the comparison is like-for-like.
Honest fills throughout (bid-based exits, market-on-touch).

Reports the no-fill accounting, which is the part that decides it: a resting order that
misses the crossing setups is not saving money, it is missing winners.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import timedelta

from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.proximity_sweep import (
    _in_entry_window,
    confirmed_windows,
    simulate_cell_honest,
    simulate_limit_pullback_entry,
    simulate_resting_entry,
    to_oracle_bars,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

OFFSETS = (1.0, 2.5, 3.0)
PULLBACKS = (0.5, 1.0, 2.0)   # concept 3: buy LIMIT below market
CHASE_PROX = 2.0
STOP = -5.0
FLOOR = 2.0


def _stats(p):
    if not p:
        return {"n": 0}
    mean = statistics.fmean(p)
    sd = statistics.pstdev(p) if len(p) > 1 else 0.0
    half = 1.96 * sd / (len(p) ** 0.5) if len(p) > 1 else 0.0
    return {"n": len(p), "mean": mean, "median": statistics.median(p),
            "win": 100.0 * len([x for x in p if x > 0]) / len(p),
            "ci_lo": mean - half, "ci_hi": mean + half,
            "ci_excl0": (mean - half > 0) or (mean + half < 0)}


def _line(label, s):
    if not s.get("n"):
        return f"  {label:<38} NO TRADES"
    return (f"  {label:<38} n={s['n']:>4} mean={s['mean']:>+7.3f}% med={s['median']:>+7.3f}% "
            f"win={s['win']:>5.1f}% CI[{s['ci_lo']:>+6.2f},{s['ci_hi']:>+6.2f}] excl0={s['ci_excl0']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    args = ap.parse_args()

    sf = build_session_factory(get_settings())
    src = DbMarketDataSource(sf)
    with sf() as session:
        windows = confirmed_windows(session, args.days)

    resting = {o: [] for o in OFFSETS}
    pull = {y: [] for y in PULLBACKS}
    pull_acct = {y: defaultdict(int) for y in PULLBACKS}
    acct_tot = {o: defaultdict(int) for o in OFFSETS}
    chase = []
    used = 0

    for symbol, confirm_at, drop_at in sorted(windows):
        start = confirm_at - timedelta(minutes=40)
        tape = src.trades(symbol, start, drop_at)
        if not tape:
            continue
        bars = to_oracle_bars(build_bars(tape, start))
        if len(bars) < 20:
            continue
        quotes = src.quotes(symbol, start, drop_at)
        if not quotes:
            continue
        rows = compute_atr_trail(bars)
        cutoff = int(confirm_at.timestamp() * 1000)
        for i, r in enumerate(rows):
            if bars[i].ts < cutoff:
                r["state"] = "warmup"
        day = confirm_at.date().isoformat()

        # Chase arm: same window filter applied, so this is like-for-like.
        for t in simulate_cell_honest(bars, rows, quotes, symbol=symbol, day=day,
                                      threshold_pct=CHASE_PROX, stop_pct=STOP,
                                      floor_start_pct=FLOOR, latency_s=1.0):
            if _in_entry_window(t.entry_ts):
                chase.append(t)

        for y in PULLBACKS:
            tr, ac = simulate_limit_pullback_entry(
                bars, rows, quotes, symbol=symbol, day=day, proximity_pct=CHASE_PROX,
                pullback_pct=y, stop_pct=STOP, floor_start_pct=FLOOR)
            pull[y].extend(tr)
            for k, v in ac.items():
                pull_acct[y][k] += v

        for o in OFFSETS:
            tr, ac = simulate_resting_entry(bars, rows, quotes, symbol=symbol, day=day,
                                            offset_pct=o, stop_pct=STOP, floor_start_pct=FLOOR)
            resting[o].extend(tr)
            for k, v in ac.items():
                acct_tot[o][k] += v
        used += 1

    print(f"windows used={used}   window filter: 07:00-16:30 ET (applied to BOTH arms)")
    print(f"exit: floor ladder start {FLOOR}%, stop {STOP}%, honest bid fills\n")

    sc = _stats([t.pnl_pct for t in chase])
    print("=" * 104)
    print(f"BASELINE — CHASE entry (proximity {CHASE_PROX}%, fill at signal-bar close/ask)")
    print("=" * 104)
    print(_line("chase, in-window only", sc))

    out = {"chase": sc}
    for o in OFFSETS:
        print("\n" + "=" * 104)
        print(f"RESTING LIMIT at trail x (1 - {o}%)")
        print("=" * 104)
        s = _stats([t.pnl_pct for t in resting[o]])
        print(_line(f"resting @ -{o}%", s))
        a = acct_tot[o]
        seg = a["segments"] or 1
        print(f"  segments={a['segments']}  filled={a['filled']} ({100*a['filled']/seg:.1f}%)  "
              f"MISSED_CROSS={a['missed_cross']}  avoided_no_cross={a['avoided_no_cross']}")
        if a["missed_cross"] + a["filled"]:
            print(f"  -> of segments that CROSSED, we captured "
                  f"{100*a['filled']/max(1,(a['filled']+a['missed_cross'])):.1f}%")
        if s.get("n") and sc.get("n"):
            print(f"  -> vs chase: {s['mean'] - sc['mean']:+.3f}pp")
        agg = defaultdict(list)
        for t in resting[o]:
            agg[t.reason].append(t.pnl_pct)
        if agg:
            print("  exits: " + ", ".join(
                f"{k}: n={len(v)} mean={statistics.fmean(v):+.2f}%" for k, v in sorted(agg.items())))
        out[f"resting_{o}"] = {"stats": s, "accounting": dict(a)}

    for y in PULLBACKS:
        print("\n" + "=" * 104)
        print(f"CONCEPT 3 — BUY LIMIT at signal close x (1 - {y}%)  [same signal, better price demanded]")
        print("=" * 104)
        s3 = _stats([t.pnl_pct for t in pull[y]])
        print(_line(f"limit pullback -{y}%", s3))
        a = pull_acct[y]
        sig = a["signals"] or 1
        print(f"  signals={a['signals']}  in-window={a['signals']-a['out_of_window']}  "
              f"FILLED={a['filled']}  MISSED_CROSS={a['missed_cross']}  no_fill_no_cross={a['no_fill_no_cross']}")
        inw = a["signals"] - a["out_of_window"]
        if inw:
            print(f"  fill rate (in-window) = {100*a['filled']/inw:.1f}%")
        if s3.get("n") and sc.get("n"):
            print(f"  -> vs chase: {s3['mean'] - sc['mean']:+.3f}pp")
        agg = defaultdict(list)
        for t in pull[y]:
            agg[t.reason].append(t.pnl_pct)
        if agg:
            print("  exits: " + ", ".join(
                f"{k}: n={len(v)} mean={statistics.fmean(v):+.2f}%" for k, v in sorted(agg.items())))
        out[f"pullback_{y}"] = {"stats": s3, "accounting": dict(a)}

    with open("/tmp/resting_entry.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\ndetail -> /tmp/resting_entry.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
