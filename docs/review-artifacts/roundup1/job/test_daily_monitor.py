"""Filesystem/lock/notification rehearsal with simulated root metadata and fake checker."""
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import daily
import release_policy as policy
from test_daily_release import NOW, report

JOB = Path(__file__).parent
LOCAL = Path(__file__).resolve().parents[4]


def setup(monkeypatch, tmp_path, *, gate_rc=0, adapter_rc=0, timeout=False, missing_report=False):
    root, repo, reports = tmp_path / "daily", tmp_path / "repo", tmp_path / "reports"
    for path in (root / "runs", repo / "ops/health", reports):
        path.mkdir(parents=True, exist_ok=True)
    gate = tmp_path / "gate.sh"
    gate.write_text("CONTROLLED gate, never run by a real shell")
    gate.chmod(0o700)
    adapter = repo / "ops/health/preopen_alert.sh"
    adapter.write_bytes(subprocess.check_output(["git", "show", policy.APP + ":ops/health/preopen_alert.sh"], cwd=LOCAL))
    installed = {}
    for name in ("daily.py", "release_policy.py"):
        (root / name).write_bytes((JOB / name).read_bytes())
        (root / name).chmod(0o600)
        installed[name] = policy.digest((root / name).read_bytes())
    manifest = dict(approved_sha=policy.APP, gate_uid=0, gate_sha256=policy.digest(gate.read_bytes()),
                    artifacts=installed, adapter_sha256=policy.ADAPTER, evidence_inputs={})
    (root / "runtime.json").write_bytes(policy.canonical(manifest))
    (root / "runtime.json").chmod(0o600)
    monkeypatch.setattr(daily, "ROOT", root)
    monkeypatch.setattr(daily, "REPO", repo)
    monkeypatch.setattr(daily, "GATE", gate)
    monkeypatch.setattr(daily.os, "geteuid", lambda: 0)
    original_stat = Path.stat
    def metadata(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == root or root in path.parents or path == gate:
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode, st_size=info.st_size, st_mtime=info.st_mtime)
        return info
    monkeypatch.setattr(Path, "stat", metadata)
    real_path = Path
    def paths(value):
        value = str(value)
        return reports if value == "/home/trader/known_defect_regression_watch" else real_path(value)
    monkeypatch.setattr(daily, "Path", paths)
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(daily, "datetime", Frozen)
    calls = []
    def command(args, **kwargs):
        args = list(map(str, args))
        calls.append(args)
        if args == ["bash", str(gate)]:
            if timeout:
                raise subprocess.TimeoutExpired(args, 240, output=b"CONTROLLED timeout partial output")
            if not missing_report:
                path = reports / "v2-restart-evidence-20261007.md"
                path.write_text(report(verdict={0: "PASS", 1: "FAIL", 2: "UNKNOWN"}[gate_rc]))
                os.utime(path, (NOW.timestamp(), NOW.timestamp()))
            return SimpleNamespace(returncode=gate_rc, stdout=b"CONTROLLED checker output", stderr=b"")
        if args[0] == "systemctl":
            return SimpleNamespace(returncode=0, stdout="Result=exit-code\nExecMainStatus=" + str(gate_rc)
                                   + "\nExecMainStartTimestamp=Wed 2026-10-07 10:20:00 UTC\n")
        assert args[0] == str(adapter) and args[1] == "ERROR"
        return SimpleNamespace(returncode=adapter_rc)
    monkeypatch.setattr(daily.subprocess, "run", command)
    return root, gate, calls


@pytest.mark.parametrize("rc", [0, 1, 2])
def test_literal_daily_gate_rc_report_receipt_and_onfailure_once(monkeypatch, tmp_path, rc):
    root, gate, calls = setup(monkeypatch, tmp_path, gate_rc=rc)
    assert daily.run() == rc
    latest = Path(json.loads((root / "latest.json").read_bytes())["run"])
    receipt = json.loads((latest / "complete.json").read_bytes())
    assert receipt["gate_rc"] == rc
    assert calls == [["bash", str(gate)]]
    if rc:
        assert daily.notify() == 0
        assert json.loads((latest / "notification-complete.json").read_bytes())["delivery_confirmed"]
        with pytest.raises(FileExistsError):
            daily.notify()
        assert len([args for args in calls if "ERROR" in args]) == 1


def test_adapter_failure_explicit_no_successful_delivery_latch(monkeypatch, tmp_path):
    root, _, calls = setup(monkeypatch, tmp_path, gate_rc=1, adapter_rc=1)
    assert daily.run() == 1 and daily.notify() == 1
    latest = Path(json.loads((root / "latest.json").read_bytes())["run"])
    assert json.loads((latest / "notification-complete.json").read_bytes()) == dict(adapter_rc=1, delivery_confirmed=False)
    assert len([args for args in calls if "ERROR" in args]) == 1


@pytest.mark.parametrize("option", [dict(timeout=True), dict(missing_report=True)])
def test_literal_timeout_or_missing_report_unknown_one_notification(monkeypatch, tmp_path, option):
    _, _, calls = setup(monkeypatch, tmp_path, **option)
    assert daily.run() == 2 and daily.notify() == 0
    assert len([args for args in calls if "ERROR" in args]) == 1


def test_overlapping_invocation_never_runs_checker(monkeypatch, tmp_path):
    root, _, calls = setup(monkeypatch, tmp_path)
    with (root / "run.lock").open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            daily.run()
    assert calls == []


@pytest.mark.parametrize("artifact", ["gate", "daily.py", "runtime.json"])
def test_pin_hash_or_parse_failure_still_notifies_unknown_once(monkeypatch, tmp_path, artifact):
    root, gate, calls = setup(monkeypatch, tmp_path)
    (gate if artifact == "gate" else root / artifact).write_text("CONTROLLED corrupt artifact")
    with pytest.raises(Exception):
        daily.run()
    assert daily.notify() == 0
    assert not any(args[0] == "bash" for args in calls)
    assert len([args for args in calls if "ERROR" in args]) == 1
    notice = next(args for args in calls if "ERROR" in args)
    assert "UNKNOWN" in notice[2]


def test_evidence_input_drift_refuses_gate(monkeypatch, tmp_path):
    root, _, calls = setup(monkeypatch, tmp_path)
    source = tmp_path / "isolated-catalog"
    source.write_text("CONTROLLED approved catalog")
    pin = json.loads((root / "runtime.json").read_bytes())
    pin["evidence_inputs"][str(source)] = policy.digest(source.read_bytes())
    (root / "runtime.json").write_bytes(policy.canonical(pin))
    assert daily.verify_runtime()["evidence_inputs"]
    source.write_text("CONTROLLED drift")
    with pytest.raises(policy.Stop):
        daily.run()
    assert calls == []
