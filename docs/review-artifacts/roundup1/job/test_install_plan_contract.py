"""Read-only candidate binding, never a staged release or live FLAGGATE."""
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[4]
APP = "4805ddc81184c76b4d5cef5c483c809edb666fe6"
TREE = "4248057079864f93066a69f355e2607840c701b9"
PLAN = ROOT / "docs/review-artifacts/roundup1/INSTALL_PLAN_2026-10-06.md"
PREOPEN = ROOT / "docs/review-artifacts/preopen-daily/PLAN_2026-10-06.md"


def candidate_file(path):
    return subprocess.run(["git", "show", APP + ":" + path], cwd=ROOT,
                          check=True, capture_output=True, text=True, timeout=5).stdout


def test_plan_and_preopen_bind_exact_candidate_tree_not_moving_main():
    tree = subprocess.run(["git", "show", "-s", "--format=%T", APP], cwd=ROOT,
                          check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    assert tree == TREE
    for path in (PLAN, PREOPEN):
        document = path.read_text()
        assert APP in document and TREE in document
        assert "UNREADY" in document
        assert "FLAGGATE151" in document and "FLAGGATE149" not in document


def test_candidate_catalog_has_actual151_process_checks_including_both_orb_consumers():
    flags = json.loads(candidate_file("ops/health/expected_flags.json"))["flags"]
    numeric = json.loads(candidate_file("ops/health/expected_numeric.json"))["settings"]
    def checks(rows):
        return sum(1 + len(row.get("also_check_services", [])) for row in rows)
    assert len(flags) == 130
    assert checks(flags) == 143
    assert checks(numeric) == 8
    assert checks(flags) + checks(numeric) == 151
    entry = next(row for row in flags if row["name"] == "orb_schwab_atr_entry_gate_enabled")
    assert entry["expected"] is True
    assert entry["owning_service"] == "orb-schwab"
    assert entry["also_check_services"] == ["oms"]


def test_install_scope_is_exact_three_new_env_keys_and_three_processes():
    document = PLAN.read_text()
    keys = re.findall(r"`(MAI_TAI_[A-Z0-9_]+)`", document)
    assert set(keys) == {
        "MAI_TAI_STRATEGY_SCHWAB_1M_V2_RESTING_BUY_ROUND_UP_ENABLED",
        "MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED",
        "MAI_TAI_ORB_SCHWAB_ATR_ENTRY_GATE_ENABLED",
    }
    assert "v2, OMS and orb-schwab exactly once" in document
    assert "stop v2 -> stop orb-schwab -> stop OMS -> start OMS once ->\nstart orb-schwab once -> start v2 once" in document
    assert "T43 waits" in document and "MIRRORHOLD Step0" in document
    assert "MainPID=0" in document and "exact reviewed cause" in document
    assert "do not invoke that clock override with a residual holding" in document
    assert "Positive live activation is **UNMEASURED**" in document
    assert "8cd374a1f7b194745b185934e827e374b06a4093df0b97b49122d608a0e9d4ad" in document


def test_daily_timer_is_checks_only_and_never_starts_application_units():
    document = PREOPEN.read_text()
    assert "Persistent=false" in document and "06:20:00 America/New_York" in document
    assert "the combined five-active-item plan" in document
    assert "start" in document and "**the timer only**" in document
    assert "No Wants/Requires dependency on an application unit" in document
    assert "Date/paper changes do not authorize any other identity refresh" in document
