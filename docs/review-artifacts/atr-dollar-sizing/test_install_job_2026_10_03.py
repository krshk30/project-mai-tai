"""Offline checks: no SSH, broker, Redis, systemd or live install invocation."""
import ast
import hashlib
import importlib.util
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def receipt(tmp_path, monkeypatch):
    module = load("verify_install_approval.py")
    hashes = {}
    for name in module.ARTIFACTS:
        data = (ROOT / name).read_bytes()
        (tmp_path / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    release = {"application_sha": module.APPROVED_SHA, "plan_commit": "a" * 40, "artifacts": hashes}
    (tmp_path / "release.json").write_text(json.dumps(release))
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({**release, "reviewer": "claude-1", "decision": "APPROVED"}))
    original = Path.stat
    def fake_stat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == approval:
            return SimpleNamespace(st_uid=0, st_mode=0o100600)
        return value
    monkeypatch.setattr(Path, "stat", fake_stat)
    return module, tmp_path, datetime(2026, 10, 3, 11, tzinfo=ZoneInfo("America/New_York"))


def test_exact_review_allows_only_the_reviewed_artifacts(receipt):
    module, root, now = receipt
    assert module.verify(root, now) is True


def test_no_receipt_never_starts(receipt):
    module, root, now = receipt
    (root / "approval.json").unlink()
    assert module.verify(root, now) is False


@pytest.mark.parametrize("field,value", [("plan_commit", "b" * 40), ("application_sha", "b" * 40), ("reviewer", "codex-2"), ("decision", "PENDING")])
def test_review_mismatch_refuses(receipt, field, value):
    module, root, now = receipt
    path = root / "approval.json"
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    assert module.verify(root, now) is False


@pytest.mark.parametrize("name", sorted(load("verify_install_approval.py").ARTIFACTS))
def test_any_changed_artifact_refuses(receipt, name):
    module, root, now = receipt
    (root / name).write_text("tampered")
    assert module.verify(root, now) is False


def test_next_day_does_not_catch_up(receipt):
    module, root, now = receipt
    assert module.verify(root, now.replace(day=4)) is False


def test_empty_manifest_cannot_authorize(receipt):
    module, root, now = receipt
    for name in ("release.json", "approval.json"):
        path = root / name
        data = json.loads(path.read_text())
        data["artifacts"] = {}
        path.write_text(json.dumps(data))
    assert module.verify(root, now) is False


def good_identities(module, after=False):
    rows = {}
    for name, (pid, start) in module.EXPECTED.items():
        if after and name in module.RESTARTED:
            pid, start = str(int(pid) + 1000000), "Sat 2026-10-03 15:00:00 UTC"
        rows[name] = {"MainPID": pid, "ExecMainStartTimestamp": start,
                      "ActiveState": "active", "SubState": "running", "NRestarts": "0"}
    return rows


def test_pinned_preflight_and_three_new_identities():
    module = load("install_evidence_2026-10-03.py")
    module.verify_identities(good_identities(module))
    module.verify_identities(good_identities(module, after=True), after=True)


@pytest.mark.parametrize("name", ["market-data", "orb", "orb-schwab", "momentum-paper", "reconciler"])
def test_untouched_identity_drift_refuses(name):
    module = load("install_evidence_2026-10-03.py")
    rows = good_identities(module, after=True)
    rows[name]["MainPID"] = "999"
    with pytest.raises(AssertionError):
        module.verify_identities(rows, after=True)


@pytest.mark.parametrize("name", ["oms", "strategy", "schwab-1m-v2"])
def test_missing_restart_refuses(name):
    module = load("install_evidence_2026-10-03.py")
    rows = good_identities(module, after=True)
    rows[name]["MainPID"] = module.EXPECTED[name][0]
    with pytest.raises(AssertionError):
        module.verify_identities(rows, after=True)


def test_all_shell_and_embedded_python_parse():
    runner = (ROOT / "run_install_2026-10-03.sh").read_text()
    subprocess.run(["bash", "-n"], input=runner, text=True, check=True)
    plan = (ROOT / "INSTALL_PLAN_2026-10-03.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)```", plan, re.S)
    assert len(blocks) == 11
    for block in blocks:
        subprocess.run(["bash", "-n"], input=block, text=True, check=True)
    for source in [runner, *blocks]:
        for code in re.findall(r"<<'PY'[^\n]*\n(.*?)\nPY(?:\n|$)", source, re.S):
            ast.parse(code)
    for path in ROOT.glob("*2026-10-03.py"):
        ast.parse(path.read_text())


def test_runner_service_scope_and_review_barrier():
    runner = (ROOT / "run_install_2026-10-03.sh").read_text()
    actions = re.findall(r"^sudo systemctl (stop|restart|start) (project-mai-tai-[^\s]+)", runner, re.M)
    assert actions == [("stop", "project-mai-tai-schwab-1m-v2.service"),
                       ("stop", "project-mai-tai-strategy.service"),
                       ("restart", "project-mai-tai-oms.service"),
                       ("start", "project-mai-tai-schwab-1m-v2.service"),
                       ("start", "project-mai-tai-strategy.service")]
    assert runner.index('"$JOB/verify_install_approval.py"') < runner.index('sudo mkdir -m 0700 "$RUN"')
    assert runner.index('"$EVIDENCE" preflight') < runner.index("p = Path('/etc/project-mai-tai/project-mai-tai.env')")
    assert "flat_now\nv2_restart_gate\nsudo systemctl stop project-mai-tai-schwab-1m-v2.service" in runner
    assert "V2_ARM_OVERRIDE=''" in runner
    assert "Final call: PASS; checked=7/7" in runner
    assert "MAI_TAI_OMS_V2_WEBULL_MIRROR_FRESH_PRICE_ENABLED': 'true'" in runner
    assert "MAI_TAI_OMS_V2_WEBULL_MIRROR_QUOTE_MAX_AGE_MS': '10000'" in runner
    timer = (ROOT / "project-mai-tai-sizing-nfq1-install-20261003.timer").read_text()
    assert "OnCalendar=2026-10-03 *:*:00 America/New_York" in timer
    assert "Persistent=false" in timer
    unit = (ROOT / "project-mai-tai-sizing-nfq1-install-20261003.service").read_text()
    assert "ConditionPathExists=!/home/trader/after-hours/2026-10-03/sizing-nfq1-install" in unit
    assert "RemainAfterExit=yes" in unit and "Restart=no" in unit
