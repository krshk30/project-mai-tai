from copy import deepcopy
import importlib.util
from pathlib import Path
import subprocess
import pytest

spec = importlib.util.spec_from_file_location("upgrade_ack", Path(__file__).with_name("upgrade_ack.py"))
ack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ack)


def record():
    return dict(application=ack.APP, unit="project-mai-tai-orb-schwab.service",
        decision="ACKNOWLEDGED_REDIS_UPGRADE_RESTART", redis_restart_utc="2026-10-07T06:31:08+00:00",
        redis_package="7.0.15-1ubuntu0.24.04.5", state=deepcopy(ack.EXPECTED), source="recorded systemd+apt receipts")


def test_recorded_upgrade_identity_is_acknowledged_without_clearing_counter():
    result = ack.validate(record(), deepcopy(ack.EXPECTED), ack.APP)
    assert result["state"]["NRestarts"] == "1"


@pytest.mark.parametrize("field", list(ack.EXPECTED))
def test_each_changed_live_identity_field_refuses(field):
    state = deepcopy(ack.EXPECTED)
    state[field] = "different"
    with pytest.raises(ValueError):
        ack.validate(record(), state, ack.APP)


@pytest.mark.parametrize("field", ["application", "unit", "decision", "redis_restart_utc", "redis_package", "state"])
def test_changed_ack_record_refuses(field):
    value = record()
    value[field] = "different"
    with pytest.raises(ValueError):
        ack.validate(value, ack.EXPECTED, ack.APP)


def raw(extra="", unknown=""):
    return ("Overall: FAIL\nFinal call: FAIL; failure\n"
        "| Restarted services | actual NRestarts orb-schwab=1 | FAIL |\n"
        "| Bar continuity | after hours | N/A_OFF_SESSION |\n" + unknown + "\nFailures:\n"
        "- orb-schwab did not return active/running on a new PID\n" + extra)


def test_acknowledged_restart_report_discloses_actual_counter_not_pass():
    rendered, allowed = ack.render(raw(), ack.validate(record(), ack.EXPECTED, ack.APP))
    assert allowed and "Final call: EXPECTED BY DESIGN;" in rendered
    assert "NRestarts orb-schwab=1" in rendered and '"NRestarts": "1"' in rendered
    assert "not all NRestarts zero" in rendered


@pytest.mark.parametrize("extra,unknown", [("- another failure\n", ""), ("", "\nUnknowns:\n- missing\n"),
    ("", "| Another | missing | UNKNOWN |\n"), ("", "| Another | bad | FAIL |\n")])
def test_ack_does_not_waive_other_failure_or_unknown(extra, unknown):
    value = raw(extra, unknown)
    assert ack.render(value, record()) == (value, False)


def test_git_exact_directory_not_global_trust(tmp_path):
    result = subprocess.run(["git", "-c", "safe.directory=/home/trader/project-mai-tai", "config", "--get-all", "safe.directory"],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"})
    assert result.returncode == 0 and result.stdout.strip() == "/home/trader/project-mai-tai"


def test_only_redis_packages_added_to_upgrade_exclusion():
    text = Path(__file__).with_name("redis-hold.conf").read_text()
    assert '"^redis-server$";' in text and '"^redis-tools$";' in text
    assert "postgres" not in text and "^redis-.*$" not in text


def test_gate_transform_changes_only_acknowledged_orb_identity_and_check():
    spec = importlib.util.spec_from_file_location("morning_install", Path(__file__).with_name("install.py"))
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["upgrade_ack"] = ack
    spec.loader.exec_module(module)
    original = ('EXPECTED_SHA=unchanged\nEXPECTED_PID=626439\nEXPECTED_ORB_SCHWAB_PID=612486\n'
        "EXPECTED_ORB_SCHWAB_START='Tue 2026-10-06 23:50:33 UTC'\n"
        'check_identity orb-schwab "$ORB_SCHWAB_UNIT" "$EXPECTED_ORB_SCHWAB_PID" "$EXPECTED_ORB_SCHWAB_START"\n')
    result = module.gate_text(original)
    assert 'EXPECTED_SHA=unchanged\nEXPECTED_PID=626439\n' in result
    assert 'EXPECTED_ORB_SCHWAB_PID=765206' in result and 'upgrade_ack.py' in result
    assert 'actual NRestarts=1' in result
    with pytest.raises(ValueError):
        module.gate_text(original.replace('612486', 'other'))
    assert subprocess.run(['bash', '-n'], input=result, text=True).returncode == 0
