from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


EASTERN_TZ = ZoneInfo("America/New_York")
TIMESTAMP_FORMAT = "%Y-%m-%d %I:%M:%S %p ET"


@dataclass(frozen=True)
class BotExpectation:
    display_name: str
    trading_start_hour: int
    decision_stale_after_seconds: int
    tick_stale_after_seconds: int
    market_data_stale_after_seconds: int
    requires_completed_rows_pre_session: bool = False


BOT_EXPECTATIONS: tuple[BotExpectation, ...] = (
    BotExpectation(
        display_name="Schwab 30 Sec Bot",
        trading_start_hour=7,
        decision_stale_after_seconds=120,
        tick_stale_after_seconds=120,
        market_data_stale_after_seconds=120,
        requires_completed_rows_pre_session=False,
    ),
    BotExpectation(
        display_name="Webull 30 Sec Bot",
        trading_start_hour=4,
        decision_stale_after_seconds=120,
        tick_stale_after_seconds=120,
        market_data_stale_after_seconds=120,
        requires_completed_rows_pre_session=True,
    ),
    BotExpectation(
        display_name="Schwab 1 Min Bot",
        trading_start_hour=7,
        decision_stale_after_seconds=180,
        tick_stale_after_seconds=180,
        market_data_stale_after_seconds=180,
        requires_completed_rows_pre_session=False,
    ),
)

CRITICAL_UNITS = (
    "project-mai-tai-market-data.service",
    "project-mai-tai-strategy.service",
    "project-mai-tai-control.service",
    "project-mai-tai-oms.service",
)

LOG_BOOTSTRAP_PATTERN = re.compile(
    r"bootstrapped \d+ Schwab historical bars for (?P<symbol>[A-Z]+) @ 60s into schwab_1m"
)


def _run_local(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _run_ssh(host: str, remote_command: str) -> str:
    return _run_local(["ssh", host, remote_command])


def _remote_json(host: str, path: str) -> object:
    raw = _run_ssh(host, f"curl -s http://127.0.0.1:8100{path}")
    return json.loads(raw)


def _parse_eastern_label(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), TIMESTAMP_FORMAT).replace(tzinfo=EASTERN_TZ)
    except ValueError:
        return None


def _age_seconds(now_et: datetime, value: str | None) -> float | None:
    observed_at = _parse_eastern_label(value)
    if observed_at is None:
        return None
    return max(0.0, (now_et - observed_at).total_seconds())


def _service_name_to_health_entry(health: dict[str, object]) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    for item in list(health.get("services", []) or []):
        if not isinstance(item, dict):
            continue
        service_name = str(item.get("service_name", "") or "")
        if service_name:
            entries[service_name] = item
    return entries


def _systemd_states(host: str) -> dict[str, str]:
    cmd = (
        "for unit in "
        + " ".join(CRITICAL_UNITS)
        + "; do printf '%s=' \"$unit\"; systemctl is-active \"$unit\" || true; done"
    )
    raw = _run_ssh(host, cmd)
    states: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        unit, state = line.split("=", 1)
        states[unit.strip()] = state.strip()
    return states


def _tail_strategy_log(host: str, lines: int) -> str:
    return _run_ssh(
        host,
        f"sudo tail -n {max(50, lines)} /var/log/project-mai-tai/strategy.log || true",
    )


def _collect_bootstrap_loop_warnings(log_text: str) -> list[str]:
    counts: dict[str, int] = {}
    for line in log_text.splitlines():
        match = LOG_BOOTSTRAP_PATTERN.search(line)
        if not match:
            continue
        symbol = match.group("symbol")
        counts[symbol] = counts.get(symbol, 0) + 1
    return [
        f"Schwab 1m repeated bootstrap loop for {symbol}: {count} recent bootstrap entries"
        for symbol, count in sorted(counts.items())
        if count >= 5
    ]


