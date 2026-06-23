"""ORB reclaim live-test — fill instrumentation (READ-ONLY).

The reclaim entry rests a LIMIT at OR_high; the whole point of the live test is to
measure the REAL fill we don't trust from the backtest. This joins the persisted
intents/orders to the `fills` table for the ORB account and reports, per ENTRY:
  intended OR_high · actual fill · slippage (cents & %) · time-to-fill · or UNFILLED;
and per trailing-stop EXIT: intended trail level · actual fill · slippage.

Read-only (SELECT only). Usage:
  MAI_TAI_DATABASE_URL=... python scripts/orb_fill_slippage.py --day 2026-06-24 [--account paper:orb]
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime

import psycopg


def _dsn(arg: str | None) -> str:
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


ENTRY_SQL = """
SELECT bo.symbol,
       (bo.payload->'metadata'->>'orb_intended_or_high')::numeric AS intended,
       bo.status,
       (bo.payload->'metadata'->>'orb_reclaim_emit_ms') AS emit_ms,
       f.price AS fill_px,
       f.filled_at
FROM broker_orders bo
JOIN broker_accounts ba ON ba.id = bo.broker_account_id
LEFT JOIN fills f ON f.order_id = bo.id
WHERE ba.name = %(acct)s
  AND bo.payload->'metadata'->>'orb_intended_or_high' IS NOT NULL
  AND (COALESCE(bo.submitted_at, bo.updated_at) AT TIME ZONE 'America/New_York')::date = %(day)s
ORDER BY COALESCE(bo.submitted_at, bo.updated_at);
"""

# Trailing-stop exits: ORB sells whose metadata carries the intended stop/limit level.
EXIT_SQL = """
SELECT bo.symbol,
       COALESCE(bo.payload->'metadata'->>'stop_price',
                bo.payload->'metadata'->>'limit_price')::numeric AS intended_level,
       bo.payload->'metadata'->>'price_source' AS price_source,
       bo.status,
       f.price AS fill_px,
       f.filled_at
FROM broker_orders bo
JOIN broker_accounts ba ON ba.id = bo.broker_account_id
JOIN strategies s ON s.id = bo.strategy_id
LEFT JOIN fills f ON f.order_id = bo.id
WHERE ba.name = %(acct)s AND s.code = 'orb' AND bo.side = 'sell'
  AND (bo.payload->'metadata' ? 'stop_price' OR bo.payload->'metadata' ? 'limit_price')
  AND (COALESCE(bo.submitted_at, bo.updated_at) AT TIME ZONE 'America/New_York')::date = %(day)s
ORDER BY COALESCE(bo.submitted_at, bo.updated_at);
"""


def _ttf(filled_at, emit_ms: str | None) -> str:
    if filled_at is None or not emit_ms:
        return "-"
    try:
        emit = datetime.fromtimestamp(int(emit_ms) / 1000, tz=filled_at.tzinfo)
        return f"{(filled_at - emit).total_seconds():.1f}s"
    except (ValueError, TypeError):
        return "-"


def main() -> int:
    ap = argparse.ArgumentParser(description="ORB reclaim fill slippage (read-only)")
    ap.add_argument("--day", required=True, help="ET date YYYY-MM-DD")
    ap.add_argument("--account", default="paper:orb")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()
    params = {"acct": args.account, "day": args.day}

    with psycopg.connect(_dsn(args.dsn)) as conn, conn.cursor() as cur:
        print(f"\n=== ORB ENTRIES — {args.account} {args.day} (intended OR_high vs actual fill) ===")
        print(f"{'SYM':6}{'intended':>9}{'fill':>9}{'slip¢':>7}{'slip%':>7}{'time-to-fill':>13}  status")
        cur.execute(ENTRY_SQL, params)
        n = filled = unfilled = 0
        slips = []
        for sym, intended, status, emit_ms, fill_px, filled_at in cur.fetchall():
            n += 1
            if fill_px is None:
                unfilled += 1
                print(f"{sym:6}{float(intended):9.4f}{'—':>9}{'—':>7}{'—':>7}{'—':>13}  UNFILLED ({status})")
                continue
            filled += 1
            slip = float(fill_px) - float(intended)
            slip_pct = slip / float(intended) * 100 if intended else 0.0
            slips.append(slip_pct)
            print(f"{sym:6}{float(intended):9.4f}{float(fill_px):9.4f}{slip*100:7.1f}{slip_pct:7.2f}"
                  f"{_ttf(filled_at, emit_ms):>13}  {status}")
        print(f"--- entries: {n}  filled: {filled}  UNFILLED(limit never touched): {unfilled}", end="")
        if slips:
            avg = sum(slips) / len(slips)
            print(f"  | mean slip {avg:+.2f}%  worst {max(slips, key=abs):+.2f}%")
        else:
            print()

        print(f"\n=== ORB TRAIL-STOP EXITS — {args.account} {args.day} (intended trail vs actual fill) ===")
        print(f"{'SYM':6}{'intended':>9}{'fill':>9}{'slip¢':>7}{'slip%':>7}  src  status")
        cur.execute(EXIT_SQL, params)
        for sym, level, src, status, fill_px, filled_at in cur.fetchall():
            if fill_px is None or level is None:
                print(f"{sym:6}{(float(level) if level else 0):9.4f}{'—':>9}{'—':>7}{'—':>7}  {src or '-':>4}  {status}")
                continue
            slip = float(fill_px) - float(level)
            slip_pct = slip / float(level) * 100 if level else 0.0
            print(f"{sym:6}{float(level):9.4f}{float(fill_px):9.4f}{slip*100:7.1f}{slip_pct:7.2f}  {src or '-':>4}  {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
