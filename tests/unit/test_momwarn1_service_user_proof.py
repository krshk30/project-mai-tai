"""Staged-caller controls only. No actual box/service-user readiness assertion."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.unit.test_momwarn1_live_health import setup as live_setup

setup = live_setup
CALLER = Path(__file__).parents[2] / "ops/health/momwarn_service_user_proof.py"
spec = importlib.util.spec_from_file_location("staged_momwarn_service_user_proof", CALLER)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


@pytest.mark.parametrize("uid,name", [(0, "root"), (123, "not-trader")])
def test_root_or_wrong_user_refused_before_systemctl_or_environment(monkeypatch, uid, name):
    monkeypatch.setattr(os, "geteuid", lambda: uid)
    monkeypatch.setattr(proof.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name=name))
    command = Mock(side_effect=AssertionError("no service read for wrong user"))
    monkeypatch.setattr(proof.subprocess, "check_output", command)
    with pytest.raises(ValueError, match="trader"):
        proof.adopt_service_environment(Path("/not-used"), "a" * 40)
    command.assert_not_called()


def test_actual_environment_adoption_never_exports_values(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    proc = tmp_path / "proc/12"
    proc.mkdir(parents=True)
    (proc / "status").write_text("Uid:\t42 42 42 42\nGid:\t43 43 43 43\nGroups:\t44 43\n")
    (proc / "environ").write_bytes(b"MAI_TAI_REDIS_URL=redis://localhost/0\0SECRET=never-export\0")
    monkeypatch.setattr(os, "geteuid", lambda: 42)
    monkeypatch.setattr(os, "getegid", lambda: 43)
    monkeypatch.setattr(os, "getgroups", lambda: [43, 44])
    monkeypatch.setattr(proof.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="trader"))
    monkeypatch.setattr(proof.grp, "getgrgid", lambda _: SimpleNamespace(gr_name="trader"))
    monkeypatch.setattr(proof, "process_identity", lambda _: "12:123")
    monkeypatch.setattr(proof, "Path", lambda value: proc / Path(value).name if str(value).startswith("/proc/12/") else Path(value))
    command = Mock(side_effect=[
        f"MainPID=12\nUser=trader\nGroup=trader\nActiveState=active\nWorkingDirectory={repository}\n"
        "ExecMainStartTimestamp=CONTROLLED-start\n", "a" * 40 + "\n",
    ])
    monkeypatch.setattr(proof.subprocess, "check_output", command)
    monkeypatch.setattr(os, "environ", {"WRONG_CALLER_ENV": "discard"})
    monkeypatch.setattr(proof.sys, "path", list(proof.sys.path))
    monkeypatch.chdir(repository)
    receipt = proof.adopt_service_environment(repository, "a" * 40)
    assert os.environ == {"MAI_TAI_REDIS_URL": "redis://localhost/0", "SECRET": "never-export"}
    assert "never-export" not in json.dumps(receipt) and "redis://" not in json.dumps(receipt)
    assert receipt["gateway_instance"] == "12:123"
    assert command.call_count == 2
    assert all("Environment" not in str(call) for call in command.call_args_list)


def permission_mapping(monkeypatch, env):
    actual = proof.path_receipt
    paths = []

    def controlled_receipt(path):
        paths.append(str(path))
        mapped = env.mem if str(path) == "/proc/meminfo" else (
            env.proc / Path(path).relative_to("/proc") if str(path).startswith("/proc/") else path)
        result = actual(mapped)
        result["path"] = str(path)
        return result

    monkeypatch.setattr(proof, "path_receipt", controlled_receipt)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    return paths


@pytest.mark.asyncio
async def test_two_actual_reader_calls_and_complete_source_path_list_CONTROLLED(setup, monkeypatch):
    env = setup()
    paths = permission_mapping(monkeypatch, env)
    result = await proof.prove(env.reader, env.client, "test", env.cfg, 1)
    assert result["status"] == "READ_LIVE_2_OK"
    assert [row["read_status"] for row in result["attempts"]] == ["OK", "OK"]
    assert result["health_verdict"] == "NOT_EVALUATED_NO_GATEWAY_PUBLISH_LAG"
    assert set(paths) == {str(env.config), *(str(env.directory / name) for name in env.reader.LOG_NAMES),
                          "/proc/12/stat", "/proc/13/stat", "/proc/meminfo"}
    assert len(paths) == 8
    assert all(row["readable"] and row["owner"] and row["mode"] for row in result["permissions"])
    assert env.client.xrevrange.call_count == 2
    env.client.xrevrange.assert_called_with("test:strategy-state-isolated", count=32)


@pytest.mark.asyncio
async def test_unreadable_log_never_becomes_root_or_local_OK(setup, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("actual file denial control requires nonroot")
    env = setup()
    permission_mapping(monkeypatch, env)
    path = env.directory / "oms.log"
    mode = path.stat().st_mode & 0o777
    path.chmod(0)
    try:
        result = await proof.prove(env.reader, env.client, "test", env.cfg, 1)
    finally:
        path.chmod(mode)
    assert result["status"] == "UNKNOWN"
    assert all(row["exception_class"] == "PermissionError" for row in result["attempts"])
    denied = next(row for row in result["permissions"] if row["path"] == str(path))
    assert denied["readable"] is False and denied["mode"] == "0000"
    assert env.client.xrevrange.call_count == 2


@pytest.mark.asyncio
async def test_readable_files_with_unknown_health_do_not_pass(setup, monkeypatch):
    env = setup()
    permission_mapping(monkeypatch, env)
    env.client.xrevrange.return_value = []
    env.client.xrevrange.side_effect = None
    result = await proof.prove(env.reader, env.client, "test", env.cfg, 1)
    assert all(row["readable"] for row in result["permissions"])
    assert result["status"] == "UNKNOWN"
    assert all(row["exception_class"] == "ValueError" for row in result["attempts"])


@pytest.mark.asyncio
@pytest.mark.parametrize("pid", [0, True, "../secret"])
async def test_malformed_pid_cannot_broaden_file_reads(monkeypatch, pid):
    receipt = Mock(side_effect=AssertionError("no path reads"))
    monkeypatch.setattr(proof, "path_receipt", receipt)
    with pytest.raises(ValueError, match="positive source"):
        await proof.prove(None, None, "test", {"oms_pid": pid, "v2_pid": 13}, 1)
    receipt.assert_not_called()


def test_caller_has_no_gateway_daemon_provider_or_write_commands():
    text = CALLER.read_text()
    assert "market_data.gateway" not in text and "massive" not in text
    assert all(value not in text for value in ("chmod", "setfacl", "xadd", "hset", "systemctl start",
                                               "systemctl restart", "systemctl stop"))
