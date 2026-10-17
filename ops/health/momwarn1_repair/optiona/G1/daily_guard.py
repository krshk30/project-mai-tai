#!/usr/bin/env python3
"""Start the existing paper session under the unchanged, calibrated Option A guard."""
from __future__ import annotations

import importlib.util
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time as wall_time
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REPO = Path("/home/trader/project-mai-tai")
sys.path.insert(0, str(REPO / "src"))
ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)


class PlannedStopNotifications:
    """Keep planned-stop transport failures separate from paper safety results."""

    def __init__(self, guard):
        self.guard = guard
        self.errors = 0
        self.lock = threading.Lock()

    def _error(self, phase, exc):
        with self.lock:
            self.errors += 1
            count = self.errors
        LOGGER.error("MOMGUARDEND1-NOTIFY-ERROR phase=%s count=%s error=%s",
                     phase, count, type(exc).__name__)

    def send(self, message, *, timeout=1):
        try:
            if self.guard._notify_systemd(message, timeout=timeout) is False:
                raise RuntimeError("notify socket absent")
        except Exception as exc:
            self._error(message, exc)

    def pulse(self):
        self.send("WATCHDOG=1")

    def _heartbeat(self, finished, deadline):
        try:
            while True:
                remaining = deadline - wall_time.monotonic()
                if remaining <= 0 or finished.wait(min(1, remaining)):
                    return
                remaining = deadline - wall_time.monotonic()
                if remaining <= 0:
                    return
                self.send("WATCHDOG=1", timeout=min(1, remaining))
        except Exception as exc:
            self._error("worker", exc)

    @contextmanager
    def during(self, seconds):
        # A helper exists only for this blocking step's existing timeout.
        self.pulse()
        finished = threading.Event()
        started = False
        try:
            try:
                heartbeat = threading.Thread(target=self._heartbeat,
                    args=(finished, wall_time.monotonic() + seconds), daemon=True)
                heartbeat.start()
                started = True
            except Exception as exc:
                self._error("worker-start", exc)
            yield
        finally:
            finished.set()
            if started:
                try:
                    heartbeat.join(timeout=2)
                    if heartbeat.is_alive():
                        raise RuntimeError("worker still alive")
                except Exception as exc:
                    self._error("worker-cleanup", exc)


def planned_paper_stop(guard, reason, redis, settings, audit):
    notifications = PlannedStopNotifications(guard)
    previous = getattr(guard, "_planned_stop_notifications", None)
    guard._planned_stop_notifications = notifications
    try:
        rc = guard.stop_paper(reason, redis, settings, audit)
    finally:
        guard._planned_stop_notifications = previous
    if rc == 0:
        notifications.send("STOPPING=1")
    return rc


def session_day(now, holidays, validation_window=None):
    day = now.date()
    end = validation_window.guard_end_for(day) if validation_window else time(18, 10)
    if now.time() >= end:
        day += timedelta(days=1)
    while day.weekday() >= 5 or day in holidays:
        day += timedelta(days=1)
    return day


def closed_timer_day(now, holidays, validation_window=None):
    end = validation_window.guard_end_for(now.date()) if validation_window else time(18, 10)
    return now.time() < end and (now.weekday() >= 5 or now.date() in holidays)


def closed_session(now, holidays, validation_window):
    return (closed_timer_day(now, holidays, validation_window)
            or now.time() >= validation_window.guard_end_for(now.date()))


def continuation_session(et, window):
    return (window.session_date == date(2026, 10, 13)
            and et.date() == date(2026, 10, 13) and time(7) <= et.time() < time(18, 10))


