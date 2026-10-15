from datetime import datetime
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "ops/bootstrap/maintenance-window"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_helper():
    from importlib.machinery import SourceFileLoader
    spec = importlib.util.spec_from_loader("maintenance", SourceFileLoader(
        "maintenance", str(CONFIG / "project-mai-tai-maintenance")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


maintenance = load_helper()
probe = load("pending_probe", ROOT / "ops/health/maintenance_pending_check.py")
ET = ZoneInfo("America/New_York")


def now(stamp="2026-10-17T00:30:00"):
    return datetime.fromisoformat(stamp).replace(tzinfo=ET)


@pytest.mark.parametrize("stamp,allowed", [
    ("2026-10-16T23:59:59", False), ("2026-10-17T00:00:00", True),
    ("2026-10-17T00:59:59", True), ("2026-10-17T01:00:00", False),
    ("2026-10-19T00:30:00", False), ("2026-11-07T00:30:00", True),
])
def test_explicit_et_window_and_boundary(stamp, allowed):
    assert maintenance.in_window(now(stamp)) is allowed
    assert maintenance.in_window(now(stamp).astimezone(ZoneInfo("UTC"))) is allowed


@pytest.mark.parametrize("service", ["redis-server", "redis.service", "postgresql@16-main.service",
                                     "project-mai-tai-oms.service"])
def test_maintainer_restart_denied_outside_window_and_fullscan_flags_it(tmp_path, service):
    flag = tmp_path / "pending-restart.json"
    assert maintenance.policy([service, "restart"], now("2026-10-15T02:31:00"), flag=flag) == 101
    code, message = probe.check(flag, tmp_path / "no-reboot")
    assert code == 1 and "PENDING_RESTART_FLAG" in message
    assert json.loads(flag.read_text())["services"] == [service]


def test_window_and_explicit_attended_actions_keep_operator_path(tmp_path):
    flag = tmp_path / "flag"
    assert maintenance.policy(["redis-server", "restart"], now(), flag=flag) == 0
    assert maintenance.policy(["redis-server", "status"], now("2026-10-15T02:31:00"), flag=flag) == 0
    assert maintenance.policy(["redis-server", "restart"], now("2026-10-15T02:31:00"),
                              attended=True, flag=flag) == 0
    assert not flag.exists()


@pytest.mark.parametrize("stamp,allowed", [
    ("2026-10-17T00:30:00", True), ("2026-10-17T01:00:00", False),
])
def test_actual_entrypoint_gates_install_before_package_action(monkeypatch, stamp, allowed):
    class Clock:
        @classmethod
        def now(cls, _tz):
            return now(stamp)
    recorded = []
    monkeypatch.setattr(maintenance, "datetime", Clock)
    monkeypatch.delenv("MAI_TAI_MAINTENANCE_ATTENDED", raising=False)
    monkeypatch.setattr(maintenance, "record_pending", lambda reason, **kw: recorded.append(reason))
    assert maintenance.main(["window"]) == (0 if allowed else 1)
    assert maintenance.main(["install"]) == (0 if allowed else 1)
    assert recorded == ([] if allowed else ["package_install_deferred"])


@pytest.mark.parametrize("output,rc,reason", [
    ("NEEDRESTART-SVC: redis-server.service\n", 0, "restart_required"),
    ("", 1, "restart_scan_UNKNOWN"),
])
def test_list_only_scan_records_pending_without_service_action(tmp_path, output, rc, reason):
    def run(args, **kwargs):
        assert args == ["needrestart", "-b", "-r", "l"]
        assert kwargs["env"]["NEEDRESTART_MODE"] == "l"
        return SimpleNamespace(stdout=output, returncode=rc)
    flag = tmp_path / "flag"
    maintenance.audit_pending(now(), flag=flag, run=run, reboot=tmp_path / "no-reboot")
    assert json.loads(flag.read_text())["reason"] == reason


def test_boundary_overrun_is_flagged_without_killing_dpkg(tmp_path):
    flag = tmp_path / "flag"
    maintenance.audit_pending(now("2026-10-17T01:00:00"), flag=flag,
        run=lambda *a, **k: SimpleNamespace(stdout="", returncode=0), reboot=tmp_path / "no-reboot")
    assert json.loads(flag.read_text())["reason"] == "maintenance_window_overrun"


def test_probe_unknown_and_reboot_required_are_not_green(tmp_path):
    flag = tmp_path / "flag"
    reboot = tmp_path / "reboot"
    flag.write_text("bad json")
    assert probe.check(flag, reboot)[0] == 2
    flag.unlink()
    reboot.touch()
    assert probe.check(flag, reboot)[0] == 1
    reboot.unlink()
    assert probe.check(flag, reboot)[0] == 0


def test_configuration_has_downloads_clock_guard_no_catchup_and_no_reboot():
    apt = (CONFIG / "60-project-mai-tai-maintenance").read_text()
    timer = (CONFIG / "apt-daily-upgrade.timer.override.conf").read_text()
    condition = (CONFIG / "apt-daily-upgrade.service.override.conf").read_text()
    assert 'Download-Upgradeable-Packages "1"' in apt
    assert 'Automatic-Reboot "false"' in apt
    assert "DPkg::Pre-Invoke" in apt and " maintenance" not in condition
    assert "project-mai-tai-maintenance window" in condition
    assert "Sat *-*-* 00:00:00 America/New_York" in timer and "Persistent=false" in timer
    assert "policy" in (CONFIG / "policy-rc.d").read_text()
    need = (CONFIG / "zz-project-mai-tai-window.conf").read_text()
    assert "$nrconf{restart} = 'l'" in need and "postgresql" in need and "redis" in need
    assert "maintenance_pending_check.py" in (ROOT / "ops/health/fullscan-maintenance.patch").read_text()
