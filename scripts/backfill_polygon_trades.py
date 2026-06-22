"""Backfill historical Polygon/Massive TRADE prints into market_capture_trades.

The live capture (market_capture_app) is forward-only from the moment it
started. This script extends the central store BACKWARD using Massive REST
`list_trades` (/v3/trades) — entitlement confirmed for large- and small-caps.
Writes the SAME schema/provider as the live capture (one unified store keyed by
symbol+event_ts), so backtests query both transparently.

Idempotent by default: a (symbol, day-window) that already has rows is SKIPPED
unless --force (which deletes that window's rows first, then re-inserts).
Timestamps are normalized to true ns (sip_timestamp is already ns; the ladder
is belt-and-suspenders) — a 1970 row can never be written.

Examples:
  # one symbol, one closed day, full session
  .venv/bin/python scripts/backfill_polygon_trades.py --symbols EHGO --start-date 2026-06-18
  # several symbols, a date range, premarket+RTH only (04:00-16:00 ET)
  .venv/bin/python scripts/backfill_polygon_trades.py \
    --symbols EHGO,CDT,SKYQ --start-date 2026-06-16 --end-date 2026-06-18 \
    --start-et 04:00 --end-et 16:00
  # see what WOULD be fetched, write nothing
  .venv/bin/python scripts/backfill_polygon_trades.py --symbols EHGO --start-date 2026-06-18 --dry-run
"""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import psycopg

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_TABLE = "market_capture_trades"


def _dsn(arg: str | None) -> str:
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def _normalize_ts_ns(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v >= 1_000_000_000_000_000_000:  # ns
        return v
    if v >= 1_000_000_000_000_000:  # us
        return v * 1_000
    if v >= 1_000_000_000_000:  # ms
        return v * 1_000_000
    if v >= 1_000_000_000:  # s
        return v * 1_000_000_000
    return None


def _conditions(value) -> str | None:
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value) or None
    return str(value)


def _trade_to_row(trade, symbol: str, provider: str) -> tuple | None:
    """Map a Massive Trade object to a market_capture_trades row tuple, or None."""
    ns = _normalize_ts_ns(getattr(trade, "sip_timestamp", None)
                          or getattr(trade, "participant_timestamp", None))
    if ns is None:
        return None
    try:
        price = Decimal(str(trade.price))
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None
    event_ts = datetime.fromtimestamp(ns / 1e9, tz=_UTC)
    size = getattr(trade, "size", None)
    exch = getattr(trade, "exchange", None)
    return (
        provider,
        symbol,
        event_ts,
        price,
        int(size) if size is not None else None,
        str(exch) if exch is not None else None,
        _conditions(getattr(trade, "conditions", None)),
        None,  # cumulative_volume — not carried per-trade by /v3/trades (matches live)
    )


def _day_bounds_ns(day: date, start_et: time | None, end_et: time | None) -> tuple[int, int] | None:
    """Return (gte_ns, lte_ns) for an intraday ET window, or None for full day."""
    if start_et is None and end_et is None:
        return None
    s = datetime.combine(day, start_et or time(0, 0), tzinfo=_ET)
    e = datetime.combine(day, end_et or time(23, 59, 59), tzinfo=_ET)
    return (int(s.timestamp() * 1e9), int(e.timestamp() * 1e9))


def _existing_count(cur, symbol: str, day: date, bounds: tuple[int, int] | None) -> int:
    if bounds is None:
        lo = datetime.combine(day, time(0, 0), tzinfo=_ET)
        hi = lo + timedelta(days=1)
    else:
        lo = datetime.fromtimestamp(bounds[0] / 1e9, tz=_UTC)
        hi = datetime.fromtimestamp(bounds[1] / 1e9, tz=_UTC)
    cur.execute(
        f"SELECT count(*) FROM {_TABLE} WHERE symbol=%s AND event_ts >= %s AND event_ts <= %s",
        (symbol, lo, hi),
    )
    return cur.fetchone()[0]


def _delete_window(cur, symbol: str, day: date, bounds: tuple[int, int] | None) -> int:
    if bounds is None:
        lo = datetime.combine(day, time(0, 0), tzinfo=_ET)
        hi = lo + timedelta(days=1)
    else:
        lo = datetime.fromtimestamp(bounds[0] / 1e9, tz=_UTC)
        hi = datetime.fromtimestamp(bounds[1] / 1e9, tz=_UTC)
    cur.execute(
        f"DELETE FROM {_TABLE} WHERE symbol=%s AND event_ts >= %s AND event_ts <= %s",
        (symbol, lo, hi),
    )
    return cur.rowcount


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill historical Polygon/Massive trades.")
    ap.add_argument("--symbols", required=True, help="comma-separated tickers")
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end-date", default=None, help="YYYY-MM-DD (default = start-date)")
    ap.add_argument("--start-et", default=None, help="intraday window start HH:MM ET (optional)")
    ap.add_argument("--end-et", default=None, help="intraday window end HH:MM ET (optional)")
    ap.add_argument("--provider", default="massive")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--batch", type=int, default=20_000)
    ap.add_argument("--force", action="store_true", help="re-backfill (delete existing window first)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + count only, write nothing")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date) if args.end_date else start
    start_et = time.fromisoformat(args.start_et) if args.start_et else None
    end_et = time.fromisoformat(args.end_et) if args.end_et else None

    api_key = os.environ.get("MAI_TAI_MASSIVE_API_KEY", "")
    if not api_key:
        raise SystemExit("no MAI_TAI_MASSIVE_API_KEY in env")
    from massive import RESTClient

    client = RESTClient(api_key)
    conn = psycopg.connect(_dsn(args.dsn))
    grand_fetched = grand_written = 0
    try:
        for symbol in symbols:
            for day in _daterange(start, end):
                bounds = _day_bounds_ns(day, start_et, end_et)
                with conn.cursor() as cur:
                    existing = _existing_count(cur, symbol, day, bounds)
                if existing and not args.force:
                    print(f"{symbol} {day}: SKIP — {existing} rows already present (use --force to replace)")
                    continue

                kwargs = {"limit": 50_000}
                if bounds is None:
                    kwargs["timestamp"] = day.isoformat()
                else:
                    kwargs["timestamp_gte"] = bounds[0]
                    kwargs["timestamp_lte"] = bounds[1]
                rows: list[tuple] = []
                fetched = 0
                for trade in client.list_trades(symbol, **kwargs):
                    fetched += 1
                    row = _trade_to_row(trade, symbol, args.provider)
                    if row is not None:
                        rows.append(row)
                grand_fetched += fetched

                if args.dry_run:
                    print(f"{symbol} {day}: DRY-RUN fetched {fetched} trades ({len(rows)} mappable), wrote 0")
                    continue
                with conn.cursor() as cur:
                    if args.force and existing:
                        deleted = _delete_window(cur, symbol, day, bounds)
                        print(f"{symbol} {day}: --force deleted {deleted} existing rows")
                    written = 0
                    for i in range(0, len(rows), args.batch):
                        chunk = rows[i : i + args.batch]
                        with cur.copy(
                            f"COPY {_TABLE} (provider,symbol,event_ts,price,size,exchange,conditions,cumulative_volume) FROM STDIN"
                        ) as copy:
                            for r in chunk:
                                copy.write_row(r)
                        written += len(chunk)
                    conn.commit()
                grand_written += written
                print(f"{symbol} {day}: fetched {fetched} -> wrote {written} rows")
    finally:
        conn.close()
    print(f"DONE: fetched {grand_fetched}, wrote {grand_written} rows across {len(symbols)} symbol(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
