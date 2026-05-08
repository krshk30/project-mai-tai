from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from project_mai_tai.strategy_core.schwab_native_30s import SchwabNativeBarBuilder


EASTERN = ZoneInfo("America/New_York")


@dataclass
class PersistedBar:
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int


def _window_bounds(session_day: str, start_hour: int, end_hour: int) -> tuple[datetime, datetime]:
    parsed_day = datetime.strptime(session_day, "%Y-%m-%d").date()
    start_dt = datetime.combine(parsed_day, time(hour=start_hour), tzinfo=EASTERN)
    end_dt = datetime.combine(parsed_day, time(hour=end_hour), tzinfo=EASTERN)
    return start_dt.astimezone(UTC), end_dt.astimezone(UTC)


def _load_rebuilt_bars(
    symbol: str,
    archive_dir: Path,
    *,
    interval_secs: int,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[int, dict[datetime, object]]:
    path = archive_dir / f"{symbol}.jsonl"
    builder = SchwabNativeBarBuilder(symbol, interval_secs=interval_secs, fill_gap_bars=False)
    trade_rows = 0
    if not path.exists():
        return trade_rows, {}
    with path.open() as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("event_type") != "trade":
                continue
            timestamp_ns = int(row.get("timestamp_ns") or 0)
            if timestamp_ns <= 0:
                continue
            timestamp = datetime.fromtimestamp(timestamp_ns / 1_000_000_000, UTC)
            if not (start_utc <= timestamp < end_utc):
                continue
            trade_rows += 1
            builder.on_trade(
                float(row["price"]),
                int(row.get("size") or 0),
                timestamp_ns,
                int(row.get("cumulative_volume")) if row.get("cumulative_volume") is not None else None,
            )
    if builder._current_bar is not None:
        builder._close_current_bar()
        builder._current_bar = None
        builder._current_bar_last_cum_volume = None
    rebuilt = {
        datetime.fromtimestamp(bar.timestamp, UTC): bar
        for bar in builder.bars
        if start_utc <= datetime.fromtimestamp(bar.timestamp, UTC) < end_utc
    }
    return trade_rows, rebuilt


def _query_persisted_rows_via_psycopg(
    conn: psycopg.Connection,
    symbol: str,
    strategy_code: str,
    *,
    interval_secs: int,
    start_utc: datetime,
    end_utc: datetime,
) -> list[tuple[datetime, float, float, float, float, int, int]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                bar_time,
                open_price::float8,
                high_price::float8,
                low_price::float8,
                close_price::float8,
                volume,
                trade_count
            from strategy_bar_history
            where strategy_code = %s
              and symbol = %s
              and interval_secs = %s
              and bar_time >= %s
              and bar_time < %s
            order by bar_time
            """,
            (strategy_code, symbol, interval_secs, start_utc, end_utc),
        )
        return cur.fetchall()


def _query_persisted_rows_via_psql(
    psql_db: str,
    symbol: str,
    strategy_code: str,
    *,
    interval_secs: int,
    start_utc: datetime,
    end_utc: datetime,
) -> list[tuple[datetime, float, float, float, float, int, int]]:
    sql = f"""
select
    bar_time,
    open_price::float8,
    high_price::float8,
    low_price::float8,
    close_price::float8,
    volume,
    trade_count
from strategy_bar_history
where strategy_code = '{strategy_code}'
  and symbol = '{symbol}'
  and interval_secs = {int(interval_secs)}
  and bar_time >= '{start_utc.isoformat()}'
  and bar_time < '{end_utc.isoformat()}'
order by bar_time;
"""
    output = subprocess.check_output(
        [
            "sudo",
            "-u",
            "postgres",
            "psql",
            "-d",
            psql_db,
            "-At",
            "-F",
            "|",
            "-c",
            sql,
        ],
        text=True,
    )
    rows: list[tuple[datetime, float, float, float, float, int, int]] = []
    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) != 7:
            continue
        rows.append(
            (
                datetime.fromisoformat(parts[0].replace(" ", "T")),
                float(parts[1]),
                float(parts[2]),
                float(parts[3]),
                float(parts[4]),
                int(parts[5]),
                int(parts[6]),
            )
        )
    return rows


def _load_persisted_bars(
    conn: psycopg.Connection | None,
    symbol: str,
    strategy_code: str,
    *,
    interval_secs: int,
    start_utc: datetime,
    end_utc: datetime,
    psql_db: str | None = None,
) -> dict[datetime, PersistedBar]:
    if conn is not None:
        rows = _query_persisted_rows_via_psycopg(
            conn,
            symbol,
            strategy_code,
            interval_secs=interval_secs,
            start_utc=start_utc,
            end_utc=end_utc,
        )
    elif psql_db:
        rows = _query_persisted_rows_via_psql(
            psql_db,
            symbol,
            strategy_code,
            interval_secs=interval_secs,
            start_utc=start_utc,
            end_utc=end_utc,
        )
    else:
        raise ValueError("either conn or psql_db is required")
    return {
        row[0].astimezone(UTC): PersistedBar(
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=int(row[5]),
            trade_count=int(row[6]),
        )
        for row in rows
    }


def _fmt_ts(timestamp: datetime) -> str:
    return timestamp.astimezone(EASTERN).isoformat()


def compare_symbol(
    conn: psycopg.Connection | None,
    symbol: str,
    archive_dir: Path,
    *,
    interval_secs: int,
    strategy_code: str,
    start_utc: datetime,
    end_utc: datetime,
    psql_db: str | None = None,
) -> None:
    trade_rows, rebuilt = _load_rebuilt_bars(
        symbol,
        archive_dir,
        interval_secs=interval_secs,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    persisted = _load_persisted_bars(
        conn,
        symbol,
        strategy_code,
        interval_secs=interval_secs,
        start_utc=start_utc,
        end_utc=end_utc,
        psql_db=psql_db,
    )
    rebuilt_times = set(rebuilt)
    persisted_times = set(persisted)
    overlap = sorted(rebuilt_times & persisted_times)
    rebuilt_only = len(rebuilt_times - persisted_times)
    persisted_only = len(persisted_times - rebuilt_times)

    price_diffs: list[float] = []
    volume_diffs: list[int] = []
    zero_persisted_vs_real = 0
    worst_rows: list[tuple[float, int, int, datetime, object, PersistedBar]] = []
    for bar_time in overlap:
        rebuilt_bar = rebuilt[bar_time]
        persisted_bar = persisted[bar_time]
        diffs = [
            abs(rebuilt_bar.open - persisted_bar.open),
            abs(rebuilt_bar.high - persisted_bar.high),
            abs(rebuilt_bar.low - persisted_bar.low),
            abs(rebuilt_bar.close - persisted_bar.close),
        ]
        volume_diff = abs(int(rebuilt_bar.volume) - persisted_bar.volume)
        trade_count_diff = abs(int(getattr(rebuilt_bar, "trade_count", 0)) - persisted_bar.trade_count)
        price_diffs.extend(diffs)
        volume_diffs.append(volume_diff)
        if persisted_bar.volume == 0 and persisted_bar.trade_count == 0 and (
            int(rebuilt_bar.volume) > 0 or int(getattr(rebuilt_bar, "trade_count", 0)) > 0
        ):
            zero_persisted_vs_real += 1
        worst_rows.append((max(diffs), volume_diff, trade_count_diff, bar_time, rebuilt_bar, persisted_bar))
    worst_rows.sort(reverse=True, key=lambda item: (item[0], item[1], item[2]))

    avg_price = sum(price_diffs) / len(price_diffs) if price_diffs else 0.0
    avg_volume = sum(volume_diffs) / len(volume_diffs) if volume_diffs else 0.0

    print(f"=== {symbol} ({interval_secs}s / {strategy_code}) ===")
    print(
        "raw_trades={raw} rebuilt_bars={rebuilt_count} persisted_bars={persisted_count} "
        "overlap={overlap_count} rebuilt_only={rebuilt_only} persisted_only={persisted_only} "
        "zero_persisted_vs_real={zero_persisted_vs_real}".format(
            raw=trade_rows,
            rebuilt_count=len(rebuilt),
            persisted_count=len(persisted),
            overlap_count=len(overlap),
            rebuilt_only=rebuilt_only,
            persisted_only=persisted_only,
            zero_persisted_vs_real=zero_persisted_vs_real,
        )
    )
    print(f"avg_abs_price_diff={avg_price:.6f} avg_abs_vol_diff={avg_volume:.1f}")
    for _price_diff, _vol_diff, _tc_diff, bar_time, rebuilt_bar, persisted_bar in worst_rows[:5]:
        print(
            "worst {time} rebuilt={rebuilt_vals} persisted={persisted_vals}".format(
                time=_fmt_ts(bar_time),
                rebuilt_vals=(
                    rebuilt_bar.open,
                    rebuilt_bar.high,
                    rebuilt_bar.low,
                    rebuilt_bar.close,
                    int(rebuilt_bar.volume),
                    int(getattr(rebuilt_bar, "trade_count", 0)),
                ),
                persisted_vals=(
                    persisted_bar.open,
                    persisted_bar.high,
                    persisted_bar.low,
                    persisted_bar.close,
                    persisted_bar.volume,
                    persisted_bar.trade_count,
                ),
            )
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", "--date", dest="day", required=True, help="ET session day string like 2026-05-06")
    parser.add_argument("--start-hour", type=int, default=4, help="ET start hour for comparison window")
    parser.add_argument("--end-hour", type=int, default=12, help="ET end hour for comparison window")
    parser.add_argument("--archive-dir", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--dsn", default="dbname=project_mai_tai")
    parser.add_argument("--strategy-code", default="macd_30s")
    parser.add_argument(
        "--interval-secs",
        type=int,
        default=30,
        choices=[30, 60],
        help="Bar interval in seconds. 30 -> macd_30s default; 60 -> schwab_1m.",
    )
    parser.add_argument("--psql-db", help="Use sudo psql against this DB instead of psycopg")
    args = parser.parse_args()

    archive_dir = Path(args.archive_dir)
    start_utc, end_utc = _window_bounds(args.day, args.start_hour, args.end_hour)
    if args.psql_db:
        for symbol in args.symbols:
            compare_symbol(
                None,
                str(symbol).upper(),
                archive_dir,
                interval_secs=args.interval_secs,
                strategy_code=args.strategy_code,
                start_utc=start_utc,
                end_utc=end_utc,
                psql_db=args.psql_db,
            )
        return

    with psycopg.connect(args.dsn) as conn:
        for symbol in args.symbols:
            compare_symbol(
                conn,
                str(symbol).upper(),
                archive_dir,
                interval_secs=args.interval_secs,
                strategy_code=args.strategy_code,
                start_utc=start_utc,
                end_utc=end_utc,
            )


if __name__ == "__main__":
    main()
