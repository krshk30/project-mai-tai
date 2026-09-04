from __future__ import annotations

from datetime import UTC, datetime, timedelta
import http.server
import socketserver
import threading
import time

import pytest

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
