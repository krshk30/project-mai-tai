from __future__ import annotations

from datetime import date
from pathlib import Path
import subprocess
import sys

import pytest

from project_mai_tai.momwarn_health_contract import OmsLogMonitor, proposed_health_v2
from project_mai_tai.momwarn_measurements import NS, session_window

NOW = session_window(date(2026, 10, 13))[0]


def receipt(age=15):
    return dict(observed_wall_ns=NOW, oms_measured_wall_ns=NOW - age * NS,
                oms_instance_id="CONTROLLED-oms-instance",
                oms_sync_seq=10, oms_sync_seconds=2.3, oms_sync_interval_seconds=15,
                oms_pass_allowance_seconds=5, oms_cadence_source="CONTROLLED-live-setting-receipt",
                oms_source="OMS-BROKER-SYNC-PASS.phase=end",
                oms_monitor_coverage="complete_since_baseline",
                v2_applicability="NOT_APPLICABLE", v2_applicability_reason="sourced_pre0700",
                v2_source_lane="schwab_anchored_current_day",
                v2_expectation_kind="recorded_first_same_day_0700",
                v2_expected_start_et="07:00", v2_expectation_ref="CONTROLLED-recorded-card")


@pytest.mark.parametrize("age", [6, 15, 20])
def test_normal_live15_cadence_preserves_end_stamp_and_fresh_monitor(age):
    payload = proposed_health_v2(receipt(age), now_ns=NOW)
    assert payload["schema_version"] == 2 and payload["oms_source_age_seconds"] == age
    assert payload["observed_wall_ns"] == NOW
    assert payload["oms_measured_wall_ns"] == NOW - age * NS
    assert payload["v2_bar_lateness_seconds"] is None


@pytest.mark.parametrize("patch", [
    {"oms_measured_wall_ns": NOW - 20 * NS - 1}, {"observed_wall_ns": NOW - 6 * NS},
    {"oms_monitor_coverage": "unreadable"}, {"oms_cadence_source": ""},
    {"v2_expectation_ref": ""}, {"v2_source_lane": "unknown"},
    {"v2_applicability": "UNKNOWN"},
])
def test_stalled_unreadable_and_unsourced_no_bar_not_promoted(patch):
    values = receipt()
    values.update(patch)
    with pytest.raises(ValueError):
        proposed_health_v2(values, now_ns=NOW)


def test_pre07_expiry_and_positive_empty_watchlist_proof_not_absence():
    at07 = NOW + (2 * 3600 + 49 * 60) * NS
    values = receipt(0)
    values.update(observed_wall_ns=at07, oms_measured_wall_ns=at07)
    with pytest.raises(ValueError):
        proposed_health_v2(values, now_ns=at07)
    values.update(v2_applicability_reason="sourced_empty_watchlist", v2_watchlist=[],
                  v2_watchlist_source="strategy-state-isolated", v2_watchlist_complete=True,
                  v2_watchlist_snapshot_ns=at07)
    assert proposed_health_v2(values, now_ns=at07)["v2_bar_lateness_seconds"] is None
    values["v2_watchlist"] = None
    with pytest.raises(ValueError):
        proposed_health_v2(values, now_ns=at07)


def test_all_applicable_lateness_retained_not_waived():
    values = receipt()
    values.update(v2_applicability="APPLICABLE", v2_source="completed_bar_due_monitor",
                  v2_instance_id="CONTROLLED-v2-instance",
                  v2_coverage="complete_current_due", v2_measured_wall_ns=NOW,
                  v2_due_wall_ns=NOW - 7 * NS, v2_completed_wall_ns=None,
                  v2_bar_lateness_seconds=7)
    assert proposed_health_v2(values, now_ns=NOW)["v2_bar_lateness_seconds"] == 7
    values["v2_coverage"] = "one_symbol_only"
    with pytest.raises(ValueError):
        proposed_health_v2(values, now_ns=NOW)


def test_actual_reader_uses_log_duration_not_mtime_and_proc_binding(tmp_path):
    process = tmp_path / "12"
    process.mkdir()
    fields = ["0"] * 20
    fields[0], fields[19] = "S", "100"
    (process / "stat").write_text("12 (oms) " + " ".join(fields))
    log = tmp_path / "oms.log"
    log.write_text("2026-10-13 08:10:45,000 INFO [oms-risk] [OMS-BROKER-SYNC-PASS] "
                   "id=10 phase=end outcome=ok duration_ms=2300.000 exceeds_note_age=False\n")
    reader = OmsLogMonitor(log, 12, verified_timezone="UTC", cadence_seconds=15,
                           cadence_source="CONTROLLED-live15", wall_ns=lambda: NOW, proc=tmp_path)
    first = reader.read()
    assert first["oms_sync_seconds"] == 2.3
    assert first["oms_measured_wall_ns"] == NOW - 15 * NS
    assert first["oms_instance_id"] == "12:100"
    reader.wall_ns = lambda: NOW + NS
    second = reader.read()
    assert second["observed_wall_ns"] == NOW + NS
    assert second["oms_measured_wall_ns"] == first["oms_measured_wall_ns"]
    log.unlink()
    with pytest.raises(OSError):
        reader.read()


def test_nonoperational_v1_cli_cannot_launch_or_write_health(tmp_path):
    root = Path(__file__).parents[2]
    output = tmp_path / "health.json"
    result = subprocess.run([sys.executable, str(root / "scripts/momwarn1_health.py"),
                             "--input", str(tmp_path / "input.json"), "--output", str(output)],
                            cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "NOT READY" in result.stderr
    assert not output.exists()
