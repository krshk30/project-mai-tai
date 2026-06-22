"""Daily post-close REST gather of the FULL scanner-qualified Polygon universe.

Pre-gathers everything next week's backtests need — raw TRADES + L1 QUOTES +
1-MINUTE bars — for every name that qualified on a given day, via Massive REST
(list_trades / list_quotes / list_aggs; all entitlements confirmed). REST, not
the live stream: recoverable, and ZERO impact on the shared market-data stream
or the live bots (notably the #350 CPU-bound strategy-engine), no gateway/bot
changes. Writes the same central tables the live capture uses (one unified
store, queried by symbol+event_ts).

Universe is resolved DYNAMICALLY per day (no fixed list): the distinct symbols
``polygon_30s`` tracked that day in strategy_bar_history == the scanner-qualified
set for that day (works for past days and today-after-close). Override with
--symbols, or use --from-scanner for today's live scanner_confirmed snapshot.

Idempotent: a (symbol, day, table) that already has rows is SKIPPED unless
--force. Timestamps normalized to ns (no 1970). COPY bulk insert.

Examples:
  # the day that just closed, full qualified universe, all data types
  .venv/bin/python scripts/gather_polygon_universe.py --date 2026-06-18
  # specific names, RTH+premarket only, bars+trades (skip the bulky-ish quotes)
  .venv/bin/python scripts/gather_polygon_universe.py --symbols EHGO,CDT --date 2026-06-18 \
      --start-et 04:00 --end-et 16:00 --skip-quotes
  # what WOULD be gathered, write nothing
  .venv/bin/python scripts/gather_polygon_universe.py --date 2026-06-18 --dry-run
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
    if v >= 1_000_000_000_000_000_000:
        return v
    if v >= 1_000_000_000_000_000:
        return v * 1_000
    if v >= 1_000_000_000_000:
        return v * 1_000_000
    if v >= 1_000_000_000:
        return v * 1_000_000_000
    return None


def _ts(ns_value) -> datetime | None:
    ns = _normalize_ts_ns(ns_value)
    return datetime.fromtimestamp(ns / 1e9, tz=_UTC) if ns else None


def _dec(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _conditions(value) -> str | None:
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value) or None
    return str(value)


def _bounds(day: date, start_et: time | None, end_et: time | None):
    if start_et is None and end_et is None:
        return None
    s = datetime.combine(day, start_et or time(0, 0), tzinfo=_ET)
    e = datetime.combine(day, end_et or time(23, 59, 59), tzinfo=_ET)
    return (int(s.timestamp() * 1e9), int(e.timestamp() * 1e9))


def _window_utc(day: date, bounds):
    if bounds is None:
        lo = datetime.combine(day, time(0, 0), tzinfo=_ET)
        return lo, lo + timedelta(days=1)
    return (datetime.fromtimestamp(bounds[0] / 1e9, tz=_UTC),
            datetime.fromtimestamp(bounds[1] / 1e9, tz=_UTC))


def _existing(cur, table, symbol, day, bounds) -> int:
    lo, hi = _window_utc(day, bounds)
    cur.execute(f"SELECT count(*) FROM {table} WHERE symbol=%s AND event_ts >= %s AND event_ts <= %s",
                (symbol, lo, hi))
    return cur.fetchone()[0]


def _delete(cur, table, symbol, day, bounds) -> int:
    lo, hi = _window_utc(day, bounds)
    cur.execute(f"DELETE FROM {table} WHERE symbol=%s AND event_ts >= %s AND event_ts <= %s",
                (symbol, lo, hi))
    return cur.rowcount


def _resolve_universe(conn, day: date, source: str) -> list[str]:
    if source == "scanner":
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM dashboard_snapshots WHERE snapshot_type='scanner_confirmed_last_nonempty' LIMIT 1")
            row = cur.fetchone()
        out = []
        if row and row[0]:
            for c in (row[0].get("all_confirmed_candidates") or []):
                t = c.get("ticker") or c.get("symbol")
                if t:
                    out.append(str(t).upper())
        return sorted(set(out))
    # default: distinct polygon_30s symbols tracked that day == qualified universe
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT symbol FROM strategy_bar_history "
            "WHERE strategy_code='polygon_30s' AND bar_time::date = %s ORDER BY 1",
            (day,))
        return [r[0].upper() for r in cur.fetchall()]


def _copy(cur, table, cols, rows, batch):
    written = 0
    collist = ",".join(cols)
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        with cur.copy(f"COPY {table} ({collist}) FROM STDIN") as cp:
            for r in chunk:
                cp.write_row(r)
        written += len(chunk)
    return written


def _gather_one(client, conn, table, symbol, day, bounds, kind, force, dry, batch) -> tuple[int, int]:
    with conn.cursor() as cur:
        existing = _existing(cur, table, symbol, day, bounds)
    if existing and not force:
        print(f"  {symbol} {day} {kind}: SKIP ({existing} rows present)")
        return (0, 0)

    kwargs = {"limit": 50_000}
    if bounds is None:
        kwargs["timestamp"] = day.isoformat()
    else:
        kwargs["timestamp_gte"] = bounds[0]
        kwargs["timestamp_lte"] = bounds[1]

    rows = []
    fetched = 0
    if kind == "trades":
        cols = ("provider", "symbol", "event_ts", "price", "size", "exchange", "conditions", "cumulative_volume")
        for t in client.list_trades(symbol, **kwargs):
            fetched += 1
            ts = _ts(getattr(t, "sip_timestamp", None) or getattr(t, "participant_timestamp", None))
            price = _dec(getattr(t, "price", None))
            if ts is None or price is None:
                continue
            rows.append(("massive", symbol, ts, price, _int(getattr(t, "size", None)),
                         str(getattr(t, "exchange", "")) or None,
                         _conditions(getattr(t, "conditions", None)), None))
    elif kind == "quotes":
        cols = ("provider", "symbol", "event_ts", "bid_price", "ask_price", "bid_size", "ask_size")
        for q in client.list_quotes(symbol, **kwargs):
            fetched += 1
            ts = _ts(getattr(q, "sip_timestamp", None) or getattr(q, "participant_timestamp", None))
            if ts is None:
                continue
            rows.append(("massive", symbol, ts, _dec(getattr(q, "bid_price", None)),
                         _dec(getattr(q, "ask_price", None)), _int(getattr(q, "bid_size", None)),
                         _int(getattr(q, "ask_size", None))))
    else:  # bars (1-minute)
        cols = ("provider", "symbol", "event_ts", "interval_secs", "open", "high", "low", "close",
                "volume", "vwap", "transactions")
        for a in client.list_aggs(symbol, 1, "minute", day.isoformat(), day.isoformat(), limit=50_000):
            fetched += 1
            ts = _ts(getattr(a, "timestamp", None))
            o, hi_, lo_, c = (_dec(getattr(a, k, None)) for k in ("open", "high", "low", "close"))
            if ts is None or None in (o, hi_, lo_, c):
                continue
            rows.append(("massive", symbol, ts, 60, o, hi_, lo_, c, _int(getattr(a, "volume", None)),
                         _dec(getattr(a, "vwap", None)), _int(getattr(a, "transactions", None))))

    if dry:
        print(f"  {symbol} {day} {kind}: DRY fetched {fetched} ({len(rows)} mappable)")
        return (fetched, 0)
    with conn.cursor() as cur:
        if force and existing:
            print(f"  {symbol} {day} {kind}: --force deleted {_delete(cur, table, symbol, day, bounds)}")
        written = _copy(cur, table, cols, rows, batch)
        conn.commit()
    print(f"  {symbol} {day} {kind}: fetched {fetched} -> wrote {written}")
    return (fetched, written)


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily post-close gather of the scanner-qualified Polygon universe.")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD trading day to gather")
    ap.add_argument("--symbols", default=None, help="override universe (CSV); else resolved dynamically")
    ap.add_argument("--universe-source", choices=["bars", "scanner"], default="bars",
                    help="bars = distinct polygon_30s symbols that day (default); scanner = today's confirmed snapshot")
    ap.add_argument("--start-et", default=None)
    ap.add_argument("--end-et", default=None)
    ap.add_argument("--skip-trades", action="store_true")
    ap.add_argument("--skip-quotes", action="store_true")
    ap.add_argument("--skip-bars", action="store_true")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--batch", type=int, default=20_000)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    day = date.fromisoformat(args.date)
    start_et = time.fromisoformat(args.start_et) if args.start_et else None
    end_et = time.fromisoformat(args.end_et) if args.end_et else None
    bounds = _bounds(day, start_et, end_et)

    if not os.environ.get("MAI_TAI_MASSIVE_API_KEY"):
        raise SystemExit("no MAI_TAI_MASSIVE_API_KEY in env")
    from massive import RESTClient
    client = RESTClient(os.environ["MAI_TAI_MASSIVE_API_KEY"])
    conn = psycopg.connect(_dsn(args.dsn))
    try:
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = _resolve_universe(conn, day, args.universe_source)
        print(f"universe ({args.universe_source if not args.symbols else 'explicit'}): {len(symbols)} symbols for {day}: {','.join(symbols)}")

        kinds = []
        if not args.skip_trades:
            kinds.append(("market_capture_trades", "trades"))
        if not args.skip_quotes:
            kinds.append(("market_capture_quotes", "quotes"))
        if not args.skip_bars:
            kinds.append(("market_capture_bars", "bars"))

        tot_f = tot_w = 0
        for symbol in symbols:
            for table, kind in kinds:
                f, w = _gather_one(client, conn, table, symbol, day, bounds, kind, args.force, args.dry_run, args.batch)
                tot_f += f
                tot_w += w
        print(f"DONE {day}: {len(symbols)} symbols, fetched {tot_f}, wrote {tot_w} rows")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
