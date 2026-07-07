#!/usr/bin/env python3
"""Intraday component-liveness check — reads a service heartbeat from Redis
(mai_tai:heartbeats) from OUTSIDE the service process and verdicts RED if the
heartbeat is stale or absent.

TEMPLATE for per-component watchdogs: parameterize via env
  WATCH_SERVICE    (default oms-risk)   — source_service to watch
  WATCH_STALE_SECS (default 180)        — age over which = RED
  WATCH_SIMULATE_AGE                    — force this age (self-test only)

Independence: stdlib only + redis-cli subprocess. Shares NO event loop / asyncio
task / DB session with the watched service, so a zombied (event-loop-blocked)
service cannot stall or kill this check.

Exit: 0 = OK, 2 = RED. Prints one 'VERDICT: <LEVEL> <detail>' line.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

STREAM = "mai_tai:heartbeats"
SERVICE = os.environ.get("WATCH_SERVICE", "oms-risk")
STALE_SECS = float(os.environ.get("WATCH_STALE_SECS", "180"))
SIMULATE_AGE = os.environ.get("WATCH_SIMULATE_AGE")
HEALTHY_STATUS = {"healthy", "starting"}
UTC = timezone.utc


def _redis_xrevrange(count=200):
    try:
        r = subprocess.run(
            ["redis-cli", "XREVRANGE", STREAM, "+", "-", "COUNT", str(count)],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout
    except Exception as exc:  # redis unreachable == fleet down; treat as absent
        print(f"(redis read failed: {exc})", file=sys.stderr)
        return ""


def latest_heartbeat(service):
    """Newest heartbeat dict for `service` (parse mirrors the readiness check:
    heartbeat JSON arrives one-per-line, .startswith('{'))."""
    for ln in _redis_xrevrange().splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("source_service") == service:
            return d
    return None


def iso_age(ts):
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (datetime.now(UTC) - t).total_seconds()


def main():
    if SIMULATE_AGE is not None:
        age, status, produced = float(SIMULATE_AGE), "healthy", "SIMULATED"
    else:
        hb = latest_heartbeat(SERVICE)
        if hb is None:
            print(f"VERDICT: RED {SERVICE} NO heartbeat in {STREAM} (zombie signature / fleet down)")
            return 2
        produced = hb.get("produced_at")
        status = (hb.get("payload") or {}).get("status", "?")
        if not produced:
            print(f"VERDICT: RED {SERVICE} heartbeat missing produced_at")
            return 2
        age = iso_age(produced)

    if age > STALE_SECS:
        print(f"VERDICT: RED {SERVICE} heartbeat {age:.0f}s stale (>{STALE_SECS:.0f}s) "
              f"status={status} — likely zombied")
        return 2
    if status not in HEALTHY_STATUS:
        print(f"VERDICT: RED {SERVICE} heartbeat fresh ({age:.0f}s) but status={status}")
        return 2
    print(f"VERDICT: OK {SERVICE} heartbeat {age:.0f}s ago status={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
