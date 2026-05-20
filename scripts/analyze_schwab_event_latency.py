from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg


EASTERN = ZoneInfo("America/New_York")


@dataclass
class PersistedBarRow:
    bar_time: datetime
    created_at: datetime
    updated_at: datetime
    volume: int
    trade_count: int


@dataclass
class LiveBarArchiveStats:
    first_received_at_ns: int | None = None
    first_recorded_at_ns: int | None = None
    last_recorded_at_ns: int | None = None
    count: int = 0

    def observe(self, *, received_at_ns: int, recorded_at_ns: int) -> None:
        self.count += 1
        if self.first_received_at_ns is None or received_at_ns < self.first_received_at_ns:
            self.first_received_at_ns = received_at_ns
        if self.first_recorded_at_ns is None or recorded_at_ns < self.first_recorded_at_ns:
            self.first_recorded_at_ns = recorded_at_ns
        if self.last_recorded_at_ns is None or recorded_at_ns > self.last_recorded_at_ns:
            self.last_recorded_at_ns = recorded_at_ns


@dataclass
class TradeBucketArchiveStats:
    trade_count: int = 0
    first_event_ns: int | None = None
    last_event_ns: int | None = None
    first_recorded_at_ns: int | None = None
    last_recorded_at_ns: int | None = None
    first_received_at_ns: int | None = None
    last_received_at_ns: int | None = None
    max_received_lag_ns: int = 0
    max_recorded_lag_ns: int = 0

    def observe(self, *, event_ns: int, received_at_ns: int, recorded_at_ns: int) -> None:
        self.trade_count += 1
        if self.first_event_ns is None or event_ns < self.first_event_ns:
            self.first_event_ns = event_ns
        if self.last_event_ns is None or event_ns > self.last_event_ns:
            self.last_event_ns = event_ns
        if self.first_received_at_ns is None or received_at_ns < self.first_received_at_ns:
            self.first_received_at_ns = received_at_ns
        if self.last_received_at_ns is None or received_at_ns > self.last_received_at_ns:
            self.last_received_at_ns = received_at_ns
        if self.first_recorded_at_ns is None or recorded_at_ns < self.first_recorded_at_ns:
            self.first_recorded_at_ns = recorded_at_ns
        if self.last_recorded_at_ns is None or recorded_at_ns > self.last_recorded_at_ns:
            self.last_recorded_at_ns = recorded_at_ns
        self.max_received_lag_ns = max(self.max_received_lag_ns, max(0, received_at_ns - event_ns))
        self.max_recorded_lag_ns = max(self.max_recorded_lag_ns, max(0, recorded_at_ns - event_ns))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Classify Schwab stale-bar episodes by joining archive arrival timing "
            "with persisted StrategyBarHistory timing."
        )
    )
    parser.add_argument("--day", required=True, help="Session day in YYYY-MM-DD (Eastern)")
    parser.add_argument(
        "--strategy-code",
        required=True,
        choices=("schwab_1m", "macd_30s"),
        help="Target bot to analyze",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        required=True,
        help="Symbol to analyze; pass multiple times for multiple symbols",
    )
    parser.add_argument("--start-hour", type=int, default=4, help="Window start hour in ET")
    parser.add_argument("--end-hour", type=int, default=20, help="Window end hour in ET")
    parser.add_argument("--dsn", help="Postgres DSN")
    parser.add_argument(
        "--psql-db",
        help="Database name for sudo -u postgres psql fallback when DSN is unavailable",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="Maximum rows to print per symbol (worst lag first)",
    )
    return parser.parse_args()


def _window_bounds(session_day: str, start_hour: int, end_hour: int) -> tuple[datetime, datetime]:
    parsed_day = datetime.strptime(session_day, "%Y-%m-%d").date()
    start_dt = datetime.combine(parsed_day, time(hour=start_hour), tzinfo=EASTERN)
    end_dt = datetime.combine(parsed_day, time(hour=end_hour), tzinfo=EASTERN)
    return start_dt.astimezone(UTC), end_dt.astimezone(UTC)


def _ns_to_dt(value_ns: int | None) -> datetime | None:
    if value_ns is None:
        return None
    return datetime.fromtimestamp(value_ns / 1_000_000_000, UTC)


def _seconds_between(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds(), 3)


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(EASTERN).isoformat()


def _fmt_float(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}"


