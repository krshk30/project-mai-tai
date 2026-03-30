from __future__ import annotations

from argparse import ArgumentParser
from datetime import UTC, datetime, timedelta
import json
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo


EXPECTED_SERVICE_NAMES = {
    "market-data-gateway",
    "strategy-engine",
    "oms-risk",
    "reconciler",
}

TARGET_SERVICE_NAMES = {
    "control": "control-plane",
    "reconciler": "reconciler",
    "strategy": "strategy-engine",
    "oms": "oms-risk",
    "market-data": "market-data-gateway",
}

IN_FLIGHT_INTENT_STATUSES = {"pending", "submitted", "accepted"}
EASTERN = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for pattern in ("%Y-%m-%d %I:%M:%S %p ET", "%Y-%m-%d %H:%M:%S %z"):
            try:
                parsed = datetime.strptime(value, pattern)
                if pattern.endswith("ET"):
                    parsed = parsed.replace(tzinfo=EASTERN)
                elif parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except ValueError:
                continue
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def load_json(url: str, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise SystemExit(f"failed to load preflight data from {url}: {exc}") from exc


def evaluate_live_deploy_preflight(
    overview: dict[str, Any],
    *,
    service_target: str,
    now: datetime | None = None,
    heartbeat_max_age_seconds: int = 120,
    recent_fill_grace_seconds: int = 180,
) -> list[str]:
    if service_target not in TARGET_SERVICE_NAMES:
        raise ValueError(f"unknown service target: {service_target}")

    now = now or utcnow()
    failures: list[str] = []
    counts = overview.get("counts", {})
    target_service_name = TARGET_SERVICE_NAMES[service_target]

    control_plane_status = str(overview.get("status", "")).lower()
    if control_plane_status != "healthy":
        failures.append(
            "control-plane overview endpoint is not healthy before deploy "
            f"(status={control_plane_status or 'unknown'})."
        )

    pending_intents = int(counts.get("pending_intents", 0) or 0)
    if pending_intents > 0:
        failures.append(
            f"{pending_intents} strategy intents are still pending/submitted/accepted."
        )

    open_virtual_positions = int(counts.get("open_virtual_positions", 0) or 0)
    if open_virtual_positions > 0:
        failures.append(f"{open_virtual_positions} virtual positions are still open.")

    open_account_positions = int(counts.get("open_account_positions", 0) or 0)
    if open_account_positions > 0:
        failures.append(f"{open_account_positions} broker account positions are still open.")

    recent_intents = overview.get("recent_intents", [])
    in_flight_recent_intents = [
        item
        for item in recent_intents
        if str(item.get("status", "")).lower() in IN_FLIGHT_INTENT_STATUSES
    ]
    if in_flight_recent_intents and pending_intents == 0:
        failures.append(
            f"{len(in_flight_recent_intents)} recent intents still show in-flight statuses."
        )

    recent_fills = overview.get("recent_fills", [])
    cutoff = now - timedelta(seconds=recent_fill_grace_seconds)
    settling_fills = [
        item
        for item in recent_fills
        if (filled_at := parse_datetime(str(item.get("filled_at", "")))) is not None
        and filled_at >= cutoff
    ]
    if settling_fills:
        failures.append(
            f"{len(settling_fills)} fills were recorded in the last {recent_fill_grace_seconds} seconds."
        )

    reconciliation = overview.get("reconciliation", {})
    latest_run = reconciliation.get("latest_run") or {}
    summary = latest_run.get("summary") or {}
    critical_findings = int(summary.get("critical_findings", 0) or 0)
    if critical_findings > 0:
        failures.append(
            f"reconciliation reports {critical_findings} critical findings in the latest run."
        )

    service_rows = {
        str(item.get("service_name", "")): item
        for item in overview.get("services", [])
        if item.get("service_name")
    }
    heartbeat_cutoff = now - timedelta(seconds=heartbeat_max_age_seconds)
    for service_name in sorted(EXPECTED_SERVICE_NAMES):
        service = service_rows.get(service_name)
        if service is None:
            failures.append(f"heartbeat for {service_name} is missing.")
            continue

        status = str(service.get("status", "")).lower()
        if status != "healthy":
            if service_name == target_service_name:
                failures.append(
                    f"target service {service_name} is not healthy before deploy (status={status or 'unknown'})."
                )
            else:
                failures.append(
                    f"service {service_name} is not healthy before deploy (status={status or 'unknown'})."
                )

        observed_at = parse_datetime(str(service.get("observed_at", "")))
        if observed_at is None:
            failures.append(f"heartbeat for {service_name} has no observed_at timestamp.")
            continue
        if observed_at < heartbeat_cutoff:
            age_seconds = int((now - observed_at).total_seconds())
            failures.append(
                f"heartbeat for {service_name} is stale ({age_seconds}s old)."
            )

    return failures


def build_argument_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Check whether a risky live service deploy is safe to run."
    )
    parser.add_argument("--service", required=True, choices=sorted(TARGET_SERVICE_NAMES))
    parser.add_argument(
        "--overview-url",
        default="http://127.0.0.1:8100/api/overview",
        help="Control-plane overview endpoint used for preflight checks.",
    )
    parser.add_argument(
        "--heartbeat-max-age-seconds",
        type=int,
        default=120,
        help="Maximum acceptable heartbeat age before preflight fails.",
    )
    parser.add_argument(
        "--recent-fill-grace-seconds",
        type=int,
        default=180,
        help="How recent a fill must be to block a live risky deploy.",
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    overview = load_json(args.overview_url)
    failures = evaluate_live_deploy_preflight(
        overview,
        service_target=args.service,
        heartbeat_max_age_seconds=args.heartbeat_max_age_seconds,
        recent_fill_grace_seconds=args.recent_fill_grace_seconds,
    )

    if failures:
        print(f"Live deploy preflight failed for {args.service}.")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"Live deploy preflight passed for {args.service}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
