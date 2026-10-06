"""Controlled rehearsals only: no production calls or claimed broker acceptance."""
import copy
from datetime import datetime, timedelta, timezone
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import attended as runner
import closeout
import release_policy as policy

NOW = datetime(2026, 10, 6, 20, 10, tzinfo=timezone.utc)


def fleet():
    return {name: dict(MainPID=100 + i, NRestarts=0, ActiveState="active", SubState="running", Result="success",
                       ExecMainCode=0, ExecMainStatus=0, ExecMainStartTimestamp="Tue 2026-10-06 00:00:00 UTC",
                       ExecMainStartTimestampMonotonic=100, InvocationID=f"{i+1:032x}", InactiveEnterTimestamp="")
            for i, name in enumerate(policy.SERVICES)}


def release():
    return dict(approved_sha=policy.APP, tree=policy.TREE, box_sha=policy.BOX, scope=policy.SCOPE,
                date_et=policy.DAY, plan_commit="a" * 40)


def decision():
    return dict(decision="APPROVED", authority=policy.AUTHORITY, attended=True, release_sha256="b" * 64,
                approved_sha=policy.APP, plan_commit="a" * 40, date_et=policy.DAY, scope=policy.SCOPE)


def test_exact_approval_control():
    policy.approval(release(), "b" * 64, decision(), NOW)


@pytest.mark.parametrize("field,value", [("approved_sha", policy.BOX), ("tree", "a" * 40),
                                         ("box_sha", policy.APP), ("scope", "all-services"),
                                         ("date_et", "2026-10-07"), ("plan_commit", "HEAD")])
def test_manifest_pin_drift_blocks(field, value):
    data = release()
    data[field] = value
    with pytest.raises(policy.Stop):
        policy.approval(data, "b" * 64, decision(), NOW)


@pytest.mark.parametrize("field,value", [("decision", "DRAFT"), ("authority", "codex"), ("attended", False),
                                         ("release_sha256", "c" * 64), ("approved_sha", policy.BOX),
                                         ("plan_commit", "c" * 40), ("date_et", "2026-10-07"), ("scope", "wide")])
def test_approval_each_binding_required(field, value):
    data = decision()
    data[field] = value
    with pytest.raises(policy.Stop):
        policy.approval(release(), "b" * 64, data, NOW)


@pytest.mark.parametrize("now", [datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc), NOW - timedelta(days=1),
                                datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)])
def test_first_write_window_exact(now):
    with pytest.raises(policy.Stop):
        policy.first_write_window(now)


