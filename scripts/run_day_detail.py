#!/usr/bin/env python3
"""Per-trade forensics for ONE day, both floor variants. R&D, read-only.

Config A = floor start 3% ; Config B = floor start 2%. Everything else identical
(proximity 2.0%, stop -5%, no filter, one entry per short-segment, same atr_oracle,
same scanner CONFIRM->drop gating).

Prints HONEST fills (ask in / bid out, market-on-touch exits) as the headline, with the
idealized bar-level number alongside so the per-trade haircut is visible. All times ET.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.proximity_sweep import (
    confirmed_windows,
    simulate_cell,
    simulate_cell_honest,
    to_oracle_bars,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

ET = ZoneInfo("America/New_York")
PROX = 2.0
STOP = -5.0
CONFIGS = {"A": 3.0, "B": 2.0}   # floor start


def et(ms):
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(ET).strftime("%H:%M:%S")


def et_dt(dt):
    return dt.astimezone(ET).strftime("%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="YYYY-MM-DD (ET)")
    args = ap.parse_args()

    sf = build_session_factory(get_settings())
    src = DbMarketDataSource(sf)
    with sf() as session:
        windows = confirmed_windows(session, 14)
    windows = [w for w in windows if w[1].astimezone(ET).date().isoformat() == args.day]
    print(f"=== {args.day} (ET) — scanner-confirmed windows: {len(windows)} ===\n")

    scanner_rows = []
    trade_rows = []
    for symbol, confirm_at, drop_at in sorted(windows):
        start = confirm_at - timedelta(minutes=40)
        tape = src.trades(symbol, start, drop_at)
        quotes = src.quotes(symbol, start, drop_at)
        note = ""
        bars = to_oracle_bars(build_bars(tape, start)) if tape else []
        if not tape:
            note = "no captured tape"
        elif len(bars) < 20:
            note = f"thin ({len(bars)} bars)"
        elif not quotes:
            note = "no quotes"
        scanner_rows.append((symbol, et_dt(confirm_at), et_dt(drop_at),
                             round((drop_at - confirm_at).total_seconds() / 60, 1),
                             len(bars), len(quotes) if quotes else 0, note or "ok"))
        if note:
            continue

        rows = compute_atr_trail(bars)
        cutoff = int(confirm_at.timestamp() * 1000)
        for i, r in enumerate(rows):
            if bars[i].ts < cutoff:
                r["state"] = "warmup"

        for cfg, floor in CONFIGS.items():
            honest = simulate_cell_honest(
                bars, rows, quotes, symbol=symbol, day=args.day, threshold_pct=PROX,
                stop_pct=STOP, floor_start_pct=floor, latency_s=1.0)
            ideal = simulate_cell(
                bars, rows, symbol=symbol, day=args.day, threshold_pct=PROX,
                fill_mode="next_open", exit_mode="floor_ladder",
                stop_pct=STOP, floor_start_pct=floor)
            ideal_by_ts = {t.entry_ts: t for t in ideal}
            for t in honest:
                it = ideal_by_ts.get(t.entry_ts)
                trade_rows.append((
                    symbol, cfg, et(t.entry_ts), et(t.exit_ts),
                    round(t.signal_prox_pct, 2) if t.signal_prox_pct is not None else None,
                    round(t.trail_at_signal, 4) if t.trail_at_signal else None,
                    round(t.entry_price, 4), round(t.exit_price, 4), t.reason,
                    round(t.pnl_pct, 2), round(it.pnl_pct, 2) if it else None,
                ))

    print("SCANNER WINDOWS")
    print(f"{'symbol':<8}{'confirm':>10}{'drop':>10}{'mins':>7}{'bars':>6}{'quotes':>9}  status")
    for r in scanner_rows:
        print(f"{r[0]:<8}{r[1]:>10}{r[2]:>10}{r[3]:>7}{r[4]:>6}{r[5]:>9}  {r[6]}")

    print(f"\nTRADES (proximity {PROX}%, stop {STOP}%, A=floor3 B=floor2, honest fills)")
    if not trade_rows:
        print("  NO TRADES")
    else:
        print(f"{'symbol':<8}{'cfg':>4}{'entry':>10}{'exit':>10}{'prox%':>7}{'trail':>9}"
              f"{'entryPx':>9}{'exitPx':>9}{'reason':>8}{'honest%':>9}{'ideal%':>8}")
        for r in trade_rows:
            print(f"{r[0]:<8}{r[1]:>4}{r[2]:>10}{r[3]:>10}"
                  f"{(r[4] if r[4] is not None else 0):>7}{(r[5] or 0):>9}"
                  f"{r[6]:>9}{r[7]:>9}{r[8]:>8}{r[9]:>+9.2f}"
                  f"{(f'{r[10]:+.2f}' if r[10] is not None else '-'):>8}")

        for cfg in CONFIGS:
            sub = [r for r in trade_rows if r[1] == cfg]
            if not sub:
                continue
            h = [r[9] for r in sub]
            i = [r[10] for r in sub if r[10] is not None]
            wins = len([x for x in h if x > 0])
            print(f"\n  Config {cfg} (floor {CONFIGS[cfg]}%): n={len(h)} "
                  f"honest mean={sum(h)/len(h):+.3f}%  win={100*wins/len(h):.1f}%"
                  + (f"  | idealized mean={sum(i)/len(i):+.3f}%  haircut={sum(h)/len(h)-sum(i)/len(i):+.3f}pp"
                     if i else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
