#!/usr/bin/env python3
"""Stop x floor-start grid for the ATR-proximity entry + floor ladder. R&D, read-only.

Exit mode is FIXED to floor_ladder -- `target` and `trail2` were already answered by the
prior grid (ladder beat target 12/12; trail2 lost 12/12), so re-sweeping them would only
inflate the cell count.

Grid: 3 proximity x 3 stops (-3/-4/-5) x 4 floor starts (2/3/4/5) x 2 filters = 72 cells.
That is a LOT of cells; the report leans on MARGINAL structure (does raising the stop help
at every floor start?) rather than the single best cell, which is what multiple comparisons
would otherwise manufacture.
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
    PROXIMITY_PCTS,
    Cell,
    confirmed_windows,
    simulate_cell,
    to_oracle_bars,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

STOPS = (-3.0, -4.0, -5.0)
FLOOR_STARTS = (2.0, 3.0, 4.0, 5.0)
FILTERS = ("none", "volume_strict")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=9)
    args = ap.parse_args()

    sf = build_session_factory(get_settings())
    src = DbMarketDataSource(sf)
    with sf() as session:
        windows = confirmed_windows(session, args.days)

    cells = {(p, s, fl, f): Cell(p, f"{s}|{fl}|{f}")
             for p in PROXIMITY_PCTS for s in STOPS for fl in FLOOR_STARTS for f in FILTERS}
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

        for p in PROXIMITY_PCTS:
            for s in STOPS:
                for fl in FLOOR_STARTS:
                    for f in FILTERS:
                        cells[(p, s, fl, f)].trades.extend(simulate_cell(
                            bars, rows, symbol=symbol, day=day, threshold_pct=p,
                            fill_mode="next_open", exit_mode="floor_ladder",
                            stop_pct=s, floor_start_pct=fl, signal_filter=filters[f],
                        ))
        used += 1
        if n % 50 == 0:
            print(f"  ...{n}/{len(windows)}", flush=True)

    print(f"\nwindows used={used}")
    print(f"CELLS SEARCHED: {len(PROXIMITY_PCTS)}x{len(STOPS)}x{len(FLOOR_STARTS)}x{len(FILTERS)} = {len(cells)}\n")

    hdr = (f"{'prox':>5} {'stop':>5} {'floor':>6} {'filter':<14} {'n':>4} {'nm':>3} "
           f"{'mean%':>8} {'med%':>7} {'win%':>6} {'CI':>19} {'ex0':>5} {'dropone':>18} {'flip':>5}")
    print(hdr); print("-" * len(hdr))
    out = {}
    for (p, s, fl, f), cell in sorted(cells.items()):
        summ = cell.summary()
        key = f"{p}|{s}|{fl}|{f}"
        if not summ.get("n"):
            out[key] = {"n": 0}
            continue
        d = cell.drop_one_by_name()
        means = [x[2] for x in d] or [summ["mean_pct"]]
        flip = any((m > 0) != (summ["mean_pct"] > 0) for m in means)
        print(f"{p:>5} {s:>5} {fl:>6} {f:<14} {summ['n']:>4} {summ['names']:>3} "
              f"{summ['mean_pct']:>+8.3f} {summ['median_pct']:>+7.3f} {summ['win_rate']:>6.1f} "
              f"[{summ['ci_lo']:>+6.2f},{summ['ci_hi']:>+6.2f}] {str(summ['ci_excludes_zero']):>5} "
              f"[{min(means):>+6.2f},{max(means):>+6.2f}] {str(flip):>5}")
        out[key] = {"summary": summ, "flip": flip,
                    "dropone_mean": [min(means), max(means)],
                    "by_reason": _by_reason(cell)}

    with open("/tmp/stop_floor_grid.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\ndetail -> /tmp/stop_floor_grid.json")
    return 0


def _by_reason(cell: Cell) -> dict:
    agg = defaultdict(list)
    for t in cell.trades:
        agg[t.reason].append(t.pnl_pct)
    return {k: {"n": len(v), "mean_pct": round(statistics.fmean(v), 3)}
            for k, v in sorted(agg.items())}


if __name__ == "__main__":
    sys.exit(main())
