from __future__ import annotations

from datetime import UTC, datetime, timedelta

from project_mai_tai.deploy_preflight import evaluate_live_deploy_preflight, parse_datetime


def _datetime_str(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _healthy_overview(now: datetime) -> dict:
    service_rows = [
        {
            "service_name": service_name,
            "status": "healthy",
            "observed_at": _datetime_str(now),
        }
        for service_name in [
            "control-plane",
            "market-data-gateway",
            "strategy-engine",
            "oms-risk",
            "reconciler",
        ]
    ]
    return {
        "status": "healthy",
        "counts": {
            "pending_intents": 0,
            "open_virtual_positions": 0,
            "open_account_positions": 0,
        },
        "recent_intents": [],
        "recent_fills": [],
        "reconciliation": {
            "latest_run": {
                "summary": {
                    "critical_findings": 0,
                }
            }
        },
        "services": service_rows,
    }


def test_live_deploy_preflight_passes_for_clean_overview() -> None:
    now = datetime(2026, 3, 30, 14, 0, tzinfo=UTC)

    failures = evaluate_live_deploy_preflight(
        _healthy_overview(now),
        service_target="strategy",
        now=now,
    )

    assert failures == []


def test_live_deploy_preflight_blocks_in_flight_intents() -> None:
    now = datetime(2026, 3, 30, 14, 0, tzinfo=UTC)
    overview = _healthy_overview(now)
    overview["counts"]["pending_intents"] = 2

    failures = evaluate_live_deploy_preflight(
        overview,
        service_target="oms",
        now=now,
    )

    assert "pending/submitted/accepted" in failures[0]


def test_live_deploy_preflight_blocks_open_positions() -> None:
    now = datetime(2026, 3, 30, 14, 0, tzinfo=UTC)
    overview = _healthy_overview(now)
    overview["counts"]["open_account_positions"] = 1

    failures = evaluate_live_deploy_preflight(
        overview,
        service_target="market-data",
        now=now,
    )

    assert any("broker account positions are still open" in item for item in failures)


def test_live_deploy_preflight_blocks_recent_fills() -> None:
    now = datetime(2026, 3, 30, 14, 0, tzinfo=UTC)
    overview = _healthy_overview(now)
    overview["recent_fills"] = [
        {"filled_at": _datetime_str(now - timedelta(seconds=30))},
    ]

    failures = evaluate_live_deploy_preflight(
        overview,
        service_target="strategy",
        now=now,
    )

    assert any("fills were recorded" in item for item in failures)


def test_live_deploy_preflight_blocks_critical_reconciliation_findings() -> None:
    now = datetime(2026, 3, 30, 14, 0, tzinfo=UTC)
    overview = _healthy_overview(now)
    overview["reconciliation"]["latest_run"]["summary"]["critical_findings"] = 1

    failures = evaluate_live_deploy_preflight(
        overview,
        service_target="oms",
        now=now,
    )

    assert any("critical findings" in item for item in failures)


def test_live_deploy_preflight_blocks_stale_or_unhealthy_services() -> None:
    now = datetime(2026, 3, 30, 14, 0, tzinfo=UTC)
    overview = _healthy_overview(now)
    overview["services"][1]["status"] = "degraded"
    overview["services"][2]["observed_at"] = _datetime_str(now - timedelta(seconds=600))

    failures = evaluate_live_deploy_preflight(
        overview,
        service_target="market-data",
        now=now,
    )

    assert any("not healthy" in item for item in failures)
    assert any("stale" in item for item in failures)


def test_live_deploy_preflight_uses_overview_status_for_control_plane() -> None:
    now = datetime(2026, 3, 30, 14, 0, tzinfo=UTC)
    overview = _healthy_overview(now)
    overview["status"] = "degraded"

    failures = evaluate_live_deploy_preflight(
        overview,
        service_target="strategy",
        now=now,
    )

    assert any("control-plane overview endpoint is not healthy" in item for item in failures)


def test_parse_datetime_accepts_control_plane_eastern_format() -> None:
    parsed = parse_datetime("2026-03-30 07:10:07 AM ET")

    assert parsed is not None
    assert parsed == datetime(2026, 3, 30, 11, 10, 7, tzinfo=UTC)
