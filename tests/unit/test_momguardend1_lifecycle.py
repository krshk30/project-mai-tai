"""Controlled source lifecycle controls; NOT native Linux/systemd acceptance.

No child commands, real Redis/provider calls or systemd operations. Virtual
watchdog/worker scheduling is explicitly a SHIM; main owns later execution.
"""
from datetime import date, datetime, time
import importlib.util
from pathlib import Path
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


daily = load("momguardend1_daily", G1 / "daily_guard.py")
rules = load("momguardend1_rules", G1 / "guard_rules.py")


class WatchdogSHIM:
    """15s expiry arithmetic, not a simulation of notify service startup."""
    def __init__(self):
        self.last_ping = 0
        self.messages = []

    def notify(self, at, message):
        assert not self.expired(at), "15s watchdog expired before notification"
        self.messages.append((at, message))
        if message == "WATCHDOG=1":
            self.last_ping = at

    def expired(self, at):
        return at - self.last_ping >= 15


@pytest.fixture
def shutdown(monkeypatch):
    now = [0]
    events = []
    watchdog = WatchdogSHIM()
    threads = []

    class EventSHIM:
        def __init__(self):
            self.finished = False
            self.stop_at = None

        def wait(self, seconds):
            if self.finished:
                return True
            now[0] += seconds
            return self.stop_at is not None and now[0] >= self.stop_at

        def set(self):
            self.finished = True
            events.append("heartbeat-finished")

    class ThreadSHIM:
        def __init__(self, *, target, args, daemon):
            self.target, self.args = target, args
            self.daemon = daemon
            threads.append(self)

        def start(self):
            events.append("heartbeat-start")

        def join(self, *, timeout):
            assert timeout == 2
            events.append("heartbeat-join")

        def is_alive(self):
            return False

    def notify(message, **kwargs):
        watchdog.notify(now[0], message)
        events.append(message)

    monkeypatch.setattr(daily.wall_time, "monotonic", lambda: now[0])
    monkeypatch.setattr(daily.threading, "Event", EventSHIM)
    monkeypatch.setattr(daily.threading, "Thread", ThreadSHIM)
    guard = SimpleNamespace(_notify_systemd=notify)
    return SimpleNamespace(now=now, events=events, watchdog=watchdog,
                           threads=threads, guard=guard)


@pytest.mark.parametrize("reason", ["scheduled_session_close", "planned_service_stop"])
def test_planned_stop_over_15s_pings_then_stopping_and_exit0_SHIM(shutdown, reason):
    s = shutdown

    def stop(actual_reason, *args):
        assert actual_reason == reason
        s.events.append("paper-stop-begin")
        notifications = s.guard._planned_stop_notifications
        with notifications.during(35):
            worker = s.threads[0]
            worker.target(*worker.args)
        s.now[0] += 1
        notifications.pulse()
        s.events.append("paper-stop-and-owner-release-confirmed")
        return 0

    s.guard.stop_paper = stop
    assert daily.planned_paper_stop(s.guard, reason, None, None, None) == 0
    pings = [at for at, message in s.watchdog.messages if message == "WATCHDOG=1"]
    assert pings == list(range(35)) + [36]
    assert not s.watchdog.expired(s.now[0])
    assert s.events.index("paper-stop-and-owner-release-confirmed") < s.events.index("STOPPING=1")
    assert s.events.index("heartbeat-join") < s.events.index("STOPPING=1")
    assert s.threads[0].args[0].finished
    assert s.guard._planned_stop_notifications is None
    assert s.watchdog.messages[-1] == (36, "STOPPING=1")


@pytest.mark.parametrize("rc", [2, 78])
def test_failed_stop_preserves_failure_without_stopping_success(shutdown, rc):
    s = shutdown
    s.guard.stop_paper = lambda *args: rc
    assert daily.planned_paper_stop(s.guard, "scheduled_session_close", None, None, None) == rc
    assert "STOPPING=1" not in s.events
    assert not s.threads
    assert s.guard._planned_stop_notifications is None


def test_stop_exception_cleans_up_heartbeat_and_propagates(shutdown):
    def stop(*args):
        raise OSError("controlled stop failure")

    shutdown.guard.stop_paper = stop
    with pytest.raises(OSError, match="controlled stop failure"):
        daily.planned_paper_stop(shutdown.guard, "scheduled_session_close", None, None, None)
    assert shutdown.guard._planned_stop_notifications is None
    assert "STOPPING=1" not in shutdown.events


