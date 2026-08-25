from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from project_mai_tai.post_restart_health_gate import (
    GateOutcome,
    HealthEndpointError,
    inspect_health_payload,
    wait_for_post_restart_health,
)


STARTED_AT = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _fleet_payload(status: str, observed_at: datetime) -> dict[str, object]:
    return {
        "services": [
            {
                "service_name": "schwab-1m-v2",
                "status": status,
                "raw_status": status,
                "effective_status": "healthy",
                "observed_at_raw": observed_at.isoformat(),
            }
        ]
    }


def test_fresh_raw_healthy_heartbeat_passes() -> None:
    observation = inspect_health_payload(
        _fleet_payload("healthy", STARTED_AT + timedelta(seconds=2)),
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
    )

    assert observation.outcome == GateOutcome.HEALTHY


def test_effective_healthy_does_not_mask_fresh_raw_stopping() -> None:
    observation = inspect_health_payload(
        _fleet_payload("stopping", STARTED_AT + timedelta(seconds=2)),
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
    )

    assert observation.outcome == GateOutcome.NOT_HEALTHY
    assert "stopping" in observation.summary


def test_pre_restart_healthy_heartbeat_is_not_accepted() -> None:
    observation = inspect_health_payload(
        _fleet_payload("healthy", STARTED_AT - timedelta(seconds=1)),
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
    )

    assert observation.outcome == GateOutcome.NOT_HEALTHY
    assert "stale" in observation.summary


def test_missing_service_is_not_healthy() -> None:
    observation = inspect_health_payload(
        {"services": []},
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
    )

    assert observation.outcome == GateOutcome.NOT_HEALTHY


def test_unparseable_service_timestamp_is_indeterminate() -> None:
    payload = _fleet_payload("healthy", STARTED_AT + timedelta(seconds=1))
    payload["services"][0]["observed_at_raw"] = "not-a-time"  # type: ignore[index]

    observation = inspect_health_payload(
        payload,
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
    )

    assert observation.outcome is None
    assert not observation.evaluable


def test_control_passes_when_own_dependencies_work_despite_fleet_degraded() -> None:
    observation = inspect_health_payload(
        {
            "service": "control-plane",
            "timestamp": (STARTED_AT + timedelta(seconds=1)).isoformat(),
            "status": "degraded",
            "database_connected": True,
            "redis_connected": True,
        },
        service_name="control-plane",
        process_started_at=STARTED_AT,
    )

    assert observation.outcome == GateOutcome.HEALTHY


def test_control_dependency_failure_is_not_healthy() -> None:
    observation = inspect_health_payload(
        {
            "service": "control-plane",
            "timestamp": (STARTED_AT + timedelta(seconds=1)).isoformat(),
            "database_connected": True,
            "redis_connected": False,
        },
        service_name="control-plane",
        process_started_at=STARTED_AT,
    )

    assert observation.outcome == GateOutcome.NOT_HEALTHY


def test_wait_rejects_old_heartbeat_then_accepts_fresh_one() -> None:
    payloads = iter(
        [
            _fleet_payload("stopping", STARTED_AT - timedelta(seconds=1)),
            _fleet_payload("healthy", STARTED_AT + timedelta(seconds=2)),
        ]
    )
    clock_values = iter(
        [
            STARTED_AT + timedelta(seconds=1),
            STARTED_AT + timedelta(seconds=2),
        ]
    )

    result = wait_for_post_restart_health(
        health_url="http://health",
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
        load_health=lambda _url: next(payloads),
        now=lambda: next(clock_values),
        sleep=lambda _seconds: None,
        report=lambda _message: None,
    )

    assert result.outcome == GateOutcome.HEALTHY
    assert result.attempts == 2


def test_wait_returns_not_healthy_when_only_old_heartbeat_exists() -> None:
    now = STARTED_AT + timedelta(seconds=60)
    result = wait_for_post_restart_health(
        health_url="http://health",
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
        load_health=lambda _url: _fleet_payload(
            "stopping", STARTED_AT - timedelta(seconds=1)
        ),
        now=lambda: now,
        sleep=lambda _seconds: None,
        report=lambda _message: None,
    )

    assert result.outcome == GateOutcome.NOT_HEALTHY


