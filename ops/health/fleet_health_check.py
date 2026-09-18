#!/usr/bin/env python3
"""Fleet FUNCTION-health checks (F3) — validate FUNCTION, not process.

Why this exists: every silent failure we've had was a component reporting *healthy*
while its function was dead — the OMS up-but-zombied, #388 deployed-but-reconcile-never-
fired, a position record-said-held while the broker was flat. The self-report is the thing
that lies. F3 checks "is it doing its job" against GROUND TRUTH (DB / fills / independent
capture), never the component's own heartbeat/snapshot.

Independence: stdlib + `psql`/`redis-cli` subprocess only — NO app imports, so a frozen
service (or a hung DB) can't take this check down the same way. Runs from an independent
cron (see fleet_health_cron.sh), like the pre-open readiness check and the OMS-liveness
watchdog — never as a long-running service.

Registry: `CHECKS` lists the enabled checks; each returns (level, name, detail). main()
prints one `VERDICT: <LEVEL> <name> <detail>` line per check + an aggregate, and exits with
the worst level (0=GREEN, 1=AMBER, 2=RED) so the cron routes to ntfy.

DESIGN CONSTRAINT (load-bearing): alert only on a signal that is RED *only* when genuinely
broken. A check that false-alarms on normal quiet gets ignored, which defeats the purpose.
So "strategy bars are stale" is RED only when the upstream feed is SIMULTANEOUSLY LIVE
(trades flowing) — i.e. it cannot be a quiet market or a feed outage; it's a frozen loop.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable, NamedTuple
from zoneinfo import ZoneInfo

# --- ground-truth access (independent: subprocess, no app import) ------------- #

_DSN_CACHE: list[str | None] = []
_EASTERN_TZ = ZoneInfo("America/New_York")
_D6_STATUS_PATH = Path("/home/trader/fanout_outcome_acceptance/STATUS.txt")
_SESSION_RE = re.compile(r"\bsession=(\d{4}-\d{2}-\d{2})\b")
_RESTART_STATE_PATH = Path(
    os.environ.get(
        "FLEET_HEALTH_RESTART_STATE",
        "/home/trader/fleet_health/restart_counts.json",
    )
)
_MAINTENANCE_PATH = Path(
    os.environ.get(
        "FLEET_HEALTH_MAINTENANCE",
        "/home/trader/fleet_health/maintenance.txt",
    )
)
_SYSTEMCTL_BIN = os.environ.get("FLEET_HEALTH_SYSTEMCTL", "systemctl")
_SOCKET_EVIDENCE_STATE_PATH = Path(
    os.environ.get(
        "FLEET_HEALTH_SOCKET_EVIDENCE_STATE",
        "/home/trader/fleet_health/socket_evidence_offsets.json",
    )
)
_MARKET_DATA_LOG_PATH = Path(
    os.environ.get(
        "FLEET_HEALTH_MARKET_DATA_LOG",
        "/var/log/project-mai-tai/market-data.log",
    )
)
_MOMENTUM_LOG_PATH = Path(
    os.environ.get(
        "FLEET_HEALTH_MOMENTUM_LOG",
        "/var/log/project-mai-tai/momentum-paper.log",
    )
)
_MARKET_DATA_POLICY_NEEDLE = b"1008"
_MOMENTUM_POLICY_COOLOFF_NEEDLE = (
    b"[MOMENTUM-PAPER-FEED-POLICY] decision=cooloff reason=feed_policy_violation"
)
_MOMENTUM_POLICY_RECOVERED_NEEDLE = (
    b"[MOMENTUM-PAPER-FEED-POLICY] decision=recovered"
)

# These units are expected to run continuously. Deliberately inactive units such as trade-coach
# and tv-alerts stay out of the inventory so an intentional stop cannot become a page.
RUNTIME_SERVICES = (
    "project-mai-tai-control.service",
    "project-mai-tai-market-capture.service",
    "project-mai-tai-market-data.service",
    "project-mai-tai-momentum-paper.service",
    "project-mai-tai-oms.service",
    "project-mai-tai-orb.service",
    "project-mai-tai-reconciler.service",
    "project-mai-tai-schwab-1m-v2.service",
    "project-mai-tai-strategy.service",
)
_RESTART_STORM_LIMIT = 3
_RESTART_SAMPLE_MAX_AGE_S = 360
# This check deliberately imports no application code. Keep these full closures in step with the
# equally explicit fleet_health_cron.sh list; both are versioned and carry the same roll-forward
# obligation rather than inheriting the health of the application they monitor.
_FULL_CLOSURES = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),
    }
)


def _dsn() -> str | None:
    """The DB DSN, read from a running project service's /proc environ (the root-only env
    file is injected there by systemd; trader owns the service processes). No app import,
    no secret written to disk. None if no service is running / not found."""
    if _DSN_CACHE:
        return _DSN_CACHE[0]
    dsn = None
    try:
        pids = subprocess.run(
            ["pgrep", "-f", "mai-tai-"], capture_output=True, text=True, timeout=5
        ).stdout.split()
    except Exception:
        pids = []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                for kv in fh.read().split(b"\0"):
                    if kv.startswith(b"MAI_TAI_DATABASE_URL="):
                        dsn = (
                            kv.split(b"=", 1)[1]
                            .decode()
                            .replace("postgresql+psycopg://", "postgresql://")
                        )
                        break
        except OSError:
            continue
        if dsn:
            break
    _DSN_CACHE.append(dsn)
    return dsn


def _scalar_int(sql: str) -> int | None:
    """Run a single-value SQL and return it as int, or None (no DSN / error / NULL)."""
    dsn = _dsn()
    if not dsn:
        return None
    try:
        out = subprocess.run(["psql", dsn, "-tAc", sql], capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    if not val:
        return None
    try:
        return int(float(val))
    except ValueError:
        return None


class ServiceRuntime(NamedTuple):
    n_restarts: int
    active_state: str
    sub_state: str


class MaintenanceWindow(NamedTuple):
    until: datetime
    reason: str


class LogCursor(NamedTuple):
    device: int
    inode: int
    offset: int


def _read_socket_evidence_state(
    path: Path,
) -> tuple[dict[str, LogCursor], bool, str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, False, None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        return {}, False, type(exc).__name__
    if not isinstance(raw, dict):
        return {}, False, "invalid_root"
    raw_logs = raw.get("logs", raw)
    if not isinstance(raw_logs, dict):
        return {}, False, "invalid_logs"
    parsed: dict[str, LogCursor] = {}
    for key, value in raw_logs.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        try:
            cursor = LogCursor(
                device=int(value["device"]),
                inode=int(value["inode"]),
                offset=int(value["offset"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if cursor.offset >= 0:
            parsed[key] = cursor
    return parsed, bool(raw.get("momentum_policy_active", False)), None


def _write_socket_evidence_state(
    path: Path,
    state: dict[str, LogCursor],
    *,
    momentum_policy_active: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "logs": {
                        key: {
                            "device": cursor.device,
                            "inode": cursor.inode,
                            "offset": cursor.offset,
                        }
                        for key, cursor in sorted(state.items())
                    },
                    "momentum_policy_active": momentum_policy_active,
                },
                stream,
                sort_keys=True,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_appended_socket_evidence(
    path: Path,
    prior: LogCursor | None,
) -> tuple[bytes, LogCursor]:
    stat = path.stat()
    start = 0
    if (
        prior is not None
        and prior.device == stat.st_dev
        and prior.inode == stat.st_ino
        and 0 <= prior.offset <= stat.st_size
    ):
        start = prior.offset
    with path.open("rb") as stream:
        stream.seek(start)
        payload = stream.read()
    return payload, LogCursor(device=stat.st_dev, inode=stat.st_ino, offset=stat.st_size)


def check_massive_socket_policy_violations(
    *,
    state_path: Path | None = None,
    market_data_log: Path | None = None,
    momentum_log: Path | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Grade new socket evidence, never a component heartbeat that can stay healthy."""

    path = _SOCKET_EVIDENCE_STATE_PATH if state_path is None else state_path
    logs = (
        (
            "market-data",
            _MARKET_DATA_LOG_PATH if market_data_log is None else market_data_log,
            "massive-1008",
        ),
        (
            "momentum-paper",
            _MOMENTUM_LOG_PATH if momentum_log is None else momentum_log,
            "feed-policy-violation",
        ),
    )
    prior, momentum_policy_active, state_error = _read_socket_evidence_state(path)
    if state_error is not None:
        return (
            (
                "RED",
                "service-runtime:socket-evidence:state-unreadable",
                f"socket evidence cursor state unreadable error={state_error} path={path}",
            ),
        )
    updated = dict(prior)
    rows: list[tuple[str, str, str]] = []
    for slug, log_path, condition in logs:
        name = f"service-runtime:{slug}:{condition}"
        try:
            payload, cursor = _read_appended_socket_evidence(
                log_path,
                prior.get(str(log_path)),
            )
        except (OSError, ValueError) as exc:
            rows.append(
                (
                    "RED",
                    name,
                    f"socket evidence unreadable path={log_path} error={type(exc).__name__}",
                )
            )
            continue
        updated[str(log_path)] = cursor
        if slug == "market-data":
            count = payload.count(_MARKET_DATA_POLICY_NEEDLE)
            active = count > 0
        else:
            cooloffs = payload.count(_MOMENTUM_POLICY_COOLOFF_NEEDLE)
            recoveries = payload.count(_MOMENTUM_POLICY_RECOVERED_NEEDLE)
            if cooloffs or recoveries:
                last_cooloff = payload.rfind(_MOMENTUM_POLICY_COOLOFF_NEEDLE)
                last_recovery = payload.rfind(_MOMENTUM_POLICY_RECOVERED_NEEDLE)
                momentum_policy_active = last_cooloff > last_recovery
            count = cooloffs
            active = momentum_policy_active
        if active:
            rows.append(
                (
                    "RED",
                    name,
                    f"socket evidence path={log_path} new_matches={count} "
                    f"bytes_scanned={len(payload)} end_offset={cursor.offset} active=1",
                )
            )
        else:
            rows.append(
                (
                    "GREEN",
                    name,
                    f"socket evidence path={log_path} new_matches={count} "
                    f"bytes_scanned={len(payload)} end_offset={cursor.offset} active=0",
                )
            )
    try:
        _write_socket_evidence_state(
            path,
            updated,
            momentum_policy_active=momentum_policy_active,
        )
    except OSError as exc:
        rows.append(
            (
                "RED",
                "service-runtime:socket-evidence:state-write-failed",
                f"socket evidence cursor write failed: {type(exc).__name__}",
            )
        )
    return tuple(rows)


