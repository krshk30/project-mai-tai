"""Authored 10:1x verify-only controls; all OS/journal leaves controlled, UNRUN."""
from copy import deepcopy
from datetime import timedelta
import json
from uuid import UUID

import pytest

from ops.weekmaint import backend, policy
from tests.unit.test_weekmaint1_policy import ACCOUNTS, NOW
from tests.unit.test_weekmaint1_review_fixes import BOOT, INVOCATION, OP, make_backend as make_backend  # noqa: F401

OMS = "project-mai-tai-oms.service"


def event(message, at=NOW, *, invocation=INVOCATION, boot=BOOT):
    return json.dumps({"_BOOT_ID": UUID(boot).hex, "_SYSTEMD_INVOCATION_ID": invocation,
        "__REALTIME_TIMESTAMP": str(int(at.timestamp()) * 1_000_000 + at.microsecond), "MESSAGE": message})


def receipt(account, at=NOW, *, runtime=UUID(int=333)):
    return event(f"[RECEIPT-FIRST-COMMIT] account={account} boot_id={runtime} elapsed_ms=100.000 "
                 f"wire_read_at={(at - timedelta(seconds=1)).isoformat()} committed_at={at.isoformat()}", at)


def observation(item, monkeypatch, lines, *, boot_age=100):
    boot_at = NOW - timedelta(seconds=boot_age)
    monkeypatch.setattr(backend, "boot_wall_time", lambda: boot_at)
    monkeypatch.setattr(backend.time, "CLOCK_BOOTTIME", 7, raising=False)
    monkeypatch.setattr(backend.time, "clock_gettime", lambda clock: boot_age)
    oms = {"ActiveState": "active", "InvocationID": INVOCATION,
        "ExecMainStartTimestamp": (boot_at + timedelta(seconds=10)).strftime("%a %Y-%m-%d %H:%M:%S UTC")}
    original = {"boot_id": str(UUID(int=99)), "started_at": (boot_at - timedelta(hours=1)).isoformat(), "steps": []}
    calls, closed = [], []
    def observe(argv, seconds):
        calls.append((argv, seconds))
        try:
            yield from lines
        finally:
            closed.append(True)
    item.journal_observation = observe
    item.unit_properties = lambda unit: dict(oms)
    item.record = lambda *args: None
    item.command = lambda *args, **kwargs: pytest.fail("no action, restart, extra probe or journal reread")
    return oms, original, calls, closed


@pytest.mark.parametrize("missing", [None, "sync", "first_account", "second_account"])
def test_one_boot_bounded_observation_needs_sync_and_both_commits(make_backend, monkeypatch, missing):
    item = make_backend()
    accounts = list(ACCOUNTS)
    lines = ([] if missing == "sync" else [event("[OMS-BROKER-SYNC-PASS] id=9 phase=end outcome=ok")])
    for i, account in enumerate(accounts):
        if missing != ("first_account" if i == 0 else "second_account"):
            lines.append(receipt(account))
    oms, original, calls, closed = observation(item, monkeypatch, lines)
    result = item.observe_current_oms_health(OP, original, oms)
    assert result["oms_completed_sync_observed"] is (missing != "sync")
    assert result["oms_first_commits_observed"] is (missing not in {"first_account", "second_account"})
    assert len(calls) == 1 and calls[0][1] == 200 and "--follow" in calls[0][0] and closed == [True]
    assert "--no-tail" in calls[0][0]
    # The two receipt runtime UUIDs differ from the OS boot UUID: still valid.
    assert str(UUID(int=333)) != BOOT


@pytest.mark.parametrize("change", ["old_boot", "old_invocation", "after_boot_deadline", "preprocess",
                                    "runtime_changed", "only_start", "wire_future"])