def exec_condition():
    """Read-only startup gate; 255 remains subject to native manager approval."""
    try:
        from project_mai_tai.settings import Settings
        from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS
        from project_mai_tai.momwarn_validation_window import MomentumValidationWindow

        manifest = verify_manifest()
        required = {"daily_guard.py", "guard_rules.py",
                    "project-mai-tai-option-a-daily-guard.service"}
        artifacts = manifest["artifacts"]
        if not isinstance(artifacts, dict) or not required <= artifacts.keys():
            raise ValueError("condition manifest lacks required artifacts")
        gateway_identity_pin(manifest)
        settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
        window = MomentumValidationWindow.from_settings(settings)
        guard = load_guard()
        guard.require_no_safety_stop()
        now = datetime.now(UTC)
        et = now.astimezone(ET)
        continuation = continuation_session(et, window)
        day = et.date() if continuation else session_day(et, US_MARKET_HOLIDAYS, window)
        # Retain the original clock gate even when calendar classification skips.
        guard.treatment_clock(day, now, window, continuation=continuation)
        if not continuation and closed_session(et, US_MARKET_HOLIDAYS, window):
            print("MOMGUARDEND1-CONDITION SKIP closed session", flush=True)
            return 1
        return 0
    except Exception as exc:
        print(f"MOMGUARDEND1-CONDITION FAIL error={type(exc).__name__}",
              file=sys.stderr, flush=True)
        return 255


def state(unit):
    output = subprocess.check_output([
        "systemctl", "show", unit, "-p", "MainPID", "-p", "ActiveState", "-p", "NRestarts",
        "-p", "ExecMainStartTimestamp",
    ], text=True, timeout=10, env={**os.environ, "TZ": "UTC", "LC_ALL": "C"})
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def load_guard():
    spec = importlib.util.spec_from_file_location("daily_guard_rules", ROOT / "guard_rules.py")
    guard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = guard
    spec.loader.exec_module(guard)
    return guard


