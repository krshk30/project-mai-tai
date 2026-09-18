from __future__ import annotations

import os
from pathlib import Path
import subprocess


WRAPPER = Path(__file__).resolve().parents[2] / "ops" / "health" / "fleet_health_cron.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_wrapper(
    tmp_path: Path,
    output: str,
    *,
    check_exit: int = 2,
    curl_exit: int = 0,
    curl_status: int | None = None,
    mode: str = "full",
) -> subprocess.CompletedProcess[str]:
    check = tmp_path / "check.sh"
    check_args = tmp_path / "check.args"
    calls = tmp_path / "curl.calls"
    fake_curl = tmp_path / "curl.sh"
    status = (200 if curl_exit == 0 else 500) if curl_status is None else curl_status
    _write_executable(
        check,
        f"#!/bin/bash\nprintf '%s' \"$*\" > {str(check_args)!r}\n"
        f"printf '%b' {output!r}\nexit {check_exit}\n",
    )
    _write_executable(
        fake_curl,
        f"#!/bin/bash\nprintf 'CALL\\n%s\\n' \"$*\" >> {str(calls)!r}\n"
        f"printf '{{\"id\":\"fixture-receipt\"}}\\n__HTTP_STATUS__:{status}\\n'\n"
        f"exit {curl_exit}\n",
    )
    env = {
        **os.environ,
        "FLEET_HEALTH_CHECK": str(check),
        "FLEET_HEALTH_PYTHON": "/bin/bash",
        "FLEET_HEALTH_CURL": str(fake_curl),
        "FLEET_HEALTH_NTFY_URL": "https://example.invalid/topic",
        "FLEET_HEALTH_OUT": str(tmp_path / "state"),
        "FLEET_HEALTH_TEST_MODE": "1",
        "FLEET_HEALTH_MODE": mode,
    }
    return subprocess.run(
        ["bash", str(WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _call_log(tmp_path: Path) -> str:
    path = tmp_path / "curl.calls"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_paper_diagnostic_scoreboard_and_aggregate_never_page(tmp_path: Path) -> None:
    output = """VERDICT: RED strategy-bar-freshness class=PAPER stale
VERDICT: RED v2-bar-continuity class=DIAGNOSTIC gap
VERDICT: RED d6-outcome-acceptance class=SCOREBOARD stale
SUMMARY: RED fleet-function-health checks=3 live_money_red=0
"""

    result = _run_wrapper(tmp_path, output)

    assert result.returncode == 0
    assert _call_log(tmp_path) == ""


def test_live_money_pages_once_until_a_silent_recovery_then_pages_again(tmp_path: Path) -> None:
    red = """VERDICT: RED stops-armed class=LIVE_MONEY naked
SUMMARY: RED fleet-function-health checks=1 live_money_red=1
"""
    green = """VERDICT: GREEN stops-armed class=LIVE_MONEY all-armed
SUMMARY: GREEN fleet-function-health checks=1 live_money_red=0
"""

    _run_wrapper(tmp_path, red)
    _run_wrapper(tmp_path, red)
    assert _call_log(tmp_path).count("CALL") == 1

    _run_wrapper(tmp_path, green, check_exit=0)
    assert _call_log(tmp_path).count("CALL") == 1

    _run_wrapper(tmp_path, red)
    calls = _call_log(tmp_path)
    assert calls.count("CALL") == 2
    assert "stops-armed" in calls
    assert "fleet-function-health checks=" not in calls
    assert "--fail-with-body --connect-timeout 10 --max-time 30" in calls
    assert "-w" in calls and "__HTTP_STATUS__:%{http_code}" in calls
    assert "[NTFY-DELIVERY] accepted=1 http_status=200" in (
        tmp_path / "state" / "alert.log"
    ).read_text(encoding="utf-8")


def test_fleet_runtime_restart_storm_pages_while_paper_findings_stay_silent(
    tmp_path: Path,
) -> None:
    output = """VERDICT: RED service-runtime:momentum-paper:restart-storm class=FLEET_RUNTIME momentum-paper +5
VERDICT: RED strategy-bar-freshness class=PAPER stale
SUMMARY: RED fleet-function-health checks=2 live_money_red=0 fleet_runtime_red=1
"""

    result = _run_wrapper(tmp_path, output, mode="runtime")

    assert result.returncode == 0
    calls = _call_log(tmp_path)
    assert calls.count("CALL") == 1
    assert "RED mai-tai service runtime: service-runtime:momentum-paper:restart-storm" in calls
    assert "strategy-bar-freshness" not in calls
    assert (tmp_path / "check.args").read_text(encoding="utf-8") == "--runtime-only"


def test_existing_momentum_red_does_not_mask_a_later_oms_red(tmp_path: Path) -> None:
    momentum_red = """VERDICT: RED service-runtime:momentum-paper:inactive class=FLEET_RUNTIME stopped
VERDICT: GREEN service-runtime:oms:inactive class=FLEET_RUNTIME active
SUMMARY: RED fleet-function-health checks=2 live_money_red=0 fleet_runtime_red=1
"""
    both_red = """VERDICT: RED service-runtime:momentum-paper:inactive class=FLEET_RUNTIME stopped
VERDICT: RED service-runtime:oms:inactive class=FLEET_RUNTIME stopped
SUMMARY: RED fleet-function-health checks=2 live_money_red=0 fleet_runtime_red=2
"""

    _run_wrapper(tmp_path, momentum_red)
    _run_wrapper(tmp_path, both_red)

    calls = _call_log(tmp_path)
    deliveries = [row for row in calls.split("CALL\n") if row]
    assert len(deliveries) == 2
    assert "service-runtime:momentum-paper:inactive" in deliveries[0]
    assert "service-runtime:oms:inactive" not in deliveries[0]
    assert "service-runtime:oms:inactive" in deliveries[1]
    assert "service-runtime:momentum-paper:inactive" not in deliveries[1]


def test_momentum_cooloff_and_gateway_1008_are_independent_once_only_pages(
    tmp_path: Path,
) -> None:
    momentum_only = """VERDICT: RED service-runtime:momentum-paper:feed-policy-violation class=FLEET_RUNTIME new_matches=1
VERDICT: GREEN service-runtime:market-data:massive-1008 class=FLEET_RUNTIME new_matches=0
SUMMARY: RED fleet-function-health checks=2 live_money_red=0 fleet_runtime_red=1
"""
    both = """VERDICT: RED service-runtime:momentum-paper:feed-policy-violation class=FLEET_RUNTIME new_matches=0
VERDICT: RED service-runtime:market-data:massive-1008 class=FLEET_RUNTIME new_matches=1
SUMMARY: RED fleet-function-health checks=2 live_money_red=0 fleet_runtime_red=2
"""

    _run_wrapper(tmp_path, momentum_only, mode="runtime")
    _run_wrapper(tmp_path, momentum_only, mode="runtime")
    _run_wrapper(tmp_path, both, mode="runtime")

    deliveries = [row for row in _call_log(tmp_path).split("CALL\n") if row]
    assert len(deliveries) == 2
    assert "momentum-paper:feed-policy-violation" in deliveries[0]
    assert "market-data:massive-1008" not in deliveries[0]
    assert "market-data:massive-1008" in deliveries[1]
    assert "momentum-paper:feed-policy-violation" not in deliveries[1]
    receipts = (tmp_path / "state" / "alert.log").read_text(encoding="utf-8")
    assert receipts.count("[NTFY-DELIVERY] accepted=1") == 2


def test_oms_recovery_clears_only_oms_fingerprint_while_momentum_stays_red(
    tmp_path: Path,
) -> None:
    both_red = """VERDICT: RED service-runtime:momentum-paper:inactive class=FLEET_RUNTIME stopped
VERDICT: RED service-runtime:oms:inactive class=FLEET_RUNTIME stopped
SUMMARY: RED fleet-function-health checks=2 live_money_red=0 fleet_runtime_red=2
"""
    oms_green = """VERDICT: RED service-runtime:momentum-paper:inactive class=FLEET_RUNTIME stopped
VERDICT: GREEN service-runtime:oms:inactive class=FLEET_RUNTIME active
SUMMARY: RED fleet-function-health checks=2 live_money_red=0 fleet_runtime_red=1
"""

    _run_wrapper(tmp_path, both_red)
    _run_wrapper(tmp_path, oms_green)

    assert _call_log(tmp_path).count("CALL") == 2
    assert (tmp_path / "state" / "paged.active").read_text(encoding="utf-8").strip() == (
        "fleet-runtime:service-runtime:momentum-paper:inactive"
    )


def test_same_service_flap_pages_on_each_new_red_transition(tmp_path: Path) -> None:
    red = """VERDICT: RED service-runtime:oms:inactive class=FLEET_RUNTIME stopped
SUMMARY: RED fleet-function-health checks=1 live_money_red=0 fleet_runtime_red=1
"""
    green = """VERDICT: GREEN service-runtime:oms:inactive class=FLEET_RUNTIME active
SUMMARY: GREEN fleet-function-health checks=1 live_money_red=0 fleet_runtime_red=0
"""

    _run_wrapper(tmp_path, red)
    _run_wrapper(tmp_path, green, check_exit=0)
    _run_wrapper(tmp_path, red)

    assert _call_log(tmp_path).count("CALL") == 2


def test_failed_delivery_is_not_recorded_and_retries_next_run(tmp_path: Path) -> None:
    red = """VERDICT: RED oms-order-lifecycle class=LIVE_MONEY stuck
SUMMARY: RED fleet-function-health checks=1 live_money_red=1
"""

    _run_wrapper(tmp_path, red, curl_exit=22)
    active = tmp_path / "state" / "paged.active"
    assert active.read_text(encoding="utf-8") == ""

    _run_wrapper(tmp_path, red)
    assert _call_log(tmp_path).count("CALL") == 2
    assert active.read_text(encoding="utf-8").strip() == "live-money:oms-order-lifecycle"


def test_http_failure_is_not_recorded_even_when_curl_exits_zero(tmp_path: Path) -> None:
    red = """VERDICT: RED service-runtime:market-data:massive-1008 class=FLEET_RUNTIME new_matches=1
SUMMARY: RED fleet-function-health checks=1 live_money_red=0 fleet_runtime_red=1
"""

    _run_wrapper(tmp_path, red, curl_status=500, mode="runtime")

    active = tmp_path / "state" / "paged.active"
    assert active.read_text(encoding="utf-8") == ""
    alert = (tmp_path / "state" / "alert.log").read_text(encoding="utf-8")
    assert "[NTFY-DELIVERY] accepted=0 http_status=500" in alert
    assert "retry next run" in alert


def test_monitor_error_is_transition_deduped(tmp_path: Path) -> None:
    _run_wrapper(tmp_path, "broken output\n", check_exit=1)
    _run_wrapper(tmp_path, "broken output\n", check_exit=1)

    calls = _call_log(tmp_path)
    assert calls.count("CALL") == 1
    assert "health-check ERROR" in calls
