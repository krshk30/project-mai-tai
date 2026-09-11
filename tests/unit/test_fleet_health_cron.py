from __future__ import annotations

import os
from pathlib import Path
import subprocess


WRAPPER = Path(__file__).resolve().parents[2] / "ops" / "health" / "fleet_health_cron.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_wrapper(
    tmp_path: Path, output: str, *, check_exit: int = 2, curl_exit: int = 0
) -> subprocess.CompletedProcess[str]:
    check = tmp_path / "check.sh"
    calls = tmp_path / "curl.calls"
    fake_curl = tmp_path / "curl.sh"
    _write_executable(
        check,
        f"#!/bin/bash\nprintf '%b' {output!r}\nexit {check_exit}\n",
    )
    _write_executable(
        fake_curl,
        f"#!/bin/bash\nprintf 'CALL\\n%s\\n' \"$*\" >> {str(calls)!r}\nexit {curl_exit}\n",
    )
    env = {
        **os.environ,
        "FLEET_HEALTH_CHECK": str(check),
        "FLEET_HEALTH_PYTHON": "/bin/bash",
        "FLEET_HEALTH_CURL": str(fake_curl),
        "FLEET_HEALTH_NTFY_URL": "https://example.invalid/topic",
        "FLEET_HEALTH_OUT": str(tmp_path / "state"),
        "FLEET_HEALTH_TEST_MODE": "1",
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


def test_monitor_error_is_transition_deduped(tmp_path: Path) -> None:
    _run_wrapper(tmp_path, "broken output\n", check_exit=1)
    _run_wrapper(tmp_path, "broken output\n", check_exit=1)

    calls = _call_log(tmp_path)
    assert calls.count("CALL") == 1
    assert "health-check ERROR" in calls
