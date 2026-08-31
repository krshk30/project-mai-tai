#!/usr/bin/env python3
"""Read-only scanner-capture volume check with an ET-window-matched baseline.

The original box-only check collapsed a failed database read into ``rows=0`` and then
diagnosed the capture writer as broken.  This checker keeps those states separate and
reports observations only.  A low-volume verdict means that the measured row rate is far
below the previous matching weekdays at the same Eastern-time cutoff; it does not assign a
cause.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import os
import subprocess
from typing import Callable, Sequence
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
BASELINE_SESSION_COUNT = 5
MIN_BASELINE_SESSIONS = 3
LOW_VOLUME_RATIO = 0.20
FRESH_FEED_MAX_MINUTES = 15
PSQL_FIELD_SEPARATOR = "|"

SQL = r"""
WITH params AS (
    SELECT
        :'today_et'::date AS today_et,
        :'cutoff_et'::time AS cutoff_et,
        :'baseline_sessions'::integer AS baseline_sessions
),
prior_matching_weekdays AS (
    SELECT DISTINCT events.trade_date
    FROM scanner_confirmed_events AS events
    CROSS JOIN params
    WHERE events.trade_date < params.today_et
      AND EXTRACT(ISODOW FROM events.trade_date) = EXTRACT(ISODOW FROM params.today_et)
    ORDER BY events.trade_date DESC
    LIMIT (SELECT baseline_sessions FROM params)
),
current_stats AS (
    SELECT
        COUNT(events.id)::bigint AS row_count,
        COUNT(DISTINCT events.symbol)::bigint AS symbol_count,
        MAX(events.created_at) AS newest_row_at
    FROM scanner_confirmed_events AS events
    CROSS JOIN params
    WHERE events.trade_date = params.today_et
      AND events.created_at < (
          (params.today_et + params.cutoff_et) AT TIME ZONE 'America/New_York'
      )
),
baseline_counts AS (
    SELECT
        days.trade_date,
        COUNT(events.id)::bigint AS row_count
    FROM prior_matching_weekdays AS days
    CROSS JOIN params
    LEFT JOIN scanner_confirmed_events AS events
      ON events.trade_date = days.trade_date
     AND events.created_at < (
         (days.trade_date + params.cutoff_et) AT TIME ZONE 'America/New_York'
     )
    GROUP BY days.trade_date
)
SELECT
    current_stats.row_count,
    current_stats.symbol_count,
    COALESCE(
        TO_CHAR(
            current_stats.newest_row_at AT TIME ZONE 'America/New_York',
            'YYYY-MM-DD HH24:MI:SS'
        ),
        '-'
    ),
    COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY baseline_counts.row_count), -1),
    COUNT(baseline_counts.trade_date)
