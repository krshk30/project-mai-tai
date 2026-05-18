#!/usr/bin/env python3
"""
check_bar_persist_lag.py

Audit `strategy_bar_history` for signal-timing delay: how long after a
bar's scheduled close did it actually get persisted?

`persist_lag_secs = (created_at - bar_time) - interval_secs`

Normal operation: <10s (covers close_grace + DB write).
Suspicious: >30s on 30s bots or >60s on 60s bots — points at the event
loop being blocked at bar-close time, which is exactly what hit GOVX on
2026-05-18 07:08:40 ET (P1_CROSS signal bar persisted 40s late because
the loop was busy with a scanner-promotion hydration replay). Fix
shipped as PR #173; this script is the validation that the fix is
holding.

Read-only. Cron-friendly: exit 0 if every symbol stays under the
warning threshold, exit 1 otherwise.

Examples
--------
# All 3 bots for today, defaults:
python scripts/check_bar_persist_lag.py --day 2026-05-18 --all-bots

# Single bot, single symbol, tight threshold for forensics:
python scripts/check_bar_persist_lag.py --day 2026-05-18 \
    --strategy-code macd_30s --symbol GOVX --warn-threshold-secs 10
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg


EASTERN = ZoneInfo("America/New_York")

_DEFAULT_INTERVALS = {
    "macd_30s": 30,
    "polygon_30s": 30,
    "schwab_1m": 60,
    "macd_1m": 60,
}

_DEFAULT_BOTS = ("macd_30s", "polygon_30s", "schwab_1m")


@dataclass
class LagRow:
    symbol: str
    bar_time: datetime
    created_at: datetime
    lag_secs: float
    decision_status: str
    decision_path: str


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Audit strategy_bar_history persist-lag (delay between bar close and DB persist)."
    )
    ap.add_argument("--day", required=True, help="Trading session day in ET, format YYYY-MM-DD")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--strategy-code",
        help="Single strategy code (e.g. macd_30s, schwab_1m, polygon_30s).",
    )
    group.add_argument(
        "--all-bots",
        action="store_true",
        help=f"Run for all default bots: {', '.join(_DEFAULT_BOTS)}.",
    )
    ap.add_argument(
        "--symbol",
        action="append",
        help="Restrict to one or more symbols (repeatable). Default: all symbols seen that day.",
    )
    ap.add_argument(
        "--interval-secs",
        type=int,
        default=None,
        help="Bar interval seconds. Default: inferred from strategy_code.",
    )
    ap.add_argument(
        "--warn-threshold-secs",
        type=float,
        default=None,
        help="Lag threshold (sec) that flags a bar as suspicious. "
        "Default: 15 for 30s bots, 30 for 60s bots.",
    )
    ap.add_argument(
        "--error-threshold-secs",
        type=float,
        default=None,
        help="Lag threshold (sec) for exit-code-1 (= regression). "
        "Default: 30 for 30s bots, 60 for 60s bots.",
    )
    ap.add_argument(
        "--worst-n",
        type=int,
        default=10,
        help="How many worst-offender rows to print per strategy. Default 10.",
    )
    ap.add_argument(
        "--start-hour",
        type=int,
        default=4,
        help="ET hour to start scanning at. Default 4 (pre-market open).",
    )
    ap.add_argument(
        "--end-hour",
        type=int,
        default=20,
        help="ET hour to stop scanning at (exclusive). Default 20 (post-market close).",
    )
    ap.add_argument(
        "--dsn",
        default=None,
        help="psycopg DSN. Default: read from MAI_TAI_DATABASE_URL env var.",
    )
    return ap.parse_args()


def _infer_interval(strategy_code: str) -> int:
    interval = _DEFAULT_INTERVALS.get(strategy_code)
    if interval is None:
        raise SystemExit(
            f"could not infer interval_secs from strategy_code={strategy_code}; pass --interval-secs"
        )
    return interval


def _default_thresholds(interval_secs: int) -> tuple[float, float]:
    if interval_secs <= 30:
        return 15.0, 30.0
    return 30.0, 60.0


def _resolve_dsn(arg_dsn: Optional[str]) -> str:
    if arg_dsn:
        return arg_dsn
    env_url = os.environ.get("MAI_TAI_DATABASE_URL", "").strip()
    if not env_url:
        raise SystemExit(
            "no DSN — pass --dsn or set MAI_TAI_DATABASE_URL"
        )
    # Strip sqlalchemy dialect prefix if present, e.g. postgresql+psycopg://...
    if "+" in env_url.split("://", 1)[0]:
        scheme, rest = env_url.split("://", 1)
        env_url = scheme.split("+", 1)[0] + "://" + rest
    return env_url


def _window_bounds(session_day: str, start_hour: int, end_hour: int) -> tuple[datetime, datetime]:
    parsed_day = datetime.strptime(session_day, "%Y-%m-%d").date()
    start_dt = datetime.combine(parsed_day, time(hour=start_hour), tzinfo=EASTERN)
    end_dt = datetime.combine(parsed_day, time(hour=end_hour), tzinfo=EASTERN)
    return start_dt.astimezone(UTC), end_dt.astimezone(UTC)


def _fmt_et(ts: datetime) -> str:
    return ts.astimezone(EASTERN).strftime("%H:%M:%S")


def _audit_strategy(
    conn: psycopg.Connection,
    *,
    strategy_code: str,
    interval_secs: int,
    start_utc: datetime,
    end_utc: datetime,
    symbols: Optional[list[str]],
    warn_threshold: float,
    error_threshold: float,
    worst_n: int,
) -> int:
    """Return the number of bars exceeding error_threshold (callers OR these)."""
    sql = """
        SELECT
            symbol,
            bar_time,
            created_at,
            EXTRACT(EPOCH FROM (created_at - bar_time)) - %s::float AS lag_secs,
            decision_status,
            decision_path
        FROM strategy_bar_history
        WHERE strategy_code = %s
          AND interval_secs = %s
          AND bar_time >= %s
          AND bar_time < %s
    """
    params: list[object] = [interval_secs, strategy_code, interval_secs, start_utc, end_utc]
    if symbols:
        sql += " AND symbol = ANY(%s)"
        params.append(symbols)
    sql += " ORDER BY bar_time"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = [
            LagRow(
                symbol=str(row[0]),
                bar_time=row[1],
                created_at=row[2],
                lag_secs=float(row[3]),
                decision_status=str(row[4] or ""),
                decision_path=str(row[5] or ""),
            )
            for row in cur.fetchall()
        ]

    print(
        f"=== {strategy_code} ({interval_secs}s)  "
        f"window={_fmt_et(start_utc)}-{_fmt_et(end_utc)} ET  "
        f"warn>{warn_threshold:.0f}s error>{error_threshold:.0f}s ==="
    )
    if not rows:
        print("  no bars in window")
        return 0

    lags = [row.lag_secs for row in rows]
    lags_sorted = sorted(lags)
    n = len(lags_sorted)
    median = lags_sorted[n // 2]
    p95 = lags_sorted[min(n - 1, int(n * 0.95))]
    p99 = lags_sorted[min(n - 1, int(n * 0.99))]
    max_lag = lags_sorted[-1]
    over_warn = sum(1 for l in lags if l > warn_threshold)
    over_error = sum(1 for l in lags if l > error_threshold)

    print(
        f"  bars={n}  median={median:.1f}s  p95={p95:.1f}s  p99={p99:.1f}s  max={max_lag:.1f}s  "
        f"over_warn={over_warn}  over_error={over_error}"
    )

    worst = sorted(rows, key=lambda r: r.lag_secs, reverse=True)[:worst_n]
    if worst and worst[0].lag_secs > warn_threshold:
        print(f"  --- worst {min(worst_n, len(worst))} offenders ---")
        for row in worst:
            if row.lag_secs <= warn_threshold:
                break
            tag = "ERROR" if row.lag_secs > error_threshold else "WARN "
            print(
                f"  {tag}  {row.symbol:>6}  bar={_fmt_et(row.bar_time)} ET  "
                f"persisted={_fmt_et(row.created_at)} ET  "
                f"lag={row.lag_secs:>6.1f}s  status={row.decision_status:<10}  path={row.decision_path}"
            )

    return over_error


def main() -> int:
    args = _parse_args()
    dsn = _resolve_dsn(args.dsn)
    start_utc, end_utc = _window_bounds(args.day, args.start_hour, args.end_hour)

    if args.all_bots:
        strategy_codes = list(_DEFAULT_BOTS)
    else:
        strategy_codes = [args.strategy_code]

    total_errors = 0
    with psycopg.connect(dsn) as conn:
        for strategy_code in strategy_codes:
            interval_secs = args.interval_secs or _infer_interval(strategy_code)
            warn_default, error_default = _default_thresholds(interval_secs)
            warn_threshold = args.warn_threshold_secs if args.warn_threshold_secs is not None else warn_default
            error_threshold = args.error_threshold_secs if args.error_threshold_secs is not None else error_default

            errors = _audit_strategy(
                conn,
                strategy_code=strategy_code,
                interval_secs=interval_secs,
                start_utc=start_utc,
                end_utc=end_utc,
                symbols=args.symbol,
                warn_threshold=warn_threshold,
                error_threshold=error_threshold,
                worst_n=args.worst_n,
            )
            total_errors += errors
            print()

    print(f"=== SUMMARY ===  total_bars_over_error_threshold={total_errors}")
    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