def test_actual_stop_two_release_windows_and_page_can_succeed_after_120_SHIM(shutdown, monkeypatch, tmp_path):
    s = shutdown
    calls = []

    def owners(*args):
        s.now[0] += 4
        return {"momentum-paper": {"CONTROL"}}

    def blocking_step(seconds):
        worker = s.threads[-1]
        worker.args[0].stop_at = s.now[0] + seconds
        worker.target(*worker.args)

    def stop_command(argv, **kwargs):
        assert argv == ["systemctl", "stop", rules.PAPER_UNIT]
        assert kwargs["timeout"] == 35
        blocking_step(34)
        calls.append("paper-stopped")
        return SimpleNamespace(returncode=0)

    def release(*args, **kwargs):
        calls.append(("release", s.now[0]))
        return s.now[0] >= 111

    def page(*args):
        blocking_step(14)
        calls.append("page-delivered")
        return True

    class RedisSHIM:
        def xadd(self, *args):
            s.now[0] += 4
            calls.append("paper-only-replace")
            return "controlled-event"

    def sleep(seconds):
        s.now[0] += seconds

    monkeypatch.setattr(rules, "_notify_systemd", s.guard._notify_systemd)
    monkeypatch.setattr(rules, "_owners", owners)
    monkeypatch.setattr(rules, "GATEWAY_LOG", SimpleNamespace(stat=lambda: SimpleNamespace(st_size=0)))
    monkeypatch.setattr(rules.subprocess, "run", stop_command)
    monkeypatch.setattr(rules, "_release_confirmed", release)
    monkeypatch.setattr(rules, "_page_with_audit", page)
    monkeypatch.setattr(rules.time, "sleep", sleep)
    settings = SimpleNamespace(redis_stream_prefix="controlled")
    assert daily.planned_paper_stop(rules, "scheduled_session_close", RedisSHIM(), settings,
                                    tmp_path / "audit.jsonl") == 0
    assert s.now[0] == 125
    assert "paper-only-replace" in calls
    assert ("release", 38) in calls and ("release", 111) in calls
    assert calls[-1] == "page-delivered"
    assert s.watchdog.messages[-1] == (125, "STOPPING=1")
    assert all(thread.args[0].finished for thread in s.threads)
    assert rules._planned_stop_notifications is None


def test_each_blocking_step_stops_its_worker_at_its_own_budget_SHIM(shutdown):
    s = shutdown
    notifications = daily.PlannedStopNotifications(s.guard)
    with notifications.during(35):
        s.threads[-1].target(*s.threads[-1].args)
        assert s.now[0] == 35
        assert s.watchdog.last_ping == 34
        assert s.watchdog.expired(49)


@pytest.mark.parametrize("failed_message", ["WATCHDOG=1", "STOPPING=1"])
def test_transport_errors_count_separately_without_changing_success(shutdown, caplog, failed_message):
    s = shutdown
    observed = []

    def notify(message, **kwargs):
        if message == failed_message:
            raise OSError("controlled notify failure")

    s.guard._notify_systemd = notify

    def stop(*args):
        notifications = s.guard._planned_stop_notifications
        observed.append(notifications)
        notifications.pulse()
        return 0

    s.guard.stop_paper = stop
    assert daily.planned_paper_stop(s.guard, "scheduled_session_close", None, None, None) == 0
    assert observed[0].errors == 1
    assert "MOMGUARDEND1-NOTIFY-ERROR" in caplog.text
    assert "count=1" in caplog.text
    assert s.guard._planned_stop_notifications is None


def test_worker_exception_is_explicit_not_only_unhandled_thread_output(shutdown, caplog):
    notifications = daily.PlannedStopNotifications(shutdown.guard)

    class BrokenWait:
        def wait(self, seconds):
            raise RuntimeError("controlled worker failure")

    notifications._heartbeat(BrokenWait(), 35)
    assert notifications.errors == 1
    assert "phase=worker" in caplog.text


@pytest.mark.parametrize("phase", ["connect", "sendall", "missing", "success"])
def test_planned_notify_transport_deadline_and_cleanup(monkeypatch, shutdown, caplog, phase):
    s = shutdown
    calls = []

    class SocketSHIM:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def settimeout(self, seconds):
            calls.append(("timeout", seconds))

        def connect(self, address):
            calls.append(("connect", address))
            s.now[0] += 0.25
            if phase == "connect":
                raise TimeoutError("controlled connect timeout")

        def sendall(self, message):
            calls.append(("sendall", message))
            if phase == "sendall":
                raise TimeoutError("controlled send timeout")

    if phase == "missing":
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    else:
        monkeypatch.setenv("NOTIFY_SOCKET", "/controlled-notify" if phase == "success" else "@controlled-notify")
    monkeypatch.setattr(rules.socket, "socket", lambda *args: SocketSHIM())
    notifications = daily.PlannedStopNotifications(rules)
    notifications.pulse()
    assert notifications.errors == (0 if phase == "success" else 1)
    assert ("MOMGUARDEND1-NOTIFY-ERROR" in caplog.text) == (phase != "success")
    if phase == "missing":
        assert not calls
    else:
        assert ("timeout", 1) in calls
        assert ("connect", "/controlled-notify" if phase == "success" else "\0controlled-notify") in calls
        assert calls[-1] == "closed"
        if phase in {"sendall", "success"}:
            assert ("timeout", 0.75) in calls