def _check_services(
    *,
    now_et: datetime,
    health: dict[str, object],
    systemd_states: dict[str, str],
) -> tuple[list[str], list[str]]:
    del now_et
    failures: list[str] = []
    warnings: list[str] = []

    for unit in CRITICAL_UNITS:
        state = systemd_states.get(unit, "unknown")
        if state != "active":
            failures.append(f"{unit} systemd state is {state}, expected active")

    health_services = _service_name_to_health_entry(health)
    service_expectations = {
        "market-data-gateway": "project-mai-tai-market-data.service",
        "strategy-engine": "project-mai-tai-strategy.service",
        "oms-risk": "project-mai-tai-oms.service",
    }
    for service_name, _unit in service_expectations.items():
        entry = health_services.get(service_name)
        if entry is None:
            failures.append(f"{service_name} missing from /health")
            continue
        effective_status = str(entry.get("effective_status", entry.get("status", "unknown")) or "unknown")
        if effective_status != "healthy":
            failures.append(f"{service_name} health is {effective_status}")

    control_status = str(health.get("status", "unknown") or "unknown")
    if control_status not in {"healthy", "starting"}:
        warnings.append(f"control-plane overall health is {control_status}")

    return failures, warnings


def _is_bot_in_session(now_et: datetime, expectation: BotExpectation) -> bool:
    return now_et.hour >= expectation.trading_start_hour


