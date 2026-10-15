from datetime import UTC, date, datetime, time, timedelta
import hashlib
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[2]
G1 = ROOT / "ops/health/momwarn1_repair/optiona/G1"
ET = ZoneInfo("America/New_York")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


daily = load("momrestart_daily", G1 / "daily_guard.py")
guard = load("momrestart_rules", G1 / "guard_rules.py")
window = SimpleNamespace(guard_end_for=lambda day: time(18, 10))


@pytest.mark.parametrize("hour,minute", [(4, 30), (6, 0), (7, 30), (12, 0)])
def test_permanent_restart_uses_current_day_and_new_baseline(hour, minute):
    now = datetime(2026, 10, 16, hour, minute, tzinfo=ET)
    assert daily.session_day(now, set(), window) == now.date()
    start, end = guard.treatment_clock(now.date(), now, window)
    assert start == now.astimezone(UTC) + timedelta(seconds=5)
    assert end.astimezone(ET) == datetime(2026, 10, 16, 18, 10, tzinfo=ET)


def test_regular_0340_start_still_waits_until_0400():
    now = datetime(2026, 10, 16, 3, 40, tzinfo=ET)
    start, end = guard.treatment_clock(now.date(), now, window)
    assert start.astimezone(ET).time() == time(4)
    assert end.astimezone(ET).time() == time(18, 10)


@pytest.mark.parametrize("stamp", ["2026-10-16T18:10:00", "2026-10-16T18:11:00", "2026-10-17T07:30:00"])
def test_closed_or_different_session_never_rearms(stamp):
    with pytest.raises(guard.Blind):
        guard.treatment_clock(date(2026, 10, 16), datetime.fromisoformat(stamp).replace(tzinfo=ET), window)


def test_stop_latch_survives_next_day_and_keeps_first_reason(tmp_path, monkeypatch):
    path = tmp_path / "state/safety-stop.json"
    monkeypatch.setattr(guard, "SAFETY_STOP_FILE", path)
    guard.require_no_safety_stop()
    guard.retain_safety_stop("heartbeat unsafe", date(2026, 10, 15))
    before = path.read_bytes()
    guard.retain_safety_stop("new baseline cannot clear", date(2026, 10, 16))
    assert path.read_bytes() == before
    with pytest.raises(guard.Blind, match="explicit human clear"):
        guard.require_no_safety_stop()


def test_corrupt_or_symlink_latch_is_not_permission_to_restart(tmp_path, monkeypatch):
    path = tmp_path / "stop"
    monkeypatch.setattr(guard, "SAFETY_STOP_FILE", path)
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(guard.Blind):
        guard.require_no_safety_stop()
    path.unlink()
    path.write_bytes(b"")
    with pytest.raises(guard.Blind):
        guard.require_no_safety_stop()


def test_storage_failure_still_stops_paper_with_nonrecoverable_exit(tmp_path, monkeypatch):
    def unwritable(*_):
        raise OSError("controlled full disk")
    stopped = []
    monkeypatch.setattr(guard, "retain_safety_stop", unwritable)
    monkeypatch.setattr(guard, "stop_paper", lambda reason, *_: stopped.append(reason))
    assert guard.safety_shutdown("unsafe", date(2026, 10, 16), None, None, tmp_path) == 78
    assert "durable_latch_UNKNOWN=OSError" in stopped[0]


@pytest.mark.parametrize("code", [errno.EACCES, errno.EIO])
def test_latch_lookup_unknown_is_not_absence(tmp_path, monkeypatch, code):
    path = tmp_path / "state/stop"
    monkeypatch.setattr(guard, "SAFETY_STOP_FILE", path)
    original = Path.lstat
    def lstat(self):
        if self == path:
            raise OSError(code, "controlled lookup failure")
        return original(self)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(guard.Blind, match="lookup UNKNOWN"):
        guard.require_no_safety_stop()