@pytest.mark.parametrize("failure", ["start", "cleanup"])
def test_worker_lifecycle_error_does_not_override_paper_zero(shutdown, monkeypatch, caplog, failure):
    s = shutdown
    worker_class = daily.threading.Thread
    if failure == "start":
        def fail_start(self):
            raise RuntimeError("controlled worker start error")
        monkeypatch.setattr(worker_class, "start", fail_start)
    else:
        monkeypatch.setattr(worker_class, "is_alive", lambda self: True)

    def stop(*args):
        with s.guard._planned_stop_notifications.during(35):
            return 0

    s.guard.stop_paper = stop
    assert daily.planned_paper_stop(s.guard, "scheduled_session_close", None, None, None) == 0
    assert "phase=worker-" + failure in caplog.text
    assert s.guard._planned_stop_notifications is None


@pytest.fixture
def main_case(monkeypatch, tmp_path, shutdown):
    import redis
    import project_mai_tai.settings as settings_module
    import project_mai_tai.momwarn_validation_window as window_module
    import project_mai_tai.strategy_core.time_utils as time_module

    s = shutdown
    clock = [datetime(2026, 10, 16, 12, tzinfo=ET)]
    calls = []
    window = SimpleNamespace(session_date=None, guard_end_for=lambda day: time(18, 10))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0]

    class RedisSHIM:
        @classmethod
        def from_url(cls, *args, **kwargs):
            calls.append("redis-open")
            return cls()

        def close(self):
            calls.append("redis-close")

    def monitor(day, output, **kwargs):
        assert not s.threads, "no independent heartbeat during monitoring"
        calls.append(("monitor", day, rules.treatment_clock(day, clock[0], window)))
        rules._notify_systemd("READY=1")
        return 0

    original_mkdir = Path.mkdir

    def local_mkdir(path, *args, **kwargs):
        if str(path).startswith("/home/trader/after-hours/"):
            calls.append("output-directory-SHIM")
            return None
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(daily, "datetime", Clock)
    monkeypatch.setattr(daily, "verify_manifest", lambda: {})
    monkeypatch.setattr(daily, "load_guard", lambda: rules)
    monkeypatch.setattr(daily, "prestart_checks", lambda *args: ({}, {}))
    monkeypatch.setattr(rules, "SAFETY_STOP_FILE", tmp_path / "safety-stop.json")
    monkeypatch.setattr(rules, "_audit", lambda *args: None)
    monkeypatch.setattr(rules, "_notify_systemd", s.guard._notify_systemd)
    monkeypatch.setattr(rules, "run_guard", monitor)
    def stop(reason, *args):
        calls.append(("stop", reason))
        with rules._planned_stop_notifications.during(35):
            pass
        return 0
    monkeypatch.setattr(rules, "stop_paper", stop)
    monkeypatch.setattr(redis, "Redis", RedisSHIM)
    monkeypatch.setattr(settings_module, "Settings", lambda **kwargs: SimpleNamespace(redis_url="redis://never-connect.invalid"))
    monkeypatch.setattr(window_module, "MomentumValidationWindow", SimpleNamespace(from_settings=lambda settings: window))
    monkeypatch.setattr(time_module, "US_MARKET_HOLIDAYS", set())
    monkeypatch.setattr(Path, "mkdir", local_mkdir)
    return SimpleNamespace(clock=clock, calls=calls, shutdown=s, window=window)


def test_inside_session_ready_precedes_planned_shutdown(main_case):
    c = main_case
    assert daily.main([]) == 0
    messages = [message for _, message in c.shutdown.watchdog.messages]
    assert messages == ["READY=1", "WATCHDOG=1", "STOPPING=1"]
    assert ("stop", "scheduled_session_close") in c.calls
    assert c.calls[-1] == "redis-close"


@pytest.mark.parametrize("stamp", [
    "2026-10-16T18:10:00", "2026-10-16T18:11:00",
    "2026-10-16T23:59:00", "2026-10-17T03:40:00",
])
def test_closed_start_and_restart_after_end_no_ready_or_paper(main_case, stamp):
    c = main_case
    c.clock[0] = datetime.fromisoformat(stamp).replace(tzinfo=ET)
    # Repeat actual main entry to represent an off-hours restart request.
    for _ in range(2):
        assert daily.main([]) == rules.SAFETY_STOP_EXIT == 78
    assert not c.calls
    assert not c.shutdown.threads
    assert not c.shutdown.watchdog.messages
    unit = (ROOT / "ops/systemd/project-mai-tai-option-a-daily-guard.service").read_text()
    assert "RestartPreventExitStatus=78" in unit


