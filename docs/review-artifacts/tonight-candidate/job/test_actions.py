"""Offline deployment artifact tests: no broker, Redis or production write."""
import importlib.util
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("oct5_actions", Path(__file__).with_name("actions.py"))
actions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(actions)


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    env = tmp_path / "primary.env"
    env.write_text("OTHER=unchanged\n" + actions.KEYS[-1] + "=true\n")
    env.chmod(0o600)
    original_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, *args, **kwargs:
                        SimpleNamespace(st_uid=0, st_gid=os.getgid(), st_mode=stat.S_IFREG | 0o600)
                        if self == env else original_stat(self, *args, **kwargs))
    monkeypatch.setattr(actions, "ENV", env)
    monkeypatch.setattr(actions.os, "chown", lambda *args: None)
    monkeypatch.setattr(actions, "journal", lambda message: None)
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    return env, attempt


def test_env_changes_exactly_three_keys_retains_handoff_and_backup(env_path):
    env, attempt = env_path
    before = env.read_bytes()
    actions.env_flags(attempt)
    assert (attempt / "project-mai-tai.env.before").read_bytes() == before
    assert env.read_text().splitlines() == ["OTHER=unchanged", actions.KEYS[-1] + "=true"] + [
        key + "=true" for key in actions.KEYS[:-1]]
    assert env.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("key", actions.KEYS)
def test_duplicate_key_refuses_before_backup_or_env_edit(env_path, key):
    env, attempt = env_path
    env.write_text(env.read_text() + key + "=false\n" + key + "=true\n")
    before = env.read_bytes()
    with pytest.raises(RuntimeError, match="duplicate"):
        actions.env_flags(attempt)
    assert env.read_bytes() == before
    assert not list(attempt.iterdir())


def test_handoff_not_already_true_is_not_excused_as_fourth_edit(env_path):
    env, attempt = env_path
    env.write_text("OTHER=unchanged\n" + actions.KEYS[-1] + "=false\n")
    before = env.read_bytes()
    with pytest.raises(RuntimeError, match="already"):
        actions.env_flags(attempt)
    assert env.read_bytes() == before
    assert not list(attempt.iterdir())


def test_exclusive_artifact_cannot_overwrite(tmp_path):
    path = tmp_path / "proof.json"
    actions.exclusive(path, "first")
    with pytest.raises(FileExistsError):
        actions.exclusive(path, "second")
    assert path.read_text() == "first"


def test_record_classifies_every_snapshot_unit_and_only_three_restarted(tmp_path):
    before = {"captured_at_utc": "2026-10-05T22:00:00Z", "services": {
        name: {} for name in ("oms", "strategy", "schwab-1m-v2", "orb", "market-data", "momentum-paper")}}
    (tmp_path / "before-restart.json").write_text(json.dumps(before))
    actions.record(tmp_path)
    record = json.loads((tmp_path / "install-record.json").read_text())
    assert record["snapshot_captured_at_utc"] == before["captured_at_utc"]
    assert set(record["service_actions"]) == set(before["services"])
    assert {k for k, v in record["service_actions"].items() if v == "restarted"} == {
        "oms", "strategy", "schwab-1m-v2"}


def test_runner_has_only_authorized_sequence_and_no_recovery_start():
    script = Path(__file__).with_name("run.sh").read_text()
    calls = [line.strip() for line in script.splitlines() if line.startswith("run systemctl ")]
    assert calls == ["run systemctl " + verb + " project-mai-tai-" + service + ".service" for verb, service in (
        ("stop", "schwab-1m-v2"), ("stop", "strategy"), ("stop", "oms"),
        ("start", "oms"), ("start", "schwab-1m-v2"), ("start", "strategy"))]
    trap = script[script.index("abort() {"):script.index("trap abort EXIT")]
    assert "systemctl start" not in trap and "systemctl restart" not in script
    assert "--clock-override" not in script and "--operator-override" not in script
    assert '"upgrade", "20261005_0022"' in Path(__file__).with_name("actions.py").read_text()


def test_approval_release_wrong_hash_refuses_before_other_reads(tmp_path):
    (tmp_path / "release.json").write_text("{}")
    with pytest.raises(RuntimeError, match="release hash drift"):
        actions.verify(tmp_path, "0" * 64)


def test_approval_requires_exact_scope_application_and_manifest(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 5, 18, 1, tzinfo=tz)
    monkeypatch.setattr(actions, "datetime", Clock)
    (tmp_path / "strict_flat_readonly.py").write_text("reviewed-helper")
    monkeypatch.setattr(actions, "FLAT_HASH", actions.digest(tmp_path / "strict_flat_readonly.py"))
    release = {"approved_sha": actions.APP, "box_sha": actions.BOX, "helper_sha256": actions.FLAT_HASH,
               "plan_commit": "a" * 40, "artifacts": {"strict_flat_readonly.py": actions.FLAT_HASH}}
    (tmp_path / "release.json").write_text(json.dumps(release))
    release_hash = actions.digest(tmp_path / "release.json")
    approval = {"reviewer": "claude-1", "decision": "APPROVED", "plan_commit": "a" * 40,
                "approved_sha": actions.APP, "release_sha256": release_hash, "date_et": "2026-10-05",
                "scope": "v2-strategy-oms-0022-all-four-on-no-recovery"}
    (tmp_path / "approval.json").write_text(json.dumps(approval))
    actions.verify(tmp_path, release_hash)
    approval["approved_sha"] = actions.BOX
    (tmp_path / "approval.json").write_text(json.dumps(approval))
    with pytest.raises(RuntimeError, match="approval"):
        actions.verify(tmp_path, release_hash)
    approval["approved_sha"] = actions.APP
    approval["scope"] = "unapproved-recovery"
    (tmp_path / "approval.json").write_text(json.dumps(approval))
    with pytest.raises(RuntimeError, match="approval"):
        actions.verify(tmp_path, release_hash)