@pytest.fixture
def identity(tmp_path):
    (tmp_path / "123").mkdir()
    (tmp_path / "123/stat").write_text("123 (gateway name) " + " ".join(["S"] + ["0"] * 18 + ["456"]))
    pin = {"version": 1, "unit": "project-mai-tai-market-data.service", "main_pid": 123,
           "start_ticks": 456, "exec_main_start_timestamp": "Wed 2026-10-14 16:31:57 UTC"}
    state = {"MainPID": "123", "ActiveState": "active", "NRestarts": "0",
             "ExecMainStartTimestamp": "Wed 2026-10-14 12:31:57 EDT"}
    return {"gateway_identity": pin}, state, tmp_path


def test_identity_is_same_absolute_start_under_et_or_utc(identity):
    manifest, state, proc = identity
    daily.verify_gateway_identity(manifest, state, proc_root=proc)
    state["ExecMainStartTimestamp"] = manifest["gateway_identity"]["exec_main_start_timestamp"]
    daily.verify_gateway_identity(manifest, state, proc_root=proc)


def test_real_process_start_ticks_change_is_refused(identity):
    manifest, state, proc = identity
    (proc / "123/stat").write_text("123 (gateway name) " + " ".join(["S"] + ["0"] * 18 + ["457"]))
    with pytest.raises(ValueError, match="instance drift"):
        daily.verify_gateway_identity(manifest, state, proc_root=proc)


def test_state_normalizes_systemctl_display_without_touching_clock(monkeypatch):
    observed = {}
    def show(argv, **kwargs):
        observed.update(kwargs["env"])
        return "MainPID=123\nActiveState=active\n"
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setattr(daily.subprocess, "check_output", show)
    assert daily.state("example")["MainPID"] == "123"
    assert observed["TZ"] == "UTC" and observed["LC_ALL"] == "C"