def _service_slug(service: str) -> str:
    return service.removeprefix("project-mai-tai-").removesuffix(".service")


def _read_maintenance_windows(
    path: Path,
) -> tuple[dict[str, MaintenanceWindow], tuple[str, ...]]:
    """Read expiring service maintenance declarations without accepting an open-ended stop."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}, ()
    except (OSError, UnicodeError) as exc:
        return {}, (f"maintenance file unreadable: {type(exc).__name__}",)

    windows: dict[str, MaintenanceWindow] = {}
    errors: list[str] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(maxsplit=2)
        if len(fields) != 3 or not fields[2].strip():
            errors.append(f"line {number}: expected <unit> <until-ISO8601-UTC> <reason>")
            continue
        service, raw_until, reason = fields
        if service not in RUNTIME_SERVICES:
            errors.append(f"line {number}: unknown unit {service}")
            continue
        try:
            until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"line {number}: invalid expiry {raw_until}")
            continue
        if until.tzinfo is None or until.utcoffset() != timedelta(0):
            errors.append(f"line {number}: expiry must be UTC {raw_until}")
            continue
        if service in windows:
            errors.append(f"line {number}: duplicate unit {service}")
            continue
        windows[service] = MaintenanceWindow(until.astimezone(UTC), reason.strip())
    return windows, tuple(errors)


def classify_service_runtime_rows(
    current: dict[str, ServiceRuntime],
    previous: dict[str, int] | None,
    *,
    elapsed_s: float | None,
    now: datetime,
    maintenance: dict[str, MaintenanceWindow],
    maintenance_errors: tuple[str, ...] = (),
) -> tuple[tuple[str, str, str], ...]:
    """Return independent service/condition verdicts so one RED cannot mask another."""

    rows: list[tuple[str, str, str]] = []
    comparison_valid = (
        previous is not None
        and elapsed_s is not None
        and 0 < elapsed_s <= _RESTART_SAMPLE_MAX_AGE_S
    )
    for service in RUNTIME_SERVICES:
        slug = _service_slug(service)
        window = maintenance.get(service)
        if window is not None and window.until > now:
            detail = f"unit={service} until={window.until.isoformat()} reason={window.reason}"
            rows.extend(
                (
                    ("MAINTENANCE", f"service-runtime:{slug}:inactive", detail),
                    ("MAINTENANCE", f"service-runtime:{slug}:restart-storm", detail),
                )
            )
            continue

        if window is not None:
            rows.append(
                (
                    "RED",
                    f"service-runtime:{slug}:maintenance-expired",
                    f"unit={service} expired={window.until.isoformat()} reason={window.reason}",
                )
            )

        runtime = current.get(service)
        if runtime is None:
            rows.extend(
                (
                    (
                        "RED",
                        f"service-runtime:{slug}:inactive",
                        f"unit={service} state=UNMEASURED missing from systemctl output",
                    ),
                    (
                        "GREEN",
                        f"service-runtime:{slug}:restart-storm",
                        f"unit={service} restart delta UNMEASURED while state is missing",
                    ),
                )
            )
            continue

        active = runtime.active_state == "active" and runtime.sub_state == "running"
        rows.append(
            (
                "GREEN" if active else "RED",
                f"service-runtime:{slug}:inactive",
                f"unit={service} state={runtime.active_state}/{runtime.sub_state}",
            )
        )
        before = previous.get(service) if previous is not None else None
        delta = runtime.n_restarts - before if before is not None else None
        storm = comparison_valid and delta is not None and delta > _RESTART_STORM_LIMIT
        if comparison_valid and delta is not None:
            restart_detail = (
                f"unit={service} NRestarts={runtime.n_restarts} delta={delta} "
                f"elapsed_s={elapsed_s:.0f} limit={_RESTART_STORM_LIMIT}"
            )
        else:
            restart_detail = (
                f"unit={service} NRestarts={runtime.n_restarts} baseline_recorded=1 "
                "delta=UNMEASURED"
            )
        rows.append(
            (
                "RED" if storm else "GREEN",
                f"service-runtime:{slug}:restart-storm",
                restart_detail,
            )
        )

    if maintenance_errors:
        rows.append(
            (
                "RED",
                "service-runtime:maintenance-file:invalid",
                "; ".join(maintenance_errors),
            )
        )
    return tuple(rows)


def classify_service_restarts(
    current: dict[str, ServiceRuntime],
    previous: dict[str, int] | None,
    *,
    elapsed_s: float | None,
) -> tuple[str, str]:
    """Grade expected-running units and restart deltas from one bounded sample interval."""

    missing = [service for service in RUNTIME_SERVICES if service not in current]
    if missing:
        return ("RED", f"expected service state missing: {','.join(missing)}")

    inactive = [
        service
        for service in RUNTIME_SERVICES
        if current[service].active_state != "active" or current[service].sub_state != "running"
    ]
    storms: list[str] = []
    comparison_valid = (
        previous is not None
        and elapsed_s is not None
        and 0 < elapsed_s <= _RESTART_SAMPLE_MAX_AGE_S
    )
    if comparison_valid:
        for service in RUNTIME_SERVICES:
            before = previous.get(service)
            if before is None:
                continue
            delta = current[service].n_restarts - before
            if delta > _RESTART_STORM_LIMIT:
                storms.append(f"{service} +{delta}")

    if inactive or storms:
        parts = []
        if inactive:
            parts.append(
                "not active/running="
                + ",".join(
                    f"{service}({current[service].active_state}/{current[service].sub_state})"
                    for service in inactive
                )
            )
        if storms:
            parts.append(
                f"NRestarts delta>{_RESTART_STORM_LIMIT} within {elapsed_s:.0f}s="
                + ",".join(storms)
            )
        return ("RED", "; ".join(parts))

    counts = ",".join(
        f"{service.removeprefix('project-mai-tai-').removesuffix('.service')}="
        f"{current[service].n_restarts}"
        for service in RUNTIME_SERVICES
    )
    if not comparison_valid:
        return ("GREEN", f"all {len(RUNTIME_SERVICES)} services active; baseline recorded {counts}")
    return (
        "GREEN",
        f"all {len(RUNTIME_SERVICES)} services active; restart deltas within limit over "
        f"{elapsed_s:.0f}s; {counts}",
    )


def _read_restart_state(path: Path) -> tuple[float, dict[str, int]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sampled_at = float(payload["sampled_at"])
        services = payload["services"]
        if not isinstance(services, dict):
            return None
        return sampled_at, {str(name): int(value) for name, value in services.items()}
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_restart_state(path: Path, *, sampled_at: float, services: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump({"sampled_at": sampled_at, "services": services}, handle, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _read_service_runtimes(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, ServiceRuntime] | None:
    try:
        result = runner(
            [
                _SYSTEMCTL_BIN,
                "show",
                *RUNTIME_SERVICES,
                "-p",
                "Id",
                "-p",
                "NRestarts",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None

    parsed: dict[str, ServiceRuntime] = {}
    fields: dict[str, str] = {}

    def flush() -> None:
        if not fields:
            return
        try:
            parsed[fields["Id"]] = ServiceRuntime(
                n_restarts=int(fields["NRestarts"]),
                active_state=fields["ActiveState"],
                sub_state=fields["SubState"],
            )
        except (KeyError, ValueError):
            return

    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            fields = {}
            continue
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    flush()
    return parsed


def check_service_restart_storms(
    *,
    now_epoch: float | None = None,
    state_path: Path | None = None,
    maintenance_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[tuple[str, str, str], ...]:
    current = _read_service_runtimes(runner)
    if current is None:
        return (
            ("RED", "service-runtime:inventory:unreadable", "systemctl service state unreadable"),
        )

    sampled_at = time.time() if now_epoch is None else now_epoch
    path = _RESTART_STATE_PATH if state_path is None else state_path
    maintenance_file = _MAINTENANCE_PATH if maintenance_path is None else maintenance_path
    maintenance, maintenance_errors = _read_maintenance_windows(maintenance_file)
    prior = _read_restart_state(path)
    previous_counts = prior[1] if prior is not None else None
    elapsed_s = sampled_at - prior[0] if prior is not None else None
    rows = list(
        classify_service_runtime_rows(
            current,
            previous_counts,
            elapsed_s=elapsed_s,
            now=datetime.fromtimestamp(sampled_at, tz=UTC),
            maintenance=maintenance,
            maintenance_errors=maintenance_errors,
        )
    )
    try:
        _write_restart_state(
            path,
            sampled_at=sampled_at,
            services={service: runtime.n_restarts for service, runtime in current.items()},
        )
    except OSError as exc:
        rows.append(
            (
                "RED",
                "service-runtime:restart-state:write-failed",
                f"restart state write failed: {type(exc).__name__}",
            )
        )
    return tuple(rows)


# --- pure decision logic (unit-tested; no I/O) -------------------------------- #


def classify_bar_freshness(
    bar_age_s: int | None,
    feed_age_s: int | None,
    *,
    stale_amber_s: int = 120,
    stale_red_s: int = 240,
    feed_fresh_max_s: int = 120,
) -> tuple[str, str]:
    """Strategy-engine bar-freshness verdict — the frozen-loop detector.

    polygon_30s persists a 30s bar per interval from the live Polygon feed. If the
    upstream feed is LIVE (market_capture_trades fresh) but bars have stopped advancing,
    the strategy loop is frozen (the exact 'reports healthy while dead' class). If the
    feed is quiet/stale, bars legitimately don't advance — NOT a strategy fault → GREEN
    (this is the no-false-alarm guard: a quiet market never reds)."""
    if bar_age_s is None:
        return ("AMBER", "no polygon_30s bars in strategy_bar_history (cannot assess)")
    if feed_age_s is None or feed_age_s > feed_fresh_max_s:
        return (
            "GREEN",
            f"bars {bar_age_s}s old but upstream feed quiet/stale "
            f"(feed_age={feed_age_s}s) — staleness not attributable to the strategy",
        )
    # Feed is LIVE → any bar staleness IS attributable to the strategy loop.
    if bar_age_s < stale_amber_s:
        return ("GREEN", f"strategy bars fresh ({bar_age_s}s) with live feed")
    if bar_age_s < stale_red_s:
        return (
            "AMBER",
            f"strategy bars slowing ({bar_age_s}s) while feed live (feed_age={feed_age_s}s)",
        )
    return (
        "RED",
        f"strategy bars STALE {bar_age_s}s while upstream feed LIVE "
        f"(feed_age={feed_age_s}s) — polygon_30s loop likely FROZEN",
    )


# --- checks (I/O + decision) -------------------------------------------------- #


def classify_order_lifecycle(
    stuck_count: int | None,
    oldest_stuck_min: float | None,
) -> tuple[str, str]:
    """OMS order-lifecycle verdict — the alive-but-not-executing detector.

    The OMS creates a trade_intents row when it CONSUMES an intent, then resolves it to a
    terminal status (filled/rejected/cancelled/...) or an order within sub-seconds; a healthy
    OMS also terminalizes orphaned intents each sync cycle. So a trade_intents row that is
    NON-terminal AND has NO broker_order AND has aged well past that (>threshold) means the
    OMS consumed the intent but never placed or resolved it — it is beating but not executing
    (the 07-01 class the liveness watchdog can't see: dead-OMS = no heartbeat = watchdog's job;
    this is alive-but-stuck). NO recent stuck intents -> GREEN (a quiet market simply produces
    no intents; that must never red — the key no-false-alarm guard)."""
    if stuck_count is None:
        return ("AMBER", "could not read trade_intents (cannot assess)")
    if stuck_count <= 0:
        return ("GREEN", "no intents stuck pre-order (OMS executing or idle)")
    age = f"{oldest_stuck_min:.0f}m" if oldest_stuck_min is not None else "?"
    return (
        "RED",
        f"{stuck_count} intent(s) CONSUMED but stuck non-terminal with NO order "
        f"(oldest {age}) — OMS alive-but-not-executing / terminalize not running",
    )


def _last_completed_session_day(today: date) -> date:
    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in _FULL_CLOSURES:
        candidate -= timedelta(days=1)
    return candidate


def classify_d6_status(contents: str | None, *, expected_session: date) -> tuple[str, str]:
    """The D6 cron cannot observe its own death; this independent monitor watches its output."""
    if not contents:
        return ("RED", f"D6 STATUS missing; expected session={expected_session.isoformat()}")
    match = _SESSION_RE.search(contents)
    if match is None:
        return (
            "RED",
            f"D6 STATUS has no readable session; expected={expected_session.isoformat()}",
        )
    try:
        observed = date.fromisoformat(match.group(1))
    except ValueError:
        return (
            "RED",
            f"D6 STATUS has malformed session={match.group(1)!r}; "
            f"expected={expected_session.isoformat()}",
        )
    if observed < expected_session:
        return (
            "RED",
            f"D6 STATUS stale session={observed.isoformat()} expected={expected_session.isoformat()}",
        )
    if observed > expected_session:
        return (
            "RED",
            f"D6 STATUS future session={observed.isoformat()} expected={expected_session.isoformat()}",
        )
    if "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in contents:
        return ("RED", f"D6 session={observed.isoformat()} completed without SUCCESS")
    return ("GREEN", f"D6 SUCCESS current for session={observed.isoformat()}")


def check_d6_status_freshness() -> tuple[str, str, str]:
    # D6 runs at ~00:17 ET; the independent fleet-health cron first runs at 09:35 ET. Therefore a
    # dead scheduler is independently paged about 9h18m later, not in real time. The scheduler's
    # own fail-closed STATUS is the evidence during that interval.
    expected = _last_completed_session_day(datetime.now(_EASTERN_TZ).date())
    try:
        contents = _D6_STATUS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        contents = None
    except PermissionError as exc:
        return (
            "RED",
            "d6-outcome-acceptance",
            f"D6 STATUS unreadable permission error={type(exc).__name__}; "
            f"expected={expected.isoformat()}",
        )
    except OSError as exc:
        return (
            "RED",
            "d6-outcome-acceptance",
            f"D6 STATUS unreadable I/O error={type(exc).__name__}; expected={expected.isoformat()}",
        )
    except UnicodeError as exc:
        return (
            "RED",
            "d6-outcome-acceptance",
            f"D6 STATUS unreadable encoding error={type(exc).__name__}; "
            f"expected={expected.isoformat()}",
        )
    level, detail = classify_d6_status(contents, expected_session=expected)
    return (level, "d6-outcome-acceptance", detail)


# ⛔⭐ REAL-MONEY SCOPE — the allowlist that keeps a SIM trade out of a real-money pager.
#
# 2026-08-03: unscoped checks let `polygon_30s` (PAPER/sim) drive pages. It reached the entries
# counter, the sawtooth check and #628 before this sweep. A pager that fires on something you will
# never act on trains you to ignore it — and this pager guards NAKED POSITIONS.
#
# ⛔ `name LIKE 'live:%'` is NOT a safe proxy: `live:polygon_30s` and `live:webull_30s` both exist
# as rows. Verified 2026-08-03 — only three accounts had any order in 30 days: live:schwab_1m_v2
# (1200), live:orb (584), paper:polygon_30s (3868). The first two are real money (v2 plus its
# Webull fan-out leg); the other `live:` rows are dormant legacy names.
REAL_MONEY_ACCOUNTS: tuple[str, ...] = ("live:schwab_1m_v2", "live:orb")
_REAL_MONEY_SQL_LIST = ", ".join(f"'{a}'" for a in REAL_MONEY_ACCOUNTS)


def _with_paper_note(detail: str, paper_count: int | None, noun: str) -> str:
    """Append a paper/sim count to a verdict WITHOUT letting it change the level.

    Visible so a real sim defect is not hidden (polygon_30s really does reject every STOP sell
    with `missing reference_price`), but it can never page. Same shape as `sawtooth_paper` in the
    P0a watch."""
    if not paper_count:
        return detail
    plural = "" if paper_count == 1 else "s"
    return f"{detail} [paper/sim: {paper_count} {noun}{plural} — not paged]"


def check_oms_order_lifecycle() -> tuple[str, str, str]:
    """Check #2: the OMS is actually EXECUTING (intent -> order/terminal), not just beating.
    Ground truth = trade_intents (what the OMS consumed) LEFT JOIN broker_orders (what it
    placed). Only reds when intents exist AND are stuck — never on a quiet market."""
    # Stuck = non-terminal status, no broker_order, aged past a generous 10-min bound
    # (normal resolution is sub-second; a resting LIMIT order has an order row so it is
    # excluded; a rejected intent is terminal so it is excluded). 6h upper bound skips
    # ancient rows from a prior day.
    terminal = "('filled','rejected','cancelled','expired','abandoned')"
    where = (
        f"ti.status NOT IN {terminal} "
        "AND bo.id IS NULL "
        "AND ti.created_at < now() - interval '10 min' "
        "AND ti.created_at > now() - interval '6 hours'"
    )
    # REAL MONEY ONLY drives the verdict; paper is counted and shown, never paged.
    joined = (
        "FROM trade_intents ti "
        "LEFT JOIN broker_orders bo ON bo.intent_id = ti.id "
        "JOIN broker_accounts ba ON ba.id = ti.broker_account_id"
    )
    real = f"ba.name IN ({_REAL_MONEY_SQL_LIST})"
    stuck = _scalar_int(f"SELECT count(*) {joined} WHERE {where} AND {real}")
    oldest = _scalar_int(
        "SELECT round(extract(epoch FROM (now()-min(ti.created_at)))/60)::int "
        f"{joined} WHERE {where} AND {real}"
    )
    paper_stuck = _scalar_int(f"SELECT count(*) {joined} WHERE {where} AND NOT ({real})")
    level, detail = classify_order_lifecycle(stuck, oldest)
    return (
        level,
        "oms-order-lifecycle",
        _with_paper_note(detail, paper_stuck, "stuck paper intent"),
    )


def classify_stops_armed(
    unprotected_count: int | None,
    owned_open_count: int | None,
) -> tuple[str, str]:
    """Stops-armed verdict — every OMS-OWNED open position must have an armed stop.

    OMS-owned = a per-strategy `virtual_positions` row (the OMS's ledger of what IT placed).
    A manual holding has NO such row → it is never counted → NEVER trips 'unprotected' (the
    scoping invariant: it's not the OMS's to protect). Protection = an `oms_armed_stops` row
    (ORB) OR an open `oms_managed_positions` row (v2's exit ladder). This is the ongoing
    observability that a stop is always armed on what the OMS holds — the check that would
    have caught a naked position before F2 fixed the restart gap.

    No-false-alarm guard: 0 unprotected → GREEN whether the fleet is flat (nothing to
    protect) or every owned position is armed. Flat must be GREEN, never RED."""
    if unprotected_count is None:
        return ("AMBER", "could not read positions/stops (cannot assess)")
    if unprotected_count <= 0:
        if not owned_open_count:
            return ("GREEN", "no OMS-owned open positions (flat — nothing to protect)")
        return ("GREEN", f"all {owned_open_count} OMS-owned open position(s) have an armed stop")
    return (
        "RED",
        f"{unprotected_count} OMS-owned open position(s) have NO armed stop — NAKED "
        "(unprotected; a manual holding can't trip this — OMS-owned only)",
    )


def check_stops_armed() -> tuple[str, str, str]:
    """Check #3: every OMS-owned open position is protected by an armed stop. Ground truth =
    virtual_positions (OMS ownership) LEFT JOIN oms_armed_stops (ORB) + oms_managed_positions
    (v2 ladder). OMS-owned ONLY — manual positions have no virtual_positions row (invariant)."""
    # 2-min settle guard on opened_at: skip a just-opened position (its arm is written in the
    # same fill-processing commit, but this margin guarantees no false RED on the open path).
    joins = (
        "FROM virtual_positions vp "
        "JOIN strategies s ON s.id = vp.strategy_id "
        "JOIN broker_accounts ba ON ba.id = vp.broker_account_id "
        "LEFT JOIN oms_armed_stops a "
        "ON a.strategy_code = s.code AND a.broker_account_name = ba.name AND a.symbol = vp.symbol "
        "LEFT JOIN oms_managed_positions m "
        "ON m.broker_account_name = ba.name AND m.symbol = vp.symbol AND m.status = 'open' "
        "WHERE vp.quantity <> 0 AND vp.opened_at < now() - interval '2 min'"
    )
    # ⛔ REAL MONEY ONLY. `paper:polygon_30s` NEVER gets an `oms_managed_positions` row (verified
    # 2026-08-03: that table holds live:orb / live:schwab_1m_v2 / paper:schwab_1m_v2 and nothing
    # else), so ANY polygon_30s position held past the 2-min settle guard counted as "NAKED" and
    # RED-paged — for a simulated position that cannot lose a cent.
    real = f"ba.name IN ({_REAL_MONEY_SQL_LIST})"
    unprotected = _scalar_int(
        f"SELECT count(*) {joins} AND {real} AND a.id IS NULL AND m.id IS NULL"
    )
    owned_open = _scalar_int(f"SELECT count(*) {joins} AND {real}")
    paper_unprotected = _scalar_int(
        f"SELECT count(*) {joins} AND NOT ({real}) AND a.id IS NULL AND m.id IS NULL"
    )
    level, detail = classify_stops_armed(unprotected, owned_open)
    return (
        level,
        "stops-armed",
        _with_paper_note(detail, paper_unprotected, "unprotected sim position"),
    )


def check_strategy_bar_freshness() -> tuple[str, str, str]:
    """Check #1: strategy-engine is actually producing bars (function), cross-checked
    against the independent Polygon capture (ground truth), not its own snapshot."""
    bar_age = _scalar_int(
        "SELECT round(extract(epoch FROM (now()-max(bar_time))))::int "
        "FROM strategy_bar_history WHERE strategy_code='polygon_30s'"
    )
    feed_age = _scalar_int(
        "SELECT round(extract(epoch FROM (now()-max(received_at))))::int "
        "FROM market_capture_trades WHERE received_at > now() - interval '10 min'"
    )
    level, detail = classify_bar_freshness(bar_age, feed_age)
    return (level, "strategy-bar-freshness", detail)


def classify_bar_continuity(
    worst_gap_min: int | None,
    gap_symbols: int | None,
    bars_seen: int | None,
    *,
    amber_gap_min: int = 2,
    red_gap_min: int = 10,
) -> tuple[str, str]:
    """schwab_1m_v2 bar-continuity verdict — the SILENT ATR-corruption detector.

    ⭐ WHY IT EXISTS (2026-07-30, live money). A hole in `strategy_bar_history` makes true range
    span the gap: `href`/`lref` reference `prev.close`, so ONE bar carries the whole outage. v2 was
    stopped 10:12-11:33 ET and NUWE's ATR read 0.149 against a true 1-minute ATR of ~0.06 —
    `loss = 3.5 * ATR` put the resting buy-stop at 4.74 while the operator's chart showed ~4.40.
    Every resting order on a gap-spanning symbol sits too high until the bad TR ages out.

    ⛔ Gaps are NOT restart-only. Same day, no outage: CRWU 25 min, AXTU 2-13 min, SNDG 3 min.
    Nobody was checking, so nobody knew. That is the whole reason this check exists.

    ⛔ NO-FALSE-ALARM GUARD: no bars at all means off-hours or an empty watchlist, NOT a fault —
    GREEN. This check must never red a quiet market (same rule as the freshness check)."""
    if not bars_seen:
        return ("GREEN", "no schwab_1m_v2 bars in the window (off-hours or empty watchlist)")
    if worst_gap_min is None or worst_gap_min <= 1:
        return ("GREEN", f"schwab_1m_v2 bars contiguous ({bars_seen} bars, no gaps)")
    if worst_gap_min < amber_gap_min:
        return (
            "GREEN",
            f"schwab_1m_v2 bars contiguous within tolerance (worst {worst_gap_min}min)",
        )
    if worst_gap_min < red_gap_min:
        return (
            "AMBER",
            f"schwab_1m_v2 BAR GAP {worst_gap_min}min on {gap_symbols} symbol(s) — "
            f"DB series holed (backtest/parity read it); live ATR is guarded by #620",
        )
    return (
        "RED",
        f"schwab_1m_v2 BAR HOLE {worst_gap_min}min on {gap_symbols} symbol(s) — "
        f"DB series holed; backtest/parity/recorder read it. Live ATR guarded by #620 — "
        f"confirm [V2-ATR-BAR-GAP] fired for these names. Do NOT restart on this alert alone.",
    )


def check_bar_continuity() -> tuple[str, str, str]:
    """Check #4: the v2 bar series has no holes — a hole silently corrupts ATR and
    misprices every resting order on that symbol."""
    window = (
        "FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 "
        "AND bar_time >= now() - interval '30 minutes'"
    )
    bars_seen = _scalar_int(f"SELECT count(*) {window}")
    worst = _scalar_int(
        "WITH g AS (SELECT symbol, bar_time, lead(bar_time) OVER "
        "(PARTITION BY symbol ORDER BY bar_time) nxt "
        f"{window}) "
        "SELECT coalesce(max(extract(epoch FROM (nxt-bar_time))/60),0)::int FROM g "
        "WHERE nxt - bar_time > interval '1 minute'"
    )
    syms = _scalar_int(
        "WITH g AS (SELECT symbol, bar_time, lead(bar_time) OVER "
        "(PARTITION BY symbol ORDER BY bar_time) nxt "
        f"{window}) "
        "SELECT count(DISTINCT symbol) FROM g WHERE nxt - bar_time > interval '1 minute'"
    )
    level, detail = classify_bar_continuity(worst, syms, bars_seen)
    return (level, "v2-bar-continuity", detail)


LIVE_MONEY = "LIVE_MONEY"
FLEET_RUNTIME = "FLEET_RUNTIME"
PAPER = "PAPER"
DIAGNOSTIC = "DIAGNOSTIC"
SCOREBOARD = "SCOREBOARD"


class CheckSpec(NamedTuple):
    check: Callable[[], tuple[str, str, str] | tuple[tuple[str, str, str], ...]]
    alert_class: str


# Every check declares its routing class here. There is deliberately no default: adding a check
# without deciding whether it can page is a construction error, not an implicit page.
RUNTIME_CHECKS = (
    CheckSpec(check_service_restart_storms, FLEET_RUNTIME),
    CheckSpec(check_massive_socket_policy_violations, FLEET_RUNTIME),
)
FUNCTION_CHECKS = (
    CheckSpec(check_strategy_bar_freshness, PAPER),
    CheckSpec(check_oms_order_lifecycle, LIVE_MONEY),
    CheckSpec(check_stops_armed, LIVE_MONEY),
    CheckSpec(check_bar_continuity, DIAGNOSTIC),
    CheckSpec(check_d6_status_freshness, SCOREBOARD),
)
CHECKS = RUNTIME_CHECKS + FUNCTION_CHECKS

_RANK = {"GREEN": 0, "MAINTENANCE": 0, "AMBER": 1, "RED": 2}
_EXIT = {"GREEN": 0, "MAINTENANCE": 0, "AMBER": 1, "RED": 2}


def _result_rows(
    result: tuple[str, str, str] | tuple[tuple[str, str, str], ...],
) -> tuple[tuple[str, str, str], ...]:
    if len(result) == 3 and isinstance(result[0], str):
        return (result,)  # type: ignore[return-value]
    return result  # type: ignore[return-value]


def main(*, runtime_only: bool = False) -> int:
    checks = RUNTIME_CHECKS if runtime_only else CHECKS
    worst = "GREEN"
    live_money_red = 0
    fleet_runtime_red = 0
    reported_checks = 0
    for spec in checks:
        try:
            rows = _result_rows(spec.check())
        except Exception as exc:  # noqa: BLE001 — a check crash must not crash the runner
            rows = (
                (
                    "AMBER",
                    getattr(spec.check, "__name__", "check"),
                    f"check errored: {exc}",
                ),
            )
        for level, name, detail in rows:
            reported_checks += 1
            print(f"VERDICT: {level} {name} class={spec.alert_class} {detail}")
            if spec.alert_class == LIVE_MONEY and level == "RED":
                live_money_red += 1
            if spec.alert_class == FLEET_RUNTIME and level == "RED":
                fleet_runtime_red += 1
            if _RANK[level] > _RANK[worst]:
                worst = level
    print(
        f"SUMMARY: {worst} fleet-function-health checks={reported_checks} "
        f"live_money_red={live_money_red} fleet_runtime_red={fleet_runtime_red}"
    )
    return _EXIT[worst]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args not in ([], ["--runtime-only"]):
        print("usage: fleet_health_check.py [--runtime-only]", file=sys.stderr)
        sys.exit(3)
    sys.exit(main(runtime_only=bool(args)))