def test_wrong_scope_or_uncommitted_evidence_never_certifies_both_accounts(make_backend, monkeypatch, change):
    item = make_backend()
    accounts = list(ACCOUNTS)
    sync = event("[OMS-BROKER-SYNC-PASS] id=9 phase=end outcome=ok")
    lines = [sync, receipt(accounts[0]), receipt(accounts[1])]
    last = json.loads(lines[-1])
    if change == "old_boot":
        last["_BOOT_ID"] = UUID(int=99).hex
    elif change == "old_invocation":
        last["_SYSTEMD_INVOCATION_ID"] = "b" * 32
    elif change == "after_boot_deadline":
        lines[-1] = receipt(accounts[1], NOW + timedelta(seconds=201))
        last = json.loads(lines[-1])
    elif change == "preprocess":
        last = json.loads(receipt(accounts[1], NOW - timedelta(seconds=95)))
    elif change == "runtime_changed":
        last = json.loads(receipt(accounts[1], runtime=UUID(int=334)))
    elif change == "only_start":
        last["MESSAGE"] = "[RECEIPT-WRITER-STARTED] account=" + accounts[1]
    else:
        last["MESSAGE"] = last["MESSAGE"].replace(
            (NOW - timedelta(seconds=1)).isoformat(), (NOW + timedelta(seconds=1)).isoformat())
    lines[-1] = json.dumps(last)
    oms, original, calls, closed = observation(item, monkeypatch, lines)
    if change == "after_boot_deadline":
        observed = item.journal_observation
        def advance_only_during_observation(argv, seconds):
            for index, line in enumerate(observed(argv, seconds)):
                if index == 2:
                    item.clock = lambda: NOW + timedelta(seconds=202)
                yield line
        item.journal_observation = advance_only_during_observation
    result = item.observe_current_oms_health(OP, original, oms)
    assert result["oms_first_commits_observed"] is False and len(calls) == 1 and closed == [True]


def test_late_boot_handler_only_reads_history_up_to_real_boot_deadline(make_backend, monkeypatch):
    item = make_backend()
    oms, original, _, _ = observation(item, monkeypatch, [], boot_age=301)
    commands = []
    def command(argv, **kwargs):
        commands.append(argv)
        return "\n".join([event("[OMS-BROKER-SYNC-PASS] id=9 phase=end outcome=ok"),
                          *(receipt(account) for account in ACCOUNTS)])
    item.command = command
    item.journal_observation = lambda *a: pytest.fail("late handler must not wait another 5 minutes")
    result = item.observe_current_oms_health(OP, original, oms)
    assert not any(result.values())
    assert len(commands) == 1 and "--follow" not in commands[0] and "--until" in commands[0]


def test_late_handler_can_certify_genuine_in_deadline_history_without_fresh_wait(make_backend, monkeypatch):
    item = make_backend()
    oms, original, _, _ = observation(item, monkeypatch, [], boot_age=301)
    in_deadline = NOW - timedelta(seconds=2)
    item.command = lambda argv, **kw: "\n".join([
        event("[OMS-BROKER-SYNC-PASS] id=9 phase=end outcome=ok", in_deadline),
        *(receipt(account, in_deadline) for account in ACCOUNTS)])
    item.journal_observation = lambda *a: pytest.fail("no new observation window for late handler")
    assert all(item.observe_current_oms_health(OP, original, oms).values())


def test_observer_rechecks_current_invocation_after_evidence(make_backend, monkeypatch):
    item = make_backend()
    lines = [event("[OMS-BROKER-SYNC-PASS] id=9 phase=end outcome=ok"), *(receipt(a) for a in ACCOUNTS)]
    oms, original, _, closed = observation(item, monkeypatch, lines)
    item.unit_properties = lambda unit: {**oms, "InvocationID": "b" * 32}
    assert not any(item.observe_current_oms_health(OP, original, oms).values())
    assert closed == [True]


