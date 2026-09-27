#!/usr/bin/env python3
"""Route routine alerts and send one end-of-day digest without changing alert bodies."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SENDERS = (
    "reject-watch",
    "seed-exposure",
    "entry-fix",
    "d6-outcome",
    "bar-gap",
    "eod",
)
LOW_URL = "https://ntfy.sh/mai-tai-routine-112964cc8f26787132a29538"
DIGEST_URL = "https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
DEFAULT_SPOOL = Path("/var/lib/project-mai-tai/low-priority-alerts")


def _spool() -> Path:
    path = Path(os.environ.get("MAI_TAI_LOW_ALERT_SPOOL", str(DEFAULT_SPOOL)))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _send(url: str, title: str, body: str, tags: str = "") -> bool:
    command = [
        os.environ.get("MAI_TAI_LOW_ALERT_CURL", "curl"),
        "-sS", "--fail-with-body", "--connect-timeout", "10", "--max-time", "30",
        "-H", f"Title: {title}", "-H", "Priority: low",
    ]
    if tags:
        command += ["-H", f"Tags: {tags}"]
    command += ["--data-binary", "@-", url]
    try:
        result = subprocess.run(
            command, input=body, text=True, capture_output=True, check=False, timeout=35,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"low-priority alert send failed: {type(exc).__name__}", file=sys.stderr)
        return False
    if result.returncode:
        print(f"low-priority alert send failed rc={result.returncode}", file=sys.stderr)
    return result.returncode == 0


def send_low(
    sender: str, title: str, body: str, tags: str = "", *, url: str = LOW_URL,
) -> bool:
    if sender not in SENDERS:
        raise ValueError(f"unrecognized low-priority sender: {sender}")
    if not _send(url, title, body, tags):
        return False
    now = datetime.now(ET)
    record = {
        "at": now.isoformat(),
        "sender": sender,
        "last_line": body.splitlines()[-1] if body.splitlines() else "",
    }
    path = _spool() / f"{now.date().isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")
        stream.flush()
        fcntl.flock(stream, fcntl.LOCK_UN)
    return True


def digest_body(day: str, spool: Path) -> str:
    counts = {sender: 0 for sender in SENDERS}
    latest = {sender: "(none)" for sender in SENDERS}
    path = spool / f"{day}.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            sender = record["sender"]
            if sender in counts:
                counts[sender] += 1
                latest[sender] = record["last_line"]
    lines = [f"Low-priority alerts for {day} ET"]
    lines += [
        f"{sender}: count={counts[sender]} last={latest[sender]}"
        for sender in SENDERS
    ]
    return "\n".join(lines)


def send_digest(now: datetime | None = None) -> bool:
    now = (now or datetime.now(ET)).astimezone(ET)
    if (now.hour, now.minute) != (20, 0):
        return True
    spool = _spool()
    day = now.date().isoformat()
    lock = spool / ".digest.lock"
    with lock.open("a", encoding="utf-8") as stream:
        os.chmod(lock, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        marker = spool / f"{day}.digested"
        if marker.exists():
            return True
        if not _send(DIGEST_URL, f"Mai Tai routine digest {day}", digest_body(day, spool)):
            return False
        marker.touch(mode=0o600)
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--digest", action="store_true")
    parser.add_argument("--sender", choices=SENDERS)
    parser.add_argument("--title")
    parser.add_argument("--tags", default="")
    args = parser.parse_args()
    if args.digest:
        return 0 if send_digest() else 1
    if not args.sender or args.title is None:
        parser.error("--sender and --title are required for an alert")
    return 0 if send_low(args.sender, args.title, sys.stdin.read(), args.tags) else 1


if __name__ == "__main__":
    raise SystemExit(main())
