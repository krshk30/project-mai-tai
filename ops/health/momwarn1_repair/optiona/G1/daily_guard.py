#!/usr/bin/env python3
"""Start the existing paper session under the unchanged, calibrated Option A guard."""
from __future__ import annotations

import importlib.util
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REPO = Path("/home/trader/project-mai-tai")
sys.path.insert(0, str(REPO / "src"))
ROOT = Path(__file__).resolve().parent


def session_day(now, holidays, validation_window=None):
    day = now.date()
    end = validation_window.guard_end_for(day) if validation_window else time(9, 40)
    if now.time() >= end:
        day += timedelta(days=1)
    while day.weekday() >= 5 or day in holidays:
        day += timedelta(days=1)
    return day


def closed_timer_day(now, holidays, validation_window=None):
    end = validation_window.guard_end_for(now.date()) if validation_window else time(9, 40)
    return now.time() < end and (now.weekday() >= 5 or now.date() in holidays)


def state(unit):
    output = subprocess.check_output([
        "systemctl", "show", unit, "-p", "MainPID", "-p", "ActiveState", "-p", "NRestarts",
        "-p", "ExecMainStartTimestamp",
    ], text=True, timeout=10)
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


def verify_gateway_identity(manifest, gateway, *, proc_root=Path("/proc")):
    pin = manifest.get("gateway_identity")
    if not isinstance(pin, dict) or type(pin.get("version")) is not int or pin["version"] != 1:
        raise ValueError("gateway identity baseline missing or unsupported")
    if (pin.get("unit") != "project-mai-tai-market-data.service"
            or type(pin.get("main_pid")) is not int or pin["main_pid"] <= 0
            or type(pin.get("start_ticks")) is not int or pin["start_ticks"] <= 0
            or not isinstance(pin.get("exec_main_start_timestamp"), str)
            or not pin["exec_main_start_timestamp"].strip()):
        raise ValueError("gateway identity baseline malformed")
    if (gateway.get("MainPID") != str(pin["main_pid"])
            or gateway.get("ActiveState") != "active"
            or gateway.get("NRestarts") != "0"
            or gateway.get("ExecMainStartTimestamp") != pin["exec_main_start_timestamp"]):
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


def prestart_checks(manifest, guard, settings, redis, now):
    gateway = state("project-mai-tai-market-data.service")
    try:
        verify_gateway_identity(manifest, gateway)
    except (KeyError, ValueError, TypeError, IndexError, OSError) as exc:
        raise guard.Blind("gateway identity drift") from exc
    owners = guard._owners(redis, settings.redis_stream_prefix)
    try:
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
    from redis import Redis
    from project_mai_tai.settings import Settings
    from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS
    from project_mai_tai.momwarn_validation_window import MomentumValidationWindow

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run-prestart", action="store_true")
    parser.add_argument("--continue-oct13", action="store_true")
    args = parser.parse_args(argv)
    manifest = verify_manifest()
    now = datetime.now(UTC)
    et = now.astimezone(ET)
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    validation_window = MomentumValidationWindow.from_settings(settings)
    continuation = args.continue_oct13 or (
        validation_window.session_date == date(2026, 10, 13)
        and et.date() == date(2026, 10, 13) and time(7) <= et.time() < time(18, 10)
    )
    if continuation:
        # Validate the date/clock even for a read-only prestart; never roll it to tomorrow.
        load_guard().treatment_clock(et.date(), now, validation_window, continuation=True)
    if args.dry_run_prestart:
        guard = load_guard()
        redis = Redis.from_url(settings.redis_url, decode_responses=True,
                               socket_timeout=5, socket_connect_timeout=5)
        try:
            gateway, _ = prestart_checks(manifest, guard, settings, redis, now)
            print(json.dumps({"status": "PASS", "dry_run_prestart": True,
                              "gateway": gateway, "at_utc": now.isoformat(),
                              "scope": "Current pre-start checks only; future session health not asserted"}),
                  flush=True)
            return 0
        finally:
            redis.close()
    if not continuation and closed_timer_day(et, US_MARKET_HOLIDAYS, validation_window):
        print("SKIP closed trading day", flush=True)
        return 0
    guard = load_guard()
    day = et.date() if continuation else session_day(et, US_MARKET_HOLIDAYS, validation_window)
    output = Path("/home/trader/after-hours") / day.isoformat() / "option-a-daily" / now.strftime("run-%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, mode=0o700)
    redis = Redis.from_url(settings.redis_url, decode_responses=True,
                           socket_timeout=5, socket_connect_timeout=5)
    audit = output / "option-a-guard.jsonl"
    try:
        gateway, detail = prestart_checks(manifest, guard, settings, redis, now)
        guard._audit(audit, {"action": "daily_window", "at_utc": now.isoformat(),
            "session_date": day.isoformat(),
            "end_et": datetime.combine(day, time(18, 10) if continuation else validation_window.guard_end_for(day), tzinfo=ET).isoformat(),
            "gateway": gateway, "redis": detail,
            "rules": "unchanged Option A safety; explicit date-scoped limits; close after guard completes"})
        # READY follows sampler launch; ExecStartPost starts paper only then.
        rc = (guard.run_guard(day, output, continuation=True) if continuation
              else guard.run_guard(day, output))
        if rc == 0:
            return guard.stop_paper("scheduled_session_close", redis, settings, audit)
        return rc
    except Exception as exc:
        return guard.stop_paper(f"daily_guard_UNKNOWN:{type(exc).__name__}:{exc}", redis, settings, audit) or 1
    finally:
        redis.close()


if __name__ == "__main__":
    raise SystemExit(main())
