"""Reader: turn scanner_confirmed_events into per-(trade_date, symbol) windows.

For each (trade_date, symbol) over a date range, emits:
  first CONFIRM event_at, first FADE event_at, first RETENTION_DROP event_at,
  confirm_path, rank_score, float_used, change_pct, and the count of distinct
  CONFIRM event_at (re-confirmations). These are the
  ``[confirmed_at -> fade_at -> retention_drop_at]`` windows the backtest consumes.

Read-only. DB URL comes from ``MAI_TAI_DATABASE_URL`` (the app's Settings alias) or
``--database-url``.

Examples:
  python scripts/scanner_confirmed_windows.py --start-date 2026-07-10
  python scripts/scanner_confirmed_windows.py --start-date 2026-07-01 --end-date 2026-07-10 --json
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime

from sqlalchemy import create_engine, text

_QUERY = text(
    """
    SELECT
        trade_date,
        symbol,
        min(event_at) FILTER (WHERE event_type = 'CONFIRM')        AS confirmed_at,
        min(event_at) FILTER (WHERE event_type = 'FADE')           AS fade_at,
        min(event_at) FILTER (WHERE event_type = 'RETENTION_DROP') AS retention_drop_at,
        count(DISTINCT event_at) FILTER (WHERE event_type = 'CONFIRM') AS confirm_count,
        (array_agg(confirm_path ORDER BY event_at)
            FILTER (WHERE event_type = 'CONFIRM' AND confirm_path IS NOT NULL))[1] AS confirm_path,
        (array_agg(rank_score ORDER BY event_at)
            FILTER (WHERE event_type = 'CONFIRM' AND rank_score IS NOT NULL))[1] AS rank_score,
        (array_agg(float_used ORDER BY event_at)
            FILTER (WHERE event_type = 'CONFIRM' AND float_used IS NOT NULL))[1] AS float_used,
        (array_agg(change_pct ORDER BY event_at)
            FILTER (WHERE event_type = 'CONFIRM' AND change_pct IS NOT NULL))[1] AS change_pct
    FROM scanner_confirmed_events
    WHERE trade_date >= :start_date AND trade_date <= :end_date
    GROUP BY trade_date, symbol
    ORDER BY trade_date, confirmed_at NULLS LAST, symbol
    """
)


def _resolve_database_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    url = os.environ.get("MAI_TAI_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "No database URL: set MAI_TAI_DATABASE_URL or pass --database-url"
        )
    return url


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None if value is None else str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--end-date",
        default=None,
        help="YYYY-MM-DD (inclusive); defaults to --start-date",
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--json", action="store_true", help="emit JSON lines")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date) if args.end_date else start_date

    engine = create_engine(_resolve_database_url(args.database_url))
    with engine.connect() as conn:
        result = conn.execute(_QUERY, {"start_date": start_date, "end_date": end_date})
        rows = result.mappings().all()

    if args.json:
        for row in rows:
            record = dict(row)
            for key in ("confirmed_at", "fade_at", "retention_drop_at"):
                record[key] = _iso(record.get(key))
            record["trade_date"] = str(record.get("trade_date"))
            for key in ("rank_score", "change_pct"):
                if record.get(key) is not None:
                    record[key] = float(record[key])
            print(json.dumps(record, default=str))
        return

    header = (
        f"{'date':<10} {'symbol':<8} {'confirmed_at':<27} {'fade_at':<27} "
        f"{'retention_drop_at':<27} {'path':<24} {'rank':>7} {'float':>12} "
        f"{'chg%':>7} {'#conf':>5}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        rank = "" if row["rank_score"] is None else f"{float(row['rank_score']):.2f}"
        chg = "" if row["change_pct"] is None else f"{float(row['change_pct']):.2f}"
        float_used = "" if row["float_used"] is None else str(row["float_used"])
        confirmed_at = _iso(row["confirmed_at"]) or ""
        fade_at = _iso(row["fade_at"]) or ""
        drop_at = _iso(row["retention_drop_at"]) or ""
        print(
            f"{str(row['trade_date']):<10} "
            f"{str(row['symbol']):<8} "
            f"{confirmed_at:<27} "
            f"{fade_at:<27} "
            f"{drop_at:<27} "
            f"{str(row['confirm_path'] or ''):<24} "
            f"{rank:>7} "
            f"{float_used:>12} "
            f"{chg:>7} "
            f"{row['confirm_count']:>5}"
        )
    print(f"\n{len(rows)} (trade_date, symbol) window(s).")


if __name__ == "__main__":
    main()
