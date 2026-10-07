"""Acknowledge only the measured Redis-upgrade-induced ORB restart; no service actions."""
import json
from pathlib import Path
import subprocess
import sys

APP = "5b8b4f642bbc3c312be436d0e92adbc22d9e9f95"
EXPECTED = {
    "MainPID": "765206", "NRestarts": "1", "ActiveState": "active",
    "SubState": "running", "ExecMainStartTimestamp": "Wed 2026-10-07 06:31:14 UTC",
    "InvocationID": "de16f6a3ae8f438a8aae3d294d7db686",
}


def validate(ack, state, head):
    if (head != APP or ack.get("application") != APP
            or ack.get("unit") != "project-mai-tai-orb-schwab.service"
            or ack.get("decision") != "ACKNOWLEDGED_REDIS_UPGRADE_RESTART"
            or ack.get("redis_restart_utc") != "2026-10-07T06:31:08+00:00"
            or ack.get("redis_package") != "7.0.15-1ubuntu0.24.04.5"
            or ack.get("state") != EXPECTED
            or any(state.get(key) != value for key, value in EXPECTED.items())):
        raise ValueError("upgrade acknowledgement does not match exact live identity")
    return {"decision": ack["decision"], "state": state, "application": head,
            "redis_restart_utc": ack["redis_restart_utc"], "source": ack["source"]}


def current():
    # Runtime manifest pins both this helper and the acknowledgement bytes.
    from daily import verify_runtime
    verify_runtime()
    result = subprocess.run(["systemctl", "show", "project-mai-tai-orb-schwab.service",
        *sum((["-p", key] for key in EXPECTED), [])], capture_output=True, text=True,
        check=True, timeout=10)
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    head = subprocess.run(["git", "-C", "/home/trader/project-mai-tai", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    return validate(json.loads(Path("/home/trader/preopen-daily/upgrade-ack.json").read_bytes()), state, head)


def render(raw, proof):
    failure = "orb-schwab did not return active/running on a new PID"
    if "\nFailures:\n" not in raw:
        return raw, False
    failures = [line[2:] for line in raw.split("\nFailures:\n", 1)[1].splitlines() if line.startswith("- ")]
    rows = [line for line in raw.splitlines() if line.startswith("| ")]
    failed = [line.split("|")[1].strip() for line in rows if line.endswith("| FAIL |")]
    if (failures != [failure] or failed != ["Restarted services"]
            or "\nUnknowns:" in raw or any(line.endswith("| UNKNOWN |") for line in rows)):
        return raw, False
    import re
    out = raw.split("\nFailures:\n", 1)[0]
    out = re.sub(r"^Overall:.*$", "Overall: EXPECTED BY DESIGN (acknowledged upgrade restart; not all NRestarts zero)", out, flags=re.M)
    out = re.sub(r"^Final call:.*$", "Final call: EXPECTED BY DESIGN; exact Redis upgrade restart acknowledged; all other checks measured", out, flags=re.M)
    out = re.sub(r"^(\| Restarted services \| .*\| )FAIL( \|)$", r"\1ACKNOWLEDGED_UPGRADE_RESTART\2", out, flags=re.M)
    out += "\nAcknowledged restart (original collector FAIL retained in immutable sidecar):\n"
    out += json.dumps(proof, sort_keys=True) + "\n"
    return out, True


if __name__ == "__main__":
    try:
        print(json.dumps(current(), sort_keys=True))
    except Exception as exc:
        print("FAIL upgrade acknowledgement: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
