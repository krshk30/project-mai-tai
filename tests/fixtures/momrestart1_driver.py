"""OFF-BOX subprocess driver. Operational systemd/Redis/monitor loop are SHIMS.

Runs the real daily main/clock/manifest/latch. No broker, provider, service or
Redis connection. This is not production or real Linux/systemd acceptance.
"""
from datetime import datetime
import importlib.util
import json
import os
import signal
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

repo = Path(__file__).resolve().parents[2]
root, stamp, enabled, mode = sys.argv[1:]
root = Path(root)
spec = importlib.util.spec_from_file_location("restart_subprocess_daily", repo / "ops/health/momwarn1_repair/optiona/G1/daily_guard.py")
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)
daily.ROOT = root
guard = daily.load_guard()
guard.SAFETY_STOP_FILE = root / "safety-stop.json"
now = datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo("America/New_York"))


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return now.astimezone(tz) if tz else now


class RedisShim:
    @classmethod
    def from_url(cls, *args, **kwargs):
        return cls()

    def close(self):
        pass


settings = SimpleNamespace(redis_url="redis://never-connect.invalid", momentum_live_enabled=enabled == "ON")


def monitor(day, output, **kwargs):
    guard.require_no_safety_stop()
    from project_mai_tai.momwarn_validation_window import MomentumValidationWindow
    start, end = guard.treatment_clock(day, now, MomentumValidationWindow())
    receipt = {"shim": "operational monitor/systemd/Redis mocked", "session_day": day.isoformat(),
               "enabled": settings.momentum_live_enabled, "coverage_starts": start.isoformat(),
               "end": end.isoformat(), "coverage": "earlier gap NOT covered",
               "ledger_before": (root / "ledger.json").read_text()}
    (root / "receipt.json").write_text(json.dumps(receipt))
    if mode == "crash":
        os._exit(1)
    if mode == "sigkill":
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "safety":
        guard.retain_safety_stop("injected safety STOP", day)
        return guard.SAFETY_STOP_EXIT
    return 0


original_mkdir = Path.mkdir


def mkdir_local(self, *args, **kwargs):
    if str(self).startswith("/home/trader/after-hours"):
        return None
    return original_mkdir(self, *args, **kwargs)


with patch.object(daily, "datetime", Clock), patch.object(daily, "load_guard", return_value=guard), \
     patch.object(daily, "prestart_checks", return_value=({"MainPID": "SHIM"}, {})), \
     patch("redis.Redis", RedisShim), \
     patch("project_mai_tai.settings.Settings", return_value=settings), \
     patch.object(guard, "run_guard", monitor), patch.object(guard, "_audit"), \
     patch.object(guard, "stop_paper", return_value=0), patch.object(Path, "mkdir", mkdir_local):
    raise SystemExit(daily.main([]))
