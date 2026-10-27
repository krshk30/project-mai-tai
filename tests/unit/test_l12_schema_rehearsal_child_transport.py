"""Controlled local child IO only: no actual subprocess/SQL/provider."""

import json
from types import SimpleNamespace

import pytest

from tools import l12_production_schema_rehearsal as parent


def terminal():
    return dict(kind="REAL_ENV_UPGRADE_RETURNED", revision="20261022_l12_retirement", connections=2,
        target_capability_sha256="a" * 64, source_review_sha256="b" * 64, permission_granted=False)


NORMAL_LOGS = b"\n".join([
    b"INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.",
    b"INFO  [alembic.runtime.migration] Will assume transactional DDL.",
    b"INFO  [alembic.runtime.migration] Running stamp_revision  -> 20261015_bot_control",
    b"INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.",
    b"INFO  [alembic.runtime.migration] Will assume transactional DDL.",
    b"INFO  [alembic.runtime.migration] Running upgrade 20261015_bot_control -> 20261023_atr_simulation, CONTROLLED",
    b"INFO  [alembic.runtime.migration] Running upgrade 20261023_atr_simulation -> 20261023_atr_aggregate, CONTROLLED",
    b"INFO  [alembic.runtime.migration] Running upgrade 20261023_atr_aggregate -> 20261022_l12_retirement, CONTROLLED",
]) + b"\n"


def controlled(monkeypatch, *, payload=None, stderr=NORMAL_LOGS, close=False, wait=None, exit_code=0):
    if payload is None:
        payload = (json.dumps(dict(kind="REAL_ENV_STAMP_RETURNED_NOT_FINAL_OUTCOME", revision="20261015_bot_control"))
            + "\n" + json.dumps(terminal()) + "\n").encode()
    data, events = {1: [payload, b""], 2: [stderr, b""]}, []

    class Pipe:
        def __init__(self, number):
            self.number = number

        def close(self):
            events.append(("close", self.number))
            if close:
                raise OSError("PRIVATE_CLOSE")

    class Process:
        pid = 123
        stdout, stderr = Pipe(1), Pipe(2)

        def wait(self, timeout):
            events.append(("wait", timeout))
            if wait is not None:
                raise wait
            return exit_code

    class Selector:
        def __init__(self):
            self.keys = {}

        def register(self, pipe, event, name):
            self.keys[pipe.number] = SimpleNamespace(fd=pipe.number, fileobj=pipe, data=name)

        def get_map(self):
            return self.keys

        def select(self, timeout):
            return [(key, None) for key in self.keys.values()]

        def unregister(self, pipe):
            del self.keys[pipe.number]

        def close(self):
            pass

    launches = []

    def popen(argv, **kwargs):
        launches.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(parent.subprocess, "Popen", popen)
    monkeypatch.setattr(parent.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(parent.os, "read", lambda fd, size: data[fd].pop(0))
    return events, launches


def test_one_child_preserves_exact_private_cwd_environment_and_terminal_receipt(monkeypatch, tmp_path):
    events, launches = controlled(monkeypatch)
    report, actual = parent.run_child(["CONTROLLED_NOT_EXECUTED"], {"MAI_TAI_DATABASE_URL": "CONTROLLED"}, tmp_path, tmp_path)
    assert actual == terminal() and report["exit_code"] == 0
    assert events == [("close", 1), ("close", 2), ("wait", 10)]
    assert len(launches) == 1 and launches[0][1]["cwd"] == tmp_path
    assert launches[0][1]["env"] == {"MAI_TAI_DATABASE_URL": "CONTROLLED"}
    assert report["backend_closure"] == "UNVERIFIED"


@pytest.mark.parametrize("fault", ["close", "wait", "stderr", "exit", "missing_stamp", "wrong_head", "overflow"])
def test_unknown_child_outcomes_never_authorize_upgrade_or_retry(monkeypatch, tmp_path, fault):
    payload = None
    if fault == "missing_stamp":
        payload = (json.dumps(terminal()) + "\n").encode()
    if fault == "wrong_head":
        value = terminal()
        value["revision"] = "UNKNOWN"
        payload = (json.dumps(dict(kind="REAL_ENV_STAMP_RETURNED_NOT_FINAL_OUTCOME", revision="20261015_bot_control"))
            + "\n" + json.dumps(value) + "\n").encode()
    events, launches = controlled(monkeypatch, payload=payload, stderr=b"PRIVATE_ERROR" if fault == "stderr" else NORMAL_LOGS,
        close=fault == "close", wait=parent.subprocess.TimeoutExpired("PRIVATE", 10) if fault == "wait" else None,
        exit_code=1 if fault == "exit" else 0)
    if fault == "overflow":
        monkeypatch.setattr(parent, "CHILD_MAX", 1)
    with pytest.raises((ValueError, OSError, parent.subprocess.TimeoutExpired)):
        parent.run_child(["CONTROLLED_NOT_EXECUTED"], {}, tmp_path, tmp_path)
    assert len(launches) == 1 and events.count(("wait", 10)) == 1
    assert ("close", 1) in events and ("close", 2) in events
    assert (tmp_path / "receipt.json").is_file()


@pytest.mark.parametrize("raw", [b"", NORMAL_LOGS.replace(b"INFO", b"WARN", 1),
    NORMAL_LOGS + b"PRIVATE_UNEXPECTED\n", NORMAL_LOGS.replace(b" -> 20261023_atr_aggregate", b" -> UNKNOWN", 1)])
def test_no_blanket_log_suppression_or_missing_transition_acceptance(raw):
    with pytest.raises(ValueError):
        parent.real_env_logs(raw)
