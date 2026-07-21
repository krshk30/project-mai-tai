#!/usr/bin/env python3
"""Out-of-sample validation for the ATR-proximity entry. R&D, read-only.

TWO TESTS, and only the second is real:

  TEST A -- STABILITY CHECK (weak). The recommended config on each half separately.
    This is NOT out-of-sample: that config was chosen while looking at all 9 days, so both
    halves informed the choice. It can only detect instability, never confirm an edge.

  TEST B -- HONEST WALK-FORWARD (the real one). Re-run the WHOLE grid on the first half
    ONLY, pick the winner there using nothing from the second half, then evaluate that
    winner on the second half exactly once. This is the test that killed the ORB "+11.2".

Reported together so the weak test can never be mistaken for the strong one.
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
from project_mai_tai.backtest.dot_entry import build_rows, make_filter
from project_mai_tai.backtest.proximity_sweep import (
    Cell,
    confirmed_windows,
    simulate_cell,
    to_oracle_bars,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

PROXIMITIES = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
FLOORS = (2.0, 3.0)
FILTERS = ("none", "volume_strict", "volume_hold", "volume_sustained")
STOP = -5.0

# The config recommended off the full 9 days (Test A subject).
LOCKED = (2.0, 3.0, "none")

MIN_N_FOR_SELECTION = 30   # a 12-trade cell must not be allowed to win the selection


def _stats(pcts: list[float]) -> dict:
    if not pcts:
        return {"n": 0}
    mean = statistics.fmean(pcts)
    sd = statistics.pstdev(pcts) if len(pcts) > 1 else 0.0
    half = 1.96 * sd / (len(pcts) ** 0.5) if len(pcts) > 1 else 0.0
    return {
        "n": len(pcts),
        "mean": mean,
        "median": statistics.median(pcts),
        "win": 100.0 * len([x for x in pcts if x > 0]) / len(pcts),
        "ci_lo": mean - half,
        "ci_hi": mean + half,
        "ci_excl0": (mean - half > 0) or (mean + half < 0),
    }


def _fmt(label: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {label:<26} NO TRADES"
    return (f"  {label:<26} n={s['n']:>4}  mean={s['mean']:>+7.3f}%  med={s['median']:>+7.3f}%  "
            f"win={s['win']:>5.1f}%  CI[{s['ci_lo']:>+6.2f},{s['ci_hi']:>+6.2f}] "
            f"excl0={str(s['ci_excl0'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=9)
    ap.add_argument("--split", type=int, default=5, help="trading days in the IN-SAMPLE half")
    args = ap.parse_args()

    sf = build_session_factory(get_settings())
    src = DbMarketDataSource(sf)
    with sf() as session:
        windows = confirmed_windows(session, args.days)

    cells = {(p, fl, f): Cell(p, f"{fl}|{f}")
             for p in PROXIMITIES for fl in FLOORS for f in FILTERS}
    used = 0
    for n, (symbol, confirm_at, drop_at) in enumerate(sorted(windows), 1):
        start = confirm_at - timedelta(minutes=40)
        tape = src.trades(symbol, start, drop_at)
        if not tape:
            continue
        bars = to_oracle_bars(build_bars(tape, start))
        if len(bars) < 20:
            continue
        rows = compute_atr_trail(bars)
        cutoff = int(confirm_at.timestamp() * 1000)
        for i, r in enumerate(rows):
            if bars[i].ts < cutoff:
                r["state"] = "warmup"
        dot = build_rows([b.high for b in bars], [b.low for b in bars], [b.close for b in bars])
        vols = [float(b.volume) for b in bars]
        filters = {k: make_filter(k, dot, vols) for k in FILTERS}
        day = confirm_at.date().isoformat()
        for p in PROXIMITIES:
            for fl in FLOORS:
                for f in FILTERS:
                    cells[(p, fl, f)].trades.extend(simulate_cell(
                        bars, rows, symbol=symbol, day=day, threshold_pct=p,
                        fill_mode="next_open", exit_mode="floor_ladder",
                        stop_pct=STOP, floor_start_pct=fl, signal_filter=filters[f]))
        used += 1

    all_days = sorted({t.day for c in cells.values() for t in c.trades})
    in_days, oos_days = set(all_days[:args.split]), set(all_days[args.split:])
    print(f"windows used={used}")
    print(f"trading days ({len(all_days)}): {all_days}")
    print(f"IN-SAMPLE  ({len(in_days)}): {sorted(in_days)}")
    print(f"OUT-SAMPLE ({len(oos_days)}): {sorted(oos_days)}\n")

    def split(cell: Cell) -> tuple[list[float], list[float]]:
        return ([t.pnl_pct for t in cell.trades if t.day in in_days],
                [t.pnl_pct for t in cell.trades if t.day in oos_days])

    # ---------------- TEST A: stability of the pre-chosen config (WEAK) ----------------
    print("=" * 100)
    print("TEST A -- STABILITY CHECK (weak: this config saw BOTH halves during selection)")
    print(f"         locked config: proximity {LOCKED[0]}% / stop {STOP}% / floor {LOCKED[1]}% / filter {LOCKED[2]}")
    print("=" * 100)
    locked_cell = cells[LOCKED]
    ins, oos = split(locked_cell)
    print(_fmt("first half", _stats(ins)))
    print(_fmt("second half", _stats(oos)))
    print(_fmt("full 9 days", _stats(ins + oos)))

    # ---------------- TEST B: honest walk-forward (THE REAL TEST) ----------------
    print("\n" + "=" * 100)
    print("TEST B -- HONEST WALK-FORWARD: select on the FIRST half only, evaluate ONCE on the second")
    print("=" * 100)
    ranked = []
    for key, cell in cells.items():
        i_p, o_p = split(cell)
        if len(i_p) >= MIN_N_FOR_SELECTION:
            ranked.append((statistics.fmean(i_p), key, i_p, o_p))
    ranked.sort(reverse=True)

    print(f"\n  in-sample ranking (n >= {MIN_N_FOR_SELECTION} to be selectable), top 8:")
    for mean, key, i_p, o_p in ranked[:8]:
        print(f"    prox={key[0]:<4} floor={key[1]:<4} filter={key[2]:<17} "
              f"in-sample n={len(i_p):>3} mean={mean:>+7.3f}%")

    if not ranked:
        print("  NO CELL met the minimum-n bar in the first half -- cannot select. Sample too thin.")
        return 1

    best_mean, best_key, best_in, best_oos = ranked[0]
    print(f"\n  SELECTED (first half only): prox={best_key[0]}% floor={best_key[1]}% filter={best_key[2]}")
    print(_fmt("  in-sample", _stats(best_in)))
    print(_fmt("  OUT-OF-SAMPLE", _stats(best_oos)))

    s_in, s_oos = _stats(best_in), _stats(best_oos)
    if s_oos.get("n", 0) == 0:
        verdict = "NO OOS TRADES -- inconclusive"
    elif s_oos["mean"] > 0 and s_oos["ci_excl0"]:
        verdict = "SURVIVED (positive, CI excludes zero)"
    elif s_oos["mean"] > 0:
        verdict = "DIRECTIONALLY SURVIVED (positive, but CI spans zero)"
    else:
        verdict = "FAILED (negative out-of-sample)"
    print(f"\n  >>> WALK-FORWARD VERDICT: {verdict}")
    print(f"      in-sample mean {s_in['mean']:+.3f}%  ->  OOS mean {s_oos.get('mean', float('nan')):+.3f}%")
    print(f"      decay: {s_oos.get('mean', 0) - s_in['mean']:+.3f}pp")

    # Full per-cell table so the reader can see the whole surface, not just the winner.
    print("\n" + "=" * 100)
    print("ALL CELLS -- in-sample vs out-of-sample mean (watch for sign flips)")
    print("=" * 100)
    print(f"  {'prox':>5} {'floor':>6} {'filter':<18} {'in_n':>5} {'in_mean':>9} {'oos_n':>6} {'oos_mean':>9} {'flip':>6}")
    out = {}
    for key in sorted(cells):
        i_p, o_p = split(cells[key])
        if not i_p and not o_p:
            continue
        im = statistics.fmean(i_p) if i_p else float("nan")
        om = statistics.fmean(o_p) if o_p else float("nan")
        flip = (im == im and om == om and (im > 0) != (om > 0))
        print(f"  {key[0]:>5} {key[1]:>6} {key[2]:<18} {len(i_p):>5} {im:>+9.3f} "
              f"{len(o_p):>6} {om:>+9.3f} {str(flip):>6}")
        out[f"{key[0]}|{key[1]}|{key[2]}"] = {"in": _stats(i_p), "oos": _stats(o_p), "flip": flip}

    with open("/tmp/oos_split.json", "w") as fh:
        json.dump({"in_days": sorted(in_days), "oos_days": sorted(oos_days),
                   "selected": list(best_key), "verdict": verdict, "cells": out},
                  fh, indent=2, default=str)
    print("\ndetail -> /tmp/oos_split.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
