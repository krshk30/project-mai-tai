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
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UNIT_PREFIX = "project-mai-tai-"
V2_SERVICE = "schwab-1m-v2"
V2_STRATEGY_CODE = "schwab_1m_v2"
REST_WARMUP_FRESH_AGE_SECONDS = 300
BOOT_WARMUP_FALLBACK_BOUND_SECONDS = 369
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
LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")
FIELD = re.compile(r"\b([a-z_]+)=([^ ]+)")


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
                preceding = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(
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


def _log_files(service: str, runner: Runner) -> list[tuple[str, list[str]]]:
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

    result: list[tuple[str, list[str]]] = []
    for _, path in sorted(found):
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
            stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
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


def _bar_continuity(runner: Runner, restart: datetime) -> tuple[int, int, int, int, int]:
    restart_utc = restart.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00")
    session_day = restart.astimezone(ET).date().isoformat()
    raw = _psql(
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
        ") SELECT"
        " (SELECT count(DISTINCT symbol) FROM ordered),"
        " count(*),"
        " count(*) FILTER (WHERE delta > 90),"
        f" count(*) FILTER (WHERE prior < TIMESTAMPTZ '{restart_utc}'"
        f" AND bar_time > TIMESTAMPTZ '{restart_utc}'),"
        f" count(*) FILTER (WHERE delta > 90 AND prior < TIMESTAMPTZ '{restart_utc}'"
        f" AND bar_time > TIMESTAMPTZ '{restart_utc}') FROM measured;",
        runner,
    )
    fields = _single_row(raw, 5, "bar-continuity query")
    try:
        return tuple(int(value) for value in fields)  # type: ignore[return-value]
    except ValueError as exc:
        raise EvidenceUnknown("bar-continuity query returned a non-numeric count") from exc


def snapshot(path: Path, runner: Runner = run_checked) -> bool:
    captured = datetime.now(UTC)
    accounts_found, managed_open, positions_nonzero = _flat_counts(runner)
    migration_head, _, _, _ = _migration_evidence(runner, columns=[], constraints=[])
    flat = accounts_found == len(LIVE_ACCOUNTS) and managed_open == 0 and positions_nonzero == 0
    payload = {
        "schema_version": 2,
        "captured_at_utc": captured.isoformat(),
        "captured_at_et": captured.astimezone(ET).isoformat(),
        "alembic_version": migration_head,
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
    if payload.get("schema_version") != 2 or not isinstance(payload.get("services"), dict):
        raise EvidenceUnknown(f"snapshot {path} has an unsupported shape")
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
    if V2_SERVICE not in restarted:
        raise EvidenceUnknown(
            f"C6 is the v2 restart artifact; --restarted must include {V2_SERVICE}"
        )
    unknown_services = restarted - set(DEFAULT_SERVICES)
    if unknown_services:
        raise EvidenceUnknown(f"unknown restarted service(s): {','.join(sorted(unknown_services))}")
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
    v2_files = _log_files(V2_SERVICE, runner)
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

    symbols, pairs, gaps, brackets, spanning = _bar_continuity(runner, v2_start)
    local_start = v2_start.astimezone(ET)
    restart_inside_bar_session = 4 <= local_start.hour < 20
    bar_ok = spanning == 0 and (not restart_inside_bar_session or brackets > 0)
    if spanning:
        failures.append(f"{spanning} bar gap(s) span the v2 restart")
    elif restart_inside_bar_session and brackets == 0:
        failures.append("no adjacent live-bar pair brackets the in-session v2 restart")
    bar_status = "PASS" if bar_ok else "FAIL"
    if not restart_inside_bar_session and brackets == 0:
        bar_status = "N/A_OFF_SESSION"
    rows.append(
        (
            "Bar continuity",
            f"session symbols={symbols}; adjacent bar pairs={pairs}; gaps>90s={gaps}/{pairs}; "
            f"pairs bracketing restart={brackets}/{pairs}; gaps spanning restart={spanning}/{gaps}",
            bar_status,
        )
    )

    traceback_total = 0
    timestamped_total = 0
    traceback_detail: list[str] = []
    for service in sorted(restarted):
        start = datetime.fromisoformat(current[service].started_at_utc).astimezone(UTC)
        evidence = parse_log_files(_log_files(service, runner), since=start)
        traceback_total += len(evidence.traceback_times_utc)
        timestamped_total += evidence.timestamped_records
        traceback_detail.append(
            f"{service}={len(evidence.traceback_times_utc)}/{evidence.timestamped_records}"
        )
        traceback_detail.extend(format_moment(stamp) for stamp in evidence.traceback_times_utc)
    if traceback_total:
        failures.append(f"{traceback_total} post-restart traceback(s) found")
    rows.append(
        (
            "Tracebacks",
            f"headers={traceback_total}/{timestamped_total} post-restart timestamped records; "
            "nearest-preceding timestamp scope; " + "; ".join(traceback_detail),
            "PASS" if traceback_total == 0 else "FAIL",
        )
    )

    generated = datetime.now(UTC)
    output = [
        "# V2 Restart Evidence",
        "",
        f"Generated: {format_moment(generated)}",
        f"V2 process start: {format_moment(v2_start)}",
        f"Overall: {'PASS' if not failures else 'FAIL'} ({len(failures)} failed checks / {len(rows)} measured checks)",
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