def test_mon0340_after_failed_state_keeps_real_clock_gate_SHIM(main_case):
    c = main_case
    c.clock[0] = datetime(2026, 10, 19, 3, 40, tzinfo=ET)
    # Manager dispatch from failed/expired-start-limit is a native proof gap.
    # This control proves the application entry on Monday needs no reset flag.
    assert daily.main([]) == 0
    row = next(call for call in c.calls if isinstance(call, tuple) and call[0] == "monitor")
    assert row[1] == date(2026, 10, 19)
    start, end = row[2]
    assert start.astimezone(ET) == datetime(2026, 10, 19, 4, tzinfo=ET)
    assert end.astimezone(ET) == datetime(2026, 10, 19, 18, 10, tzinfo=ET)
    assert c.shutdown.watchdog.messages[0][1] == "READY=1"


def test_mon0340_does_not_clear_safety_latch(main_case):
    c = main_case
    c.clock[0] = datetime(2026, 10, 19, 3, 40, tzinfo=ET)
    rules.SAFETY_STOP_FILE.write_text("retained STOP")
    assert daily.main([]) == 78
    assert rules.SAFETY_STOP_FILE.read_text() == "retained STOP"
    assert not c.calls
    assert not c.shutdown.threads


def test_in_session_hang_has_no_shutdown_worker_and_expires_SHIM(main_case, monkeypatch):
    c = main_case

    def hung_monitor(*args, **kwargs):
        rules._notify_systemd("READY=1")
        rules._notify_systemd("WATCHDOG=1")
        assert not c.shutdown.threads
        assert not c.shutdown.watchdog.expired(14)
        assert c.shutdown.watchdog.expired(15)
        # Abort the controlled call instead of issuing a real signal/command.
        raise KeyboardInterrupt("SHIM watchdog kill")

    monkeypatch.setattr(rules, "run_guard", hung_monitor)
    with pytest.raises(KeyboardInterrupt, match="SHIM watchdog kill"):
        daily.main([])
    assert not c.shutdown.threads
    assert not any(isinstance(call, tuple) and call[0] == "stop" for call in c.calls)


def test_safety_stop_return_never_creates_planned_heartbeat(main_case, monkeypatch):
    monkeypatch.setattr(rules, "run_guard", lambda *args, **kwargs: 78)
    assert daily.main([]) == 78
    assert not main_case.shutdown.threads
    assert not main_case.shutdown.watchdog.messages


def test_planned_service_signal_uses_shutdown_scope_only(main_case, monkeypatch):
    def planned_stop(*args, **kwargs):
        assert not main_case.shutdown.threads
        rules._notify_systemd("READY=1")
        raise rules.PlannedStop()

    monkeypatch.setattr(rules, "run_guard", planned_stop)
    assert daily.main([]) == 0
    assert ("stop", "planned_service_stop") in main_case.calls
    assert main_case.shutdown.watchdog.messages[-1][1] == "STOPPING=1"
    assert main_case.shutdown.threads[0].args[0].finished


def test_monitor_failure_uses_existing_safety_stop_without_worker(main_case, monkeypatch):
    def broken_monitor(*args, **kwargs):
        raise RuntimeError("controlled monitor failure")

    seen = []

    def safety_stop(reason, *args):
        assert not main_case.shutdown.threads
        seen.append(reason)
        return 78

    monkeypatch.setattr(rules, "run_guard", broken_monitor)
    monkeypatch.setattr(rules, "safety_shutdown", safety_stop)
    assert daily.main([]) == 78
    assert seen == ["daily_guard_UNKNOWN:RuntimeError:controlled monitor failure"]
    assert not main_case.shutdown.watchdog.messages


def test_permanent_unit_keeps_watchdog_and_paper_only_stop_hooks():
    unit = (ROOT / "ops/systemd/project-mai-tai-option-a-daily-guard.service").read_text()
    for directive in ("Type=notify", "WatchdogSec=15s", "Restart=on-failure",
                      "RestartPreventExitStatus=78", "StartLimitIntervalSec=300", "StartLimitBurst=3"):
        assert directive in unit.splitlines()
    stop_hooks = [line for line in unit.splitlines() if line.startswith("ExecStop")]
    assert stop_hooks == ["ExecStopPost=/bin/systemctl stop project-mai-tai-momentum-paper.service"]
    assert "SuccessExitStatus=78" not in unit
