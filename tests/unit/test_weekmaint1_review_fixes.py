"""F1-F6 controls: real backend policy with fake OS/process/journal leaves only.

These are authored, unrun controls; no systemd/package/provider command is allowed.
"""
from copy import deepcopy
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

import pytest

from ops.weekmaint import backend, collector, policy
from tests.unit.test_weekmaint1_policy import ACCOUNTS, Backend, NOW, snapshot

BOOT = str(UUID(int=100))
INVOCATION = "a" * 32
OP = str(UUID(int=200))


@pytest.fixture
def make_backend(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("control attempted a real OS/package operation")
    # Exercise the real permission parser with this controlled test role, not
    # root privileges. Production CLI remains root-only with owner_uid=0.
    read = collector.private_json
    monkeypatch.setattr(backend, "private_json", lambda path: read(path, owner_uid=os.getuid()))
    monkeypatch.setattr(backend, "current_boot_id", lambda: BOOT)
    config = {"state_directory": str(tmp_path), "approved_head": "f" * 40,
        "lookup_day": "2026-10-19", "approved_WEEKMAINT1_manifest": True,
        "live_accounts": ACCOUNTS.copy(), "reviewed_timer_started_services": True,
        "timer_started_services": [],
        "units": {"project-mai-tai-weekmaint.service": {"writer": False},
                  "project-mai-tai-weekmaint-boot.service": {"writer": False},
                  "project-mai-tai-oms.service": {"writer": True}}}
    def make():
        return backend.SystemBackend(deepcopy(config), None, None, run=forbidden, popen=forbidden, clock=lambda: NOW)
    return make


@pytest.mark.parametrize("unit", ["project-mai-tai-weekmaint.service", "project-mai-tai-weekmaint-boot.service"])
@pytest.mark.parametrize("change,allowed", [(None, True), ("other_cgroup", False), ("other_invocation", False),
                                          ("writer", False), ("other_unit", False)])
def test_exact_own_nonwriter_oneshot_only(make_backend, monkeypatch, unit, change, allowed):
    item = make_backend()
    invocation = INVOCATION if change != "other_invocation" else "b" * 32
    target = "project-mai-tai-oms.service" if change == "other_unit" else unit
    if change == "writer":
        item.config["units"][unit]["writer"] = True
    monkeypatch.setenv("INVOCATION_ID", INVOCATION)
    original = Path.read_text
    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/cgroup":
            own = "project-mai-tai-unrelated.service" if change == "other_cgroup" else unit
            return f"0::/system.slice/{own}\n"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    item.command = lambda argv, **kw: (f"Id={target}\nLoadState=loaded\nActiveState=activating\n"
        f"SubState=start\nInvocationID={invocation}\n")
    if allowed:
        assert item.unit_properties(target)["ActiveState"] == "activating"
    else:
        with pytest.raises(policy.Refused, match="transitional"):
            item.unit_properties(target)


@pytest.mark.parametrize("healthy", [False, True])
def test_run_job_consumes_real_boot_report_verdict_not_discarded(healthy):
    item = Backend()
    item.claim_single_report = lambda operation: True
    item.read_boot_health_and_monday_schedule = lambda operation: {key: healthy for key in policy.BOOT_REQUIRED}
    item.write_boot_report_and_unique_health_line = lambda *args: item.effects.append(("report", args[-1]))
    item.clear_marker = lambda operation: item.effects.append("clear_marker")
    item.verify_and_report_once = lambda operation: backend.SystemBackend.verify_and_report_once(item, operation)
    if healthy:
        report = policy.run_job(item, NOW, dry_run=False)
        assert report["status"] == "EXECUTION_FINISHED_VERIFY_RECEIPT" and "clear_marker" in item.effects
    else:
        with pytest.raises(policy.Refused, match="verification_failed"):
            policy.run_job(item, NOW, dry_run=False)
        assert ("report", False) in item.effects and "clear_marker" not in item.effects
        assert item.effects[-1] == "report_failure_keep_inventory"


@pytest.mark.parametrize("change,good", [(None, True), ("old_time", False), ("old_boot", False),
    ("old_invocation", False), ("future", False), ("not_completed", False), ("changed_current_invocation", False)])
def test_sync_health_is_bound_to_current_identity_and_durable_barrier(make_backend, change, good):
    item = make_backend()
    barrier = NOW - timedelta(seconds=10)
    original = {"boot_id": BOOT, "started_at": (NOW - timedelta(minutes=10)).isoformat(), "steps": [
        {"step": "RESTORE_BARRIER", "data": {"at": barrier.isoformat(), "boot_id": BOOT,
                                             "oms_invocation_id": INVOCATION}}]}
    event_at = NOW - timedelta(seconds=1)
    if change == "old_time":
        event_at = barrier - timedelta(microseconds=1)
    if change == "future":
        event_at = NOW + timedelta(seconds=1)
    event = {"_BOOT_ID": UUID(BOOT).hex, "_SYSTEMD_INVOCATION_ID": INVOCATION,
        "__REALTIME_TIMESTAMP": str(int(event_at.timestamp()) * 1_000_000 + event_at.microsecond),
        "MESSAGE": "[OMS-BROKER-SYNC-PASS] id=7 phase=end outcome=ok duration_ms=100"}
    if change == "old_boot":
        event["_BOOT_ID"] = UUID(int=99).hex
    if change == "old_invocation":
        event["_SYSTEMD_INVOCATION_ID"] = "b" * 32
    if change == "not_completed":
        event["MESSAGE"] = "[OMS-BROKER-SYNC-PASS] id=7 phase=start accounts=all"
    commands = []
    def command(argv, **kwargs):
        commands.append(argv)
        return json.dumps(event)
    item.command = command
    oms = {"ActiveState": "active", "InvocationID": INVOCATION}
    item.unit_properties = lambda unit: {**oms, "InvocationID": "c" * 32} if change == "changed_current_invocation" else oms
    assert item.completed_sync_current_invocation(OP, original, oms) is good
    assert len(commands) == 1 and "--boot" in commands[0] and "json" in commands[0]


def test_same_boot_without_restore_barrier_is_unknown_not_operation_start_fallback(make_backend):
    item = make_backend()
    item.command = lambda *a, **k: pytest.fail("missing barrier must not consume old journal")
    assert not item.completed_sync_current_invocation(OP, {"boot_id": BOOT, "steps": []},
                                                      {"ActiveState": "active", "InvocationID": INVOCATION})


class Child:
    pid = 12345

    def __init__(self, *, overrun=False, status=0):
        self.overrun, self.status = overrun, status
        self.returncode = None
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is not None and self.overrun:
            raise subprocess.TimeoutExpired("controlled-unattended", timeout)
        self.returncode = self.status
        return self.status

    def poll(self):
        return self.returncode

    def kill(self):
        pytest.fail("must not kill package child")

    def terminate(self):
        pytest.fail("must not terminate package child")


@pytest.mark.parametrize("fault", ["start_record", "overrun_record", "overrun_logger", "base_exception"])
def test_diagnostic_failure_never_returns_before_same_known_child_finishes(make_backend, fault):
    item = make_backend()
    child = Child(overrun=fault in {"overrun_record", "overrun_logger"})
    spawns = []
    def spawn(argv, **kwargs):
        spawns.append(argv)
        return child
    item.popen = spawn
    def record(operation, step, data):
        if fault == "base_exception":
            raise KeyboardInterrupt("controlled diagnostic interruption")
        if ((fault == "start_record" and step == "SECURITY_PROCESS_STARTED")
                or (fault == "overrun_record" and step == "PACKAGE_OVERRUN_NOT_KILLED")):
            raise OSError("controlled record/fsync failure")
    item.record = record
    def command(*args, **kwargs):
        if fault == "overrun_logger":
            raise OSError("controlled logger failure")
        return ""
    item.command = command
    with pytest.raises(KeyboardInterrupt if fault == "base_exception" else OSError):
        item.security_process(OP, io.BytesIO(), {})
    assert child.returncode == 0 and len(spawns) == 1 and child.waits[0] == 7200
    if child.overrun:
        assert child.waits == [7200, None]


@pytest.mark.parametrize("overrun,status,good", [(False, 0, True), (False, 17, False), (True, 0, False)])
def test_package_result_does_not_reboot_or_treat_late_success_as_success(make_backend, overrun, status, good):
    item = make_backend()
    child = Child(overrun=overrun, status=status)
    item.popen = lambda *a, **k: child
    item.record = lambda *a, **k: None
    item.command = lambda *a, **k: ""
    if good:
        item.security_process(OP, io.BytesIO(), {})
    else:
        with pytest.raises(policy.Refused, match="security_result"):
            item.security_process(OP, io.BytesIO(), {})
    assert child.returncode == status


def test_unresolved_fence_survives_different_SEC_and_missing_inventory(make_backend):
    item = make_backend()
    item._lock_held = True
    operation = item.claim_once_per_SEC_success("a" * 64)
    assert not item.operation_path(operation).exists()
    second = make_backend()
    second._lock_held = True
    with pytest.raises(policy.Refused, match="unresolved_operation"):
        second.claim_once_per_SEC_success("b" * 64)
    assert not (item.state / f"sec-{'b' * 64}.claimed").exists()


def test_global_fence_only_releases_after_reviewed_actual_HEALTHY_report(make_backend):
    item = make_backend()
    item._lock_held = True
    operation = item.claim_once_per_SEC_success("a" * 64)
    item.save(item.operation_path(operation), {"steps": []})
    report_path = item.state / f"report-{operation}.json"
    item.save(report_path, {"operation": operation, "healthy": False, "evidence": {}})
    with pytest.raises(policy.Refused, match="HEALTHY"):
        item.clear_marker(operation)
    assert (item.state / "active-operation.json").exists()
    item.save(report_path, {"operation": operation, "healthy": True,
                           "evidence": {key: True for key in policy.BOOT_REQUIRED}})
    item.clear_marker(operation)
    assert not (item.state / "active-operation.json").exists()
    assert (item.state / f"sec-{'a' * 64}.claimed").exists()
    with pytest.raises(FileExistsError):
        item.claim_once_per_SEC_success("a" * 64)
    assert UUID(item.claim_once_per_SEC_success("b" * 64))


def test_failed_diagnostic_sinks_cannot_remove_unresolved_fence(make_backend):
    item = make_backend()
    item._lock_held = True
    operation = item.claim_once_per_SEC_success("a" * 64)
    def failed(*args, **kwargs):
        raise OSError("controlled diagnostic failure")
    item.record = failed
    item.command = failed
    item.report_failure_keep_inventory(operation)
    with pytest.raises(policy.Refused, match="unresolved_operation"):
        item.claim_once_per_SEC_success("b" * 64)


def test_poststop_snapshot_reuses_one_original_evidence_and_never_calls_provider_again(make_backend):
    item = make_backend()
    calls = []
    facts = snapshot()
    item.database_inventory = lambda: ([], deepcopy(facts))
    item.read_writer_inventory = lambda: {}
    class Collector:
        async def collect(self, rows, db_facts):
            calls.append("one start read")
            return deepcopy(facts["broker_evidence"])
    item.collector = Collector()
    first = item.read_snapshot()
    with pytest.raises(policy.Refused, match="install_exclusion_held_UNKNOWN"):
        policy.gates(first, NOW)
    # Controlled positive prerequisite only; this is not an actual OS lock proof.
    item._lock_held = True
    later = NOW + timedelta(seconds=2)
    item.clock = lambda: later
    second = item.read_snapshot()
    policy.gates(second, later, stopped_at=NOW + timedelta(seconds=1))
    assert first["broker_evidence"] == second["broker_evidence"] and calls == ["one start read"]
    item.clock = lambda: NOW + timedelta(seconds=31)
    with pytest.raises(policy.Refused, match="freshness"):
        policy.gates(item.read_snapshot(), item.clock(), stopped_at=NOW + timedelta(seconds=1))
    assert calls == ["one start read"]


@pytest.mark.parametrize("terminal_kind", ["filled_order", "rejected_order", "completed_intent", "cancelled_intent"])
def test_ALL_status_two_hour_activity_gate_not_only_nonterminal(terminal_kind):
    facts = snapshot()
    facts["nonterminal_bot_order_rows"] = []
    facts["nonterminal_live_intents_today"] = 0
    facts["recent_or_unknown_bot_order_intent_count"] = 1
    with pytest.raises(policy.Refused, match="recent_or_unknown"):
        policy.gates(facts, NOW)
