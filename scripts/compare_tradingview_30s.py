from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from project_mai_tai.strategy_core.schwab_native_30s import SchwabNativeBarBuilder


EASTERN = ZoneInfo("America/New_York")
BAR_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class BarSnapshot:
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int = 0


def _epoch_from_ns(value: int) -> float:
    if value > 1_000_000_000_000_000_000:
        return value / 1_000_000_000
    if value > 1_000_000_000_000:
        return value / 1_000
    return float(value)


def _window_bounds(session_day: date, start_hour: int, end_hour: int) -> tuple[datetime, datetime]:
    start_dt = datetime.combine(session_day, time(hour=start_hour), tzinfo=EASTERN)
    end_dt = datetime.combine(session_day, time(hour=end_hour), tzinfo=EASTERN)
    return start_dt.astimezone(UTC), end_dt.astimezone(UTC)


def _within_window(ts_epoch: float, *, start_utc: datetime, end_utc: datetime) -> bool:
    current = datetime.fromtimestamp(ts_epoch, UTC)
    return start_utc <= current < end_utc


def _load_tradingview_csv(
    csv_path: Path,
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[int, BarSnapshot]:
    bars: dict[int, BarSnapshot] = {}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts = int(float(row["time"]))
            if not _within_window(float(ts), start_utc=start_utc, end_utc=end_utc):
                continue
            bars[ts] = BarSnapshot(
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["Volume"]),
            )
    return bars