def verify_manifest():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    for name, expected in manifest["artifacts"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("daily guard artifact hash mismatch: " + name)
    return manifest


def start_instant(value):
    """systemctl's display zone is not part of a Linux process identity."""
    if not isinstance(value, str):
        raise ValueError("start timestamp absent")
    zone = value.rsplit(" ", 1)[-1]
    offsets = {"UTC": 0, "GMT": 0, "EST": -5, "EDT": -4}
    if zone not in offsets:
        raise ValueError("start timestamp has unsupported/unknown explicit zone")
    stamp = datetime.strptime(value.rsplit(" ", 1)[0], "%a %Y-%m-%d %H:%M:%S")
    return stamp.replace(tzinfo=timezone(timedelta(hours=offsets[zone]))).astimezone(UTC)


def gateway_identity_pin(manifest):
    """Validate the baseline shape without probing a process or manager."""
    pin = manifest.get("gateway_identity")
    if not isinstance(pin, dict) or type(pin.get("version")) is not int or pin["version"] != 1:
        raise ValueError("gateway identity baseline missing or unsupported")
    if (pin.get("unit") != "project-mai-tai-market-data.service"
            or type(pin.get("main_pid")) is not int or pin["main_pid"] <= 0
            or type(pin.get("start_ticks")) is not int or pin["start_ticks"] <= 0
            or not isinstance(pin.get("exec_main_start_timestamp"), str)
            or not pin["exec_main_start_timestamp"].strip()):
        raise ValueError("gateway identity baseline malformed")
    start_instant(pin["exec_main_start_timestamp"])
    return pin


def verify_gateway_identity(manifest, gateway, *, proc_root=Path("/proc")):
    pin = gateway_identity_pin(manifest)
    if (gateway.get("MainPID") != str(pin["main_pid"])
            or gateway.get("ActiveState") != "active"
            or gateway.get("NRestarts") != "0"
            or start_instant(gateway.get("ExecMainStartTimestamp")) != start_instant(pin["exec_main_start_timestamp"])):
        raise ValueError("gateway identity drift")
    raw = (proc_root / str(pin["main_pid"]) / "stat").read_bytes()
    if len(raw) > 4096:
        raise ValueError("gateway process stat exceeds bound")
    fields = raw.decode().rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X"} or int(fields[19]) != pin["start_ticks"]:
        raise ValueError("gateway process instance drift")


def verify_owners(owners, *, warning_enabled):
    required = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper"}
    if type(warning_enabled) is not bool:
        raise ValueError("warning flag unreadable")
    allowed = required | ({"momentum-warning"} if warning_enabled else set())
    if not required <= set(owners) <= allowed:
        raise ValueError("gateway owners incomplete or unexpected")
    shared = owners["momentum-paper"] | owners.get("momentum-warning", set())
    if len(shared) > 16:
        raise ValueError("paper warning union slot contested")


def verify_live_owner_scope(manifest, settings, redis, guard, owners, *, proc_root=Path("/proc")):
    enabled = getattr(settings, "momentum_live_enabled", False)
    if type(enabled) is not bool:
        raise ValueError("live flag unreadable")
    if not enabled and "momentum-live" not in owners:
        return owners
    path = REPO / "ops/health/momlive1_owner_compat.py"
    expected = manifest.get("momlive_owner_compat_sha256")
    if not isinstance(expected, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("live owner verifier source unproven")
    spec = importlib.util.spec_from_file_location("momlive_owner_scope", path)
    compat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compat)
    oms = state("project-mai-tai-oms.service")
    if oms.get("ActiveState") != "active" or not str(oms.get("MainPID", "")).isdigit():
        raise ValueError("live owner OMS process unreadable")
    pid = int(oms["MainPID"])
    if pid <= 0:
        raise ValueError("live owner OMS process absent")
    raw = (proc_root / str(pid) / "stat").read_bytes()
    if len(raw) > 4096:
        raise ValueError("live owner OMS process stat oversized")
    fields = raw.decode().rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X"}:
        raise ValueError("live owner OMS process dead")
    instance = str(pid) + ":" + str(int(fields[19]))
    rows = redis.xrevrange(settings.redis_stream_prefix + ":momentum-live-owner-proof", count=1)
    if len(rows) != 1 or "data" not in rows[0][1]:
        raise ValueError("live owner proof absent")
    proof = compat.decode_proof(rows[0][1]["data"])
    accounts = {settings.strategy_schwab_1m_v2_account_name,
                settings.strategy_schwab_1m_v2_webull_account_name}
    compat.validate_live_owner(owners.get("momentum-live", set()), proof,
        enabled=enabled, now_ns=wall_time.time_ns(),
        max_age_ns=int(guard.HEARTBEAT_AGE_BOUND * 1_000_000_000),
        expected_instance=instance, expected_accounts=accounts)
    current = state("project-mai-tai-oms.service")
    if any(current.get(key) != oms.get(key) for key in
           ("MainPID", "ActiveState", "ExecMainStartTimestamp")):
        raise ValueError("live owner OMS identity changed during proof")
    # Only the independently verified literal owner is removed from the old gate.
    return {name: symbols for name, symbols in owners.items() if name != "momentum-live"}


def prestart_checks(manifest, guard, settings, redis, now):
    gateway = state("project-mai-tai-market-data.service")
    try:
        verify_gateway_identity(manifest, gateway)
    except (KeyError, ValueError, TypeError, IndexError, OSError) as exc:
        raise guard.Blind("gateway identity drift") from exc
    owners = guard._owners(redis, settings.redis_stream_prefix)
    try:
        owners = verify_live_owner_scope(manifest, settings, redis, guard, owners)
        verify_owners(owners, warning_enabled=settings.market_data_momentum_warning_enabled)
    except (KeyError, ValueError, TypeError) as exc:
        raise guard.Blind("gateway owners incomplete or paper slot contested") from exc
    trigger, detail = guard.RedisSafety(redis).sample()
    if trigger:
        raise guard.Blind(trigger)
    signals = guard.LiveSignals(redis, settings.redis_stream_prefix)
    status, age, _ = signals.heartbeat(now)
    if status != "healthy" or age > guard.HEARTBEAT_AGE_BOUND:
        raise guard.Blind("gateway heartbeat unreadable before start")
    return gateway, detail


def main(argv=None):
    # No Redis construction, run output, notify, or child command on this path.
    if argv is None:
        argv = sys.argv[1:]
    if "--exec-condition" in argv:
        if argv != ["--exec-condition"]:
            print("MOMGUARDEND1-CONDITION FAIL incompatible arguments", file=sys.stderr)
            return 255
        return exec_condition()
    from redis import Redis
    from project_mai_tai.settings import Settings
    from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS
    from project_mai_tai.momwarn_validation_window import MomentumValidationWindow

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run-prestart", action="store_true")
    parser.add_argument("--paper-prestart", action="store_true")
    parser.add_argument("--continue-oct13", action="store_true")
    args = parser.parse_args(argv)
    manifest = verify_manifest()
    now = datetime.now(UTC)
    et = now.astimezone(ET)
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    validation_window = MomentumValidationWindow.from_settings(settings)
    continuation = args.continue_oct13 or continuation_session(et, validation_window)
    guard = load_guard()
    day = et.date() if continuation else session_day(et, US_MARKET_HOLIDAYS, validation_window)
    try:
        guard.require_no_safety_stop()
    except guard.Blind as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return guard.SAFETY_STOP_EXIT
    if continuation:
        # Validate the date/clock even for a read-only prestart; never roll it to tomorrow.
        guard.treatment_clock(et.date(), now, validation_window, continuation=True)
    else:
        guard.treatment_clock(day, now, validation_window)
    if args.dry_run_prestart or args.paper_prestart:
        redis = Redis.from_url(settings.redis_url, decode_responses=True,
                               socket_timeout=5, socket_connect_timeout=5)
        try:
            gateway, _ = prestart_checks(manifest, guard, settings, redis, now)
            if args.paper_prestart:
                current = state("project-mai-tai-option-a-daily-guard.service")
                if current.get("ActiveState") not in {"active", "activating"} or int(current.get("MainPID", "0")) <= 0:
                    raise guard.Blind("paper cannot start without its guard")
                if current["ActiveState"] == "active":
                    # The initial ExecStartPost is still activating. A later
                    # paper restart must reset only monitoring, never the ledger.
                    os.kill(int(current["MainPID"]), signal.SIGUSR1)
                return 0
            print(json.dumps({"status": "PASS", "dry_run_prestart": True,
                              "gateway": gateway, "at_utc": now.isoformat(),
                              "scope": "Current pre-start checks only; future session health not asserted"}),
                  flush=True)
            return 0
        except guard.Blind as exc:
            if not args.paper_prestart:
                raise
            try:
                guard.retain_safety_stop(f"paper_prestart_STOP:{exc}", day)
            except OSError as error:
                print(f"paper prestart durable latch UNKNOWN:{type(error).__name__}",
                      file=sys.stderr, flush=True)
            # ExecStartPre has no running paper child to stop. A synchronous
            # systemctl stop of its own unit would deadlock/kill this verifier.
            # The existing guard sees the durable latch on its next tick.
            return guard.SAFETY_STOP_EXIT
        finally:
            redis.close()
    if not continuation and closed_session(et, US_MARKET_HOLIDAYS, validation_window):
        print("SKIP closed session", flush=True)
        return guard.SAFETY_STOP_EXIT
    while True:
        now = datetime.now(UTC)
        output = Path("/home/trader/after-hours") / day.isoformat() / "option-a-daily" / now.strftime("run-%Y%m%dT%H%M%S%fZ")
        output.mkdir(parents=True, mode=0o700)
        redis = Redis.from_url(settings.redis_url, decode_responses=True,
                               socket_timeout=5, socket_connect_timeout=5)
        audit = output / "option-a-guard.jsonl"
        try:
            guard.require_no_safety_stop()
            gateway, detail = prestart_checks(manifest, guard, settings, redis, now)
            guard._audit(audit, {"action": "daily_window", "at_utc": now.isoformat(),
                "session_date": day.isoformat(),
                "end_et": datetime.combine(day, time(18, 10) if continuation else validation_window.guard_end_for(day), tzinfo=ET).isoformat(),
                "gateway": gateway, "redis": detail,
                "rules": "unchanged Option A safety; each restart has a new baseline; earlier gap NOT covered"})
            rc = (guard.run_guard(day, output, continuation=True) if continuation
                  else guard.run_guard(day, output))
            if rc == 0:
                return planned_paper_stop(guard, "scheduled_session_close", redis, settings, audit)
            return rc
        except guard.PlannedStop:
            return planned_paper_stop(guard, "planned_service_stop", redis, settings, audit)
        except guard.RestartBaseline:
            continue
        except Exception as exc:
            reason = f"daily_guard_UNKNOWN:{type(exc).__name__}:{exc}"
            return guard.safety_shutdown(reason, day, redis, settings, audit)
        finally:
            redis.close()


if __name__ == "__main__":
    raise SystemExit(main())
