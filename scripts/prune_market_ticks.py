"""Retention pruner for market_trade_ticks / market_quote_ticks.

Deletes rows older than --keep-days in bounded batches so the append-only tick
tables don't grow uncontrolled. Intended as a daily cron:

  0 9 * * * cd /home/trader/project-mai-tai && \
    .venv/bin/python scripts/prune_market_ticks.py --keep-days 14

Read-only-safe to dry-run with --dry-run (reports counts, deletes nothing).
If volume ever outgrows DELETE, switch the tables to native range partitioning
by received_at::date and DROP old partitions instead (see
docs/v2-tick-capture-design.md).
"""
from __future__ import annotations

import argparse
import os

import psycopg

TABLES = ("market_trade_ticks", "market_quote_ticks")


def _dsn(arg: str | None) -> str:
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-days", type=int, default=14)
    ap.add_argument("--batch", type=int, default=50_000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dsn", default=None)
    ap.add_argument(
        "--tables",
        default=",".join(TABLES),
        help="comma-separated table names to prune (default: the Schwab tick tables)",
    )
    args = ap.parse_args()
    cutoff_sql = f"now() - interval '{int(args.keep_days)} days'"
    tables = tuple(t.strip() for t in args.tables.split(",") if t.strip())
    _ALLOWED = {
        "market_trade_ticks", "market_quote_ticks",
        "market_capture_trades", "market_capture_quotes",
    }
    for t in tables:
        if t not in _ALLOWED:  # guard against SQL injection via --tables
            raise SystemExit(f"refusing to prune unknown table: {t!r}")

    with psycopg.connect(_dsn(args.dsn)) as conn:
        for table in tables:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {table} WHERE received_at < {cutoff_sql}")
                stale = cur.fetchone()[0]
            if args.dry_run:
                print(f"{table}: {stale} rows older than {args.keep_days}d (dry-run, none deleted)")
                continue
            deleted = 0
            while True:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {table} WHERE id IN ("
                        f"  SELECT id FROM {table} WHERE received_at < {cutoff_sql} "
                        f"  ORDER BY id LIMIT %s)",
                        (args.batch,),
                    )
                    n = cur.rowcount
                conn.commit()
                deleted += n
                if n < args.batch:
                    break
            print(f"{table}: deleted {deleted} rows older than {args.keep_days}d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