def _classify_schwab_1m(
    *,
    receive_first_at: datetime | None,
    archive_first_at: datetime | None,
    persist_created_at: datetime | None,
    bar_close_at: datetime,
) -> str:
    receive_lag_s = _seconds_between(receive_first_at, bar_close_at)
    persist_created_lag_s = _seconds_between(persist_created_at, bar_close_at)
    if archive_first_at is None and persist_created_at is not None:
        return "history_only_or_missing_live_bar"
    if archive_first_at is not None and persist_created_at is not None and persist_created_at < archive_first_at:
        return "history_replay_before_live_bar_arrived"
    if receive_lag_s is not None and receive_lag_s > 15:
        return "live_bar_received_late_before_drain"
    if (
        archive_first_at is not None
        and persist_created_at is not None
        and persist_created_at > archive_first_at + timedelta(seconds=10)
    ):
        return "persist_or_runtime_processing_late"
    if persist_created_lag_s is not None and persist_created_lag_s > 15:
        return "persist_late_without_archive_proof"
    return "timing_looks_normal"


def _classify_macd_30s(
    *,
    max_trade_recorded_lag_s: float | None,
    persist_updated_after_archive_s: float | None,
    persist_updated_lag_s: float | None,
) -> str:
    if max_trade_recorded_lag_s is not None and max_trade_recorded_lag_s > 30:
        return "trade_event_received_or_archived_late"
    if persist_updated_after_archive_s is not None and persist_updated_after_archive_s > 10:
        return "persist_or_bar_revision_late_after_archive"
    if persist_updated_lag_s is not None and persist_updated_lag_s > 30:
        return "persist_late_without_archive_proof"
    return "timing_looks_normal"