@pytest.mark.parametrize("active,signal_expected", [("active", True), ("activating", False)])
def test_paper_restart_requests_new_guard_baseline_only_after_initial_start(tmp_path, monkeypatch, active, signal_expected):
    now = datetime(2026, 10, 16, 7, 30, tzinfo=ET)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now
    class Redis:
        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()
        def close(self):
            pass
    monkeypatch.setattr(daily, "datetime", Clock)
    monkeypatch.setattr(daily, "verify_manifest", lambda: {})
    monkeypatch.setattr(daily, "load_guard", lambda: guard)
    monkeypatch.setattr(guard, "SAFETY_STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(daily, "prestart_checks", lambda *args: ({}, {}))
    monkeypatch.setattr(daily, "state", lambda unit: {"ActiveState": active, "MainPID": "123"})
    import redis
    monkeypatch.setattr(redis, "Redis", Redis)
    import project_mai_tai.settings as settings
    monkeypatch.setattr(settings, "Settings", lambda **kwargs: SimpleNamespace(redis_url="redis://never-connect.invalid"))
    sent = []
    monkeypatch.setattr(daily.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert daily.main(["--paper-prestart"]) == 0
    assert sent == ([(123, signal.SIGUSR1)] if signal_expected else [])


@pytest.mark.parametrize("signum,expected", [(signal.SIGUSR1, guard.RestartBaseline),
                                           (signal.SIGTERM, guard.PlannedStop)])
def test_actual_guard_loop_rebaseline_signal_closes_sampler_without_latch(tmp_path, monkeypatch, signum, expected):
    now = datetime(2026, 10, 16, 7, 30, tzinfo=ET)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now
    monkeypatch.setattr(guard, "datetime", Clock)
    handlers = {}
    stopped = []
    sampler = SimpleNamespace(stderr=None, pid=123, poll=lambda: None,
        terminate=lambda: stopped.append("terminate"), wait=lambda **kwargs: 0)
    monkeypatch.setattr(guard, "SAFETY_STOP_FILE", tmp_path / "safety-stop.json")
    monkeypatch.setattr(guard, "Settings", lambda **kwargs: SimpleNamespace(redis_url="redis://never-connect.invalid", redis_stream_prefix="test"))
    monkeypatch.setattr(guard, "Redis", SimpleNamespace(from_url=lambda *args, **kwargs: SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(guard, "LiveSignals", lambda *args: None)
    monkeypatch.setattr(guard, "RedisSafety", lambda *args: None)
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *args, **kwargs: sampler)
    monkeypatch.setattr(guard, "retain_sampler_exit", lambda *args: None)
    monkeypatch.setattr(guard.signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    def notify(message):
        if message == "READY=1":
            handlers[signum](signum, None)
    monkeypatch.setattr(guard, "_notify_systemd", notify)
    with pytest.raises(expected):
        guard.run_guard(now.date(), tmp_path / "audit")
    assert stopped == ["terminate"]
    assert not guard.SAFETY_STOP_FILE.exists()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("point", ["before_open", "directory_fsync"])
def test_stop_signal_cannot_relabel_detected_harm_before_durable_commit(tmp_path, monkeypatch, signum, point):
    now = datetime(2026, 10, 16, 7, 30, tzinfo=ET)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now
    handlers = {}
    sampler = SimpleNamespace(stderr=None, pid=123, poll=lambda: None,
        terminate=lambda: None, wait=lambda **kwargs: 0)
    latch = tmp_path / "state/safety-stop.json"
    monkeypatch.setattr(guard, "datetime", Clock)
    monkeypatch.setattr(guard, "SAFETY_STOP_FILE", latch)
    monkeypatch.setattr(guard, "Settings", lambda **kwargs: SimpleNamespace(redis_url="redis://never-connect.invalid", redis_stream_prefix="test"))
    monkeypatch.setattr(guard, "Redis", SimpleNamespace(from_url=lambda *args, **kwargs: SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(guard, "LiveSignals", lambda *args: None)
    monkeypatch.setattr(guard, "RedisSafety", lambda *args: None)
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *args, **kwargs: sampler)
    monkeypatch.setattr(guard, "retain_sampler_exit", lambda *args: None)
    monkeypatch.setattr(guard, "SamplerEvidence", lambda *args, **kwargs: SimpleNamespace(read=lambda now: "detected harmful condition"))
    monkeypatch.setattr(guard.signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    monkeypatch.setattr(guard, "_notify_systemd", lambda *args: None)
    monkeypatch.setattr(guard.time, "sleep", lambda *args: None)
    stopped = []
    monkeypatch.setattr(guard, "stop_paper", lambda reason, *args: stopped.append(reason) or 0)
    original_open, original_fsync = os.open, os.fsync
    fired = []
    def open_at_signal(path, *args):
        if point == "before_open" and Path(path) == latch:
            handlers[signum](signum, None)
            fired.append(point)
        return original_open(path, *args)
    def fsync_at_signal(fd):
        if point == "directory_fsync" and stat.S_ISDIR(os.fstat(fd).st_mode):
            handlers[signum](signum, None)
            fired.append(point)
        return original_fsync(fd)
    monkeypatch.setattr(guard.os, "open", open_at_signal)
    monkeypatch.setattr(guard.os, "fsync", fsync_at_signal)
    assert guard.run_guard(now.date(), tmp_path / "audit") == 78
    assert fired == [point]
    assert stopped == ["detected harmful condition"]
    assert json.loads(latch.read_text())["reason"] == "detected harmful condition"
    with pytest.raises(guard.Blind, match="explicit human clear"):
        guard.require_no_safety_stop()


@pytest.mark.parametrize("cause", ["owner UNKNOWN", "gateway heartbeat unreadable", "Redis unsafe", "guard absent", "infrastructure"])
def test_actual_paper_prestart_blind_latches_but_dryrun_stays_readonly(tmp_path, monkeypatch, cause):
    now = datetime(2026, 10, 16, 7, 30, tzinfo=ET)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now
    class Redis:
        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()
        def close(self):
            pass
    latch = tmp_path / "stop"
    monkeypatch.setattr(daily, "datetime", Clock)
    monkeypatch.setattr(daily, "verify_manifest", lambda: {})
    monkeypatch.setattr(daily, "load_guard", lambda: guard)
    monkeypatch.setattr(guard, "SAFETY_STOP_FILE", latch)
    import redis
    monkeypatch.setattr(redis, "Redis", Redis)
    import project_mai_tai.settings as settings
    monkeypatch.setattr(settings, "Settings", lambda **kwargs: SimpleNamespace(redis_url="redis://never-connect.invalid"))
    def blind(*args):
        if cause == "infrastructure":
            raise ConnectionError("controlled transport unavailable")
        raise guard.Blind(cause)
    monkeypatch.setattr(daily, "prestart_checks", blind)
    with pytest.raises(ConnectionError if cause == "infrastructure" else guard.Blind):
        daily.main(["--dry-run-prestart"])
    assert not latch.exists()
    if cause == "infrastructure":
        with pytest.raises(ConnectionError):
            daily.main(["--paper-prestart"])
        assert not latch.exists()
        return
    if cause == "guard absent":
        monkeypatch.setattr(daily, "prestart_checks", lambda *args: ({}, {}))
        monkeypatch.setattr(daily, "state", lambda *args: {"ActiveState": "inactive", "MainPID": "0"})
    assert daily.main(["--paper-prestart"]) == 78
    assert cause in json.loads(latch.read_text())["reason"] or cause == "guard absent"
    monkeypatch.setattr(daily, "prestart_checks", lambda *args: pytest.fail("latched start cannot reread healthy and resume"))
    assert daily.main(["--paper-prestart"]) == 78


@pytest.mark.parametrize("field,value", [("MainPID", "124"), ("NRestarts", "1"),
    ("ActiveState", "failed"), ("ExecMainStartTimestamp", "Wed 2026-10-14 12:31:58 EDT"),
    ("ExecMainStartTimestamp", "Wed 2026-10-14 12:31:57 UNKNOWN")])
def test_real_identity_drift_or_unknown_zone_still_refuses(identity, field, value):
    manifest, state, proc = identity
    state[field] = value
    with pytest.raises(ValueError):
        daily.verify_gateway_identity(manifest, state, proc_root=proc)


@pytest.fixture
def subprocess_root(tmp_path):
    names = ("daily_guard.py", "guard_rules.py", "sampler.py")
    for name in names:
        shutil.copyfile(G1 / name, tmp_path / name)
    hashes = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in names}
    (tmp_path / "manifest.json").write_text(json.dumps({"artifacts": hashes}))
    (tmp_path / "ledger.json").write_text(json.dumps({"episode": "owned-1", "entry_attempts": 1, "filled": 3}))
    return tmp_path


def child(root, hour, minute, enabled, mode="normal"):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, "-B", str(ROOT / "tests/fixtures/momrestart1_driver.py"),
        str(root), f"2026-10-16T{hour:02}:{minute:02}:00", enabled, mode],
        capture_output=True, text=True, timeout=15, env=env)


@pytest.mark.parametrize("hour,minute", [(4, 30), (6, 0), (7, 30), (12, 0)])
@pytest.mark.parametrize("enabled", ["ON", "OFF"])
def test_real_subprocess_daily_main_restart_matrix_SHIM(subprocess_root, hour, minute, enabled):
    root = subprocess_root
    ledger = (root / "ledger.json").read_bytes()
    result = child(root, hour, minute, enabled)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "receipt.json").read_text())
    assert receipt["session_day"] == "2026-10-16"
    assert receipt["enabled"] == (enabled == "ON")
    assert receipt["coverage"] == "earlier gap NOT covered"
    assert (root / "ledger.json").read_bytes() == ledger


@pytest.mark.parametrize("crash,exit_status", [("crash", 1), ("sigkill", -9)])
def test_restart_on_failure_SHIM_recovers_crash_but_not_safety_stop(subprocess_root, crash, exit_status):
    """Actual child crashes/exits; this Python loop is NOT Linux systemd."""
    root = subprocess_root
    attempts = []
    for mode in (crash, "normal"):
        result = child(root, 12, 0, "ON", mode)
        attempts.append(result.returncode)
        if result.returncode in (0, guard.SAFETY_STOP_EXIT):
            break
    assert attempts == [exit_status, 0]
    result = child(root, 12, 0, "ON", "safety")
    assert result.returncode == guard.SAFETY_STOP_EXIT, result.stderr
    (root / "receipt.json").unlink()
    result = child(root, 12, 1, "ON")
    assert result.returncode == guard.SAFETY_STOP_EXIT
    assert not (root / "receipt.json").exists()


def test_repeated_crashes_obey_bounded_systemd_SHIM(subprocess_root):
    unit = (ROOT / "ops/systemd/project-mai-tai-option-a-daily-guard.service").read_text()
    burst = int(next(line.split("=", 1)[1] for line in unit.splitlines() if line.startswith("StartLimitBurst=")))
    attempts = [child(subprocess_root, 12, 0, "ON", "crash").returncode for _ in range(burst)]
    assert attempts == [1, 1, 1]


def test_sampler_actual_restart_parser_and_first_baseline(tmp_path, monkeypatch):
    sampler = load("momrestart_sampler", G1 / "sampler.py")
    log = tmp_path / "gateway.log"
    log.write_text("old1008 (policy violation)\n")
    output = tmp_path / "samples.jsonl"
    now = datetime(2026, 10, 16, 4, 30, tzinfo=ET)
    end = datetime(2026, 10, 16, 18, 10, tzinfo=ET)
    clock = iter([now, end])
    monkeypatch.setattr(sampler.time, "sleep", lambda _: None)
    assert sampler.main(["--gateway-log", str(log), "--output", str(output),
        "--treatment-date", "2026-10-16", "--end-utc", end.isoformat(),
        "--restart-session"], clock=lambda: next(clock)) == 0
    row = json.loads(output.read_text())
    assert row["status"] == "BASELINE" and row["new_1008_lines"] == 0
    assert row["mode"] == "session_restart"
    assert row["coverage"] == "new baseline only; earlier gap NOT covered"


def test_sampler_restart_keeps_new_1008_safety_stop(tmp_path, monkeypatch):
    sampler = load("momrestart_sampler_stop", G1 / "sampler.py")
    log = tmp_path / "gateway.log"
    log.write_text("baseline\n")
    output = tmp_path / "samples.jsonl"
    now = datetime(2026, 10, 16, 7, 30, tzinfo=ET)
    end = datetime(2026, 10, 16, 18, 10, tzinfo=ET)
    calls = 0
    def clock():
        nonlocal calls
        calls += 1
        if calls == 2:
            with log.open("a") as f:
                f.write("received1008 (policy violation)\n")
        return now + timedelta(seconds=calls - 1)
    monkeypatch.setattr(sampler.time, "sleep", lambda _: None)
    assert sampler.main(["--gateway-log", str(log), "--output", str(output),
        "--treatment-date", "2026-10-16", "--end-utc", end.isoformat(),
        "--restart-session"], clock=clock) == 3
    assert json.loads(output.read_text().splitlines()[-1])["status"] == "STOP_TRIGGER"


def test_units_bound_recovery_and_block_latched_safety():
    unit = (ROOT / "ops/systemd/project-mai-tai-option-a-daily-guard.service").read_text()
    paper = (ROOT / "ops/systemd/project-mai-tai-momentum-paper.service").read_text()
    for content in (unit, paper):
        assert "Restart=on-failure" in content
        assert "RestartPreventExitStatus=78" in content
        assert "StartLimitBurst=3" in content
    assert "ExecStopPost=/bin/systemctl stop project-mai-tai-momentum-paper.service" in unit
    assert "--paper-prestart" in paper
