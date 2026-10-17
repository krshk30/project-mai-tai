"""Actual validation/report methods with controlled DB/OS leaves only. UNRUN."""

from contextlib import contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from ops.weekmaint import backend, policy
from tests.unit.test_weekmaint1_policy import NOW


@pytest.fixture
def controlled(tmp_path, monkeypatch):
    statements, effects = [], []
    item = object.__new__(backend.SystemBackend)
    item.config = {"expected_database": "controlled", "expected_PGDATA": str(tmp_path),
        "expected_postgres_system_identifier": "123", "security_window_helper": "controlled-helper",
        "security_window_helper_sha256": "same", "security_apt_config": "controlled-config",
        "security_apt_config_sha256": "same", "reviewed_all_existing_operator_blacklists": True,
        "preserved_blacklists_sha256": "same", "RETAIN1_files": {"controlled-retain": "same"}}
    item.live_contract_approved = item.security_window_approved = True
    item.state = Path("/var/lib/project-mai-tai-weekmaint")
    item.clock = lambda: NOW
    item.approved_head = "f" * 40
    item.timer_started_body = lambda *_args: False
    item._performance_diagnostics = {}
    monkeypatch.setattr(backend, "digest", lambda _path: "same")
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    read = Path.read_text
    def read_text(path, *args, **kwargs):
        if str(path) == "/proc/self/cgroup":
            return "0::/system.slice/project-mai-tai-weekmaint.service\n"
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return str(UUID(int=4))
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(backend, "private_json", lambda path: json.loads(Path(path).read_text()))
    class Connection:
        jit = "off"
        identity_error = None
        def execute(self, statement):
            sql = str(statement)
            statements.append(sql)
            if "data_directory" in sql:
                if self.identity_error is not None:
                    raise self.identity_error
                return SimpleNamespace(one=lambda: ("controlled", None, str(tmp_path)))
            if "system_identifier" in sql:
                return SimpleNamespace(scalar_one=lambda: 123)
            if "current_setting('jit')" in sql:
                if isinstance(self.jit, Exception):
                    raise self.jit
                return SimpleNamespace(scalar_one=lambda: self.jit)
            return SimpleNamespace(scalar_one=lambda: 1)
        @contextmanager
        def begin_nested(self):
            effects.append("diagnostic_savepoint")
            try:
                yield
            except Exception:
                effects.append("diagnostic_rollback")
                raise
    connection = Connection()
    class Engine:
        count = 0
        @contextmanager
        def connect(self):
            self.count += 1
            yield connection
    item.engine = Engine()
    item.command = lambda argv, **_kwargs: effects.append(tuple(argv)) or "PONG"
    item.recorded_unit_states_match = lambda *_args: True
    item.observe_current_oms_health = lambda *_args: {key: True for key in policy.BOOT_REQUIRED}
    later = NOW + timedelta(days=1)
    item.config["reviewed_monday_timers"] = {key: {"unit": key, "approved_next_wall": later.isoformat(),
        "fragment_sha256": "same"} for key in (
            "guard_0340_scheduled", "preopen_scheduled", "universe_scheduled", "SEC_scheduled", "prune_scheduled")}
    item.unit_properties = lambda _unit: {"ActiveState": "active", "NextElapseUSecRealtime": "controlled",
                                         "FragmentPath": "controlled"}
    monkeypatch.setattr(backend, "timer_wall", lambda _value: later)
    original_open = os.open
    def private_health_log(path, flags, mode=0o777, **kwargs):
        if str(path) == "/var/log/project-mai-tai/weekmaint-health.log":
            path = tmp_path / "controlled-health.log"
        return original_open(path, flags, mode, **kwargs)
    monkeypatch.setattr(backend.os, "open", private_health_log)
    return SimpleNamespace(item=item, connection=connection, statements=statements, effects=effects, path=tmp_path)


@pytest.mark.parametrize("value,severity", [("off", "INFO"), ("on", "CRITICAL"), (None, "CRITICAL"),
                                            ("unexpected", "CRITICAL"), (RuntimeError("jit read"), "CRITICAL")])
def test_validate_jit_performance_warning_survives_inventory_and_healthy_report(controlled, value, severity):
    case = controlled
    case.connection.jit = value
    case.item.validate_runtime_inputs()
    assert case.item.engine.count == 1
    assert sum("current_setting('jit')" in sql for sql in case.statements) == 1
    assert not any("ALTER" in sql or "SET jit" in sql for sql in case.statements)
    diagnostic = case.item._performance_diagnostics["jit"]
    assert diagnostic["severity"] == severity
    assert diagnostic["report_line"].startswith(severity + " [WEEKMAINT-JIT]")
    assert "REPORT_ONLY_CONTINUE" in diagnostic["report_line"]
    case.item.state = case.path
    operation = str(UUID(int=5))
    case.item.persist_inventory_before_stop(operation, {"writer_units": {}})
    case.item.claim_single_report = lambda _operation: True
    case.item.clear_marker = lambda _operation: case.effects.append("healthy_marker_released")
    assert case.item.verify_and_report_once(operation) == "HEALTHY"
    report = json.loads((case.path / f"report-{operation}.json").read_text())
    assert report["healthy"] is True
    assert report["report_lines"] == [diagnostic["report_line"]]
    assert report["evidence"]["performance_diagnostics"]["jit"] == diagnostic
    assert "healthy_marker_released" in case.effects


def test_original_identity_read_failure_is_not_replaced_by_jit_diagnostic(controlled):
    original = RuntimeError("controlled original identity query failure")
    controlled.connection.identity_error = original
    with pytest.raises(RuntimeError) as caught:
        controlled.item.validate_runtime_inputs()
    assert caught.value is original
    assert not any("current_setting('jit')" in sql for sql in controlled.statements)
    assert controlled.item._performance_diagnostics == {}


def test_wrong_database_still_refuses_even_when_jit_is_off(controlled):
    controlled.item.config["expected_database"] = "different"
    with pytest.raises(policy.Refused, match="local_PG_identity"):
        controlled.item.validate_runtime_inputs()