def _query_persisted_rows(
    *,
    conn: psycopg.Connection | None,
    psql_db: str | None,
    strategy_code: str,
    symbol: str,
    interval_secs: int,
    start_utc: datetime,
    end_utc: datetime,
) -> list[PersistedBarRow]:
    sql = """
        select
            bar_time,
            created_at,
            updated_at,
            volume,
            trade_count
        from strategy_bar_history
        where strategy_code = %s
          and symbol = %s
          and interval_secs = %s
          and bar_time >= %s
          and bar_time < %s
        order by bar_time
    """
    rows: list[tuple[Any, ...]]
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, (strategy_code, symbol, interval_secs, start_utc, end_utc))
            rows = cur.fetchall()
    elif psql_db:
        psql_sql = (
            "select "
            "bar_time,created_at,updated_at,volume,trade_count "
            "from strategy_bar_history "
            f"where strategy_code = '{strategy_code}' "
            f"and symbol = '{symbol}' "
            f"and interval_secs = {int(interval_secs)} "
            f"and bar_time >= '{start_utc.isoformat()}' "
            f"and bar_time < '{end_utc.isoformat()}' "
            "order by bar_time"
        )
        output = subprocess.check_output(
            ["sudo", "-u", "postgres", "psql", "-d", psql_db, "-At", "-F", "|", "-c", psql_sql],
            text=True,
        )
        rows = []
        for line in output.splitlines():
            parts = line.split("|")
            if len(parts) != 5:
                continue
            rows.append(
                (
                    datetime.fromisoformat(parts[0].replace(" ", "T")),
                    datetime.fromisoformat(parts[1].replace(" ", "T")),
                    datetime.fromisoformat(parts[2].replace(" ", "T")),
                    int(parts[3]),
                    int(parts[4]),
                )
            )
    else:
        raise ValueError("either --dsn or --psql-db is required")

    result: list[PersistedBarRow] = []
    for bar_time, created_at, updated_at, volume, trade_count in rows:
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=UTC)
        else:
            bar_time = bar_time.astimezone(UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        else:
            created_at = created_at.astimezone(UTC)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        else:
            updated_at = updated_at.astimezone(UTC)
        result.append(
            PersistedBarRow(
                bar_time=bar_time,
                created_at=created_at,
                updated_at=updated_at,
                volume=int(volume),
                trade_count=int(trade_count),
            )
        )
    return result


def _load_schwab_1m_archive_stats(
    *,
    archive_root: Path,
    day: str,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[datetime, LiveBarArchiveStats]:
    path = archive_root / day / f"{symbol}.jsonl"
    stats: dict[datetime, LiveBarArchiveStats] = {}
    if not path.exists():
        return stats
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(payload.get("event_type", "")).lower() != "live_bar":
                continue
            try:
                interval_secs = int(payload.get("interval_secs", 0) or 0)
                timestamp = float(payload.get("timestamp", 0) or 0)
                received_at_ns = int(payload.get("received_at_ns") or payload.get("recorded_at_ns", 0) or 0)
                recorded_at_ns = int(payload.get("recorded_at_ns", 0) or 0)
            except (TypeError, ValueError):
                continue
            if interval_secs != 60 or timestamp <= 0 or recorded_at_ns <= 0 or received_at_ns <= 0:
                continue
            bar_time = datetime.fromtimestamp(timestamp, UTC)
            if not (start_utc <= bar_time < end_utc):
                continue
            current = stats.setdefault(bar_time, LiveBarArchiveStats())
            current.observe(received_at_ns=received_at_ns, recorded_at_ns=recorded_at_ns)
    return stats


def _load_macd_30s_trade_bucket_stats(
    *,
    archive_root: Path,
    day: str,
    symbol: str,
    interval_secs: int,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[datetime, TradeBucketArchiveStats]:
    path = archive_root / day / f"{symbol}.jsonl"
    stats: dict[datetime, TradeBucketArchiveStats] = defaultdict(TradeBucketArchiveStats)
    if not path.exists():
        return {}
    start_ns = int(start_utc.timestamp() * 1_000_000_000)
    end_ns = int(end_utc.timestamp() * 1_000_000_000)
    bucket_width_ns = int(interval_secs) * 1_000_000_000
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(payload.get("event_type", "")).lower() != "trade":
                continue
            try:
                event_ns = int(payload.get("timestamp_ns", 0) or 0)
                received_at_ns = int(payload.get("received_at_ns") or payload.get("recorded_at_ns", 0) or 0)
                recorded_at_ns = int(payload.get("recorded_at_ns", 0) or 0)
            except (TypeError, ValueError):
                continue
            if event_ns <= 0 or recorded_at_ns <= 0 or received_at_ns <= 0:
                continue
            if event_ns < start_ns or event_ns >= end_ns:
                continue
            bucket_start_ns = (event_ns // bucket_width_ns) * bucket_width_ns
            bucket_time = datetime.fromtimestamp(bucket_start_ns / 1_000_000_000, UTC)
            stats[bucket_time].observe(
                event_ns=event_ns,
                received_at_ns=received_at_ns,
                recorded_at_ns=recorded_at_ns,
            )
    return dict(stats)


def _print_header(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


def _analyze_schwab_1m(
    *,
    conn: psycopg.Connection | None,
    psql_db: str | None,
    archive_root: Path,
    day: str,
    symbols: list[str],
    start_utc: datetime,
    end_utc: datetime,
    top: int,
) -> None:
    for symbol in symbols:
        persisted_rows = _query_persisted_rows(
            conn=conn,
            psql_db=psql_db,
            strategy_code="schwab_1m",
            symbol=symbol,
            interval_secs=60,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        archive_stats = _load_schwab_1m_archive_stats(
            archive_root=archive_root,
            day=day,
            symbol=symbol,
            start_utc=start_utc,
            end_utc=end_utc,
        )

        rows: list[tuple[float, str]] = []
        for row in persisted_rows:
            bar_close_at = row.bar_time + timedelta(seconds=60)
            archive = archive_stats.get(row.bar_time)
            receive_first_at = _ns_to_dt(archive.first_received_at_ns) if archive else None
            archive_first_at = _ns_to_dt(archive.first_recorded_at_ns) if archive else None
            receive_lag_s = _seconds_between(receive_first_at, bar_close_at)
            archive_lag_s = _seconds_between(archive_first_at, bar_close_at)
            persist_created_lag_s = _seconds_between(row.created_at, bar_close_at)
            persist_updated_lag_s = _seconds_between(row.updated_at, bar_close_at)
            persist_after_archive_s = _seconds_between(row.created_at, archive_first_at)
            classification = _classify_schwab_1m(
                receive_first_at=receive_first_at,
                archive_first_at=archive_first_at,
                persist_created_at=row.created_at,
                bar_close_at=bar_close_at,
            )
            sort_key = max(
                receive_lag_s or 0.0,
                archive_lag_s or 0.0,
                persist_created_lag_s or 0.0,
                persist_updated_lag_s or 0.0,
            )
            line = " | ".join(
                [
                    f"bar={_fmt_dt(row.bar_time)}",
                    f"close={_fmt_dt(bar_close_at)}",
                    f"receive_first={_fmt_dt(receive_first_at)}",
                    f"receive_lag_s={_fmt_float(receive_lag_s)}",
                    f"archive_first={_fmt_dt(archive_first_at)}",
                    f"archive_lag_s={_fmt_float(archive_lag_s)}",
                    f"persist_created={_fmt_dt(row.created_at)}",
                    f"persist_created_lag_s={_fmt_float(persist_created_lag_s)}",
                    f"persist_updated={_fmt_dt(row.updated_at)}",
                    f"persist_updated_lag_s={_fmt_float(persist_updated_lag_s)}",
                    f"persist_after_archive_s={_fmt_float(persist_after_archive_s)}",
                    f"archive_count={archive.count if archive else 0}",
                    f"class={classification}",
                ]
            )
            rows.append((sort_key, line))

        _print_header(f"{symbol} schwab_1m")
        if not rows:
            print("No persisted rows in window.")
            continue
        for _sort_key, line in sorted(rows, key=lambda item: item[0], reverse=True)[:top]:
            print(line)


def _analyze_macd_30s(
    *,
    conn: psycopg.Connection | None,
    psql_db: str | None,
    archive_root: Path,
    day: str,
    symbols: list[str],
    start_utc: datetime,
    end_utc: datetime,
    top: int,
) -> None:
    for symbol in symbols:
        persisted_rows = _query_persisted_rows(
            conn=conn,
            psql_db=psql_db,
            strategy_code="macd_30s",
            symbol=symbol,
            interval_secs=30,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        archive_stats = _load_macd_30s_trade_bucket_stats(
            archive_root=archive_root,
            day=day,
            symbol=symbol,
            interval_secs=30,
            start_utc=start_utc,
            end_utc=end_utc,
        )

        rows: list[tuple[float, str]] = []
        for row in persisted_rows:
            bar_close_at = row.bar_time + timedelta(seconds=30)
            archive = archive_stats.get(row.bar_time)
            last_trade_event_at = _ns_to_dt(archive.last_event_ns) if archive else None
            last_trade_received_at = _ns_to_dt(archive.last_received_at_ns) if archive else None
            last_trade_recorded_at = _ns_to_dt(archive.last_recorded_at_ns) if archive else None
            max_trade_received_lag_s = (
                round(archive.max_received_lag_ns / 1_000_000_000, 3) if archive else None
            )
            max_trade_recorded_lag_s = (
                round(archive.max_recorded_lag_ns / 1_000_000_000, 3) if archive else None
            )
            persist_updated_lag_s = _seconds_between(row.updated_at, bar_close_at)
            persist_updated_after_archive_s = _seconds_between(row.updated_at, last_trade_recorded_at)
            classification = _classify_macd_30s(
                max_trade_recorded_lag_s=max_trade_received_lag_s,
                persist_updated_after_archive_s=persist_updated_after_archive_s,
                persist_updated_lag_s=persist_updated_lag_s,
            )
            sort_key = max(
                max_trade_received_lag_s or 0.0,
                max_trade_recorded_lag_s or 0.0,
                persist_updated_lag_s or 0.0,
                persist_updated_after_archive_s or 0.0,
            )
            line = " | ".join(
                [
                    f"bar={_fmt_dt(row.bar_time)}",
                    f"close={_fmt_dt(bar_close_at)}",
                    f"last_trade_event={_fmt_dt(last_trade_event_at)}",
                    f"last_trade_received={_fmt_dt(last_trade_received_at)}",
                    f"max_trade_received_lag_s={_fmt_float(max_trade_received_lag_s)}",
                    f"last_trade_recorded={_fmt_dt(last_trade_recorded_at)}",
                    f"max_trade_recorded_lag_s={_fmt_float(max_trade_recorded_lag_s)}",
                    f"persist_created={_fmt_dt(row.created_at)}",
                    f"persist_updated={_fmt_dt(row.updated_at)}",
                    f"persist_updated_lag_s={_fmt_float(persist_updated_lag_s)}",
                    f"persist_updated_after_archive_s={_fmt_float(persist_updated_after_archive_s)}",
                    f"archive_trade_count={archive.trade_count if archive else 0}",
                    f"class={classification}",
                ]
            )
            rows.append((sort_key, line))

        _print_header(f"{symbol} macd_30s")
        if not rows:
            print("No persisted rows in window.")
            continue
        for _sort_key, line in sorted(rows, key=lambda item: item[0], reverse=True)[:top]:
            print(line)


def main() -> None:
    args = _parse_args()
    if not args.dsn and not args.psql_db:
        raise SystemExit("Either --dsn or --psql-db is required.")

    start_utc, end_utc = _window_bounds(args.day, args.start_hour, args.end_hour)
    archive_root = Path("/var/lib/project-mai-tai/schwab_ticks")
    conn = psycopg.connect(args.dsn) if args.dsn else None
    try:
        symbols = sorted({str(symbol).upper() for symbol in args.symbols if str(symbol).strip()})
        if args.strategy_code == "schwab_1m":
            _analyze_schwab_1m(
                conn=conn,
                psql_db=args.psql_db,
                archive_root=archive_root,
                day=args.day,
                symbols=symbols,
                start_utc=start_utc,
                end_utc=end_utc,
                top=args.top,
            )
        else:
            _analyze_macd_30s(
                conn=conn,
                psql_db=args.psql_db,
                archive_root=archive_root,
                day=args.day,
                symbols=symbols,
                start_utc=start_utc,
                end_utc=end_utc,
                top=args.top,
            )
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
