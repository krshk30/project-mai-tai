from __future__ import annotations

import argparse
import csv
from datetime import UTC, date, datetime, timedelta
import os
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Massive/Polygon aggregate bars to CSV.",
    )
    parser.add_argument("--symbol", required=True, help="Ticker symbol, for example AAPL.")
    parser.add_argument(
        "--interval-secs",
        type=int,
        default=30,
        help="Aggregate interval in seconds. Defaults to 30.",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=2,
        help="Calendar months to look back from --end-date. Defaults to 2.",
    )
    parser.add_argument(
        "--end-date",
        default=date.today().isoformat(),
        help="Inclusive end date in YYYY-MM-DD. Defaults to today.",
    )
    parser.add_argument(
        "--start-date",
        default="",
        help="Optional override for the inclusive start date in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Optional Massive API key. Falls back to MAI_TAI_MASSIVE_API_KEY, MASSIVE_API_KEY, or POLYGON_API_KEY.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output CSV path. Defaults to data/exports/<symbol>_<interval>_<start>_<end>.csv.",
    )
    return parser.parse_args()


def _resolve_api_key(raw_value: str) -> str:
    candidates = [
        raw_value.strip(),
        os.getenv("MAI_TAI_MASSIVE_API_KEY", "").strip(),
        os.getenv("MASSIVE_API_KEY", "").strip(),
        os.getenv("POLYGON_API_KEY", "").strip(),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    raise SystemExit(
        "Massive API key is required. Pass --api-key or set MAI_TAI_MASSIVE_API_KEY, MASSIVE_API_KEY, or POLYGON_API_KEY."
    )


def _subtract_months(value: date, months: int) -> date:
    year = value.year
    month = value.month - months
    while month <= 0:
        month += 12
        year -= 1

    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def _resolve_date_range(args: argparse.Namespace) -> tuple[date, date]:
    end_date = date.fromisoformat(args.end_date)
    if args.start_date.strip():
        start_date = date.fromisoformat(args.start_date)
    else:
        start_date = _subtract_months(end_date, args.months)
    if start_date > end_date:
        raise SystemExit("start date must be on or before end date")
    return start_date, end_date


def _default_output_path(symbol: str, interval_secs: int, start_date: date, end_date: date) -> Path:
    return REPO_ROOT / "data" / "exports" / f"{symbol}_{interval_secs}s_{start_date}_{end_date}.csv"


def _fetch_bars(
    *,
    api_key: str,
    symbol: str,
    interval_secs: int,
    start_date: date,
    end_date: date,
) -> list[dict[str, object]]:
    provider = MassiveSnapshotProvider(api_key)
    client = provider._get_rest_client()
    multiplier, timespan = provider._resolve_agg_interval(interval_secs)

    rows: list[dict[str, object]] = []
    for agg in client.list_aggs(
        symbol.upper(),
        multiplier,
        timespan,
        from_=start_date.isoformat(),
        to=(end_date + timedelta(days=1)).isoformat(),
        adjusted=True,
        sort="asc",
        limit=50_000,
    ):
        timestamp_raw = getattr(agg, "timestamp", None)
        close = getattr(agg, "close", None)
        if timestamp_raw is None or close is None:
            continue

        timestamp = timestamp_raw / 1000 if timestamp_raw > 1_000_000_000_000 else float(timestamp_raw)
        timestamp_utc = datetime.fromtimestamp(timestamp, tz=UTC)
        timestamp_et = timestamp_utc.astimezone(EASTERN_TZ)
        if timestamp_et.date() < start_date or timestamp_et.date() > end_date:
            continue

        rows.append(
            {
                "symbol": symbol.upper(),
                "timestamp_unix": f"{timestamp:.3f}",
                "timestamp_utc": timestamp_utc.isoformat(),
                "timestamp_et": timestamp_et.isoformat(),
                "open": f"{float(getattr(agg, 'open', close) or close):.8f}",
                "high": f"{float(getattr(agg, 'high', close) or close):.8f}",
                "low": f"{float(getattr(agg, 'low', close) or close):.8f}",
                "close": f"{float(close):.8f}",
                "volume": int(getattr(agg, "volume", 0) or 0),
                "trade_count": int(
                    getattr(agg, "transactions", None)
                    or getattr(agg, "trade_count", None)
                    or 0
                ),
                "vwap": (
                    f"{float(getattr(agg, 'vwap', None)):.8f}"
                    if getattr(agg, "vwap", None) is not None
                    else ""
                ),
            }
        )

    return rows


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "symbol",
        "timestamp_unix",
        "timestamp_utc",
        "timestamp_et",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trade_count",
        "vwap",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = _parse_args()
    api_key = _resolve_api_key(args.api_key)
    start_date, end_date = _resolve_date_range(args)
    symbol = args.symbol.upper()
    output_path = args.output or _default_output_path(symbol, args.interval_secs, start_date, end_date)

    rows = _fetch_bars(
        api_key=api_key,
        symbol=symbol,
        interval_secs=args.interval_secs,
        start_date=start_date,
        end_date=end_date,
    )
    _write_csv(output_path, rows)

    print(f"symbol={symbol}")
    print(f"interval_secs={args.interval_secs}")
    print(f"start_date={start_date.isoformat()}")
    print(f"end_date={end_date.isoformat()}")
    print(f"rows={len(rows)}")
    print(f"output={output_path}")


if __name__ == "__main__":
    main()
