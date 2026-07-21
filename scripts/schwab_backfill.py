#!/usr/bin/env python3
"""Schwab REST 1-min bar backfill -- FRESH PULL, read-only, R&D corpus.

WHY A FRESH PULL AND NOT A HOLE-PATCH. `strategy_bar_history` is an operational log of when
the bot happened to be watching (26 of 37 symbol-days had >5min gaps). Patching its holes with
REST bars would splice two sources into one series, and the ATR trailing stop is RECURSIVE --
a spliced series yields a THIRD answer, different from either source. That is precisely the
defect diagnosed 2026-07-21 (only 54.2% of ATR flips agreed between Polygon- and Schwab-built
bars). So we pull complete, continuous sessions and use them alone.

Writes JSON files under a data dir. Touches NO production table and NO live service. The only
Schwab endpoint used is GET /marketdata/v1/pricehistory (read-only).

Gotchas honoured (see project_mai_tai_schwab_rest_gotchas):
  - explicit startDate/endDate epoch-ms; `period=1` returns the PRIOR session during RTH
  - needExtendedHoursData=true, since v2's window opens at 07:00 ET (pre-market)
  - ~120 RPM per credential -> paced; one call per symbol covers the whole range
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

ET = ZoneInfo("America/New_York")
OUT = Path("/var/lib/project-mai-tai/schwab_rest_bars")
PACE_S = 0.6           # ~100 req/min, under the ~120 RPM ceiling


async def fetch(adapter, symbol: str, start_ms: int, end_ms: int):
    path = (
        f"/marketdata/v1/pricehistory?symbol={symbol}"
        f"&periodType=day&frequencyType=minute&frequency=1"
        f"&startDate={start_ms}&endDate={end_ms}"
        f"&needExtendedHoursData=true&needPreviousClose=false"
    )
    status, _h, body = await adapter._authorized_request_json("GET", path)
    return status, body


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="cap symbols (0 = all)")
    ap.add_argument("--symbol", help="single symbol smoke test")
    args = ap.parse_args()

    settings = get_settings()
    adapter = SchwabBrokerAdapter(settings)
    sf = build_session_factory(settings)

    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        with sf() as s:
            rows = s.execute(text("""
                SELECT DISTINCT symbol FROM scanner_confirmed_events
                WHERE event_at > now() - (:d || ' days')::interval
                ORDER BY symbol
            """), {"d": args.days}).all()
        symbols = [r[0] for r in rows]
        if args.limit:
            symbols = symbols[: args.limit]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"backfill: {len(symbols)} symbols, {args.days} days -> {OUT}")
    print(f"range: {start.astimezone(ET):%Y-%m-%d %H:%M} .. {end.astimezone(ET):%Y-%m-%d %H:%M} ET\n")

    ok = empty = failed = 0
    total_bars = 0
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        try:
            status, body = await fetch(adapter, sym, start_ms, end_ms)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(symbols)}] {sym:<8} EXCEPTION {exc}")
            failed += 1
            await asyncio.sleep(PACE_S)
            continue

        if status != 200 or not isinstance(body, dict):
            print(f"  [{i}/{len(symbols)}] {sym:<8} HTTP {status} {str(body)[:110]}")
            failed += 1
            await asyncio.sleep(PACE_S)
            continue

        candles = body.get("candles") or []
        if not candles:
            print(f"  [{i}/{len(symbols)}] {sym:<8} EMPTY (no candles returned)")
            empty += 1
            await asyncio.sleep(PACE_S)
            continue

        # Split into per-ET-day files so a backtest can load one session cleanly.
        by_day: dict[str, list] = {}
        for c in candles:
            d = datetime.fromtimestamp(c["datetime"] / 1000, timezone.utc).astimezone(ET).date().isoformat()
            by_day.setdefault(d, []).append(c)
        for d, rows_ in by_day.items():
            day_dir = OUT / d
            day_dir.mkdir(parents=True, exist_ok=True)
            (day_dir / f"{sym}.json").write_text(json.dumps(rows_), encoding="utf-8")
        total_bars += len(candles)
        ok += 1
        span = f"{len(by_day)}d"
        print(f"  [{i}/{len(symbols)}] {sym:<8} {len(candles):>6} bars across {span}")
        await asyncio.sleep(PACE_S)

    dt = time.time() - t0
    print(f"\nDONE in {dt/60:.1f} min — ok={ok} empty={empty} failed={failed} "
          f"total_bars={total_bars}")
    print(f"corpus at {OUT}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