def _check_bot(
    *,
    now_et: datetime,
    bot: dict[str, object],
    expectation: BotExpectation,
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []

    watchlist_count = int(bot.get("watchlist_count", 0) or 0)
    listening_status = dict(bot.get("listening_status", {}) or {})
    latest_decision_at = str(listening_status.get("latest_decision_at", bot.get("latest_decision_at", "")) or "")
    latest_bot_tick_at = str(listening_status.get("latest_bot_tick_at", bot.get("latest_bot_tick_at", "")) or "")
    latest_market_data_at = str(
        listening_status.get("latest_market_data_at", bot.get("latest_market_data_at", "")) or ""
    )
    decision_age = _age_seconds(now_et, latest_decision_at)
    tick_age = _age_seconds(now_et, latest_bot_tick_at)
    market_data_age = _age_seconds(now_et, latest_market_data_at)
    listening_state = str(listening_status.get("state", "") or "").upper()
    data_health = dict(bot.get("data_health", {}) or {})
    data_health_status = str(data_health.get("status", "unknown") or "unknown").lower()
    recent_decisions = list(bot.get("recent_decisions", []) or [])
    indicator_snapshots = list(bot.get("indicator_snapshots", []) or [])
    in_session = _is_bot_in_session(now_et, expectation)

    if data_health_status in {"critical", "error", "degraded"}:
        failures.append(f"{expectation.display_name} data health is {data_health_status}")

    if in_session and listening_state not in {"LISTENING", "READY"}:
        failures.append(f"{expectation.display_name} listening state is {listening_state or 'unknown'}")
    elif not in_session and listening_state not in {"LISTENING", "READY", "STARTING"}:
        warnings.append(f"{expectation.display_name} listening state is {listening_state or 'unknown'}")

    if watchlist_count <= 0:
        warnings.append(f"{expectation.display_name} has empty watchlist/feed at check time")
        return failures, warnings

    if market_data_age is None or market_data_age > expectation.market_data_stale_after_seconds:
        failures.append(
            f"{expectation.display_name} market data is stale at {latest_market_data_at or 'missing'}"
        )

    if tick_age is None or tick_age > expectation.tick_stale_after_seconds:
        if in_session or expectation.requires_completed_rows_pre_session:
            failures.append(
                f"{expectation.display_name} bot ticks are stale at {latest_bot_tick_at or 'missing'}"
            )
        else:
            warnings.append(
                f"{expectation.display_name} bot ticks are stale at {latest_bot_tick_at or 'missing'}"
            )

    real_decisions = [
        item for item in recent_decisions if not bool(item.get("is_placeholder")) and str(item.get("last_bar_at", "") or "")
    ]
    if in_session or expectation.requires_completed_rows_pre_session:
        if decision_age is None:
            failures.append(f"{expectation.display_name} has no completed decision timestamp")
        elif decision_age > expectation.decision_stale_after_seconds:
            failures.append(
                f"{expectation.display_name} completed decisions are stale at {latest_decision_at}"
            )
        if not real_decisions:
            failures.append(f"{expectation.display_name} has no completed decision rows visible")

    if expectation.display_name == "Schwab 1 Min Bot":
        if in_session and not indicator_snapshots:
            failures.append("Schwab 1 Min Bot has no indicator snapshots after session start")
        last_bar_ages: list[float] = []
        for snapshot in indicator_snapshots:
            age = _age_seconds(now_et, str(snapshot.get("last_bar_at", "") or ""))
            if age is not None:
                last_bar_ages.append(age)
        if in_session and last_bar_ages and min(last_bar_ages) > expectation.decision_stale_after_seconds:
            failures.append("Schwab 1 Min Bot indicator snapshots are stale across active symbols")

    return failures, warnings


def run_check(host: str, log_lines: int) -> dict[str, object]:
    now_et = datetime.now(EASTERN_TZ)
    health = _remote_json(host, "/health")
    bots_payload = _remote_json(host, "/api/bots")
    systemd_states = _systemd_states(host)
    strategy_log = _tail_strategy_log(host, log_lines)

    failures: list[str] = []
    warnings: list[str] = []

    service_failures, service_warnings = _check_services(
        now_et=now_et,
        health=health if isinstance(health, dict) else {},
        systemd_states=systemd_states,
    )
    failures.extend(service_failures)
    warnings.extend(service_warnings)

    bots_by_name = {
        str(bot.get("display_name", "") or ""): bot
        for bot in list((bots_payload or {}).get("bots", []) or [])
        if isinstance(bot, dict)
    }
    for expectation in BOT_EXPECTATIONS:
        bot = bots_by_name.get(expectation.display_name)
        if bot is None:
            failures.append(f"{expectation.display_name} missing from /api/bots")
            continue
        bot_failures, bot_warnings = _check_bot(
            now_et=now_et,
            bot=bot,
            expectation=expectation,
        )
        failures.extend(bot_failures)
        warnings.extend(bot_warnings)

    warnings.extend(_collect_bootstrap_loop_warnings(strategy_log))

    status = "pass" if not failures else "fail"
    return {
        "status": status,
        "checked_at": now_et.strftime(TIMESTAMP_FORMAT),
        "host": host,
        "failures": failures,
        "warnings": warnings,
        "systemd": systemd_states,
        "health_status": (health if isinstance(health, dict) else {}).get("status", "unknown"),
        "bot_summaries": {
            name: {
                "watchlist_count": bot.get("watchlist_count", 0),
                "latest_decision_at": dict(bot.get("listening_status", {}) or {}).get("latest_decision_at", ""),
                "latest_bot_tick_at": dict(bot.get("listening_status", {}) or {}).get("latest_bot_tick_at", ""),
                "latest_market_data_at": dict(bot.get("listening_status", {}) or {}).get("latest_market_data_at", ""),
                "data_health": dict(bot.get("data_health", {}) or {}).get("status", "unknown"),
            }
            for name, bot in bots_by_name.items()
            if name in {item.display_name for item in BOT_EXPECTATIONS}
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Mai Tai live morning readiness before trading.")
    parser.add_argument("--host", default="mai-tai-vps", help="SSH host for the live VPS")
    parser.add_argument("--log-lines", type=int, default=400, help="How many recent strategy log lines to inspect")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args()

    try:
        result = run_check(args.host, args.log_lines)
    except subprocess.CalledProcessError as exc:
        error_result = {
            "status": "fail",
            "checked_at": datetime.now(EASTERN_TZ).strftime(TIMESTAMP_FORMAT),
            "host": args.host,
            "failures": [
                f"command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}".strip()
            ],
            "warnings": [],
        }
        print(json.dumps(error_result, indent=2))
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI guard
        error_result = {
            "status": "fail",
            "checked_at": datetime.now(EASTERN_TZ).strftime(TIMESTAMP_FORMAT),
            "host": args.host,
            "failures": [f"unexpected error: {exc}"],
            "warnings": [],
        }
        print(json.dumps(error_result, indent=2))
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Morning readiness: {result['status'].upper()} @ {result['checked_at']}")
        for failure in list(result.get("failures", []) or []):
            print(f"FAIL: {failure}")
        for warning in list(result.get("warnings", []) or []):
            print(f"WARN: {warning}")
        if not result.get("failures"):
            print("No hard readiness failures detected.")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
