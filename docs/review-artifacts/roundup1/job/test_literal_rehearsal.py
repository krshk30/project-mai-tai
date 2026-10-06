"""Execute the literal filesystem/sequence mechanics against fake system/provider boundaries.

Every provider/census/official-report result here is explicitly controlled, not recorded live proof.
"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import attended
import closeout
import make_release
import post_proof
import release_policy as policy
from test_attended_release import catalog_control, fleet, row47_proof
from test_armed_readonly import reply
from test_paper_coverage import inactive_paper, paper_output
from test_raw_gate_admission import OMS, V2, snapshot

LOCAL = Path(__file__).resolve().parents[4]
JOB = Path(__file__).parent
NOW = datetime(2026, 10, 6, 20, 10, tzinfo=timezone.utc)


class FakeSystem(attended.Real):
    def __init__(self, job, release, attempt, repo, *, failed_row47=False, fault=None, unknown_flags=False):
        super().__init__(job, release, attempt)
        self.system = fleet()
        if unknown_flags:
            self.system["momentum-paper"] = inactive_paper()
        self.head = policy.BOX
        self.calls = []
        self.failed_row47, self.fault, self.unknown_flags = failed_row47, fault, unknown_flags
        self.repo = repo

    def now(self):
        return NOW

    def fleet(self):
        return copy.deepcopy(self.system)

    def sql(self, since=None):
        return dict(tickets=[])

    def command(self, args, *, check=True, **kwargs):
        args = list(map(str, args))
        self.calls.append(args)
        if self.fault and self.fault in " ".join(args):
            raise policy.Stop("controlled injected command failure")
        output, rc = b"", 0
        if "rev-parse" in args:
            output = ((policy.TREE if args[-1].endswith("^{tree}") else self.head) + "\n").encode()
        elif "git" in args and "show" in args:
            output = subprocess.check_output(["git", "show", args[-1]], cwd=LOCAL)
        elif "archive" in args:
            output = b"CONTROLLED source archive; not a live backup"
        elif "switch" in args:
            self.head = policy.APP
        elif args[0] == "systemctl" and args[1] in {"start", "stop", "restart"}:
            name = args[2].removeprefix("project-mai-tai-").removesuffix(".service")
            item = self.system[name]
            if args[1] == "stop":
                item.update(MainPID=0, ActiveState="inactive", SubState="dead", Result="success")
                if name == "orb-schwab" and self.failed_row47:
                    item.update(ActiveState="failed", SubState="failed", Result="exit-code", ExecMainCode=1, ExecMainStatus=1)
            else:
                item.update(MainPID=fleet()[name]["MainPID"] + 1000, ActiveState="active", SubState="running",
                            Result="success", NRestarts=0, ExecMainStartTimestampMonotonic=200,
                            ExecMainStartTimestamp="Tue 2026-10-06 20:10:00 UTC")
                if name == "control":
                    item["InvocationID"] = "f" * 32
                log = self.repo / "logs" / (name + ".log")
                lines = {"oms": "[BROKER-SYNC-CENSUS] live:orb: ok=1 failed=0 consecutive_now=0",
                         "orb-schwab": "[ORB-SCHWAB] mode=LIVE live_sending=True",
                         "schwab-1m-v2": "[V2-BOOT-HOLD] restoration_complete=1\n2026-10-06 20:10:00,001 [V2-LINE-RESTORE] sym=CONTROLLED outcome=published entry_allowed=0"}
                with log.open("a") as stream:
                    if name == "control":
                        from test_control_install import control_log
                        stream.write(control_log(item["MainPID"]))
                    else:
                        stream.write("2026-10-06 20:10:00,000 " + lines[name] + "\n")
        elif args[0] == "journalctl":
            output = b"\n".join(json.dumps(row).encode() for row in row47_proof()[2])
        elif args[:2] == ["systemctl", "reset-failed"]:
            assert args[2] == "project-mai-tai-orb-schwab.service"
            self.system["orb-schwab"].update(ActiveState="inactive", SubState="dead", Result="success")
        elif args[0] == "systemctl" and args[1] == "show":
            if args[2].endswith(".timer"):
                output = b"NextElapseUSecRealtime=Wed 2026-10-07 10:20:00 UTC\nLastTriggerUSec=\n"
            else:
                output = b"ExecMainStartTimestamp=\nActiveState=inactive\n"
        elif any(arg.endswith("strict_flat_readonly.py") for arg in args):
            evidence = snapshot()
            for row in evidence["account_stamps"]:
                row["updated_at"] = NOW.isoformat()
            output = json.dumps(evidence).encode()
        elif any(arg.endswith("census_readonly.py") for arg in args):
            output = b'{"rc":0,"controlled":true}'
        elif any(arg.endswith("redis_checkpoint.py") for arg in args):
            output = b'{"controlled":true,"evicted_keys":0,"used_memory":1}'
        elif any(arg.endswith("armed_readonly.py") for arg in args):
            import armed_readonly
            output = policy.canonical(armed_readonly.proof(reply(), NOW))
        elif any(arg.endswith("control_display_proof.py") for arg in args):
            from test_control_install import control_receipt
            phase = args[args.index("--phase") + 1]
            pid = int(args[args.index("--pid") + 1])
            old = int(args[args.index("--old-pid") + 1]) if "--old-pid" in args else None
            output = policy.canonical(control_receipt(phase, pid, old, self.system["control"], self.system["oms"]["MainPID"]))
        elif any(arg.endswith("preflight_oms_restart.sh") for arg in args):
            output, rc = OMS.encode(), 1
        elif any(arg.endswith("preflight_v2_restart.sh") for arg in args):
            raw = V2
            if "--clock-override" in args:
                reason = args[args.index("--clock-override") + 1]
                raw = raw.replace("  [ok]    past 18:00 ET", "  [OVERRIDE] clock gate (<18:00 ET) overridden by OPERATOR\n             reason: " + reason)
            output, rc = raw.encode(), 1
        elif any(arg.endswith("expected_flags_check.py") for arg in args):
            output = catalog_control()[1].encode()
            if self.unknown_flags:
                output = paper_output().encode()
                rc = 2
        elif "snapshot" in args:
            Path(args[args.index("--output") + 1]).write_text(json.dumps(dict(captured_at_utc=NOW.isoformat(),
                                                                          services={name: {} for name in policy.CHANGED})))
        elif "report" in args:
            Path(args[args.index("--output") + 1]).write_text("CONTROLLED official report\nFinal call: PASS; controlled inputs, no live acceptance claim\n")
        elif args[0] == "systemctl" and args[1] in {"enable", "is-enabled", "is-active", "daemon-reload"}:
            pass
        elif args[0] in {"bash", "systemd-analyze", "runuser"} or "git" in args:
            pass
        else:
            raise AssertionError("unrecognized production boundary: " + repr(args))
        if check:
            policy.need(rc == 0, "controlled command refusal")
        self.receipt("fake-command.json", policy.canonical(dict(args=args, rc=rc, controlled=True)))
        return SimpleNamespace(returncode=rc, stdout=output, stderr=b"")


def setup(monkeypatch, tmp_path, *, retry_env=None, **options):
    repo, job, attempt = tmp_path / "repo", tmp_path / "job", tmp_path / "attempt"
    for directory in (repo, job, attempt, repo / "logs", tmp_path / "helpers", tmp_path / "units"):
        directory.mkdir(parents=True, exist_ok=True)
    application = {}
    for path in make_release.SOURCES:
        raw = subprocess.check_output(["git", "show", policy.APP + ":" + path], cwd=LOCAL)
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        application[path] = policy.digest(raw)
    for name in make_release.NAMES:
        (job / name).write_bytes((JOB / name).read_bytes())
    (job / "release.json").write_text("CONTROLLED release, not reviewer approval")
    gate, env = tmp_path / "preopen.sh", tmp_path / "environment"
    gate.write_bytes((JOB / "preopen.baseline.sh").read_bytes())
    gate.chmod(0o700)
    env.write_text("MAI_TAI_PROTECTED_SYMBOLS=TE,CYN\nUNCHANGED=1\n" + policy.RETRY_ENABLED + "=true\n" + policy.RETRY_MAX + "=1\n")
    env.chmod(0o600)
    for name in policy.CHANGED:
        (repo / "logs" / (name + ".log")).write_text("CONTROLLED old-process log\n")
    for name in closeout.CATALOGS:
        (tmp_path / "helpers" / name).write_bytes(subprocess.check_output(["git", "show", policy.BOX + ":ops/health/" + name], cwd=LOCAL))
    journal = tmp_path / "deployments.md"
    journal.write_text("CONTROLLED journal\n")
    monkeypatch.setattr(attended, "REPO", repo)
    monkeypatch.setattr(attended, "GATE", gate)
    monkeypatch.setattr(attended, "ENV", env)
    monkeypatch.setattr(closeout, "HELPERS", tmp_path / "helpers")
    monkeypatch.setattr(closeout, "ROOT", tmp_path / "daily")
    monkeypatch.setattr(closeout, "UNIT_DIRECTORY", tmp_path / "units")
    monkeypatch.setattr(closeout, "JOURNAL", journal)
    # Identity ownership is explicitly controlled; do not chown files or invoke root.
    monkeypatch.setattr(attended.os, "chown", lambda *args: None)
    monkeypatch.setattr(attended.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=0))
    original_stat = Path.stat
    def metadata(path, *args, **kwargs):
        stat = original_stat(path, *args, **kwargs)
        if path in (env, gate):
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.st_mode, st_size=stat.st_size)
        return stat
    monkeypatch.setattr(Path, "stat", metadata)
    real_path = Path
    def proof_path(value):
        value = str(value)
        if value.startswith("/proc/"):
            path = tmp_path / "proc" / value.removeprefix("/proc/")
            path.parent.mkdir(parents=True, exist_ok=True)
            flags = {key: "true" for key in policy.PM + policy.NEW_ENV}
            flags.update(MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED="true", MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED="false")
            flags.update({policy.RETRY_ENABLED: "true", policy.RETRY_MAX: "0"})
            for key, value in (retry_env or {}).items():
                if value is None:
                    flags.pop(key, None)
                else:
                    flags[key] = value
            path.write_bytes(b"\0".join((key + "=" + value).encode() for key, value in flags.items()))
            return path
        return real_path(value)
    monkeypatch.setattr(post_proof, "Path", proof_path)
    fx = FakeSystem(job, {"application_blobs": application}, attempt, repo, **options)
    # Log file location is the single host-only read boundary of checkpoint().
    original_checkpoint = fx.checkpoint
    def checkpoint(completed):
        if completed == 3:
            monkeypatch.setattr(attended, "Path", lambda value: repo / "logs" if str(value) == "/var/log/project-mai-tai" else real_path(value))
        original_checkpoint(completed)
        monkeypatch.setattr(attended, "Path", real_path)
    monkeypatch.setattr(fx, "checkpoint", checkpoint)
    original_action = fx.action
    def action(verb, name):
        if name == "control":
            monkeypatch.setattr(attended, "Path", lambda value: repo / "logs/control.log"
                                if str(value) == "/var/log/project-mai-tai/control.log" else real_path(value))
        try:
            return original_action(verb, name)
        finally:
            monkeypatch.setattr(attended, "Path", real_path)
    monkeypatch.setattr(fx, "action", action)
    return fx, gate, env


@pytest.mark.parametrize("failed_row47", [False, True])
def test_literal_full_sequence_backups_gate_diff_hashes_units_timer_only(monkeypatch, tmp_path, failed_row47):
    fx, gate, env = setup(monkeypatch, tmp_path, failed_row47=failed_row47)
    attended.Sequence(fx).run()
    actions = [args for args in fx.calls if args[0] == "systemctl" and args[1] in {"start", "stop", "restart"}]
    assert actions == [["systemctl", action, "project-mai-tai-" + name + ".service"] for action, name in policy.PHASES]
    assert (fx.attempt / "COMPLETE.json").exists()
    assert len([args for args in actions if args[1] == "restart"]) == 1
    assert json.loads((fx.attempt / "COMPLETE.json").read_bytes())["control_display"]["page"]["trades"] == 1
    assert (fx.attempt / "source-before.tar").exists() and (fx.attempt / "env.before").exists()
    assert (fx.attempt / "env.diff").exists()
    assert "UNCHANGED=1" not in (fx.attempt / "env.diff").read_text()
    assert policy.RETRY_ENABLED not in (fx.attempt / "env.diff").read_text()
    assert env.read_text().count(policy.RETRY_ENABLED + "=true") == 1
    assert env.read_text().count(policy.RETRY_MAX + "=0") == 1
    assert (fx.attempt / "env.prewrite.diff").exists()
    assert (fx.attempt / "numeric-catalog.diff").exists()
    assert (tmp_path / "helpers/expected_numeric.json").read_bytes() == (JOB / policy.NUMERIC_ARTIFACT).read_bytes()
    assert (fx.repo / "ops/health/expected_numeric.json").read_bytes() != (tmp_path / "helpers/expected_numeric.json").read_bytes()
    assert json.loads((fx.attempt / "COMPLETE.json").read_bytes())["flaggate_coverage"]["total"] == 153
    assert (fx.attempt / "preopen.diff").exists()
    pin = json.loads((tmp_path / "daily/runtime.json").read_bytes())
    assert pin["artifacts"]["retry_zero_readonly.py"] == policy.digest((JOB / "retry_zero_readonly.py").read_bytes())
    assert pin["gate_sha256"] == policy.digest(gate.read_bytes())
    assert pin["artifacts"]["daily.py"] == policy.digest((JOB / "daily.py").read_bytes())
    assert "UNCHANGED=1" in env.read_text()
    assert [args for args in fx.calls if args[:2] == ["systemctl", "enable"]] == [["systemctl", "enable", "--now", "project-mai-tai-preopen.timer"]]
    assert not any(args == ["bash", str(gate)] for args in fx.calls)
    for name in policy.NEW_ENV:
        assert env.read_text().count(name + "=true") == 1


@pytest.mark.parametrize("fault", ["armed_readonly.py", "control_display_proof.py", "restart project-mai-tai-control", "switch --detach", "pip install", "stop project-mai-tai-oms", "start project-mai-tai-orb-schwab", "systemd-analyze verify", "enable --now"])
def test_literal_partial_failure_traps_actual_state_no_recovery(monkeypatch, tmp_path, fault):
    fx, _, _ = setup(monkeypatch, tmp_path, fault=fault)
    with pytest.raises(policy.Stop):
        attended.Sequence(fx).run()
    assert (fx.attempt / "STOP.json").exists() and not (fx.attempt / "COMPLETE.json").exists()
    stop = json.loads((fx.attempt / "STOP.json").read_bytes())
    assert stop["actual"] == fx.system and stop["recovery_authorized"] is False
    assert len([args for args in fx.calls if "ERROR" in args]) == 1


def test_night_exact_paper_unknown2_retained_allows_timer_only(monkeypatch, tmp_path):
    fx, gate, _ = setup(monkeypatch, tmp_path, unknown_flags=True)
    attended.Sequence(fx).run()
    coverage = json.loads((fx.attempt / "flaggate-coverage.json").read_bytes())
    assert (coverage["original_rc"], coverage["checked"], coverage["unknown"]) == (2, 151, 2)
    assert coverage["original_verdict"] == "UNKNOWN"
    assert (tmp_path / "daily").exists() and policy.digest(gate.read_bytes()) != policy.BASELINE_GATE
    assert not any(args == ["systemctl", "start", "project-mai-tai-momentum-paper.service"] for args in fx.calls)


def test_night_other_unknown_or_paper_drift_stops_before_gate_timer(monkeypatch, tmp_path):
    fx, gate, _ = setup(monkeypatch, tmp_path, unknown_flags=True)
    original = fx.command
    def bad(args, **kwargs):
        value = original(args, **kwargs)
        if any(str(arg).endswith("expected_flags_check.py") for arg in args):
            value.stdout = value.stdout.replace(b"reason=momentum-paper is not active", b"reason=controlled read failure")
        return value
    monkeypatch.setattr(fx, "command", bad)
    with pytest.raises(policy.Stop):
        attended.Sequence(fx).run()
    assert policy.digest(gate.read_bytes()) == policy.BASELINE_GATE
    assert not (tmp_path / "daily").exists()


def test_current_attempt_row47_missing_signal_never_resets(monkeypatch, tmp_path):
    fx, _, _ = setup(monkeypatch, tmp_path, failed_row47=True)
    original = fx.command
    def missing(args, **kwargs):
        value = original(args, **kwargs)
        if args[0] == "journalctl":
            value.stdout = b"\n".join(json.dumps(row).encode() for row in row47_proof()[2][1:])
        return value
    monkeypatch.setattr(fx, "command", missing)
    with pytest.raises(policy.Stop, match="SIGTERM"):
        attended.Sequence(fx).run()
    assert not any(args[:2] == ["systemctl", "reset-failed"] for args in fx.calls)
    assert (fx.attempt / "STOP.json").exists()