def test_actual_reader_has_one_finite_subscription_then_closes_only_its_reader(make_backend, monkeypatch):
    item = make_backend()
    calls, actions = [], []
    clock = {"now": 100.0}
    class Output:
        def fileno(self):
            return 123
        def close(self):
            actions.append("close_reader_pipe")
    class Reader:
        stdout = Output()
        returncode = None
        def poll(self):
            return self.returncode
        def terminate(self):
            actions.append("terminate_only_journal_reader")
        def wait(self, timeout):
            assert timeout == 2
            self.returncode = 0
            return 0
        def kill(self):
            pytest.fail("ordinary reader cleanup must not require kill")
    class Selector:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def register(self, *args):
            pass
        def select(self, timeout):
            assert timeout == 5
            clock["now"] = 105.0
            return []
    def spawn(argv, **kwargs):
        calls.append(argv)
        assert kwargs == {"stdout": backend.subprocess.PIPE, "stderr": backend.subprocess.DEVNULL}
        return Reader()
    item.popen = spawn
    monkeypatch.setattr(backend.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(backend.os, "set_blocking", lambda fd, blocking: None)
    monkeypatch.setattr(backend.selectors, "DefaultSelector", Selector)
    assert list(item.journal_observation(["/usr/bin/journalctl", "--follow"], 5)) == []
    assert len(calls) == 1 and actions == ["terminate_only_journal_reader", "close_reader_pipe"]


@pytest.mark.parametrize("prior,current,expected", [("active", "active", True), ("active", "inactive", False),
    ("inactive", "inactive", True), ("inactive", "active", False)])
def test_verification_only_preserves_original_service_states_no_actions(make_backend, prior, current, expected):
    item = make_backend()
    item.command = lambda *a, **k: pytest.fail("verify must never start or stop any service")
    original = {"units": {OMS: {"ActiveState": prior, "writer": True}}}
    assert item.recorded_unit_states_match(original, {OMS: {"ActiveState": current}}) is expected


@pytest.mark.parametrize("body", sorted(backend.TIMER_BODIES) + ["project-mai-tai-preopen-controlled.service"])
def test_no_reboot_restore_never_starts_timer_owned_bodies(make_backend, body):
    item = make_backend()
    item.config["units"][body] = {"writer": True}
    item.config["timer_started_services"] = [body]
    original = {"boot_id": BOOT, "steps": [], "units": {OMS: {"writer": True, "ActiveState": "active"},
                          body: {"writer": True, "ActiveState": "active"}}}
    item.save(item.operation_path(OP), original)
    commands = []
    item.command = lambda argv, **kwargs: commands.append(argv) or ""
    item.unit_properties = lambda unit: {"ActiveState": "active", "InvocationID": INVOCATION}
    item.restore_recorded_active_inventory(OP)
    assert commands == [["/bin/systemctl", "start", OMS]]
    recorded = backend.private_json(item.operation_path(OP))
    assert [step["step"] for step in recorded["steps"]] == ["RESTORE_INTENT", "RESTORE_BARRIER"]


def test_no_reboot_restore_cannot_be_used_after_reboot(make_backend):
    item = make_backend()
    item.save(item.operation_path(OP), {"boot_id": str(UUID(int=99)), "units": {
        OMS: {"writer": True, "ActiveState": "active"}}})
    item.command = lambda *args, **kwargs: pytest.fail("no postboot restore or start intent")
    with pytest.raises(policy.Refused, match="verification_only"):
        item.restore_recorded_active_inventory(OP)


def test_unreviewed_external_timer_body_map_refuses_before_restore(make_backend):
    item = make_backend()
    item.config.pop("reviewed_timer_started_services")
    with pytest.raises(policy.Refused, match="timer_body_inventory"):
        item.timer_started_body(OMS, {})


def test_finished_invoking_nonwriter_is_not_restored_as_daemon(make_backend):
    item = make_backend()
    unit = "project-mai-tai-weekmaint.service"
    original = {"units": {unit: {"writer": False, "ActiveState": "activating", "SubState": "start"}}}
    assert item.recorded_unit_states_match(original, {unit: {"ActiveState": "inactive"}})


def test_missing_first_commit_is_failed_loud_and_cannot_release_global_fence(make_backend):
    item = make_backend()
    checks = {key: True for key in policy.BOOT_REQUIRED}
    checks["oms_first_commits_observed"] = False
    item.claim_single_report = lambda operation: True
    item.read_boot_health_and_monday_schedule = lambda operation: deepcopy(checks)
    reports = []
    item.write_boot_report_and_unique_health_line = lambda operation, evidence, good: reports.append((evidence, good))
    item.clear_marker = lambda operation: pytest.fail("failed health must never clear global fence")
    assert policy.boot_report(item, OP) == "FAILED_LOUD_NO_RETRY"
    assert reports == [(checks, False)]


def test_failed_probe_gets_one_failed_report_no_service_recovery(make_backend):
    item = make_backend()
    item.claim_single_report = lambda operation: True
    def missing(operation):
        raise policy.Refused("current_OMS_unknown")
    item.read_boot_health_and_monday_schedule = missing
    reports, failures = [], []
    item.write_boot_report_and_unique_health_line = lambda operation, evidence, good: reports.append((evidence, good))
    item.report_failure_keep_inventory = lambda operation: failures.append(operation)
    item.clear_marker = lambda operation: pytest.fail("probe failure cannot release fence")
    with pytest.raises(policy.Refused, match="current_OMS_unknown"):
        policy.boot_report(item, OP)
    assert len(reports) == 1 and reports[0][1] is False and failures == [OP]
    assert all(reports[0][0][key] is False for key in policy.BOOT_REQUIRED)
