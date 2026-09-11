from __future__ import annotations

import os
from pathlib import Path
import subprocess


WRAPPER = Path(__file__).resolve().parents[2] / "ops" / "health" / "reconcile_alert_cron.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run(tmp_path: Path, rows: str, *, curl_exit: int = 0) -> subprocess.CompletedProcess[str]:
    calls = tmp_path / "curl.calls"
    fake_curl = tmp_path / "curl.sh"
    _write_executable(
        fake_curl,
        f"#!/bin/bash\nprintf 'CALL\\n%s\\n' \"$*\" >> {str(calls)!r}\nexit {curl_exit}\n",
    )
    env = {
        **os.environ,
        "RECONCILE_ALERT_OUT": str(tmp_path / "state"),
        "RECONCILE_ALERT_CURL": str(fake_curl),
        "RECONCILE_ALERT_NTFY_URL": "https://example.invalid/topic",
        "RECONCILE_ALERT_TEST_MODE": "1",
        "RECONCILE_ALERT_ROWS": rows,
    }
    return subprocess.run(
        ["bash", str(WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _calls(tmp_path: Path) -> str:
    path = tmp_path / "curl.calls"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_active_reconciliation_incident_pages_once_then_recurs_after_resolution(
    tmp_path: Path,
) -> None:
    row = (
        "position-quantity:live:orb:TNON|position_quantity_mismatch|TNON|"
        "Our records claim a position missing at the broker|live:orb|"
        "broker_missing_owned_position|0|1|1"
    )

    _run(tmp_path, row)
    _run(tmp_path, row)
    assert _calls(tmp_path).count("CALL") == 1

    _run(tmp_path, "")
    assert _calls(tmp_path).count("CALL") == 1

    _run(tmp_path, row)
    calls = _calls(tmp_path)
    assert calls.count("CALL") == 2
    assert "net_fill_balance=1" in calls
    assert "--fail-with-body --connect-timeout 10 --max-time 30" in calls


def test_failed_delivery_retries_instead_of_consuming_the_transition(tmp_path: Path) -> None:
    row = (
        "position-quantity:live:schwab_1m_v2:X|position_quantity_mismatch|X|"
        "Owned mismatch|live:schwab_1m_v2|broker_more_than_net_fills|2|1|1"
    )

    _run(tmp_path, row, curl_exit=22)
    active = tmp_path / "state" / "paged.active"
    assert active.read_text(encoding="utf-8") == ""

    _run(tmp_path, row)
    assert _calls(tmp_path).count("CALL") == 2
    assert active.read_text(encoding="utf-8").strip() == "position-quantity:live:schwab_1m_v2:X"


def test_wrapper_has_no_manual_position_or_protected_symbol_escape_hatch() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "MAI_TAI_PROTECTED_SYMBOLS" not in source
    assert "severity='critical'" in source
    assert "broker_only_manual" not in source
    assert "('live:schwab_1m_v2','live:orb')" in source
