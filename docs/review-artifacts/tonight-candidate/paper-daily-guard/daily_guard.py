#!/usr/bin/env python3
"""Start the existing paper session under the unchanged, calibrated Option A guard."""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REPO = Path("/home/trader/project-mai-tai")
sys.path.insert(0, str(REPO / "src"))
ROOT = Path(__file__).resolve().parent


def session_day(now, holidays):
    day = now.date()
    if now.time() >= time(9, 40):
        day += timedelta(days=1)
    while day.weekday() >= 5 or day in holidays:
        day += timedelta(days=1)
    return day


def closed_timer_day(now, holidays):
    return now.time() < time(9, 40) and (now.weekday() >= 5 or now.date() in holidays)


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


def main():
    from redis import Redis
    from project_mai_tai.settings import Settings
    from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS

    verify_manifest()
    now = datetime.now(UTC)
    et = now.astimezone(ET)
    if closed_timer_day(et, US_MARKET_HOLIDAYS):
        print("SKIP closed trading day", flush=True)
        return 0
    guard = load_guard()
    day = session_day(et, US_MARKET_HOLIDAYS)
    output = Path("/home/trader/after-hours") / day.isoformat() / "option-a-daily" / now.strftime("run-%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, mode=0o700)
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    redis = Redis.from_url(settings.redis_url, decode_responses=True,
                           socket_timeout=5, socket_connect_timeout=5)
    audit = output / "option-a-guard.jsonl"
    try:
        gateway = state("project-mai-tai-market-data.service")
        if gateway["MainPID"] != "2907" or gateway["ActiveState"] != "active" or gateway["NRestarts"] != "0":
            raise guard.Blind("gateway identity drift")
        owners = guard._owners(redis, settings.redis_stream_prefix)
        if set(owners) != {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper"} or len(owners["momentum-paper"]) > 16:
            raise guard.Blind("gateway owners incomplete or paper slot contested")
        trigger, detail = guard.RedisSafety(redis).sample()
        if trigger:
            raise guard.Blind(trigger)
        signals = guard.LiveSignals(redis, settings.redis_stream_prefix)
        status, age, _ = signals.heartbeat(now)
        if status != "healthy" or age > guard.HEARTBEAT_AGE_BOUND:
            raise guard.Blind("gateway heartbeat unreadable before start")
        guard._audit(audit, {"action": "daily_window", "at_utc": now.isoformat(),
            "session_date": day.isoformat(), "end_et": datetime.combine(day, time(9, 40), tzinfo=ET).isoformat(),
            "gateway": gateway, "redis": detail,
            "rules": "unchanged Option A; detection09:30/feed09:40:01; daily close after guard completes"})
        # READY follows sampler launch; ExecStartPost starts paper only then.
        rc = guard.run_guard(day, output)
        if rc == 0:
            return guard.stop_paper("scheduled_session_close", redis, settings, audit)
        return rc
    except Exception as exc:
        return guard.stop_paper(f"daily_guard_UNKNOWN:{type(exc).__name__}:{exc}", redis, settings, audit) or 1
    finally:
        redis.close()


if __name__ == "__main__":
    raise SystemExit(main())