def _load_schwab_builder_bars(
    jsonl_path: Path,
    *,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[int, BarSnapshot]:
    builder = SchwabNativeBarBuilder(ticker=symbol, interval_secs=30, time_provider=lambda: 0.0, fill_gap_bars=False)
    last_seen_epoch: float | None = None
    with jsonl_path.open(encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            if str(payload.get("event_type", "")).lower() != "trade":
                continue
            try:
                ts_ns = int(payload.get("timestamp_ns") or 0)
            except (TypeError, ValueError):
                continue
            if ts_ns <= 0:
                continue
            epoch = _epoch_from_ns(ts_ns)
            if not _within_window(epoch, start_utc=start_utc, end_utc=end_utc):
                continue
            price = float(payload.get("price") or 0)
            size = int(payload.get("size") or 0)
            builder.on_trade(price, size, ts_ns, payload.get("cumulative_volume"))
            last_seen_epoch = epoch
    if last_seen_epoch is not None:
        builder.time_provider = lambda: last_seen_epoch + 31
        builder.check_bar_closes()

    bars: dict[int, BarSnapshot] = {}
    for bar in builder.bars:
        if not _within_window(bar.timestamp, start_utc=start_utc, end_utc=end_utc):
            continue
        bars[int(bar.timestamp)] = BarSnapshot(
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
            trades=int(getattr(bar, "trade_count", 0) or 0),
        )
    return bars


def _load_persisted_bars(
    *,
    conn: psycopg.Connection,
    symbol: str,
    strategy_code: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[int, BarSnapshot]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                extract(epoch from bar_time)::bigint as bar_epoch,
                open_price::float8,
                high_price::float8,
                low_price::float8,
                close_price::float8,
                volume,
                trade_count
            from strategy_bar_history
            where strategy_code = %s
              and symbol = %s
              and interval_secs = 30
              and bar_time >= %s
              and bar_time < %s
            order by bar_time
            """,
            (strategy_code, symbol, start_utc, end_utc),
        )
        rows = cur.fetchall()
    return {
        int(row[0]): BarSnapshot(
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            trades=int(row[6] or 0),
        )
        for row in rows
    }


def _format_et(ts_epoch: int) -> str:
    return datetime.fromtimestamp(ts_epoch, UTC).astimezone(EASTERN).strftime("%Y-%m-%d %I:%M:%S %p ET")


def _diff_row(left: BarSnapshot, right: BarSnapshot) -> dict[str, float]:
    return {
        field: abs(getattr(left, field) - getattr(right, field))
        for field in BAR_FIELDS
    }


def _pairwise_compare(
    *,
    left_name: str,
    left: dict[int, BarSnapshot],
    right_name: str,
    right: dict[int, BarSnapshot],
) -> dict[str, Any]:
    common = sorted(set(left) & set(right))
    missing_in_right = sorted(set(left) - set(right))
    missing_in_left = sorted(set(right) - set(left))
    stats: dict[str, dict[str, float]] = {}
    top_rows: list[dict[str, Any]] = []
    for field in BAR_FIELDS:
        diffs = [abs(getattr(left[ts], field) - getattr(right[ts], field)) for ts in common]
        stats[field] = {
            "avg_abs": (sum(diffs) / len(diffs)) if diffs else 0.0,
            "max_abs": max(diffs) if diffs else 0.0,
        }

    for ts in common:
        left_bar = left[ts]
        right_bar = right[ts]
        diff_map = _diff_row(left_bar, right_bar)
        price_score = max(diff_map["open"], diff_map["high"], diff_map["low"], diff_map["close"])
        trade_diff = abs(left_bar.trades - right_bar.trades)
        if price_score == 0 and diff_map["volume"] == 0 and trade_diff == 0:
            continue
        top_rows.append(
            {
                "time_et": _format_et(ts),
                "price_score": price_score,
                "volume_diff": diff_map["volume"],
                "trade_diff": trade_diff,
                left_name: left_bar,
                right_name: right_bar,
            }
        )
    top_rows.sort(key=lambda row: (row["price_score"], row["volume_diff"], row["trade_diff"]), reverse=True)

    return {
        "left_name": left_name,
        "right_name": right_name,
        "left_count": len(left),
        "right_count": len(right),
        "common": len(common),
        "missing_in_right": len(missing_in_right),
        "missing_in_left": len(missing_in_left),
        "missing_in_right_times": [_format_et(ts) for ts in missing_in_right[:5]],
        "missing_in_left_times": [_format_et(ts) for ts in missing_in_left[:5]],
        "stats": stats,
        "top_rows": top_rows[:5],
    }


def _zero_persisted_vs_rebuilt(
    rebuilt: dict[int, BarSnapshot],
    persisted: dict[int, BarSnapshot],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ts in sorted(set(rebuilt) & set(persisted)):
        rebuilt_bar = rebuilt[ts]
        persisted_bar = persisted[ts]
        if persisted_bar.volume == 0 and persisted_bar.trades == 0 and (
            rebuilt_bar.volume > 0 or rebuilt_bar.trades > 0
        ):
            rows.append(
                {
                    "time_et": _format_et(ts),
                    "rebuilt": rebuilt_bar,
                    "persisted": persisted_bar,
                }
            )
    return rows


def compare_pair(
    *,
    csv_path: Path,
    jsonl_path: Path,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    conn: psycopg.Connection | None = None,
    strategy_code: str = "macd_30s",
) -> dict[str, Any]:
    tradingview = _load_tradingview_csv(csv_path, start_utc=start_utc, end_utc=end_utc)
    rebuilt = _load_schwab_builder_bars(
        jsonl_path,
        symbol=symbol,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    persisted = (
        _load_persisted_bars(
            conn=conn,
            symbol=symbol,
            strategy_code=strategy_code,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        if conn is not None
        else {}
    )

    results = {
        "tv_vs_rebuilt": _pairwise_compare(
            left_name="tv",
            left=tradingview,
            right_name="rebuilt",
            right=rebuilt,
        ),
    }
    if conn is not None:
        results["rebuilt_vs_persisted"] = _pairwise_compare(
            left_name="rebuilt",
            left=rebuilt,
            right_name="persisted",
            right=persisted,
        )
        results["tv_vs_persisted"] = _pairwise_compare(
            left_name="tv",
            left=tradingview,
            right_name="persisted",
            right=persisted,
        )

    return {
        "symbol": symbol,
        "tv_bars": len(tradingview),
        "rebuilt_bars": len(rebuilt),
        "persisted_bars": len(persisted),
        "three_way_common": len(set(tradingview) & set(rebuilt) & set(persisted)) if conn is not None else 0,
        "zero_persisted_vs_rebuilt": _zero_persisted_vs_rebuilt(rebuilt, persisted) if conn is not None else [],
        "results": results,
    }


def _print_pairwise(label: str, payload: dict[str, Any]) -> None:
    summary = {
        "left_count": payload["left_count"],
        "right_count": payload["right_count"],
        "common": payload["common"],
        "missing_in_right": payload["missing_in_right"],
        "missing_in_left": payload["missing_in_left"],
        "missing_in_right_times": payload["missing_in_right_times"],
        "missing_in_left_times": payload["missing_in_left_times"],
    }
    for field, field_stats in payload["stats"].items():
        summary[f"{field}_avg_abs"] = round(field_stats["avg_abs"], 4)
        summary[f"{field}_max_abs"] = round(field_stats["max_abs"], 4)
    print(label)
    print(json.dumps(summary, indent=2))
    for row in payload["top_rows"]:
        left_name = payload["left_name"]
        right_name = payload["right_name"]
        print(
            "TOP",
            row["time_et"],
            json.dumps(
                {
                    "price_score": round(row["price_score"], 4),
                    "volume_diff": round(row["volume_diff"], 1),
                    "trade_diff": row["trade_diff"],
                    left_name: {
                        "ohlc": [
                            round(row[left_name].open, 4),
                            round(row[left_name].high, 4),
                            round(row[left_name].low, 4),
                            round(row[left_name].close, 4),
                        ],
                        "volume": round(row[left_name].volume, 1),
                        "trades": row[left_name].trades,
                    },
                    right_name: {
                        "ohlc": [
                            round(row[right_name].open, 4),
                            round(row[right_name].high, 4),
                            round(row[right_name].low, 4),
                            round(row[right_name].close, 4),
                        ],
                        "volume": round(row[right_name].volume, 1),
                        "trades": row[right_name].trades,
                    },
                }
            ),
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare TradingView 30s CSV bars against Schwab archived trade-tick rebuilds, "
            "and optionally persisted StrategyBarHistory bars, over an ET session window."
        )
    )
    parser.add_argument("--day", required=True, help="ET session day in YYYY-MM-DD")
    parser.add_argument("--start-hour", type=int, default=4, help="ET hour to start comparison window")
    parser.add_argument("--end-hour", type=int, default=12, help="ET hour to end comparison window")
    parser.add_argument("--dsn", help="Optional Postgres DSN for persisted StrategyBarHistory comparison")
    parser.add_argument("--strategy-code", default="macd_30s")
    parser.add_argument(
        "--pair",
        action="append",
        nargs=3,
        metavar=("SYMBOL", "TRADINGVIEW_CSV", "SCHWAB_JSONL"),
        help="Comparison triple. Repeat for multiple symbols.",
        required=True,
    )
    args = parser.parse_args()

    session_day = datetime.strptime(args.day, "%Y-%m-%d").date()
    start_utc, end_utc = _window_bounds(session_day, args.start_hour, args.end_hour)

    conn: psycopg.Connection | None = None
    try:
        if args.dsn:
            conn = psycopg.connect(args.dsn)

        for symbol, csv_file, jsonl_file in args.pair:
            result = compare_pair(
                csv_path=Path(csv_file),
                jsonl_path=Path(jsonl_file),
                symbol=symbol.upper(),
                start_utc=start_utc,
                end_utc=end_utc,
                conn=conn,
                strategy_code=args.strategy_code,
            )
            print(result["symbol"])
            print(
                json.dumps(
                    {
                        "tv_bars": result["tv_bars"],
                        "rebuilt_bars": result["rebuilt_bars"],
                        "persisted_bars": result["persisted_bars"],
                        "three_way_common": result["three_way_common"],
                        "zero_persisted_vs_rebuilt": len(result["zero_persisted_vs_rebuilt"]),
                    },
                    indent=2,
                )
            )
            if result["zero_persisted_vs_rebuilt"]:
                print("ZERO_PERSISTED_VS_REBUILT")
                for row in result["zero_persisted_vs_rebuilt"][:5]:
                    print(
                        "TOP",
                        row["time_et"],
                        json.dumps(
                            {
                                "rebuilt": {
                                    "ohlc": [
                                        round(row["rebuilt"].open, 4),
                                        round(row["rebuilt"].high, 4),
                                        round(row["rebuilt"].low, 4),
                                        round(row["rebuilt"].close, 4),
                                    ],
                                    "volume": round(row["rebuilt"].volume, 1),
                                    "trades": row["rebuilt"].trades,
                                },
                                "persisted": {
                                    "ohlc": [
                                        round(row["persisted"].open, 4),
                                        round(row["persisted"].high, 4),
                                        round(row["persisted"].low, 4),
                                        round(row["persisted"].close, 4),
                                    ],
                                    "volume": round(row["persisted"].volume, 1),
                                    "trades": row["persisted"].trades,
                                },
                            }
                        ),
                    )
                print()
            _print_pairwise("TV_VS_REBUILT", result["results"]["tv_vs_rebuilt"])
            if "rebuilt_vs_persisted" in result["results"]:
                _print_pairwise("REBUILT_VS_PERSISTED", result["results"]["rebuilt_vs_persisted"])
                _print_pairwise("TV_VS_PERSISTED", result["results"]["tv_vs_persisted"])
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
