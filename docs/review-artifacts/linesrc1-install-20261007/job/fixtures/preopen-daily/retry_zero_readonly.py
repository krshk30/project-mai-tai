"""Bounded process-env proof for zero budget. No Settings defaults or broker calls."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

from release_policy import (RETRY_ENABLED, RETRY_MAX, canonical,
                            digest, moment, need)

SERVICES = ("schwab-1m-v2", "oms")
FIELDS = ("MainPID", "ActiveState", "SubState", "NRestarts",
          "ExecMainStartTimestampMonotonic", "InvocationID")
EXPECTED = {RETRY_ENABLED: "true", RETRY_MAX: "0"}
MAX = 262144


def values(raw):
    need(type(raw) is bytes and 0 < len(raw) <= MAX, "process environment absent/overflow")
    pairs = []
    for piece in raw.split(b"\0"):
        if not piece:
            continue
        need(b"=" in piece, "malformed process environment")
        key, value = piece.split(b"=", 1)
        pairs.append((key.decode("utf-8"), value.decode("utf-8")))
    for key, expected in EXPECTED.items():
        found = [(name, value) for name, value in pairs if name.upper() == key]
        need(found == [(key, expected)], "explicit retry process value missing/alias/duplicate/drift: " + key)
    return dict(EXPECTED)


def identity(before, after):
    need(type(before) is dict and set(before) == set(FIELDS) and before == after,
         "retry process state incomplete/changed")
    need(all(type(value) is str for value in before.values()), "retry state types unreadable")
    need(re.fullmatch(r"[1-9][0-9]*", before["MainPID"]) is not None
         and before["ActiveState"] == "active" and before["SubState"] == "running"
         and before["NRestarts"] == "0"
         and re.fullmatch(r"[1-9][0-9]*", before["ExecMainStartTimestampMonotonic"]) is not None
         and re.fullmatch(r"[0-9a-f]{32}", before["InvocationID"]) is not None,
         "retry owner inactive/restarted/unbound")


def proof(service, before, raw, after):
    need(service in SERVICES, "foreign retry owner")
    identity(before, after)
    return dict(service=service, before=before, after=after,
                values=values(raw), environ_sha256=digest(raw))


def state(service):
    need(service in SERVICES, "foreign retry owner")
    result = subprocess.run(["systemctl", "show", "project-mai-tai-" + service + ".service",
                             *["--property=" + key for key in FIELDS]],
                            check=True, capture_output=True, timeout=5)
    need(not result.stderr.strip() and len(result.stdout) <= 4096, "retry unit state unreadable")
    pairs = [line.split("=", 1) for line in result.stdout.decode().splitlines()]
    need(len(pairs) == len(FIELDS) and all(len(pair) == 2 for pair in pairs), "retry state malformed")
    current = dict(pairs)
    identity(current, current)
    return current


def environ(pid):
    need(re.fullmatch(r"[1-9][0-9]*", pid) is not None, "retry PID unbound")
    with Path("/proc/" + pid + "/environ").open("rb") as stream:
        raw = stream.read(MAX + 1)
    need(len(raw) <= MAX, "retry environment overflow")
    return raw


def collect():
    rows = []
    for service in SERVICES:
        before = state(service)
        raw = environ(before["MainPID"])
        rows.append(proof(service, before, raw, state(service)))
    return dict(verdict="PASS", kind="explicit-retry-zero-process-proof", checked=2, total=2,
                measured_at_utc=datetime.now(timezone.utc).isoformat(), rows=rows)


def validate(receipt, since, until):
    need(type(receipt) is dict and receipt.get("verdict") == "PASS"
         and receipt.get("kind") == "explicit-retry-zero-process-proof"
         and type(receipt.get("checked")) is int and receipt["checked"] == 2
         and type(receipt.get("total")) is int and receipt["total"] == 2,
         "retry proof verdict/coverage unreadable")
    need(since <= moment(receipt["measured_at_utc"]) <= until, "retry receipt stale/future")
    rows = receipt["rows"]
    need(type(rows) is list and len(rows) == 2 and {row["service"] for row in rows} == set(SERVICES),
         "retry owner population incomplete/duplicate")
    for row in rows:
        identity(row["before"], row["after"])
        need(row["values"] == EXPECTED and re.fullmatch(r"[0-9a-f]{64}", row["environ_sha256"]) is not None,
             "retry receipt explicit values/hash missing")
    return receipt


def catalog(raw, baseline):
    from catalog_policy import numeric_candidate
    need(raw == numeric_candidate(baseline), "retry numeric artifact differs from exact candidate transformation")
    return json.loads(raw)["settings"]


def main():
    try:
        since = datetime.now(timezone.utc)
        receipt = collect()
        print(canonical(validate(receipt, since, datetime.now(timezone.utc))).decode(), end="")
        return 0
    except Exception as exc:
        # Never print raw environment or provider exceptions containing credentials.
        print("UNKNOWN retry proof error_type=" + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