def test_healthy_heartbeat_after_deadline_does_not_pass() -> None:
    result = wait_for_post_restart_health(
        health_url="http://health",
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
        load_health=lambda _url: _fleet_payload(
            "healthy", STARTED_AT + timedelta(seconds=61)
        ),
        now=lambda: STARTED_AT + timedelta(seconds=61),
        sleep=lambda _seconds: None,
        report=lambda _message: None,
    )

    assert result.outcome == GateOutcome.NOT_HEALTHY
    assert "after the SLA deadline" in result.summary


def test_wait_returns_could_not_tell_when_endpoint_never_answers() -> None:
    def unavailable(_url: str) -> dict[str, object]:
        raise HealthEndpointError("connection refused")

    now = STARTED_AT + timedelta(seconds=60)
    result = wait_for_post_restart_health(
        health_url="http://health",
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
        load_health=unavailable,
        now=lambda: now,
        sleep=lambda _seconds: None,
        report=lambda _message: None,
    )

    assert result.outcome == GateOutcome.INDETERMINATE
    assert "connection refused" in result.summary


def test_final_unreachable_endpoint_cannot_turn_earlier_unhealthy_into_a_verdict() -> None:
    attempts = 0

    def stale_then_unavailable(_url: str) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _fleet_payload("stopping", STARTED_AT - timedelta(seconds=1))
        raise HealthEndpointError("connection refused at deadline")

    clock_values = iter(
        [
            STARTED_AT + timedelta(seconds=1),
            STARTED_AT + timedelta(seconds=60),
        ]
    )
    result = wait_for_post_restart_health(
        health_url="http://health",
        service_name="schwab-1m-v2",
        process_started_at=STARTED_AT,
        load_health=stale_then_unavailable,
        now=lambda: next(clock_values),
        sleep=lambda _seconds: None,
        report=lambda _message: None,
    )

    assert result.outcome == GateOutcome.INDETERMINATE
    assert "connection refused at deadline" in result.summary


def test_deploy_service_invokes_gate_before_reporting_success() -> None:
    deploy_script = (ROOT / "ops/systemd/deploy_service.sh").read_text(encoding="utf-8")

    invocation = '"$REPO_DIR/.venv/bin/python" -m project_mai_tai.post_restart_health_gate'
    gate_call = 'run_post_restart_health_gates "$DEPLOYED_SHA"'
    assert invocation in deploy_script
    assert deploy_script.rindex(gate_call) < deploy_script.index(
        'echo "Service deploy finished for $SERVICE_TARGET."'
    )
    assert "A rollback is a separate production mutation" in deploy_script


def test_deploy_service_maps_every_restartable_unit_to_a_health_identity() -> None:
    deploy_script = (ROOT / "ops/systemd/deploy_service.sh").read_text(encoding="utf-8")

    for service_name in (
        "control-plane",
        "reconciler",
        "strategy-engine",
        "oms-risk",
        "market-data-gateway",
        "schwab-1m-v2",
    ):
        assert f'echo "{service_name}"' in deploy_script


def test_strategy_uses_measured_240_second_sla_while_other_units_use_60() -> None:
    deploy_script = (ROOT / "ops/systemd/deploy_service.sh").read_text(encoding="utf-8")

    assert "DEFAULT_POST_RESTART_HEALTH_SLA_SECONDS=60" in deploy_script
    assert "STRATEGY_POST_RESTART_HEALTH_SLA_SECONDS=240" in deploy_script
    assert "first fresh heartbeat at 113s, healthy at 181s" in deploy_script
    assert 'project-mai-tai-strategy.service)' in deploy_script
    assert 'echo "$STRATEGY_POST_RESTART_HEALTH_SLA_SECONDS"' in deploy_script


def test_could_not_tell_uses_the_fleet_exit_code() -> None:
    module = (ROOT / "src/project_mai_tai/post_restart_health_gate.py").read_text(
        encoding="utf-8"
    )

    assert "raise SystemExit(3)" in module
