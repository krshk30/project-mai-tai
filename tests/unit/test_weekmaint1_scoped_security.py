"""Authored scoped-window controls; fake /proc and no package/systemd execution."""
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_maintenance_window import load_helper

NOW = datetime(2026, 10, 17, 14, tzinfo=UTC)


def scoped(monkeypatch, tmp_path):
    module = load_helper()
    permit = tmp_path / "package-window.json"
    row = {"version": 1, "boot_id": "controlled-boot", "invocation_id": "a" * 32,
           "SEC_success_sha256": "b" * 64,
           "started_at": (NOW - timedelta(seconds=1)).isoformat(),
           "expires_at": (NOW + timedelta(seconds=60)).isoformat()}
    permit.write_text(json.dumps(row))
    permit.chmod(0o600)
    read_text = Path.read_text
    lstat, fstat = Path.lstat, module.os.fstat
    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/cgroup":
            return "0::/system.slice/project-mai-tai-weekmaint.service\n"
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return "controlled-boot\n"
        return read_text(path, *args, **kwargs)
    def parent_stat(path):
        value = lstat(path)
        if path == tmp_path:
            return SimpleNamespace(st_mode=value.st_mode, st_uid=0)
        return value
    def file_stat(fd):
        value = fstat(fd)
        return SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_size=value.st_size)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "lstat", parent_stat)
    monkeypatch.setattr(module.os, "fstat", file_stat)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    return module, permit, row


def test_postSEC_install_only_has_scoped_permission_not_protected_restart_permission(monkeypatch, tmp_path):
    module, permit, _ = scoped(monkeypatch, tmp_path)
    assert not module.in_window(NOW)
    assert module.weekmaint_install_window(NOW, permit=permit)
    for unit in ("project-mai-tai-oms.service", "redis-server", "postgresql@16-main.service"):
        assert module.policy([unit, "restart"], NOW, flag=tmp_path / "deferred.json") == 101


@pytest.mark.parametrize("change", ["wrong_invocation", "wrong_boot", "expired", "future", "too_long",
                                     "missing_SEC", "wrong_day", "world_readable", "symlink"])
def test_scope_replay_or_untrusted_permit_refuses(monkeypatch, tmp_path, change):
    module, permit, row = scoped(monkeypatch, tmp_path)
    when = NOW
    if change == "wrong_invocation":
        row["invocation_id"] = "c" * 32
    elif change == "wrong_boot":
        row["boot_id"] = "other"
    elif change == "expired":
        row["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif change == "future":
        row["started_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "too_long":
        row["expires_at"] = (NOW + timedelta(hours=3)).isoformat()
    elif change == "missing_SEC":
        row.pop("SEC_success_sha256")
    elif change == "wrong_day":
        when += timedelta(days=2)
    permit.write_text(json.dumps(row))
    if change == "world_readable":
        permit.chmod(0o644)
    if change == "symlink":
        target = tmp_path / "real"
        permit.rename(target)
        permit.symlink_to(target)
    assert not module.weekmaint_install_window(when, permit=permit)