class Fake:
    def __init__(self, fail=None, midnight=False):
        self.before = fleet()
        self.current = copy.deepcopy(self.before)
        self.calls = []
        self.actions = []
        self.fail = fail
        self.midnight = midnight
        self.completed = 0

    def call(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise policy.Stop("controlled refusal")

    def now(self):
        return NOW + timedelta(days=1) if self.midnight and self.completed else NOW

    def initial(self):
        self.call("initial")

    def prepare(self):
        self.call("prepare")

    def gates(self, completed):
        self.call("gates" + str(completed))
        policy.states(self.before, self.current, completed)

    def v2_gate(self):
        self.call("v2-gate")

    def oms_gate(self):
        self.call("oms-gate")

    def action(self, action, name):
        self.call(action + "-" + name)
        self.actions.append((action, name))
        state = self.current[name]
        if action == "stop":
            state.update(MainPID=0, ActiveState="inactive", SubState="dead")
        else:
            state.update(MainPID=self.before[name]["MainPID"] + 1000, ActiveState="active", SubState="running",
                         ExecMainStartTimestampMonotonic=200, ExecMainStartTimestamp="Tue 2026-10-06 20:11:00 UTC")
            if name == "control":
                state["InvocationID"] = "f" * 32

    def checkpoint(self, completed):
        self.call("checkpoint" + str(completed))
        policy.states(self.before, self.current, completed)
        self.completed = completed

    def finish_proof(self):
        self.call("proof")

    def closeout(self):
        self.call("closeout-timer-only")

    def complete(self):
        self.call("complete")

    def abort(self, phase, completed):
        self.calls.append(("abort", phase, completed, copy.deepcopy(self.current)))


@pytest.mark.parametrize("midnight", [False, True])
def test_full_sequence_exact_three_owners_once_timer_only_no_late_clock_abort(midnight):
    fx = Fake(midnight=midnight)
    sequence = runner.Sequence(fx)
    sequence.run()
    assert fx.actions == list(policy.PHASES)
    assert fx.calls[-1] == "complete"
    assert sequence.first_stopped
    for name in set(policy.SERVICES) - set(policy.CHANGED):
        assert fx.current[name] == fx.before[name]


@pytest.mark.parametrize("failure", ["initial", "prepare", "v2-gate", "oms-gate", "proof", "closeout-timer-only",
    *["gates" + str(n) for n in range(len(policy.PHASES))], *["checkpoint" + str(n) for n in range(1, len(policy.PHASES) + 1)],
    *[action + "-" + name for action, name in policy.PHASES]])
def test_every_phase_abort_records_actual_and_never_recovers(failure):
    fx = Fake(fail=failure)
    with pytest.raises(policy.Stop):
        runner.Sequence(fx).run()
    assert fx.calls[-1][0] == "abort"
    assert fx.calls[-1][3] == fx.current
    assert fx.actions == list(policy.PHASES[:len(fx.actions)])
    assert "complete" not in fx.calls


@pytest.mark.parametrize("name", policy.SERVICES)
def test_each_untouched_or_early_identity_drift_blocks(name):
    before = fleet()
    after = copy.deepcopy(before)
    after[name]["MainPID"] += 1
    with pytest.raises(policy.Stop):
        policy.states(before, after, 0)


def row47_proof():
    before = fleet()["orb-schwab"]
    after = dict(before, MainPID=0, ActiveState="failed", SubState="failed", Result="exit-code", ExecMainCode=1, ExecMainStatus=1)
    unit = "project-mai-tai-orb-schwab.service"
    messages = [(str(before["MainPID"]), "SIGTERM received"),
                (str(before["MainPID"]), '  File "/home/trader/project-mai-tai/.venv/bin/mai-tai-orb-schwab", line 8, in <module>'),
                (str(before["MainPID"]), "asyncio.exceptions.CancelledError"),
                ("1", unit + ": Main process exited, code=exited, status=1/FAILURE"),
                ("1", "Stopping " + unit + " - Project Mai Tai separate default-off ORB Schwab producer...")]
    rows = [dict(_SYSTEMD_UNIT=unit, _PID=pid, MESSAGE=message, __REALTIME_TIMESTAMP=str(int(NOW.timestamp() * 1e6)))
            for pid, message in messages]
    for row in rows:
        if row["_PID"] == "1":
            row.update(_SYSTEMD_UNIT="init.scope", UNIT=unit, INVOCATION_ID=before["InvocationID"])
            if row["MESSAGE"].startswith("Stopping"):
                row["JOB_TYPE"] = "stop"
        else:
            row["_SYSTEMD_INVOCATION_ID"] = before["InvocationID"]
    return before, after, rows


def row47_call(before, after, rows, **kwargs):
    return policy.row47(before, after, rows, NOW, NOW + timedelta(seconds=1), intended=kwargs.get("intended", True),
                        unit=kwargs.get("unit", "project-mai-tai-orb-schwab.service"))


def test_controlled_row47_complete_old_pid_proof_only():
    assert row47_call(*row47_proof())["pid"] > 0


@pytest.mark.parametrize("field,value", [("MainPID", 1), ("ActiveState", "active"), ("SubState", "running"),
                                         ("Result", "timeout"), ("ExecMainCode", 2), ("ExecMainStatus", 2),
                                         ("NRestarts", 1), ("ExecMainStartTimestampMonotonic", 200)])
def test_row47_unit_state_negatives(field, value):
    before, after, rows = row47_proof()
    after[field] = value
    with pytest.raises(policy.Stop):
        row47_call(before, after, rows)


@pytest.mark.parametrize("index", range(5))
def test_row47_each_signature_required(index):
    before, after, rows = row47_proof()
    rows.pop(index)
    with pytest.raises(policy.Stop):
        row47_call(before, after, rows)


@pytest.mark.parametrize("field,value", [("_PID", "9999"), ("_SYSTEMD_UNIT", "project-mai-tai-oms.service"),
                                         ("__REALTIME_TIMESTAMP", "1"), ("MESSAGE", "SIGKILL received")])
def test_row47_event_binding_negatives(field, value):
    before, after, rows = row47_proof()
    rows[0][field] = value
    with pytest.raises(policy.Stop):
        row47_call(before, after, rows)


@pytest.mark.parametrize("kwargs", [dict(intended=False), dict(unit="project-mai-tai-oms.service")])
def test_row47_cannot_reset_other_or_unintended_stop(kwargs):
    with pytest.raises(policy.Stop):
        row47_call(*row47_proof(), **kwargs)


def test_real_action_resets_exact_unit_only_after_complete_row47(monkeypatch, tmp_path):
    before, after, rows = row47_proof()
    clean = dict(after, ActiveState="inactive", SubState="dead", Result="success")
    fx = runner.Real(tmp_path, {}, tmp_path)
    fx.before = {"orb-schwab": before}
    queue = iter([before, after, clean])
    monkeypatch.setattr(fx, "fleet", lambda: {"orb-schwab": next(queue)})
    monkeypatch.setattr(fx, "now", lambda: NOW)
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        raw = b"\n".join(json.dumps(row).encode() for row in rows) if args[0] == "journalctl" else b""
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")
    monkeypatch.setattr(fx, "command", command)
    monkeypatch.setattr(fx, "receipt", lambda *args: None)
    fx.action("stop", "orb-schwab")
    assert [args for args in calls if args[0] == "systemctl"] == [
        ["systemctl", "stop", "project-mai-tai-orb-schwab.service"],
        ["systemctl", "reset-failed", "project-mai-tai-orb-schwab.service"]]


def test_env_changes_three_booleans_and_zero_only_retains_all_other_bytes():
    raw = b"SECRET=untouched\nMAI_TAI_PROTECTED_SYMBOLS=TE,CYN\nOTHER=true\n" + policy.RETRY_ENABLED.encode() + b"=true\n" + policy.NEW_ENV[0].encode() + b"=false\n"
    value = runner.env_candidate(raw)
    assert value.startswith(raw[:raw.index(policy.NEW_ENV[0].encode())])
    assert all(value.count((key + "=true\n").encode()) == 1 for key in policy.NEW_ENV)
    assert value.count((policy.RETRY_MAX + "=0\n").encode()) == 1
    assert len(value.splitlines()) == 8


def test_duplicate_env_cannot_be_repaired_to_green():
    with pytest.raises(policy.Stop):
        runner.env_candidate((policy.RETRY_ENABLED + "=true\nA=1\nexport A=2\n").encode())


def test_case_alias_duplicate_new_key_blocks_instead_of_shadowing_process():
    key = policy.NEW_ENV[0]
    with pytest.raises(policy.Stop):
        runner.env_candidate((policy.RETRY_ENABLED + "=true\n" + key + "=false\n" + key.lower() + "=false\n").encode())


def test_retry_enabled_key_is_retained_byte_for_byte():
    raw = b"MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_ENABLED=true\n"
    assert runner.env_candidate(raw).startswith(raw)
    assert runner.env_candidate(raw).count(b"RETRY_ONE_ENABLED") == 1


def catalog_control():
    repo = Path(__file__).resolve().parents[4]
    catalog = json.loads(subprocess.check_output(["git", "show", policy.APP + ":ops/health/expected_flags.json"], cwd=repo))["flags"]
    catalog += json.loads(Path(__file__).with_name(policy.NUMERIC_ARTIFACT).read_bytes())["settings"]
    raw = "\n".join("PASS flag=" + row["name"] + " service=" + name + " pid=100 controlled-snapshot"
                    for row in catalog for name in (row["owning_service"], *row.get("also_check_services", [])))
    return catalog, raw + "\nFinal call: PASS; checked=153/153 mismatches=0 unknown=0\n"


def test_flaggate_exact153_control_and_unknowns_not_pass():
    catalog, raw = catalog_control()
    closeout.flag_result(0, raw, catalog)
    for rc, changed in [(2, raw.replace("PASS; checked=153/153 mismatches=0 unknown=0", "UNKNOWN; checked=151/153 mismatches=0 unknown=2")),
                        (0, raw.replace("153/153", "151/151")), (1, raw), (0, "\n".join(raw.splitlines()[1:]))]:
        with pytest.raises(policy.Stop):
            closeout.flag_result(rc, changed, catalog)
