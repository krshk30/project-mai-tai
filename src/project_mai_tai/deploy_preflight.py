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


# ⛔⭐⭐ MEASURED 2026-09-04: a COLD `/api/overview` takes 5.5-6.7s (codex-2, at deploy time) while
# a warm one is ~0.01s. The old 5.0s budget sat INSIDE that spread, so the fail-closed deploy gate
# refused on latency alone and the operator had to warm the endpoint by hand and re-run.
# ⛔ That workaround is the actual hazard: it trains "preflight failed -> poke it and retry" on a
# SAFETY gate. 20s covers the measured cold worst case with headroom while still bounding a hang.
_PREFLIGHT_HTTP_TIMEOUT_SECONDS = 20.0


def load_json(
    url: str, *, timeout_seconds: float = _PREFLIGHT_HTTP_TIMEOUT_SECONDS
) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except TimeoutError as exc:
        # ⛔ A READ timeout does NOT raise URLError — it escapes as a bare TimeoutError, so before
        # this it surfaced as an unhandled traceback while a genuinely DEAD control plane produced
        # a clean message. The routine failure had the worst diagnostics of the two.
        # ⛔ The distinction is load-bearing: "slow" invites a retry, "unreachable" must NOT. Never
        # collapse these two into one message.
        raise SystemExit(
            f"preflight TIMED OUT after {timeout_seconds:.0f}s loading {url}. The control plane "
            "ANSWERED THE CONNECTION but did not finish in time — this is NOT the same as it being "
            "down. Check control-plane latency/load before assuming it is safe to retry, and do "
            "NOT simply warm the endpoint to make this pass."
        ) from exc
    except URLError as exc:
        raise SystemExit(
            f"failed to load preflight data from {url}: {exc}. The control plane is UNREACHABLE "
            "(not merely slow) — do not deploy until it is serving."
        ) from exc


def evaluate_live_deploy_preflight(
    overview: dict[str, Any],
    *,
    service_target: str,
    now: datetime | None = None,
    heartbeat_max_age_seconds: int = 120,
    recent_fill_grace_seconds: int = 180,
    warnings: list[str] | None = None,
) -> list[str]:
    if service_target not in TARGET_SERVICE_NAMES:
        raise ValueError(f"unknown service target: {service_target}")

    now = now or utcnow()
    failures: list[str] = []
    warning_sink = warnings if warnings is not None else []
    counts = overview.get("counts", {})
    target_service_name = TARGET_SERVICE_NAMES[service_target]

    overview_errors = overview.get("errors")
    if not isinstance(overview_errors, list):
        failures.append("control-plane overview errors are unreadable before deploy.")
    elif overview_errors:
        failures.append(
            f"control-plane overview reports {len(overview_errors)} error(s): "
            + "; ".join(str(error) for error in overview_errors[:3])
        )

    pending_intents = int(counts.get("pending_intents", 0) or 0)
    if pending_intents > 0:
        failures.append(f"{pending_intents} strategy intents are still pending/submitted/accepted.")

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
    total_findings = int(summary.get("total_findings", 0) or 0)
    if total_findings > 0:
        failures.append(
            f"reconciliation reports {total_findings} total findings in the latest run."
        )

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
    required_service_names = EXPECTED_SERVICE_NAMES | {target_service_name}
    for service_name in sorted(set(service_rows) | required_service_names):
        service = service_rows.get(service_name)
        if service is None:
            failures.append(f"heartbeat for {service_name} is missing.")
            continue

        status = str(service.get("effective_status", service.get("status", ""))).lower()
        observed_at = parse_datetime(
            str(service.get("observed_at_raw") or service.get("observed_at", ""))
        )
        heartbeat_is_fresh = True
        if observed_at is None:
            failures.append(f"heartbeat for {service_name} has no observed_at timestamp.")
            heartbeat_is_fresh = False
        elif observed_at < heartbeat_cutoff:
            age_seconds = int((now - observed_at).total_seconds())
            failures.append(f"heartbeat for {service_name} is stale ({age_seconds}s old).")
            heartbeat_is_fresh = False

        if status == "healthy":
            continue

        details = service.get("details")
        paper_observer = (
            isinstance(details, dict)
            and details.get("execution_mode") == "paper"
            and details.get("broker_route") == "none"
        )
        if service_name not in required_service_names and paper_observer:
            if heartbeat_is_fresh:
                reason = str(details.get("feed_reason") or "unspecified")
                warning_sink.append(
                    "NON-BLOCKING paper observer: "
                    f"{service_name} status={status or 'unknown'} reason={reason}"
                )
            continue

        service_label = "target service" if service_name == target_service_name else "service"
        failures.append(
            f"{service_label} {service_name} is not healthy before deploy "
            f"(status={status or 'unknown'})."
        )

    return failures


def build_argument_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Check whether a risky live service deploy is safe to run.")
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
    warnings: list[str] = []
    failures = evaluate_live_deploy_preflight(
        overview,
        service_target=args.service,
        heartbeat_max_age_seconds=args.heartbeat_max_age_seconds,
        recent_fill_grace_seconds=args.recent_fill_grace_seconds,
        warnings=warnings,
    )

    for warning in warnings:
        print(warning)

    if failures:
        print(f"Live deploy preflight failed for {args.service}.")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"Live deploy preflight passed for {args.service}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
