#!/usr/bin/env python3
"""Daily Schwab access-token refresh cadence watch.

The dedicated refresher normally emits ``[SCHWAB-TOKEN-REFRESHED]`` every ~29
minutes.  Seven retained complete days measured 48-50 successes/day.  This
check grades one *complete Eastern calendar day* and refuses to turn missing or
unreadable evidence into a zero.

Threshold derivation (not intuition): the complete observed spread is
``50 - 48 = 2``.  Extend the measured range by that spread in each direction:
46-52 on a normal 24-hour day.  A count of 30 is therefore at least 18 missed
cycles versus the measured floor, or about 8.7 hours at 29 minutes/cycle.

Exit codes / verdicts:
  0 HEALTHY          count inside the duration-scaled range
  1 NOT_HEALTHY      complete readable window, count outside the range
  3 COULD_NOT_TELL   partial day, absent/unreadable evidence, or unbracketed window

Application log timestamps are emitted by ``project_mai_tai.log`` in the VPS
process timezone (UTC on the production host).  The checked window is Eastern;
DST is handled by converting its two boundaries to UTC.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import glob
import gzip
import math
from pathlib import Path
import re
import sys
from typing import Iterable, TextIO
from zoneinfo import ZoneInfo


MARKER = "[SCHWAB-TOKEN-REFRESHED]"
EASTERN = ZoneInfo("America/New_York")
BASELINE_LOW = 48
BASELINE_HIGH = 50
NORMAL_DAY_MINIMUM = BASELINE_LOW - (BASELINE_HIGH - BASELINE_LOW)  # 46
NORMAL_DAY_MAXIMUM = BASELINE_HIGH + (BASELINE_HIGH - BASELINE_LOW)  # 52
REFRESH_CADENCE_MINUTES = 29
COMPLETION_LAG = timedelta(minutes=35)
COVERAGE_BUCKET = timedelta(hours=1)
DEFAULT_GLOB = "/var/log/project-mai-tai/control.log*"
LOG_TS = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2})[ T](?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?:[,.](?P<fraction>\d{1,6}))?"
)


@dataclass(frozen=True)
class Result:
    code: int
    verdict: str
    detail: str


def eastern_window(day: date) -> tuple[datetime, datetime]:
    start_et = datetime.combine(day, time.min, tzinfo=EASTERN)
    end_et = datetime.combine(day + timedelta(days=1), time.min, tzinfo=EASTERN)
    return start_et.astimezone(UTC), end_et.astimezone(UTC)


def limits_for_window(start: datetime, end: datetime) -> tuple[int, int]:
    """Scale the 46-52 range for 23/25-hour DST transition days."""
    seconds = (end - start).total_seconds()
    ratio = seconds / timedelta(days=1).total_seconds()
    return math.ceil(NORMAL_DAY_MINIMUM * ratio), math.floor(NORMAL_DAY_MAXIMUM * ratio)


def parse_log_timestamp(line: str) -> datetime | None:
    match = LOG_TS.match(line)
    if match is None:
        return None
    fraction = (match.group("fraction") or "").ljust(6, "0")
    stamp = f"{match.group('day')}T{match.group('clock')}"
    parsed = datetime.fromisoformat(stamp).replace(tzinfo=UTC)
    return parsed.replace(microsecond=int(fraction or "0"))


def _open_log(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, mode="rt", encoding="utf-8", errors="strict")
    return path.open(mode="rt", encoding="utf-8", errors="strict")


def _could_not_tell(detail: str) -> Result:
    return Result(3, "COULD_NOT_TELL", detail)


def evaluate(paths: Iterable[Path], day: date, now: datetime) -> Result:
    start, end = eastern_window(day)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    if now < end + COMPLETION_LAG:
        return _could_not_tell(
            f"day={day.isoformat()} is not complete plus the 35-minute evidence lag "
            f"(window_end={end.isoformat()}, now={now.isoformat()})"
        )

    unique_paths = sorted({path.resolve() for path in paths})
    if not unique_paths:
        return _could_not_tell("no control.log files matched; missing evidence is not zero")

    earliest: datetime | None = None
    latest: datetime | None = None
    count = 0
    timestamped_in_window = 0
    marker_without_timestamp = 0
    covered_buckets: set[int] = set()

    for path in unique_paths:
        try:
            with _open_log(path) as handle:
                for line in handle:
                    stamp = parse_log_timestamp(line)
                    if MARKER in line and stamp is None:
                        marker_without_timestamp += 1
                        continue
                    if stamp is None:
                        continue
                    earliest = stamp if earliest is None else min(earliest, stamp)
                    latest = stamp if latest is None else max(latest, stamp)
                    if start <= stamp < end:
                        timestamped_in_window += 1
                        bucket = int((stamp - start).total_seconds() // COVERAGE_BUCKET.total_seconds())
                        covered_buckets.add(bucket)
                        if MARKER in line:
                            count += 1
        except (OSError, UnicodeError, gzip.BadGzipFile, EOFError) as exc:
            return _could_not_tell(f"cannot read {path}: {type(exc).__name__}: {exc}")

    if marker_without_timestamp:
        return _could_not_tell(
            f"found {marker_without_timestamp} refresh marker line(s) without a parseable UTC timestamp"
        )
    if earliest is None or latest is None:
        return _could_not_tell("matched logs contain no parseable timestamps")
    if earliest > start or latest < end:
        return _could_not_tell(
            "log population does not bracket the requested day; refusing a partial count "
            f"(coverage={earliest.isoformat()}..{latest.isoformat()}, "
            f"window={start.isoformat()}..{end.isoformat()})"
        )
    if timestamped_in_window == 0:
        return _could_not_tell(
            "the bracketed file population has no timestamped control records inside the day"
        )

    bucket_count = math.ceil((end - start).total_seconds() / COVERAGE_BUCKET.total_seconds())
    missing_buckets = sorted(set(range(bucket_count)) - covered_buckets)
    if missing_buckets:
        return _could_not_tell(
            "log population has no timestamped evidence in hourly window bucket(s) "
            f"{','.join(str(item) for item in missing_buckets)}; a missing middle rotation "
            "must not become a low refresh count"
        )

    minimum, maximum = limits_for_window(start, end)
    hours = (end - start).total_seconds() / 3600
    common = (
        f"day_et={day.isoformat()} window_utc={start.isoformat()}..{end.isoformat()} "
        f"hours={hours:.0f} refreshes={count} healthy_range={minimum}..{maximum} "
        f"timestamped_lines={timestamped_in_window} coverage_buckets={len(covered_buckets)}/{bucket_count} "
        f"files={len(unique_paths)}; "
        "range=measured 48..50 extended by observed spread 2, scaled by window duration"
    )
    if count < minimum:
        missed = max(0, BASELINE_LOW - count)
        return Result(
            1,
            "NOT_HEALTHY",
            f"{common}; at least {missed} below measured floor "
            f"(~{missed * REFRESH_CADENCE_MINUTES / 60:.1f} cadence-hours)",
        )
    if count > maximum:
        return Result(
            1,
            "NOT_HEALTHY",
            f"{common}; count is above the measured cadence range (hot loop or duplicated "
            "evidence requires investigation)",
        )
    return Result(0, "HEALTHY", common)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("day must be YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=_parse_day, help="Eastern calendar day; default: yesterday ET")
    parser.add_argument("--log-glob", default=DEFAULT_GLOB)
    parser.add_argument(
        "--now",
        help="ISO timestamp override for deterministic controls; default: current UTC",
    )
    args = parser.parse_args(argv)

    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(UTC)
    day = args.day or (now.astimezone(EASTERN).date() - timedelta(days=1))
    paths = [Path(item) for item in glob.glob(args.log_glob)]
    result = evaluate(paths, day, now)
    print(f"VERDICT: {result.verdict} {result.detail}")
    return result.code


if __name__ == "__main__":
    sys.exit(main())
