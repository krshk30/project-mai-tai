#!/usr/bin/env python3
"""ATR-proximity entry sweep: exit geometry x confirmation filter. R&D, read-only.

Grid: 3 proximity thresholds x 3 exit modes x 4 filters = 36 cells. Fill is fixed at
next_open (the honest one; the prior run showed same_bar differs by <0.1% and not even
consistently in sign, so it is not worth doubling the grid).

Run NICED -- heavy R&D contends with the OMS loop (the 07-08 stalls).
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
from project_mai_tai.backtest.dot_entry import FILTER_KINDS, build_rows, make_filter
from project_mai_tai.backtest.proximity_sweep import (
    PROXIMITY_PCTS,
    Cell,
    confirmed_windows,
    simulate_cell,
    to_oracle_bars,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

EXIT_MODES = ("target", "floor_ladder", "trail2")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=9)
    args = ap.parse_args()

    sf = build_session_factory(get_settings())
    src = DbMarketDataSource(sf)
    with sf() as session:
        windows = confirmed_windows(session, args.days)
    print(f"confirmed windows: {len(windows)}", flush=True)

    cells: dict[tuple, Cell] = {
        (p, e, f): Cell(p, f"{e}|{f}")
        for p in PROXIMITY_PCTS for e in EXIT_MODES for f in FILTER_KINDS
    }
    used = 0
    for n, (symbol, confirm_at, drop_at) in enumerate(sorted(windows), 1):
        start = confirm_at - timedelta(minutes=40)
        tape = src.trades(symbol, start, drop_at)
        if not tape:
            continue
        obars = build_bars(tape, start)
        bars = to_oracle_bars(obars)
        if len(bars) < 20:
            continue
        rows = compute_atr_trail(bars)
        cutoff_ms = int(confirm_at.timestamp() * 1000)
        for i, r in enumerate(rows):
            if bars[i].ts < cutoff_ms:
                r["state"] = "warmup"

        dot = build_rows([b.high for b in bars], [b.low for b in bars], [b.close for b in bars])
        vols = [float(b.volume) for b in bars]
        filters = {k: make_filter(k, dot, vols) for k in FILTER_KINDS}

        day = confirm_at.date().isoformat()
        for p in PROXIMITY_PCTS:
            for e in EXIT_MODES:
                for f in FILTER_KINDS:
                    cells[(p, e, f)].trades.extend(simulate_cell(
                        bars, rows, symbol=symbol, day=day, threshold_pct=p,
                        fill_mode="next_open", exit_mode=e, signal_filter=filters[f],
                    ))
        used += 1
        if n % 50 == 0:
            print(f"  ...{n}/{len(windows)}", flush=True)

    print(f"\nwindows used={used}")
    print(f"CELLS SEARCHED: {len(PROXIMITY_PCTS)}x{len(EXIT_MODES)}x{len(FILTER_KINDS)} = {len(cells)}\n")

    hdr = f"{'prox':>5} {'exit':<13} {'filter':<14} {'n':>4} {'names':>5} {'mean%':>8} {'med%':>8} {'win%':>6} {'CI':>20} {'excl0':>6} {'dropone_mean':>20} {'flip':>5}"
    print(hdr)
    print("-" * len(hdr))
    out = {}
    for (p, e, f), cell in sorted(cells.items()):
        s = cell.summary()
        key = f"{p}|{e}|{f}"
        if not s.get("n"):
            print(f"{p:>5} {e:<13} {f:<14} {0:>4}  -- no trades --")
            out[key] = {"n": 0}
            continue
        d = cell.drop_one_by_name()
        means = [x[2] for x in d] or [s["mean_pct"]]
        flip = any((m > 0) != (s["mean_pct"] > 0) for m in means)
        print(f"{p:>5} {e:<13} {f:<14} {s['n']:>4} {s['names']:>5} "
              f"{s['mean_pct']:>+8.3f} {s['median_pct']:>+8.3f} {s['win_rate']:>6.1f} "
              f"[{s['ci_lo']:>+7.3f},{s['ci_hi']:>+7.3f}] {str(s['ci_excludes_zero']):>6} "
              f"[{min(means):>+7.3f},{max(means):>+7.3f}] {str(flip):>5}")
        out[key] = {"summary": s, "flip": flip,
                    "dropone_mean": [min(means), max(means)],
                    "by_reason": _by_reason(cell)}

    with open("/tmp/proximity_grid.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\ndetail -> /tmp/proximity_grid.json")
    return 0


def _by_reason(cell: Cell) -> dict:
    agg = defaultdict(list)
    for t in cell.trades:
        agg[t.reason].append(t.pnl_pct)
    return {k: {"n": len(v), "median_pct": round(statistics.median(v), 3),
                "mean_pct": round(statistics.fmean(v), 3)}
            for k, v in sorted(agg.items())}


if __name__ == "__main__":
    sys.exit(main())