FROM current_stats
LEFT JOIN baseline_counts ON TRUE
GROUP BY current_stats.row_count, current_stats.symbol_count, current_stats.newest_row_at;
""".strip()


@dataclass(frozen=True)
class DatabaseObservation:
    row_count: int
    symbol_count: int
    newest_row_at_et: str
    baseline_median: float
    baseline_sessions: int


@dataclass(frozen=True)
class Verdict:
    status: str
    exit_code: int
    line: str


def _psql_target(database_url: str) -> tuple[list[str], dict[str, str]]:
    parsed = urlsplit(database_url.replace("+psycopg", ""))
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("database URL must use postgres or postgresql")
    if not parsed.hostname or not parsed.path.strip("/") or not parsed.username:
        raise ValueError("database URL is missing host, database, or user")
    env = os.environ.copy()
    env["PGPASSWORD"] = unquote(parsed.password or "")
    command = [
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
        "-F",
        PSQL_FIELD_SEPARATOR,
        "-h",
        parsed.hostname,
        "-p",
        str(parsed.port or 5432),
        "-U",
        unquote(parsed.username),
        "-d",
        parsed.path.strip("/"),
        "-f",
        "-",
    ]
    return command, env


def read_database_observation(
    database_url: str,
    now_et: datetime,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> DatabaseObservation:
    command, env = _psql_target(database_url)
    command[2:2] = [
        "-v",
        f"today_et={now_et.date().isoformat()}",
        "-v",
        f"cutoff_et={now_et.strftime('%H:%M:%S')}",
        "-v",
        f"baseline_sessions={BASELINE_SESSION_COUNT}",
    ]
    result = runner(
        command,
        input=SQL,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"database query failed with rc={result.returncode}")
    fields = result.stdout.strip().split(PSQL_FIELD_SEPARATOR)
    if len(fields) != 5:
        raise RuntimeError("database query returned an unexpected field count")
    try:
        return DatabaseObservation(
            row_count=int(fields[0]),
            symbol_count=int(fields[1]),
            newest_row_at_et=fields[2],
            baseline_median=float(fields[3]),
            baseline_sessions=int(fields[4]),
        )
    except ValueError as exc:
        raise RuntimeError("database query returned non-numeric count data") from exc


def read_feed_age_minutes(
    *,
    now: datetime,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    result = runner(
        ["redis-cli", "--raw", "XREVRANGE", "mai_tai:market-data", "+", "-", "COUNT", "1"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"market-data freshness read failed with rc={result.returncode}")
    first_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    try:
        last_event_ms = int(first_line.split("-", 1)[0])
    except ValueError as exc:
        raise RuntimeError("market-data freshness read returned no stream id") from exc
    now_ms = int(now.timestamp() * 1000)
    return max(0, (now_ms - last_event_ms) // 60_000)


def classify(
    observation: DatabaseObservation,
    *,
    now_et: datetime,
    feed_age_minutes: int,
) -> Verdict:
    window = f"{now_et.date().isoformat()} 00:00-{now_et.strftime('%H:%M:%S')} ET"
    common = (
        f"rows={observation.row_count} symbols={observation.symbol_count} window=\"{window}\" "
        f"newest_row_et=\"{observation.newest_row_at_et}\" "
        f"same_weekday_baseline_median={observation.baseline_median:g} "
        f"baseline_sessions={observation.baseline_sessions} feed_age_minutes={feed_age_minutes}"
    )
    if observation.baseline_sessions < MIN_BASELINE_SESSIONS or observation.baseline_median <= 0:
        return Verdict(
            "COULD_NOT_TELL",
            2,
            f"SCANNER_CAPTURE COULD_NOT_TELL {common} reason=baseline_population_insufficient",
        )
    ratio = observation.row_count / observation.baseline_median
    ratio_text = f"{ratio * 100:.1f}%"
    if feed_age_minutes >= FRESH_FEED_MAX_MINUTES:
        return Verdict(
            "COULD_NOT_TELL",
            2,
            f"SCANNER_CAPTURE COULD_NOT_TELL {common} current_vs_baseline={ratio_text} "
            "reason=market_data_not_fresh",
        )
    if ratio < LOW_VOLUME_RATIO:
        return Verdict(
            "LOW_VOLUME",
            1,
            f"SCANNER_CAPTURE LOW_VOLUME {common} current_vs_baseline={ratio_text} "
            f"threshold_below={LOW_VOLUME_RATIO * 100:.1f}% cause=NOT_DETERMINED",
        )
    return Verdict(
        "OBSERVED",
        0,
        f"SCANNER_CAPTURE OBSERVED {common} current_vs_baseline={ratio_text} "
        f"threshold_below={LOW_VOLUME_RATIO * 100:.1f}%",
    )


def run_check(
    *,
    now: datetime,
    database_url: str | None,
    database_reader: Callable[[str, datetime], DatabaseObservation] = read_database_observation,
    feed_reader: Callable[..., int] = read_feed_age_minutes,
) -> Verdict:
    now_et = now.astimezone(EASTERN)
    if not database_url:
        return Verdict(
            "COULD_NOT_TELL",
            2,
            "SCANNER_CAPTURE COULD_NOT_TELL database_read=FAILED "
            f"window=\"{now_et.date().isoformat()} 00:00-{now_et.strftime('%H:%M:%S')} ET\" "
            "row_count=UNMEASURED cause=NOT_DETERMINED reason=database_url_missing",
        )
    try:
        observation = database_reader(database_url, now_et)
    except Exception as exc:  # noqa: BLE001 - the monitor must turn every read failure into UNKNOWN.
        return Verdict(
            "COULD_NOT_TELL",
            2,
            "SCANNER_CAPTURE COULD_NOT_TELL database_read=FAILED "
            f"window=\"{now_et.date().isoformat()} 00:00-{now_et.strftime('%H:%M:%S')} ET\" "
            f"row_count=UNMEASURED cause=NOT_DETERMINED error={type(exc).__name__}",
        )
    try:
        feed_age = feed_reader(now=now)
    except Exception as exc:  # noqa: BLE001 - unreadable independent evidence is not green.
        return Verdict(
            "COULD_NOT_TELL",
            2,
            "SCANNER_CAPTURE COULD_NOT_TELL "
            f"rows={observation.row_count} symbols={observation.symbol_count} "
            f"newest_row_et=\"{observation.newest_row_at_et}\" "
            "feed_age_minutes=UNMEASURED cause=NOT_DETERMINED "
            f"error={type(exc).__name__}",
        )
    return classify(observation, now_et=now_et, feed_age_minutes=feed_age)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--now",
        help="ISO timestamp used only by deterministic controls; production omits it",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(tz=EASTERN)
    if now.tzinfo is None or now.utcoffset() is None:
        raise SystemExit("--now must carry an explicit timezone")
    verdict = run_check(now=now, database_url=os.environ.get("MAI_TAI_DATABASE_URL"))
    print(verdict.line)
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
