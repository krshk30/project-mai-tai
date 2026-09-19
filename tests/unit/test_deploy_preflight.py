from __future__ import annotations

from datetime import UTC, datetime, timedelta
import http.server
import socketserver
import sys
import threading
import time

import pytest

import project_mai_tai.deploy_preflight as deploy_preflight
from project_mai_tai.deploy_preflight import (
    _PREFLIGHT_HTTP_TIMEOUT_SECONDS,
    evaluate_live_deploy_preflight,
    load_json,
    parse_datetime,
)


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
        "errors": [],
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
                    "total_findings": 0,
                    "critical_findings": 0,
                }
            }
        },
        "services": service_rows,
    }


def _real_degraded_overview() -> dict:
    """Safety-field projection captured from the live box on 2026-09-18 at 16:39 ET."""
    services = [
        {
            "service_name": "market-data-gateway",
            "status": "healthy",
            "effective_status": "healthy",
            "observed_at_raw": "2026-09-18T20:39:45.163200Z",
            "details": {},
        },
        {
            "service_name": "momentum-paper",
            "status": "degraded",
            "effective_status": "degraded",
            "observed_at_raw": "2026-09-18T20:39:48+00:00",
            "details": {
                "execution_mode": "paper",
                "broker_route": "none",
                "streamer_connected": "false",
                "feed_reason": "feed_policy_violation",
                "consecutive_policy_violations": "13",
            },
        },
        {
            "service_name": "oms-risk",
            "status": "healthy",
            "effective_status": "healthy",
            "observed_at_raw": "2026-09-18T20:39:51.985579Z",
            "details": {},
        },
        {
            "service_name": "orb",
            "status": "healthy",
            "effective_status": "healthy",
            "observed_at_raw": "2026-09-18T20:39:49.883754Z",
            "details": {"execution_mode": "paper", "broker_route": "none"},
        },
        {
            "service_name": "reconciler",
            "status": "healthy",
            "effective_status": "healthy",
            "observed_at_raw": "2026-09-18T20:39:43.034111Z",
            "details": {},
        },
        {
            "service_name": "schwab-1m-v2",
            "status": "healthy",
            "effective_status": "healthy",
            "observed_at_raw": "2026-09-18T20:39:47.220857Z",
            "details": {},
        },
        {
            "service_name": "strategy-engine",
            "status": "healthy",
            "effective_status": "healthy",
            "observed_at_raw": "2026-09-18T20:39:44.174956Z",
            "details": {},
        },
    ]
    return {
        "generated_at": "2026-09-18 04:39:53 PM ET",
        "status": "degraded",
        "errors": [],
        "counts": {
            "strategies": 10,
            "broker_accounts": 12,
            "pending_intents": 0,
            "recent_fills": 20,
            "open_virtual_positions": 0,
            "open_account_positions": 0,
            "open_incidents": 6,
            "latest_reconciliation_findings": 0,
            "blacklisted_symbols": 0,
        },
        "recent_intents": [],
        "recent_fills": [],
        "reconciliation": {
            "latest_run": {
                "status": "completed",
                "summary": {
                    "total_findings": 0,
                    "critical_findings": 0,
                },
            }
        },
        "services": services,
    }


def _service(overview: dict, name: str) -> dict:
    return next(row for row in overview["services"] if row["service_name"] == name)


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


def test_degraded_paper_observer_is_non_blocking_with_a_loud_warning() -> None:
    now = datetime(2026, 9, 18, 20, 40, tzinfo=UTC)
    overview = _real_degraded_overview()
    warnings: list[str] = []

    failures = evaluate_live_deploy_preflight(
        overview,
        service_target="oms",
        now=now,
        warnings=warnings,
    )

    assert failures == []
    assert warnings == [
        "NON-BLOCKING paper observer: momentum-paper status=degraded reason=feed_policy_violation"
    ]


def test_paper_observer_without_broker_route_blocks() -> None:
    now = datetime(2026, 9, 18, 20, 40, tzinfo=UTC)
    overview = _real_degraded_overview()
    _service(overview, "momentum-paper")["details"].pop("broker_route")

    failures = evaluate_live_deploy_preflight(overview, service_target="oms", now=now)

    assert any("service momentum-paper is not healthy" in item for item in failures)


def test_live_service_cannot_use_the_paper_observer_exception() -> None:
    now = datetime(2026, 9, 18, 20, 40, tzinfo=UTC)
    overview = _real_degraded_overview()
    _service(overview, "momentum-paper")["details"]["execution_mode"] = "live"

    failures = evaluate_live_deploy_preflight(overview, service_target="oms", now=now)

    assert any("service momentum-paper is not healthy" in item for item in failures)


def test_degraded_oms_still_blocks_even_if_it_claims_paper_observer() -> None:
    now = datetime(2026, 9, 18, 20, 40, tzinfo=UTC)
    overview = _real_degraded_overview()
    oms = _service(overview, "oms-risk")
    oms["status"] = "degraded"
    oms["effective_status"] = "degraded"
    oms["details"] = {"execution_mode": "paper", "broker_route": "none"}

    failures = evaluate_live_deploy_preflight(overview, service_target="oms", now=now)

    assert any("target service oms-risk is not healthy" in item for item in failures)


def test_total_reconciliation_findings_still_block() -> None:
    now = datetime(2026, 9, 18, 20, 40, tzinfo=UTC)
    overview = _real_degraded_overview()
    overview["reconciliation"]["latest_run"]["summary"]["total_findings"] = 1

    failures = evaluate_live_deploy_preflight(overview, service_target="oms", now=now)

    assert any("1 total findings" in item for item in failures)


