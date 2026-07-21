#!/usr/bin/env python3
"""Honest-fill rerun: idealized bar-level walk vs quote-based fills, SAME signals.

The bar-level walk is an upper bound -- entry at a trade print, stop/floor filling exactly
at their levels, no spread, no latency. Measured spreads on these names are 0.20-0.89%
(median ~0.5%) while the surviving OOS signal is +0.353%/trade, so the haircut plausibly
exceeds the entire edge. This runs BOTH fill models over the SAME signals so the difference
IS the haircut, measured rather than assumed.

Quote coverage starts 2026-07-13 (trades go back to 07-09), so this is a ~7-day window.
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
    Cell,
    confirmed_windows,
    simulate_cell,
    simulate_cell_honest,
    to_oracle_bars,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

# The locked config (floor 2 per the OOS head-to-head), plus neighbours for context.
PROXIMITIES = (1.5, 2.0, 2.5)
FLOOR_START = 2.0
STOP = -5.0
LATENCIES = (0.5, 1.0, 2.0)   # report a BAND, never a point


def _stats(pcts):
    if not pcts:
        return {"n": 0}
    mean = statistics.fmean(pcts)
    sd = statistics.pstdev(pcts) if len(pcts) > 1 else 0.0
    half = 1.96 * sd / (len(pcts) ** 0.5) if len(pcts) > 1 else 0.0
    return {"n": len(pcts), "mean": mean, "median": statistics.median(pcts),
            "win": 100.0 * len([x for x in pcts if x > 0]) / len(pcts),
            "ci_lo": mean - half, "ci_hi": mean + half,
            "ci_excl0": (mean - half > 0) or (mean + half < 0)}


def _fmt(label, s):
    if not s.get("n"):
        return f"  {label:<34} NO TRADES"
    return (f"  {label:<34} n={s['n']:>4} mean={s['mean']:>+7.3f}% med={s['median']:>+7.3f}% "
            f"win={s['win']:>5.1f}% CI[{s['ci_lo']:>+6.2f},{s['ci_hi']:>+6.2f}] excl0={s['ci_excl0']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    args = ap.parse_args()

    sf = build_session_factory(get_settings())
    src = DbMarketDataSource(sf)
    with sf() as session:
        windows = confirmed_windows(session, args.days)

    ideal = {p: Cell(p, "ideal") for p in PROXIMITIES}
    honest = {(p, lat): Cell(p, f"honest@{lat}s") for p in PROXIMITIES for lat in LATENCIES}
    used = no_quotes = 0

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
            no_quotes += 1
            continue
        rows = compute_atr_trail(bars)
        cutoff = int(confirm_at.timestamp() * 1000)
        for i, r in enumerate(rows):
            if bars[i].ts < cutoff:
                r["state"] = "warmup"
        day = confirm_at.date().isoformat()

        for p in PROXIMITIES:
            ideal[p].trades.extend(simulate_cell(
                bars, rows, symbol=symbol, day=day, threshold_pct=p,
                fill_mode="next_open", exit_mode="floor_ladder",
                stop_pct=STOP, floor_start_pct=FLOOR_START))
            for lat in LATENCIES:
                honest[(p, lat)].trades.extend(simulate_cell_honest(
                    bars, rows, quotes, symbol=symbol, day=day, threshold_pct=p,
                    stop_pct=STOP, floor_start_pct=FLOOR_START, latency_s=lat))
        used += 1

    print(f"windows used={used}  skipped_no_quotes={no_quotes}")
    print(f"config: stop {STOP}% / floor start {FLOOR_START}% / no filter\n")

    out = {}
    for p in PROXIMITIES:
        print("=" * 96)
        print(f"PROXIMITY {p}%")
        print("=" * 96)
        si = _stats([t.pnl_pct for t in ideal[p].trades])
        print(_fmt("IDEALIZED (bar-level, upper bound)", si))
        for lat in LATENCIES:
            sh = _stats([t.pnl_pct for t in honest[(p, lat)].trades])
            print(_fmt(f"HONEST (ask in / bid out, {lat}s lat)", sh))
            if si.get("n") and sh.get("n"):
                print(f"      -> HAIRCUT vs idealized: {sh['mean'] - si['mean']:+.3f}pp")
            out[f"{p}|{lat}"] = {"ideal": si, "honest": sh}
        c = honest[(p, 1.0)]
        agg = defaultdict(list)
        for t in c.trades:
            agg[t.reason].append(t.pnl_pct)
        print(f"  exits @1.0s: " + ", ".join(
            f"{k}: n={len(v)} mean={statistics.fmean(v):+.2f}%" for k, v in sorted(agg.items())))
        print()

    with open("/tmp/honest_fill.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("detail -> /tmp/honest_fill.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
