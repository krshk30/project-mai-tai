#!/usr/bin/env python3
"""Reviewed local candidate: publish only a sanctioned gateway restart identity."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

UNIT = "project-mai-tai-market-data.service"
FIELDS = ("MainPID", "ActiveState", "NRestarts", "ExecMainStartTimestamp")
PIN_FIELDS = {"version", "unit", "main_pid", "start_ticks", "exec_main_start_timestamp"}
REQUIRED_ARTIFACTS = {
    "daily_guard.py", "failure_stop.py", "guard_rules.py", "sampler.py",
    "project-mai-tai-option-a-daily-guard.service",
    "project-mai-tai-option-a-daily-guard-failure.service",
    "project-mai-tai-option-a-daily-guard.timer",
}


def need(value, reason):
    if not value:
        raise ValueError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "SHA256 missing/malformed")
    return value


def bounded_read(path, limit=131072):
    need(not path.is_symlink(), "symlink refused: " + path.name)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    need(len(raw) <= limit, "file exceeds bound: " + path.name)
    return raw


def validate_pin(pin):
    need(isinstance(pin, dict) and set(pin) == PIN_FIELDS, "identity fields missing/unexpected")
    need(type(pin["version"]) is int and pin["version"] == 1 and pin["unit"] == UNIT,
         "identity version/unit invalid")
    for name in ("main_pid", "start_ticks"):
        need(type(pin[name]) is int and pin[name] > 0, "identity positive integer required: " + name)
    stamp = pin["exec_main_start_timestamp"]
    need(isinstance(stamp, str) and bool(stamp.strip()) and len(stamp) <= 128,
         "identity start timestamp unreadable")
    return pin


def capture(reader=None, proc_root=Path("/proc")):
    if reader is None:
        def reader():
            command = ["systemctl", "show", UNIT]
            for field in FIELDS:
                command.extend(["-p", field])
            raw = subprocess.check_output(command, text=True, timeout=10)
            need(len(raw) <= 4096, "systemctl output exceeds bound")
            return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    first = reader()
    need(first.get("ActiveState") == "active" and first.get("NRestarts") == "0",
         "gateway inactive or restart count changed")
    pid = first.get("MainPID", "")
    need(isinstance(pid, str) and re.fullmatch(r"[1-9][0-9]*", pid), "MainPID unreadable")
    def process_ticks():
        raw = bounded_read(proc_root / pid / "stat", 4096)
        need(raw.split(b" ", 1)[0] == pid.encode(), "process stat PID mismatch")
        parts = raw.decode("ascii").rsplit(")", 1)
        need(len(parts) == 2, "process stat command field malformed")
        fields = parts[1].split()
        need(len(fields) >= 20 and fields[0] not in {"Z", "X", "x"}, "gateway process dead/malformed")
        return int(fields[19])
    pin = validate_pin({"version": 1, "unit": UNIT, "main_pid": int(pid),
                        "start_ticks": process_ticks(),
                        "exec_main_start_timestamp": first.get("ExecMainStartTimestamp")})
    need(reader() == first, "gateway systemd identity raced during capture")
    need(process_ticks() == pin["start_ticks"], "gateway process instance raced during capture")
    return pin


def proposal(root, request, manifest_raw, current):
    """The request is a hash-pinned receipt from the reviewed install, not an approval oracle."""
    need(request.get("version") == 1 and type(request.get("version")) is int,
         "restart receipt version invalid")
    need(request.get("gateway_restart_authorized") is True and request.get("restart_completed") is True,
         "explicit authorized completed gateway restart receipt required")
    need(isinstance(request.get("install_id"), str)
         and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", request["install_id"]), "install ID invalid")
    need(sha(manifest_raw) == digest(request.get("manifest_before_sha256")), "manifest preimage drift")
    before = validate_pin(request.get("before_gateway_identity"))
    after = validate_pin(request.get("after_gateway_identity"))
    need(current == after, "live identity differs from completed restart receipt")
    need(before["main_pid"] != after["main_pid"] and before["start_ticks"] < after["start_ticks"],
         "receipt does not prove a new gateway PID and advancing process start")
    need(before["exec_main_start_timestamp"] != after["exec_main_start_timestamp"],
         "gateway start timestamp did not change")
    checks = request.get("post_checks")
    need(isinstance(checks, dict) and all(checks.get(k) == "PASS" for k in ("identity", "flags", "trading")),
         "post-restart identity/flags/trading evidence absent or not PASS")
    manifest = json.loads(manifest_raw)
    need(isinstance(manifest, dict) and isinstance(manifest.get("artifacts"), dict), "manifest malformed")
    artifacts = manifest["artifacts"]
    need(REQUIRED_ARTIFACTS <= set(artifacts), "guard artifact pins incomplete")
    if "gateway_identity" in manifest:
        need(validate_pin(manifest["gateway_identity"]) == before, "existing gateway baseline drift")
    reviewed = digest(request.get("reviewed_daily_guard_sha256"))
    old_source = digest(request.get("daily_guard_before_sha256"))
    need(artifacts["daily_guard.py"] == old_source, "old daily guard hash differs from reviewed preimage")
    reviewed_sources = {"daily_guard.py": reviewed}
    if "reviewed_guard_rules_sha256" in request or "guard_rules_before_sha256" in request:
        rules_before = digest(request.get("guard_rules_before_sha256"))
        need(artifacts.get("guard_rules.py") == rules_before, "old guard rules hash differs from reviewed preimage")
        reviewed_sources["guard_rules.py"] = digest(request.get("reviewed_guard_rules_sha256"))
    for name, expected in artifacts.items():
        need(isinstance(name, str) and Path(name).name == name and name not in {".", ".."},
             "artifact path not a simple filename")
        actual = sha(bounded_read(root / name))
        need(actual == reviewed_sources.get(name, digest(expected)),
             "installed artifact drift: " + name)
    result = deepcopy(manifest)
    result["gateway_identity"] = deepcopy(current)
    result["artifacts"].update(reviewed_sources)
    need({k: v for k, v in result.items() if k not in {"artifacts", "gateway_identity"}}
         == {k: v for k, v in manifest.items() if k not in {"artifacts", "gateway_identity"}},
         "unrelated manifest fields changed")
    need({k: v for k, v in result["artifacts"].items() if k not in reviewed_sources}
         == {k: v for k, v in artifacts.items() if k not in reviewed_sources}, "unrelated artifact pins changed")
    return (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()


def publish(root, request, backup_dir, *, reader=None, proc_root=Path("/proc")):
    path = root / "manifest.json"
    need(not path.is_symlink(), "manifest symlink refused")
    # All writers must hold this same manifest-inode lock during this install.
    with path.open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = bounded_read(path)
        pin = capture(reader, proc_root)
        new = proposal(root, request, old, pin)
        need(backup_dir.is_dir() and not backup_dir.is_symlink(), "existing backup directory required")
        backup = backup_dir / (request["install_id"] + "-manifest-before.json")
        with backup.open("xb") as stream:
            stream.write(old)
            stream.flush()
            os.fsync(stream.fileno())
        need(sha(bounded_read(backup)) == sha(old), "backup verification failed")
        metadata = path.stat()
        fd, temporary = tempfile.mkstemp(prefix=".manifest-poststep-", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), metadata.st_mode & 0o777)
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
                stream.write(new)
                stream.flush()
                os.fsync(stream.fileno())
            need(capture(reader, proc_root) == pin, "gateway changed before manifest publication")
            need(bounded_read(path) == old, "manifest raced before publication")
            # Recheck sources after capture; never refresh hashes to make drift pass.
            need(proposal(root, request, old, pin) == new, "proposal changed before publication")
            os.replace(temporary, path)
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        need(bounded_read(path) == new, "published manifest verification failed")
        need(capture(reader, proc_root) == pin, "gateway changed after publication; install remains failed")
        return {"status": "PUBLISHED", "gateway_identity": pin,
                "guard_admission": "NOT_EVALUATED",
                "manifest_before_sha256": sha(old), "manifest_after_sha256": sha(new),
                "backup": str(backup), "observed_utc": datetime.now(UTC).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--request-sha256")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--capture-only", action="store_true", help="Read only actual gateway identity")
    parser.add_argument("--apply", action="store_true", help="Future reviewed install only; default is preview")
    args = parser.parse_args()
    if args.capture_only:
        need(not args.apply and args.root is None and args.request is None
             and args.request_sha256 is None and args.backup_dir is None,
             "capture-only cannot be combined with publication arguments")
        print(json.dumps(capture(), indent=2))
        return
    need(args.root is not None and args.request is not None and args.request_sha256 is not None,
         "root and explicit hash-pinned restart receipt required")
    raw = bounded_read(args.request)
    need(sha(raw) == digest(args.request_sha256), "restart receipt hash mismatch")
    request = json.loads(raw)
    need(isinstance(request, dict), "restart receipt is not an object")
    if args.apply:
        need(args.backup_dir is not None, "backup directory required for apply")
        result = publish(args.root, request, args.backup_dir)
    else:
        old = bounded_read(args.root / "manifest.json")
        pin = capture()
        new = proposal(args.root, request, old, pin)
        result = {"status": "PREVIEW_ONLY", "gateway_identity": pin,
                  "guard_admission": "NOT_EVALUATED",
                  "manifest_before_sha256": sha(old), "manifest_proposed_sha256": sha(new),
                  "proposed_manifest": json.loads(new)}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
