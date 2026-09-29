#!/usr/bin/env python3
"""Capture and grade the durable evidence for a schwab-1m-v2 restart.

The collector is read-only. Run ``snapshot`` before the deploy and ``report`` after it. The
snapshot makes "services deliberately not restarted" falsifiable; a post-deploy PID alone cannot
prove that a service stayed up. Every report line carries the population it measured. Instrument
failures exit 2 and never degrade to a clean zero.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from project_mai_tai.strategy_core.time_utils import is_fillable_et_session

ET = ZoneInfo("America/New_York")
UNIT_PREFIX = "project-mai-tai-"
V2_SERVICE = "schwab-1m-v2"
V2_STRATEGY_CODE = "schwab_1m_v2"
REST_WARMUP_FRESH_AGE_SECONDS = 300
PERSIST_BAR_AGE_LIMIT_SECONDS = 300
BOOT_WARMUP_FALLBACK_BOUND_SECONDS = 369
# A bar series counts as live at the v2 stop when its last bar before the stop started within
# this many seconds of it. Bars persist ~60 s after their start, so a stop mid-bar leaves the
# previous bar as the newest row: 3 bars keeps a live series in and a 57-minute-old one out.
LIVE_AT_STOP_BOUND_SECONDS = 180
MASSIVE_AGGREGATES_URL = "https://api.massive.com/v2/aggs/ticker"
MASSIVE_SOURCE_NOTE = (
    "Massive 1-minute aggregates adjusted=false; aggregate-eligible prints only; "
    "condition codes unavailable; later corrections may revise history; "
    "lookup refused during 09:30-16:00 ET"
)
LIVE_ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
DEFAULT_SERVICES = (
    "control",
    "market-capture",
    "market-data",
    "oms",
    "orb",
    "reconciler",
    "schwab-1m-v2",
    "strategy",
    "tv-alerts",
)
INTENTIONALLY_INACTIVE_SERVICES = frozenset({"tv-alerts"})
LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
FIELD = re.compile(r"\b([a-z_]+)=([^ ]+)")
WATCHLIST_UPDATE = re.compile(r"schwab_1m_v2 watchlist updated count=(\d+) sample=([^ ]*)")
FRESH_WARMUP = re.compile(
    r"\[V2-(?:REST|STREAMER)-WARMED\].* for ([A-Z0-9.\-^]+) "
    r"\(bar_age_seconds=([0-9.]+) "
)
SAFE_SYMBOL = re.compile(r"[A-Z0-9.\-^]{1,16}\Z")


class EvidenceUnknown(RuntimeError):
    """The instrument could not establish a measurement."""


Runner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class ServiceState:
    service: str
    pid: int
    active_state: str
    sub_state: str
    n_restarts: int
    started_at_utc: str


@dataclass(frozen=True)
class TracebackEvidence:
    timestamped_records: int
    traceback_times_utc: tuple[datetime, ...]


def run_checked(args: Sequence[str], *, timeout: int = 30) -> str:
    try:
        completed = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceUnknown(f"could not run {args[0]!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise EvidenceUnknown(
            f"{args[0]!r} exited {completed.returncode}: {detail[0] if detail else '<no output>'}"
        )
    return completed.stdout


def _systemctl_value(service: str, field: str, runner: Runner) -> str:
    value = runner(
        ["systemctl", "show", f"{UNIT_PREFIX}{service}.service", "-p", field, "--value"]
    ).strip()
    if not value:
        raise EvidenceUnknown(f"systemd returned no {field} for {service}")
    return value


def _parse_systemd_time(raw: str) -> datetime:
    match = re.fullmatch(r"[A-Za-z]{3} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (UTC|GMT)", raw)
    if match is None:
        raise EvidenceUnknown(f"unparseable systemd UTC timestamp: {raw!r}")
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def service_state(service: str, runner: Runner = run_checked) -> ServiceState:
    try:
        pid = int(_systemctl_value(service, "MainPID", runner))
        n_restarts = int(_systemctl_value(service, "NRestarts", runner))
    except ValueError as exc:
        raise EvidenceUnknown(
            f"systemd returned a non-numeric PID/restart count for {service}"
        ) from exc
    active_state = _systemctl_value(service, "ActiveState", runner)
    sub_state = _systemctl_value(service, "SubState", runner)
    started_raw = runner(
        [
            "systemctl",
            "show",
            f"{UNIT_PREFIX}{service}.service",
            "-p",
            "ExecMainStartTimestamp",
            "--value",
        ]
    ).strip()
    deliberately_inactive = (
        service in INTENTIONALLY_INACTIVE_SERVICES
        and pid == 0
        and active_state == "inactive"
        and sub_state == "dead"
    )
    if not started_raw and not deliberately_inactive:
        raise EvidenceUnknown(f"systemd returned no ExecMainStartTimestamp for {service}")
    started = _parse_systemd_time(started_raw).isoformat() if started_raw else ""
    return ServiceState(
        service=service,
        pid=pid,
        active_state=active_state,
        sub_state=sub_state,
        n_restarts=n_restarts,
        started_at_utc=started,
    )


def format_moment(value: datetime) -> str:
    value = value.astimezone(UTC)
    return (
        f"{value.astimezone(ET).strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"({value.strftime('%Y-%m-%d %H:%M:%S UTC')})"
    )


def parse_log_files(
    files: Iterable[tuple[str, Iterable[str]]], *, since: datetime
) -> TracebackEvidence:
    """Scope traceback headers by their nearest preceding timestamped line in the same file."""

    since = since.astimezone(UTC)
    timestamped_records = 0
    traceback_times: list[datetime] = []
    for name, lines in files:
        preceding: datetime | None = None
        for line in lines:
            match = LOG_TIMESTAMP.match(line)
            if match is not None:
                preceding = datetime.strptime(match.group(1).split(",", 1)[0], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
                if preceding >= since:
                    timestamped_records += 1
            if "Traceback (most recent call last):" not in line:
                continue
            if preceding is None:
                raise EvidenceUnknown(
                    f"{name} contains a traceback before any timestamp; its time scope is unknown"
                )
            if preceding >= since:
                traceback_times.append(preceding)
    return TracebackEvidence(timestamped_records, tuple(traceback_times))


def _log_files(
    service: str, runner: Runner, *, since: datetime | None = None
) -> list[tuple[str, list[str]]]:
    root = "/var/log/project-mai-tai"
    listing = runner(
        [
            "sudo",
            "-n",
            "find",
            root,
            "-maxdepth",
            "1",
            "-type",
            "f",
            "-name",
            f"{service}.log*",
            "-printf",
            "%T@|%p\\n",
        ]
    )
    found: list[tuple[float, str]] = []
    for line in listing.splitlines():
        try:
            stamp, path = line.split("|", 1)
            found.append((float(stamp), path))
        except ValueError as exc:
            raise EvidenceUnknown(f"unparseable log inventory line: {line!r}") from exc
    if not found:
        raise EvidenceUnknown(f"no log files found for {service}")

    cutoff = since.astimezone(UTC).timestamp() if since is not None else None
    result: list[tuple[str, list[str]]] = []
    for modified_at, path in sorted(found):
        # A file last modified before process start cannot contain post-start evidence.
        if cutoff is not None and modified_at < cutoff:
            continue
        raw = runner(["sudo", "-n", "zcat", "-f", "--", path])
        result.append((path, raw.splitlines()))
    return result


def _timestamped_lines_after(
    files: Iterable[tuple[str, Iterable[str]]], since: datetime
) -> list[tuple[datetime, str]]:
    rows: list[tuple[datetime, str]] = []
    for _, lines in files:
        for line in lines:
            match = LOG_TIMESTAMP.match(line)
            if match is None:
                continue
            stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
            if stamp >= since:
                rows.append((stamp, line))
    return sorted(rows)


def _psql(sql: str, runner: Runner) -> str:
    return runner(
        [
            "sudo",
            "-n",
            "-u",
            "postgres",
            "psql",
            "-d",
            "project_mai_tai",
            "-X",
            "-tA",
            "-F",
            "|",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ]
    ).strip()


def _single_row(raw: str, expected_fields: int, label: str) -> list[str]:
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        raise EvidenceUnknown(f"{label} returned {len(lines)} rows, expected exactly 1")
    fields = lines[0].split("|")
    if len(fields) != expected_fields:
        raise EvidenceUnknown(f"{label} returned {len(fields)} fields, expected {expected_fields}")
    return fields


def _flat_counts(runner: Runner) -> tuple[int, int, int]:
    account_sql = ",".join(f"'{name}'" for name in LIVE_ACCOUNTS)
    fields = _single_row(
        _psql(
            "SELECT "
            f"(SELECT count(*) FROM broker_accounts WHERE name IN ({account_sql})), "
            "(SELECT count(*) FROM oms_managed_positions "
            f" WHERE status='open' AND broker_account_name IN ({account_sql})), "
            "(SELECT count(*) FROM account_positions ap JOIN broker_accounts ba "
            " ON ba.id=ap.broker_account_id "
            f" WHERE ba.name IN ({account_sql}) AND ap.quantity <> 0);",
            runner,
        ),
        3,
        "flat-state query",
    )
    try:
        return tuple(int(value) for value in fields)  # type: ignore[return-value]
    except ValueError as exc:
        raise EvidenceUnknown("flat-state query returned a non-numeric count") from exc


def _latest_v2_watchlist(runner: Runner, *, now: datetime) -> tuple[datetime, tuple[str, ...]]:
    raw = runner(
        ["redis-cli", "--raw", "XREVRANGE", "mai_tai:strategy-state-isolated", "+", "-", "COUNT", "80"]
    )
    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("source_service") != "schwab-1m-v2":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("watchlist"), list):
            raise EvidenceUnknown("latest v2 state has no complete watchlist")
        produced = _parse_utc(str(event.get("produced_at", "")), label="v2 state")
        age = (now - produced).total_seconds()
        if not 0 <= age <= 30:
            raise EvidenceUnknown(f"latest v2 watchlist state age={age:.1f}s is not fresh")
        if any(
            not isinstance(symbol, str) or not SAFE_SYMBOL.fullmatch(symbol)
            for symbol in payload["watchlist"]
        ):
            raise EvidenceUnknown("latest v2 watchlist has an invalid symbol")
        symbols = tuple(sorted(set(payload["watchlist"])))
        return produced, symbols
    raise EvidenceUnknown("no v2 state in recent isolated-state stream entries")


def _migration_evidence(
    runner: Runner, *, columns: list[str], constraints: list[str]
) -> tuple[str, int, int, list[str]]:
    head = _psql("SELECT version_num FROM alembic_version;", runner)
    if not head or "\n" in head:
        raise EvidenceUnknown(f"alembic_version did not return one value: {head!r}")
    checked = 0
    found = 0
    missing: list[str] = []
    for item in columns:
        if "." not in item:
            raise EvidenceUnknown(f"--schema-column must be TABLE.COLUMN, got {item!r}")
        table, column = item.split(".", 1)
        value = _psql(
            "SELECT count(*) FROM information_schema.columns "
            f"WHERE table_name='{table}' AND column_name='{column}';",
            runner,
        )
        checked += 1
        if value == "1":
            found += 1
        else:
            missing.append(f"column:{item}={value or '<empty>'}")
    for item in constraints:
        if "." not in item:
            raise EvidenceUnknown(f"--schema-constraint must be TABLE.NAME, got {item!r}")
        table, constraint = item.split(".", 1)
        value = _psql(
            "SELECT count(*) FROM information_schema.table_constraints "
            f"WHERE table_name='{table}' AND constraint_name='{constraint}';",
            runner,
        )
        checked += 1
        if value == "1":
            found += 1
        else:
            missing.append(f"constraint:{item}={value or '<empty>'}")
    return head, found, checked, missing


def _parse_expected_flag(value: str) -> tuple[str, str, str]:
    try:
        service, assignment = value.split(":", 1)
        key, expected = assignment.split("=", 1)
    except ValueError as exc:
        raise EvidenceUnknown(f"--expect-flag must be SERVICE:KEY=VALUE, got {value!r}") from exc
    if not service or not key:
        raise EvidenceUnknown(f"incomplete --expect-flag {value!r}")
    return service, key, expected


def _process_environment(pid: int, runner: Runner) -> dict[str, str]:
    raw = runner(["sudo", "-n", "cat", f"/proc/{pid}/environ"])
    values: dict[str, str] = {}
    for item in raw.split("\0"):
        if "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    if not values:
        raise EvidenceUnknown(f"/proc/{pid}/environ was empty or unreadable")
    return values


@dataclass(frozen=True)
class BarContinuity:
    symbols: int
    pairs: int
    gaps: int
    brackets: int
    spanning: int
    excluded: int
    stopped_at_utc: datetime
    live_floor_utc: datetime
    pending_symbols: tuple[str, ...] = ()
    gap_results: tuple["RestartGapResult", ...] = ()


@dataclass(frozen=True)
class BackfillSymbolResult:
    symbol: str
    watched_at_stop: bool
    start_utc: datetime
    first_post_utc: datetime | None
    filled_minutes: tuple[datetime, ...]
    missing_print_minutes: tuple[datetime, ...]
    quiet_minutes: tuple[datetime, ...]
    verdict: str
    reason: str = ""
    unverified_minutes: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class BackfillContinuity:
    watched_at_stop: tuple[str, ...]
    added_after_restart: tuple[str, ...]
    symbols: tuple[BackfillSymbolResult, ...]
    verdict: str
    reason: str = ""


@dataclass(frozen=True)
class MinuteAggregate:
    timestamp_utc: datetime
    transactions: int


@dataclass(frozen=True)
class RestartGapResult:
    symbol: str
    prior_utc: datetime
    following_utc: datetime
    verdict: str
    per_minute_counts: tuple[tuple[datetime, int], ...] = ()
    reason: str = ""


class AggregateSourceUnknown(RuntimeError):
    """The independent Massive aggregate source did not answer conclusively."""


AggregateFetcher = Callable[[str, datetime, datetime], Sequence[MinuteAggregate]]


def _parse_utc(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceUnknown(f"unparseable {label} timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise EvidenceUnknown(f"timezone-free {label} timestamp: {value!r}")
    return parsed.astimezone(UTC)


def _fetch_massive_minute_aggregates(
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    api_key: str,
) -> tuple[MinuteAggregate, ...]:
    """Fetch one exact historical gap from Massive without exposing the API key."""

    if not api_key:
        raise AggregateSourceUnknown("MAI_TAI_MASSIVE_API_KEY is absent from the running v2")
    start_ms = int(start_utc.astimezone(UTC).timestamp() * 1000)
    end_ms = int(end_utc.astimezone(UTC).timestamp() * 1000)
    url = (
        f"{MASSIVE_AGGREGATES_URL}/{quote(symbol, safe='')}/range/1/minute/"
        f"{start_ms}/{end_ms}?adjusted=false&sort=asc&limit=50000"
    )
    request = Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "mai-tai-restart-evidence"},
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed HTTPS endpoint
            status = int(getattr(response, "status", 0))
            body = response.read()
    except HTTPError as exc:
        raise AggregateSourceUnknown(f"Massive HTTP {exc.code}") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise AggregateSourceUnknown(f"Massive request failed: {type(exc).__name__}") from exc
    if status != 200:
        raise AggregateSourceUnknown(f"Massive HTTP {status}")
    if not body:
        raise AggregateSourceUnknown("Massive returned an empty response body")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise AggregateSourceUnknown("Massive returned malformed JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") not in {"OK", "DELAYED"}:
        raise AggregateSourceUnknown("Massive response status was not OK")
    results = payload.get("results")
    if results is None:
        if payload.get("resultsCount") == 0 or payload.get("queryCount") == 0:
            results = []
        else:
            raise AggregateSourceUnknown("Massive response omitted aggregate results")
    if not isinstance(results, list):
        raise AggregateSourceUnknown("Massive aggregate results were not a list")

    aggregates: list[MinuteAggregate] = []
    seen: set[datetime] = set()
    for item in results:
        if not isinstance(item, dict):
            raise AggregateSourceUnknown("Massive returned a malformed aggregate row")
        try:
            timestamp = datetime.fromtimestamp(int(item["t"]) / 1000.0, UTC)
            transactions = int(item["n"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise AggregateSourceUnknown(
                "Massive aggregate row lacked a timestamp or transaction count"
            ) from exc
        if timestamp in seen or transactions <= 0:
            raise AggregateSourceUnknown("Massive returned duplicate or empty aggregate minutes")
        seen.add(timestamp)
        if start_utc <= timestamp <= end_utc:
            aggregates.append(MinuteAggregate(timestamp, transactions))
    return tuple(sorted(aggregates, key=lambda row: row.timestamp_utc))


def _massive_gap_fetcher(
    runner: Runner,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AggregateFetcher:
    api_key: str | None = None

    def fetch(symbol: str, start_utc: datetime, end_utc: datetime) -> Sequence[MinuteAggregate]:
        nonlocal api_key
        now_et = clock().astimezone(ET)
        if now_et.weekday() < 5 and time(9, 30) <= now_et.time() < time(16):
            raise AggregateSourceUnknown("Massive gap lookup refused during 09:30-16:00 ET")
        if api_key is None:
            pid = int(_systemctl_value(V2_SERVICE, "MainPID", runner))
            api_key = _process_environment(pid, runner).get("MAI_TAI_MASSIVE_API_KEY", "")
        return _fetch_massive_minute_aggregates(symbol, start_utc, end_utc, api_key=api_key)

    return fetch


def _v2_stopped_at(start: datetime, runner: Runner) -> datetime:
    """When the previous v2 process left the unit (systemd ``InactiveEnterTimestamp``).

    For a plain ``systemctl restart`` this is the same second as the new start; for a stop-then-
    start outage it is the moment the bar hole began. It is the only anchor that says whether a
    symbol's bar series was alive when v2 went down.
    """
    stopped = _parse_systemd_time(_systemctl_value(V2_SERVICE, "InactiveEnterTimestamp", runner))
    if stopped > start:
        raise EvidenceUnknown(
            f"systemd reports {V2_SERVICE} stopped at {stopped.isoformat()} which is after its "
            f"start at {start.isoformat()}"
        )
    return stopped


def _bar_continuity(
    runner: Runner,
    restart: datetime,
    *,
    aggregate_fetcher: AggregateFetcher | None = None,
) -> BarContinuity:
    """Count restart-spanning bar gaps among the series that were LIVE when v2 stopped.

    A pair whose earlier bar is older than ``LIVE_AT_STOP_BOUND_SECONDS`` before the stop belongs
    to a symbol that had already left the watchlist (or stopped trading) before v2 went down; the
    restart cannot have created that hole. 2026-09-15: AIXC was unsubscribed at 16:44 ET and
    re-promoted at 18:24 ET around a 17:40 ET restart, and the un-floored count BLOCKED the
    pre-open gate on a subscription gap while the three symbols held at the restart all read 60 s.
    Those pairs are still counted, as ``excluded``, so the denominator stays on the line.
    """
    restart_utc = restart.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00")
    stopped = _v2_stopped_at(restart, runner)
    live_floor = stopped - timedelta(seconds=LIVE_AT_STOP_BOUND_SECONDS)
    live_floor_utc = live_floor.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00")
    session_day = restart.astimezone(ET).date().isoformat()
    bracketing = f"prior < TIMESTAMPTZ '{restart_utc}' AND bar_time > TIMESTAMPTZ '{restart_utc}'"
    live_at_stop = f"prior >= TIMESTAMPTZ '{live_floor_utc}'"
    ctes = (
        "WITH ordered AS ("
        " SELECT symbol, bar_time,"
        " lag(bar_time) OVER (PARTITION BY symbol ORDER BY bar_time) AS prior"
        " FROM strategy_bar_history"
        f" WHERE strategy_code='{V2_STRATEGY_CODE}' AND interval_secs=60 AND source='live'"
        f" AND (bar_time AT TIME ZONE 'America/New_York')::date=DATE '{session_day}'"
        " AND (bar_time AT TIME ZONE 'America/New_York')::time >= TIME '04:00'"
        " AND (bar_time AT TIME ZONE 'America/New_York')::time < TIME '20:00'"
        "), measured AS ("
        " SELECT symbol, prior, bar_time, extract(epoch FROM bar_time-prior) AS delta"
        " FROM ordered WHERE prior IS NOT NULL"
        "), live_before AS ("
        " SELECT DISTINCT ON (symbol) symbol, bar_time AS prior"
        f" FROM ordered WHERE bar_time < TIMESTAMPTZ '{restart_utc}'"
        " ORDER BY symbol, bar_time DESC"
        "), pending AS ("
        " SELECT live_before.symbol, live_before.prior FROM live_before"
        f" WHERE live_before.prior >= TIMESTAMPTZ '{live_floor_utc}'"
        " AND NOT EXISTS (SELECT 1 FROM ordered later"
        " WHERE later.symbol=live_before.symbol"
        f" AND later.bar_time > TIMESTAMPTZ '{restart_utc}')"
        ")"
    )
    raw = _psql(
        ctes + " SELECT"
        " (SELECT count(DISTINCT symbol) FROM ordered),"
        " count(*),"
        " count(*) FILTER (WHERE delta > 90),"
        f" count(*) FILTER (WHERE {bracketing} AND {live_at_stop}),"
        f" count(*) FILTER (WHERE delta > 90 AND {bracketing} AND {live_at_stop}),"
        f" count(*) FILTER (WHERE {bracketing} AND NOT ({live_at_stop})),"
        " (SELECT count(*) FROM pending) FROM measured;",
        runner,
    )
    fields = _single_row(raw, 7, "bar-continuity query")
    try:
        counts = [int(value) for value in fields]
    except ValueError as exc:
        raise EvidenceUnknown("bar-continuity query returned a non-numeric count") from exc
    symbols, pairs, gaps, brackets, spanning, excluded, pending_count = counts

    detail_rows: list[tuple[str, str, datetime, datetime | None]] = []
    if spanning or pending_count:
        detail_raw = _psql(
            ctes + " SELECT kind, symbol, prior, following FROM ("
            " SELECT 'GAP' AS kind, symbol, prior, bar_time AS following"
            " FROM measured"
            f" WHERE delta > 90 AND {bracketing} AND {live_at_stop}"
            " UNION ALL"
            " SELECT 'PENDING' AS kind, symbol, prior, NULL::timestamptz AS following"
            " FROM pending"
            ") evidence ORDER BY kind, symbol;",
            runner,
        )
        for line in detail_raw.splitlines():
            values = line.split("|")
            if len(values) != 4 or values[0] not in {"GAP", "PENDING"}:
                raise EvidenceUnknown(f"malformed bar-continuity detail row: {line!r}")
            following = _parse_utc(values[3], label="following bar") if values[3] else None
            detail_rows.append(
                (values[0], values[1], _parse_utc(values[2], label="prior bar"), following)
            )
    gap_rows = [row for row in detail_rows if row[0] == "GAP"]
    pending_rows = [row for row in detail_rows if row[0] == "PENDING"]
    if len(gap_rows) != spanning or len(pending_rows) != pending_count:
        raise EvidenceUnknown(
            "bar-continuity detail population did not match its counts: "
            f"gaps={len(gap_rows)}/{spanning} pending={len(pending_rows)}/{pending_count}"
        )

    fetcher = aggregate_fetcher
    if gap_rows and fetcher is None:
        fetcher = _massive_gap_fetcher(runner)
    gap_results: list[RestartGapResult] = []
    for _, symbol, prior, following in gap_rows:
        if fetcher is None:
            raise EvidenceUnknown("restart gap has no independent aggregate source")
        if following is None:
            raise EvidenceUnknown(f"restart gap for {symbol} has no following bar")
        missing_start = prior + timedelta(minutes=1)
        missing_end = following - timedelta(minutes=1)
        if missing_start > missing_end:
            raise EvidenceUnknown(f"restart gap for {symbol} has no missing minute")
        try:
            aggregates = tuple(fetcher(symbol, missing_start, missing_end))
            by_minute = {row.timestamp_utc.astimezone(UTC): row.transactions for row in aggregates}
            expected_minutes: list[datetime] = []
            minute = missing_start
            while minute <= missing_end:
                expected_minutes.append(minute)
                minute += timedelta(minutes=1)
            unexpected = set(by_minute) - set(expected_minutes)
            if unexpected:
                raise AggregateSourceUnknown("Massive returned minutes outside the requested gap")
            counts_by_minute = tuple(
                (minute, by_minute.get(minute, 0)) for minute in expected_minutes
            )
            verdict = "HOLE" if sum(count for _, count in counts_by_minute) else "NO_TRADES_IN_GAP"
            gap_results.append(
                RestartGapResult(symbol, prior, following, verdict, counts_by_minute)
            )
        except AggregateSourceUnknown as exc:
            gap_results.append(
                RestartGapResult(symbol, prior, following, "COULD_NOT_TELL", reason=str(exc))
            )

    return BarContinuity(
        symbols,
        pairs,
        gaps,
        brackets,
        spanning,
        excluded,
        stopped,
        live_floor,
        tuple(row[1] for row in pending_rows),
        tuple(gap_results),
    )


def _format_restart_gap(result: RestartGapResult) -> str:
    missing_start = result.prior_utc + timedelta(minutes=1)
    missing_end = result.following_utc - timedelta(minutes=1)
    diagnostic = {
        "HOLE": "PERSISTED_GAP_WITH_PRINTS",
        "NO_TRADES_IN_GAP": "PERSISTED_GAP_NO_ELIGIBLE_PRINTS",
        "COULD_NOT_TELL": "PERSISTED_GAP_UNGRADED",
    }.get(result.verdict, "PERSISTED_GAP_UNGRADED")
    prefix = (
        f"{diagnostic} symbol={result.symbol} "
        f"window={format_moment(missing_start)}..{format_moment(missing_end)}"
    )
    if result.reason:
        return f"{prefix} reason={result.reason} source=({MASSIVE_SOURCE_NOTE})"
    counts = ",".join(
        f"{minute.astimezone(ET).strftime('%H:%M')}={count}"
        for minute, count in result.per_minute_counts
    )
    total = sum(count for _, count in result.per_minute_counts)
    return (
        f"{prefix} eligible_transactions={total} per_minute={counts or '-'} "
        f"source=({MASSIVE_SOURCE_NOTE})"
    )


def _watchlist_updates(
    lines: Sequence[tuple[datetime, str]], *, through: datetime | None = None
) -> list[tuple[datetime, frozenset[str]]]:
    updates: list[tuple[datetime, frozenset[str]]] = []
    for stamp, line in lines:
        if through is not None and stamp > through:
            continue
        match = WATCHLIST_UPDATE.search(line)
        if match is None:
            continue
        count = int(match.group(1))
        sample = frozenset(symbol for symbol in match.group(2).split(",") if symbol)
        if count > 5 or len(sample) != count or any(not SAFE_SYMBOL.fullmatch(s) for s in sample):
            raise EvidenceUnknown(
                f"watchlist update at {format_moment(stamp)} has only a partial/invalid sample "
                f"({len(sample)}/{count}); cannot establish the watched population"
            )
        updates.append((stamp, sample))
    return updates


def _symbol_bar_rows(
    symbol: str, session_day: str, runner: Runner
) -> tuple[tuple[datetime, datetime, datetime], ...]:
    if SAFE_SYMBOL.fullmatch(symbol) is None:
        raise EvidenceUnknown(f"invalid bar-continuity symbol {symbol!r}")
    raw = _psql(
        "SELECT bar_time, created_at, updated_at FROM strategy_bar_history "
        f"WHERE strategy_code='{V2_STRATEGY_CODE}' AND interval_secs=60 "
        f"AND symbol='{symbol}' "
        f"AND (bar_time AT TIME ZONE 'America/New_York')::date=DATE '{session_day}' "
        "AND (bar_time AT TIME ZONE 'America/New_York')::time >= TIME '04:00' "
        "AND (bar_time AT TIME ZONE 'America/New_York')::time < TIME '20:00' "
        "ORDER BY bar_time;",
        runner,
    )
    rows: list[tuple[datetime, datetime, datetime]] = []
    for line in raw.splitlines():
        fields = line.split("|")
        if len(fields) != 3:
            raise EvidenceUnknown(f"malformed bar row for {symbol}: {line!r}")
        bar, created, updated = (
            _parse_utc(value, label=f"{symbol} {label}")
            for value, label in zip(fields, ("bar", "created", "updated"), strict=True)
        )
        if bar.second or bar.microsecond or (rows and bar <= rows[-1][0]):
            raise EvidenceUnknown(f"unordered/non-minute bar history for {symbol}")
        rows.append((bar, created, updated))
    return tuple(rows)


def _grade_backfill_symbol(
    symbol: str,
    *,
    watched_at_stop: bool,
    start_utc: datetime,
    restart_utc: datetime,
    first_live_utc: datetime | None,
    rows: Sequence[tuple[datetime, datetime, datetime]],
    fetcher: AggregateFetcher,
    first_live_seen_utc: datetime | None = None,
) -> BackfillSymbolResult:
    prior = max((bar for bar, _, _ in rows if bar < restart_utc), default=None)
    start = prior + timedelta(minutes=1) if watched_at_stop and prior else start_utc.replace(
        second=0, microsecond=0
    )
    if watched_at_stop and prior is None:
        return BackfillSymbolResult(
            symbol, True, start_utc, None, (), (), (), "COULD_NOT_TELL",
            "no pre-restart bar for a watched-at-stop symbol",
        )
    # A historical REST replay can be created after restart. Only the fresh warmup marker
    # identifies the current-time bar that ends the recovery window.
    first_post = first_live_utc
    current_floor = max(restart_utc, start_utc).replace(second=0, microsecond=0)
    current_row = next((row for row in rows if row[0] == first_post), None)
    if (
        first_post is None
        or first_post < current_floor
        or current_row is None
        or max(current_row[1], current_row[2]) < restart_utc
    ):
        return BackfillSymbolResult(
            symbol, watched_at_stop, start, None, (), (), (), "COULD_NOT_TELL",
            "no fresh post-restart warmup bar in persisted strategy history",
        )
    end = first_post - timedelta(minutes=1)
    expected: list[datetime] = []
    minute = start
    while minute <= end:
        expected.append(minute)
        minute += timedelta(minutes=1)
    present = {bar for bar, _, _ in rows}
    filled = tuple(minute for minute in expected if minute in present)
    absent = tuple(minute for minute in expected if minute not in present)
    if not absent:
        return BackfillSymbolResult(
            symbol, watched_at_stop, start, first_post, filled, (), (), "PASS"
        )
    try:
        aggregates = tuple(fetcher(symbol, absent[0], absent[-1]))
        counts = {item.timestamp_utc.astimezone(UTC): item.transactions for item in aggregates}
        if len(counts) != len(aggregates) or set(counts) - set(expected):
            raise AggregateSourceUnknown("independent tape returned duplicate/out-of-window minutes")
    except AggregateSourceUnknown as exc:
        return BackfillSymbolResult(
            symbol, watched_at_stop, start, first_post, filled, (), (), "COULD_NOT_TELL",
            str(exc),
        )
    seen = first_live_seen_utc or first_post
    printed = tuple(
        minute for minute in absent
        if counts.get(minute, 0) > 0
        and (seen - minute).total_seconds() <= PERSIST_BAR_AGE_LIMIT_SECONDS
    )
    unverified = tuple(
        minute for minute in absent
        if counts.get(minute, 0) > 0
        and (seen - minute).total_seconds() > PERSIST_BAR_AGE_LIMIT_SECONDS
    )
    quiet = tuple(minute for minute in absent if counts.get(minute, 0) == 0)
    verdict = (
        "MISSING_PRINTED_MINUTE" if printed else "COULD_NOT_TELL" if unverified else "PASS"
    )
    return BackfillSymbolResult(
        symbol, watched_at_stop, start, first_post, filled, printed, quiet,
        verdict,
        "older REST replay may exist only in strategy memory" if unverified else "",
        unverified,
    )


def _fresh_warmup_minutes(
    lines: Sequence[tuple[datetime, str]],
) -> dict[str, list[tuple[datetime, datetime]]]:
    result: dict[str, list[tuple[datetime, datetime]]] = {}
    for stamp, line in lines:
        match = FRESH_WARMUP.search(line)
        if match is None:
            continue
        age = float(match.group(2))
        if not 0 <= age <= REST_WARMUP_FRESH_AGE_SECONDS:
            raise EvidenceUnknown(f"invalid fresh warmup age for {match.group(1)}: {age}")
        bar = (stamp - timedelta(seconds=age) + timedelta(milliseconds=500)).replace(
            second=0, microsecond=0
        )
        result.setdefault(match.group(1), []).append((stamp, bar))
    return result


def _rest_backfill_continuity(
    runner: Runner,
    restart: datetime,
    stopped: datetime,
    post_lines: Sequence[tuple[datetime, str]],
    before: dict,
    *,
    aggregate_fetcher: AggregateFetcher | None = None,
) -> BackfillContinuity:
    session_start = datetime.combine(restart.astimezone(ET).date(), time(4), ET).astimezone(UTC)
    session_end = datetime.combine(restart.astimezone(ET).date(), time(20), ET).astimezone(UTC)
    post_lines = [row for row in post_lines if row[0] < session_end]
    pre_files = _log_files(V2_SERVICE, runner, since=session_start)
    pre_lines = [
        row for row in _timestamped_lines_after(pre_files, since=session_start)
        if row[0] < stopped
    ]
    snapshot_watchlist = before.get("v2_watchlist")
    if snapshot_watchlist is not None:
        produced = _parse_utc(
            str(snapshot_watchlist["produced_at_utc"]), label="snapshot v2 watchlist"
        )
        if produced > stopped:
            raise EvidenceUnknown("snapshot watchlist was produced after v2 stopped")
        later_lines = [
            row for row in pre_lines
            if row[0] > produced and WATCHLIST_UPDATE.search(row[1])
        ]
        later_updates = _watchlist_updates(later_lines[-1:])
        watched = (
            later_updates[-1][1]
            if later_updates
            else frozenset(snapshot_watchlist["symbols"])
        )
    else:
        pre_updates = _watchlist_updates(
            [row for row in pre_lines if WATCHLIST_UPDATE.search(row[1])][-1:]
        )
        if not pre_updates:
            return BackfillContinuity((), (), (), "COULD_NOT_TELL", "no pre-stop watchlist state")
        watched = pre_updates[-1][1]
    post_updates = _watchlist_updates(post_lines)
    warmup_minutes = _fresh_warmup_minutes(post_lines)
    added_at: dict[str, datetime] = {}
    prior_set: frozenset[str] = frozenset()
    for stamp, selected in post_updates:
        for symbol in selected - prior_set:
            added_at[symbol] = stamp
        prior_set = selected
    added = set(added_at)
    if not watched and not added:
        return BackfillContinuity((), (), (), "N/A", "nothing watched at stop or added after restart")
    if watched and not post_updates:
        return BackfillContinuity(
            tuple(sorted(watched)), (), (), "COULD_NOT_TELL", "no post-restart watchlist state"
        )
    fetcher = aggregate_fetcher or _massive_gap_fetcher(runner)
    results: list[BackfillSymbolResult] = []
    session_day = restart.astimezone(ET).date().isoformat()
    for symbol in sorted(watched | added):
        start = stopped if symbol in watched else added_at[symbol]
        warmups = [
            (stamp, bar) for stamp, bar in warmup_minutes.get(symbol, ())
            if stamp >= (restart if symbol in watched else start)
        ]
        results.append(
            _grade_backfill_symbol(
                symbol,
                watched_at_stop=symbol in watched,
                start_utc=start,
                restart_utc=restart,
                first_live_utc=warmups[0][1] if warmups else None,
                rows=_symbol_bar_rows(symbol, session_day, runner),
                fetcher=fetcher,
                first_live_seen_utc=warmups[0][0] if warmups else None,
            )
        )
    verdict = (
        "MISSING_PRINTED_MINUTE"
        if any(row.verdict == "MISSING_PRINTED_MINUTE" for row in results)
        else "COULD_NOT_TELL"
        if any(row.verdict == "COULD_NOT_TELL" for row in results)
        else "PASS"
    )
    return BackfillContinuity(tuple(sorted(watched)), tuple(sorted(added)), tuple(results), verdict)


def _restart_inside_bar_session(restart: datetime) -> bool:
    return is_fillable_et_session(restart, 4, 20)


def snapshot(path: Path, runner: Runner = run_checked) -> bool:
    captured = datetime.now(UTC)
    accounts_found, managed_open, positions_nonzero = _flat_counts(runner)
    migration_head, _, _, _ = _migration_evidence(runner, columns=[], constraints=[])
    watchlist_at, watchlist_symbols = _latest_v2_watchlist(runner, now=captured)
    flat = accounts_found == len(LIVE_ACCOUNTS) and managed_open == 0 and positions_nonzero == 0
    payload = {
        "schema_version": 3,
        "captured_at_utc": captured.isoformat(),
        "captured_at_et": captured.astimezone(ET).isoformat(),
        "alembic_version": migration_head,
        "v2_watchlist": {
            "produced_at_utc": watchlist_at.isoformat(),
            "symbols": list(watchlist_symbols),
        },
        "live_exposure": {
            "accounts_found": accounts_found,
            "accounts_expected": len(LIVE_ACCOUNTS),
            "open_managed_rows": managed_open,
            "nonzero_account_position_rows": positions_nonzero,
        },
        "services": {name: asdict(service_state(name, runner)) for name in DEFAULT_SERVICES},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"{'PASS' if flat else 'FAIL'}: "
        f"captured services={len(DEFAULT_SERVICES)}/{len(DEFAULT_SERVICES)}; "
        f"live accounts={accounts_found}/{len(LIVE_ACCOUNTS)}; "
        f"open managed rows={managed_open}; "
        f"nonzero account-position rows={positions_nonzero}; "
        f"alembic_version={migration_head}; at "
        f"{format_moment(captured)} -> {path}"
    )
    return flat


def _load_snapshot(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceUnknown(f"could not read snapshot {path}: {exc}") from exc
    if payload.get("schema_version") not in {2, 3} or not isinstance(payload.get("services"), dict):
        raise EvidenceUnknown(f"snapshot {path} has an unsupported shape")
    if payload["schema_version"] == 3:
        watchlist = payload.get("v2_watchlist")
        if not isinstance(watchlist, dict) or not isinstance(watchlist.get("symbols"), list):
            raise EvidenceUnknown(f"snapshot {path} has no v2 watchlist")
        _parse_utc(str(watchlist.get("produced_at_utc", "")), label="snapshot v2 watchlist")
        if any(
            not isinstance(symbol, str) or not SAFE_SYMBOL.fullmatch(symbol)
            for symbol in watchlist["symbols"]
        ):
            raise EvidenceUnknown(f"snapshot {path} has invalid v2 watchlist symbols")
    missing_services = set(DEFAULT_SERVICES) - set(payload["services"])
    exposure = payload.get("live_exposure")
    if missing_services or not isinstance(exposure, dict) or not payload.get("alembic_version"):
        raise EvidenceUnknown(
            f"snapshot {path} is missing services or pre-restart exposure evidence"
        )
    exposure_fields = {
        "accounts_found",
        "accounts_expected",
        "open_managed_rows",
        "nonzero_account_position_rows",
    }
    if not exposure_fields.issubset(exposure):
        raise EvidenceUnknown(f"snapshot {path} has incomplete pre-restart exposure evidence")
    return payload


def _fields(line: str) -> dict[str, str]:
    return dict(FIELD.findall(line))


def _integer_fields(line: str, *names: str) -> dict[str, int]:
    values = _fields(line)
    parsed: dict[str, int] = {}
    for name in names:
        raw = values.get(name)
        try:
            parsed[name] = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise EvidenceUnknown(f"restart marker has no numeric {name}: {line!r}") from exc
    return parsed


def report(args: argparse.Namespace, runner: Runner = run_checked) -> int:
    before = _load_snapshot(args.snapshot)
    restarted = set(args.restarted)
    expected_quiet_services = set(args.expected_quiet_service)
    if V2_SERVICE not in restarted:
        raise EvidenceUnknown(
            f"C6 is the v2 restart artifact; --restarted must include {V2_SERVICE}"
        )
    unknown_services = restarted - set(DEFAULT_SERVICES)
    if unknown_services:
        raise EvidenceUnknown(f"unknown restarted service(s): {','.join(sorted(unknown_services))}")
    unexpected_quiet_declarations = expected_quiet_services - restarted
    if unexpected_quiet_declarations:
        raise EvidenceUnknown(
            "expected-quiet service was not declared restarted: "
            + ",".join(sorted(unexpected_quiet_declarations))
        )
    if not args.schema_column and not args.schema_constraint and not args.no_schema_change:
        raise EvidenceUnknown(
            "declare --no-schema-change or name every added --schema-column/--schema-constraint"
        )
    if args.no_schema_change and (args.schema_column or args.schema_constraint):
        raise EvidenceUnknown(
            "--no-schema-change cannot be combined with schema-column/schema-constraint evidence"
        )

    current = {name: service_state(name, runner) for name in DEFAULT_SERVICES}
    before_services = before["services"]
    failures: list[str] = []
    rows: list[tuple[str, str, str]] = []

    restarted_ok = 0
    for service in sorted(restarted):
        old_pid = int(before_services[service]["pid"])
        state = current[service]
        good = (
            state.pid > 0
            and state.pid != old_pid
            and state.active_state == "active"
            and state.sub_state == "running"
            and state.n_restarts == 0
        )
        restarted_ok += int(good)
        if not good:
            failures.append(f"{service} did not return active/running on a new PID")
    rows.append(
        (
            "Restarted services",
            f"new active/running PID={restarted_ok}/{len(restarted)}; "
            + "; ".join(
                f"{name} {before_services[name]['pid']}->{current[name].pid} "
                f"NRestarts={current[name].n_restarts} "
                f"started={format_moment(datetime.fromisoformat(current[name].started_at_utc))}"
                for name in sorted(restarted)
            ),
            "PASS" if restarted_ok == len(restarted) else "FAIL",
        )
    )

    untouched = sorted(set(DEFAULT_SERVICES) - restarted)
    unchanged = [
        name
        for name in untouched
        if int(before_services[name]["pid"]) == current[name].pid
        and before_services[name]["active_state"] == current[name].active_state
        and before_services[name]["sub_state"] == current[name].sub_state
        and int(before_services[name]["n_restarts"]) == current[name].n_restarts
    ]
    changed = [name for name in untouched if name not in unchanged]
    if changed:
        failures.append(f"deliberately untouched service PID changed: {','.join(changed)}")
    rows.append(
        (
            "Services not restarted",
            f"unchanged PID={len(unchanged)}/{len(untouched)}; "
            + "; ".join(
                f"{name}={before_services[name]['pid']}->{current[name].pid}" for name in untouched
            ),
            "PASS" if not changed else "FAIL",
        )
    )

    before_exposure = before["live_exposure"]
    before_flat = (
        int(before_exposure["accounts_found"]) == len(LIVE_ACCOUNTS)
        and int(before_exposure["accounts_expected"]) == len(LIVE_ACCOUNTS)
        and int(before_exposure["open_managed_rows"]) == 0
        and int(before_exposure["nonzero_account_position_rows"]) == 0
    )
    accounts_found, managed_open, positions_nonzero = _flat_counts(runner)
    after_flat = (
        accounts_found == len(LIVE_ACCOUNTS) and managed_open == 0 and positions_nonzero == 0
    )
    flat_ok = before_flat and after_flat
    if not flat_ok:
        failures.append("live accounts are not provably flat before and after restart")
    rows.append(
        (
            "Both live accounts flat before/after",
            f"accounts={','.join(LIVE_ACCOUNTS)}; "
            f"before resolved={before_exposure['accounts_found']}/{len(LIVE_ACCOUNTS)} "
            f"open managed rows={before_exposure['open_managed_rows']} "
            "nonzero account-position rows="
            f"{before_exposure['nonzero_account_position_rows']}; "
            f"after resolved={accounts_found}/{len(LIVE_ACCOUNTS)} "
            f"open managed rows={managed_open} "
            f"nonzero account-position rows={positions_nonzero}",
            "PASS" if flat_ok else "FAIL",
        )
    )

    migration_head, schema_found, schema_total, schema_missing = _migration_evidence(
        runner, columns=args.schema_column, constraints=args.schema_constraint
    )
    prior_migration_head = str(before["alembic_version"])
    migration_ok = migration_head == args.expected_alembic_head
    migration_transition_ok = (
        migration_head == prior_migration_head
        if args.no_schema_change
        else migration_head != prior_migration_head
    )
    schema_ok = schema_found == schema_total
    if not migration_ok:
        failures.append(f"alembic head is {migration_head}, expected {args.expected_alembic_head}")
    if not schema_ok:
        failures.append("migration schema object missing: " + ",".join(schema_missing))
    if not migration_transition_ok:
        failures.append(
            "alembic head movement contradicts the declared schema-change mode: "
            f"{prior_migration_head}->{migration_head}"
        )
    schema_text = (
        f"schema objects found={schema_found}/{schema_total}"
        if schema_total
        else "schema objects=0/0 (deploy declared no schema change)"
    )
    rows.append(
        (
            "Migration",
            f"alembic_version={prior_migration_head}->{migration_head} "
            f"expected={args.expected_alembic_head}; {schema_text}",
            "PASS" if migration_ok and migration_transition_ok and schema_ok else "FAIL",
        )
    )

    flag_matches = 0
    flag_details: list[str] = []
    for encoded in args.expect_flag:
        service, key, expected = _parse_expected_flag(encoded)
        if service not in restarted:
            raise EvidenceUnknown(f"flag check names non-restarted service {service!r}")
        state = current[service]
        actual = _process_environment(state.pid, runner).get(key, "<absent>")
        matched = actual == expected
        flag_matches += int(matched)
        flag_details.append(f"{service} pid={state.pid} {key}={actual} expected={expected}")
        if not matched:
            failures.append(f"running process flag mismatch: {service}:{key}")
    rows.append(
        (
            "Running-process flags",
            f"matched={flag_matches}/{len(args.expect_flag)}; " + "; ".join(flag_details),
            "PASS" if flag_matches == len(args.expect_flag) else "FAIL",
        )
    )

    v2_start = datetime.fromisoformat(current[V2_SERVICE].started_at_utc).astimezone(UTC)
    v2_files = _log_files(V2_SERVICE, runner, since=v2_start)
    v2_lines = _timestamped_lines_after(v2_files, v2_start)
    complete = [
        row
        for row in v2_lines
        if "[V2-BOOT-RESTORE]" in row[1]
        and "restoration_complete=1" in row[1]
        and "evaluated=" in row[1]
        and "rest_warmed=" in row[1]
    ]
    timeout_markers = [
        row
        for row in v2_lines
        if "[V2-BOOT-REST-WARMUP-TIMEOUT]" in row[1] and "outcome=warmup_gate_released" in row[1]
    ]
    warmup_ok = False
    completion_stamp: datetime | None = None
    if not complete:
        failures.append("no post-restart restoration_complete=1 line")
        warmup_text = f"complete markers=0/{len(v2_lines)} post-restart timestamped records"
    else:
        stamp, line = complete[-1]
        completion_stamp = stamp
        counts = _integer_fields(
            line,
            "evaluated",
            "confirmed",
            "rest_warmed",
            "timeout_released",
            "could_not_tell",
        )
        evaluated = counts["evaluated"]
        confirmed = counts["confirmed"]
        rest_warmed = counts["rest_warmed"]
        timeout_released = counts["timeout_released"]
        ready = rest_warmed + timeout_released
        warmup_pending = evaluated - ready
        warmup_ok = (
            evaluated > 0
            and confirmed == evaluated
            and counts["could_not_tell"] == 0
            and rest_warmed >= 0
            and timeout_released >= 0
            and warmup_pending == 0
        )
        timeout_text = "seeded fallback=not used"
        if timeout_released:
            prior_timeout_markers = [row for row in timeout_markers if row[0] <= stamp]
            if not prior_timeout_markers:
                warmup_ok = False
                timeout_text = "seeded fallback marker=missing"
            else:
                timeout_stamp, timeout_line = prior_timeout_markers[-1]
                timeout_counts = _integer_fields(
                    timeout_line,
                    "bound_seconds",
                    "evaluated",
                    "confirmed",
                    "released",
                )
                symbols = _fields(timeout_line).get("symbols", "")
                timeout_symbols = [
                    symbol for symbol in symbols.split(",") if symbol and symbol != "-"
                ]
                timeout_ok = (
                    timeout_counts["bound_seconds"] == BOOT_WARMUP_FALLBACK_BOUND_SECONDS
                    and timeout_counts["evaluated"] == evaluated
                    and timeout_counts["confirmed"] == evaluated
                    and timeout_counts["released"] == timeout_released
                    and len(timeout_symbols) == timeout_released
                )
                warmup_ok = warmup_ok and timeout_ok
                timeout_text = (
                    f"seeded fallback released={timeout_counts['released']}/{evaluated}; "
                    f"symbols={symbols or '<missing>'}; "
                    f"bound={timeout_counts['bound_seconds']}s; "
                    f"marker at {format_moment(timeout_stamp)}"
                )
        if not warmup_ok:
            failures.append("REST warmup completion population is inconsistent or unproven")
        warmup_text = (
            f"rest_warmed={rest_warmed}/evaluated={evaluated}; "
            f"timeout_released={timeout_released}/evaluated={evaluated}; "
            f"warmup_pending={warmup_pending}/{evaluated}; warmup_pending_symbols=-; "
            f"fresh_bar_age_bound={REST_WARMUP_FRESH_AGE_SECONDS}s; "
            f"{timeout_text}; "
            f"marker at {format_moment(stamp)}"
        )
    rows.append(("REST warmup", warmup_text, "PASS" if warmup_ok else "FAIL"))

    released = [
        row
        for row in v2_lines
        if "[V2-BOOT-HOLD] released" in row[1]
        and "restoration_complete=1" in row[1]
        and (completion_stamp is None or row[0] >= completion_stamp)
    ]

    if not released:
        failures.append("no literal post-restart BOOT-HOLD release with restoration_complete=1")
        hold_text = f"release markers=0/{len(v2_lines)} post-restart timestamped records"
    else:
        stamp, line = released[-1]
        hold_text = (
            f"release markers={len(released)}/{len(v2_lines)} post-restart timestamped records; "
            f"at {format_moment(stamp)}; literal={line}"
        )
    rows.append(("BOOT-HOLD released", hold_text, "PASS" if released else "FAIL"))

    bars = _bar_continuity(runner, v2_start)
    restart_inside_bar_session = _restart_inside_bar_session(v2_start)
    if restart_inside_bar_session:
        try:
            backfill = _rest_backfill_continuity(
                runner, v2_start, bars.stopped_at_utc, v2_lines, before
            )
        except EvidenceUnknown as exc:
            backfill = BackfillContinuity((), (), (), "COULD_NOT_TELL", str(exc))
    else:
        backfill = BackfillContinuity((), (), (), "N/A_OFF_SESSION", "restart outside bar session")
    if backfill.verdict == "MISSING_PRINTED_MINUTE":
        missing = sum(len(item.missing_print_minutes) for item in backfill.symbols)
        failures.append(
            f"REST backfill coverage not proven for {missing} independently printed minute(s)"
        )
    elif backfill.verdict == "COULD_NOT_TELL":
        failures.append(f"REST backfill continuity COULD_NOT_TELL: {backfill.reason or 'per-symbol evidence incomplete'}")
    bar_status = "FAIL" if backfill.verdict == "MISSING_PRINTED_MINUTE" else backfill.verdict
    per_symbol = "; ".join(
        f"{item.symbol} status={item.verdict} watched_at_stop={int(item.watched_at_stop)} "
        f"window={format_moment(item.start_utc)}.."
        f"{format_moment(item.first_post_utc) if item.first_post_utc else '?'} "
        f"filled={len(item.filled_minutes)}/"
        f"{len(item.filled_minutes) + len(item.missing_print_minutes) + len(item.quiet_minutes) + len(item.unverified_minutes)} "
        f"missing_printed={len(item.missing_print_minutes)} "
        f"minutes={','.join(format_moment(minute) for minute in item.missing_print_minutes) or '-'} "
        f"unverified_old={len(item.unverified_minutes)} "
        f"quiet={len(item.quiet_minutes)} reason={item.reason or '-'}"
        for item in backfill.symbols
    ) or backfill.reason or "-"
    gap_detail = "; ".join(_format_restart_gap(result) for result in bars.gap_results)
    if not gap_detail:
        gap_detail = "independent restart-gap classifications=0/0"
    rows.append(
        (
            "Bar continuity",
            f"watched_at_stop={len(backfill.watched_at_stop)} "
            f"symbols={','.join(backfill.watched_at_stop) or '-'}; "
            f"added_after_restart={len(backfill.added_after_restart)} "
            f"symbols={','.join(backfill.added_after_restart) or '-'}; "
            f"per_symbol={per_symbol}; persisted strategy_bar_history INFO: "
            f"session symbols={bars.symbols}; adjacent bar pairs={bars.pairs}; "
            f"gaps>90s={bars.gaps}/{bars.pairs}; "
            f"v2 stopped at {format_moment(bars.stopped_at_utc)}; "
            f"live-at-stop floor={LIVE_AT_STOP_BOUND_SECONDS}s "
            f"({format_moment(bars.live_floor_utc)}); "
            f"pairs bracketing restart={bars.brackets}/{bars.pairs} (series live at stop); "
            f"gaps spanning restart={bars.spanning}/{bars.gaps}; "
            f"bracketing pairs NOT live at stop (subscription gaps, excluded)={bars.excluded}; "
            f"pending next bar={len(bars.pending_symbols)} "
            f"symbols={','.join(bars.pending_symbols) or '-'}; {gap_detail}",
            bar_status,
        )
    )

    traceback_total = 0
    timestamped_total = 0
    traceback_detail: list[str] = []
    services_without_records: list[str] = []
    unexpected_silent_services: list[str] = []
    for service in sorted(restarted):
        start = datetime.fromisoformat(current[service].started_at_utc).astimezone(UTC)
        evidence = parse_log_files(_log_files(service, runner, since=start), since=start)
        traceback_total += len(evidence.traceback_times_utc)
        timestamped_total += evidence.timestamped_records
        if evidence.timestamped_records == 0:
            services_without_records.append(service)
            if service in expected_quiet_services:
                traceback_detail.append(f"{service}=N/A_EXPECTED_QUIET(0/0)")
            else:
                unexpected_silent_services.append(service)
                traceback_detail.append(f"{service}=UNMEASURED(0/0)")
        else:
            traceback_detail.append(
                f"{service}={len(evidence.traceback_times_utc)}/{evidence.timestamped_records}"
            )
        traceback_detail.extend(format_moment(stamp) for stamp in evidence.traceback_times_utc)
    if traceback_total:
        failures.append(f"{traceback_total} post-restart traceback(s) found")
    if unexpected_silent_services:
        failures.append(
            "no post-restart timestamped log records for unexpected-silent service(s): "
            + ",".join(unexpected_silent_services)
        )
    traceback_status = (
        "FAIL"
        if traceback_total or unexpected_silent_services
        else "PARTIAL_N/A"
        if services_without_records
        else "PASS"
    )
    rows.append(
        (
            "Tracebacks",
            f"headers={traceback_total}/{timestamped_total} post-restart timestamped records; "
            "nearest-preceding timestamp scope; " + "; ".join(traceback_detail),
            traceback_status,
        )
    )

    generated = datetime.now(UTC)
    output = [
        "# V2 Restart Evidence",
        "",
        f"Generated: {format_moment(generated)}",
        f"V2 process start: {format_moment(v2_start)}",
        f"Overall: {'PASS' if not failures else 'FAIL'} "
        f"({len(failures)} failed checks / {len(rows)} reported checks)",
        "",
        "| Check | Measured evidence | Result |",
        "| --- | --- | --- |",
    ]
    output.extend(
        f"| {name} | {evidence.replace('|', '/')} | {status} |" for name, evidence, status in rows
    )
    if failures:
        output.extend(["", "Failures:", *(f"- {failure}" for failure in failures)])
    rendered = "\n".join(output) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("snapshot", help="capture all fleet PIDs before the restart")
    capture.add_argument("--output", required=True, type=Path)

    after = sub.add_parser("report", help="collect and grade post-restart evidence")
    after.add_argument("--snapshot", required=True, type=Path)
    after.add_argument("--restarted", action="append", required=True, choices=DEFAULT_SERVICES)
    after.add_argument(
        "--expected-quiet-service", action="append", default=[], choices=DEFAULT_SERVICES
    )
    after.add_argument("--expect-flag", action="append", required=True)
    after.add_argument("--expected-alembic-head", required=True)
    after.add_argument("--schema-column", action="append", default=[])
    after.add_argument("--schema-constraint", action="append", default=[])
    after.add_argument("--no-schema-change", action="store_true")
    after.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            return 0 if snapshot(args.output) else 1
        return report(args)
    except EvidenceUnknown as exc:
        print(f"UNMEASURED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