def test_overview_errors_still_block() -> None:
    now = datetime(2026, 9, 18, 20, 40, tzinfo=UTC)
    overview = _real_degraded_overview()
    overview["errors"] = ["redis:heartbeats:connection refused"]

    failures = evaluate_live_deploy_preflight(overview, service_target="oms", now=now)

    assert any("redis:heartbeats:connection refused" in item for item in failures)


def test_missing_overview_errors_field_blocks_as_unreadable() -> None:
    now = datetime(2026, 9, 18, 20, 40, tzinfo=UTC)
    overview = _real_degraded_overview()
    overview.pop("errors")

    failures = evaluate_live_deploy_preflight(overview, service_target="oms", now=now)

    assert "control-plane overview errors are unreadable before deploy." in failures


def test_unknown_degraded_service_without_capability_details_blocks() -> None:
    now = datetime(2026, 9, 18, 20, 40, tzinfo=UTC)
    overview = _real_degraded_overview()
    overview["services"].append(
        {
            "service_name": "future-service",
            "status": "degraded",
            "observed_at_raw": "2026-09-18T20:39:45+00:00",
        }
    )

    failures = evaluate_live_deploy_preflight(overview, service_target="oms", now=now)

    assert any("service future-service is not healthy" in item for item in failures)


def test_stale_paper_observer_blocks_because_its_capability_claim_is_stale() -> None:
    now = datetime(2026, 9, 18, 20, 40, tzinfo=UTC)
    overview = _real_degraded_overview()
    _service(overview, "momentum-paper")["observed_at_raw"] = "2026-09-18T20:30:00+00:00"
    warnings: list[str] = []

    failures = evaluate_live_deploy_preflight(
        overview,
        service_target="oms",
        now=now,
        warnings=warnings,
    )

    assert any("heartbeat for momentum-paper is stale (600s old)" in item for item in failures)
    assert warnings == []


def test_cli_prints_the_non_blocking_paper_observer_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    overview = _real_degraded_overview()
    monkeypatch.setattr(deploy_preflight, "load_json", lambda _url: overview)
    monkeypatch.setattr(
        deploy_preflight,
        "utcnow",
        lambda: datetime(2026, 9, 18, 20, 40, tzinfo=UTC),
    )
    monkeypatch.setattr(sys, "argv", ["deploy_preflight", "--service", "oms"])

    assert deploy_preflight.main() == 0
    output = capsys.readouterr().out
    assert (
        "NON-BLOCKING paper observer: momentum-paper "
        "status=degraded reason=feed_policy_violation" in output
    )
    assert "Live deploy preflight passed for oms." in output


def test_parse_datetime_accepts_control_plane_eastern_format() -> None:
    parsed = parse_datetime("2026-03-30 07:10:07 AM ET")

    assert parsed is not None
    assert parsed == datetime(2026, 3, 30, 11, 10, 7, tzinfo=UTC)


# ═══════════════════════════════════════════════════════════════════════════════════════════
# 2026-09-04 deploy: the fail-closed gate refused on LATENCY, not on a safety predicate.
# A cold /api/overview measured 5.5-6.7s against a 5.0s budget, so the deploy could only be made
# to pass by warming the endpoint by hand and re-running.
# ⛔ Both failures below still FAIL CLOSED — that was never in doubt. What was wrong is that the
# ROUTINE failure (slow) escaped as an unhandled TimeoutError while the SERIOUS one (unreachable)
# produced the clean message. A gate whose common failure looks like a crash gets retried until
# it goes green, which is how a safety gate stops being one.
# ═══════════════════════════════════════════════════════════════════════════════════════════


def _slow_server(delay: float):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            time.sleep(delay)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_a_slow_control_plane_raises_a_CLEAN_timeout_message_not_a_traceback() -> None:
    """⛔ A read timeout is NOT a URLError. Before the fix it escaped unhandled."""
    srv = _slow_server(2.0)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/overview"
        with pytest.raises(SystemExit) as exc:
            load_json(url, timeout_seconds=0.5)
        msg = str(exc.value)
        assert "TIMED OUT" in msg
        assert "not the same as it being" in msg.lower() or "NOT the same" in msg
    finally:
        srv.shutdown()


def test_an_unreachable_control_plane_says_UNREACHABLE_not_slow() -> None:
    """⛔ The two must never collapse into one message: 'slow' invites a retry, 'down' must not."""
    with pytest.raises(SystemExit) as exc:
        load_json("http://127.0.0.1:9/api/overview", timeout_seconds=0.5)
    msg = str(exc.value)
    assert "UNREACHABLE" in msg
    assert "TIMED OUT" not in msg


def test_the_timeout_budget_covers_the_measured_cold_latency() -> None:
    """PINNED. Cold /api/overview measured 5.5-6.7s on 2026-09-04; the old budget was 5.0s.

    ⛔ If anyone lowers this back under the measured cold worst case, the gate starts refusing on
    latency again and the hand-warming workaround comes back with it.
    """
    assert _PREFLIGHT_HTTP_TIMEOUT_SECONDS >= 10.0, (
        "the budget must clear the measured 6.7s cold read with headroom"
    )


def test_a_healthy_endpoint_still_loads() -> None:
    """The control: the gate must still READ the overview, not just fail politely."""
    srv = _slow_server(0.0)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/overview"
        assert load_json(url, timeout_seconds=5.0) == {}
    finally:
        srv.shutdown()
