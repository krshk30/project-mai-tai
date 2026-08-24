from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
import json
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from project_mai_tai.deploy_preflight import parse_datetime


DEFAULT_SLA_SECONDS = 60
DEFAULT_POLL_SECONDS = 2.0


class GateOutcome(str, Enum):
    HEALTHY = "HEALTHY"
    NOT_HEALTHY = "NOT_HEALTHY_WITHIN_SLA"
    INDETERMINATE = "COULD_NOT_TELL"


@dataclass(frozen=True)
class Observation:
    outcome: GateOutcome | None
    summary: str
    evaluable: bool
    observed_at: datetime | None = None


@dataclass(frozen=True)
class GateResult:
    outcome: GateOutcome
    summary: str
    attempts: int


class HealthEndpointError(RuntimeError):
    """The health endpoint did not produce a usable JSON object."""


def load_health_json(url: str, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HealthEndpointError(f"{type(exc).__name__}: {exc}") from exc

    if not isinstance(payload, dict):
        raise HealthEndpointError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def inspect_health_payload(
    payload: dict[str, Any],
    *,
    service_name: str,
    process_started_at: datetime,
) -> Observation:
    if service_name == "control-plane":
        if payload.get("service") != "control-plane":
            return Observation(None, "health payload does not identify control-plane", False)

        observed_at = parse_datetime(str(payload.get("timestamp") or ""))
        if observed_at is None:
            return Observation(None, "control-plane timestamp is missing or unparseable", False)
        timestamp = observed_at.isoformat()
        if observed_at <= process_started_at:
            return Observation(
                GateOutcome.NOT_HEALTHY,
                f"control-plane response is stale (timestamp={timestamp})",
                True,
            )

        database_connected = payload.get("database_connected")
        redis_connected = payload.get("redis_connected")
        if not isinstance(database_connected, bool) or not isinstance(redis_connected, bool):
            return Observation(
                None,
                "control-plane response lacks boolean database/redis connection fields",
                False,
            )
        if database_connected and redis_connected:
            return Observation(
                GateOutcome.HEALTHY,
                f"fresh control-plane response at {timestamp}; database and Redis connected",
                True,
                observed_at,
            )
        return Observation(
            GateOutcome.NOT_HEALTHY,
            "fresh control-plane response but dependencies are unhealthy "
            f"(database_connected={database_connected}, redis_connected={redis_connected})",
            True,
        )

    services = payload.get("services")
    if not isinstance(services, list):
        return Observation(None, "health payload has no services list", False)

    service = next(
        (
            item
            for item in services
            if isinstance(item, dict) and item.get("service_name") == service_name
        ),
        None,
    )
    if service is None:
        return Observation(
            GateOutcome.NOT_HEALTHY,
            f"health payload has no heartbeat for {service_name}",
            True,
        )

    observed_value = service.get("observed_at_raw") or service.get("observed_at")
    observed_at = parse_datetime(str(observed_value or ""))
    if observed_at is None:
        return Observation(
            None, f"{service_name} heartbeat timestamp is missing or unparseable", False
        )
    timestamp = observed_at.isoformat()

    status_value = service.get("raw_status", service.get("status"))
    if not isinstance(status_value, str) or not status_value:
        return Observation(None, f"{service_name} heartbeat has no raw status", False)
    status = status_value.lower()

    if observed_at <= process_started_at:
        return Observation(
            GateOutcome.NOT_HEALTHY,
            f"{service_name} heartbeat is stale (status={status}, observed_at={timestamp})",
            True,
        )
    if status == "healthy":
        return Observation(
            GateOutcome.HEALTHY,
            f"fresh healthy {service_name} heartbeat at {timestamp}",
            True,
            observed_at,
        )
    return Observation(
        GateOutcome.NOT_HEALTHY,
        f"fresh {service_name} heartbeat is {status} at {timestamp}",
        True,
    )


def wait_for_post_restart_health(
    *,
    health_url: str,
    service_name: str,
    process_started_at: datetime,
    sla_seconds: int = DEFAULT_SLA_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    load_health: Callable[[str], dict[str, Any]] = load_health_json,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = print,
) -> GateResult:
    deadline = process_started_at.timestamp() + sla_seconds
    attempts = 0
    final_evaluable: Observation | None = None
    latest_error = "health endpoint was not queried"
    last_reported = ""

    while True:
        attempts += 1
        try:
            payload = load_health(health_url)
            observation = inspect_health_payload(
                payload,
                service_name=service_name,
                process_started_at=process_started_at,
            )
            if observation.evaluable:
                final_evaluable = observation
            else:
                final_evaluable = None
                latest_error = observation.summary
            message = f"attempt {attempts}: {observation.summary}"
            if message != last_reported:
                report(message)
                last_reported = message
            if observation.outcome == GateOutcome.HEALTHY:
                if (
                    observation.observed_at is not None
                    and observation.observed_at.timestamp() > deadline
                ):
                    final_evaluable = Observation(
                        GateOutcome.NOT_HEALTHY,
                        f"{service_name} first healthy heartbeat was after the SLA deadline "
                        f"({observation.observed_at.isoformat()})",
                        True,
                        observation.observed_at,
                    )
                    break
                return GateResult(GateOutcome.HEALTHY, observation.summary, attempts)
        except HealthEndpointError as exc:
            final_evaluable = None
            latest_error = str(exc)
            message = f"attempt {attempts}: health endpoint unavailable ({latest_error})"
            if message != last_reported:
                report(message)
                last_reported = message

        remaining = deadline - now().timestamp()
        if remaining <= 0:
            break
        sleep(min(poll_seconds, remaining))

    if final_evaluable is not None:
        return GateResult(GateOutcome.NOT_HEALTHY, final_evaluable.summary, attempts)
    return GateResult(GateOutcome.INDETERMINATE, latest_error, attempts)


def build_argument_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Require a fresh healthy heartbeat from a restarted service."
    )
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--process-start-epoch", required=True, type=float)
    parser.add_argument("--pid", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--sla-seconds", type=int, default=DEFAULT_SLA_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    if args.sla_seconds <= 0 or args.poll_seconds <= 0:
        raise SystemExit("SLA and poll interval must both be positive")

    process_started_at = datetime.fromtimestamp(args.process_start_epoch, tz=UTC)
    print(
        "Post-restart identity: "
        f"service={args.service_name} pid={args.pid} "
        f"process_started_at={process_started_at.isoformat()} deployed_sha={args.expected_sha}"
    )
    print(
        "Identity note: PID and SHA come from systemd/git deployment evidence; "
        "the current heartbeat schema does not attest them."
    )
    result = wait_for_post_restart_health(
        health_url=args.health_url,
        service_name=args.service_name,
        process_started_at=process_started_at,
        sla_seconds=args.sla_seconds,
        poll_seconds=args.poll_seconds,
    )
    print(
        f"POST-RESTART GATE: {result.outcome.value} after {result.attempts} attempt(s): "
        f"{result.summary}"
    )
    if result.outcome == GateOutcome.HEALTHY:
        return
    if result.outcome == GateOutcome.NOT_HEALTHY:
        raise SystemExit(1)
    raise SystemExit(3)


if __name__ == "__main__":
    main()
