#!/usr/bin/env python3
"""Read-only, one-second gateway 1008 evidence for Option A's first session.

This collector never stops a service. A STOP_TRIGGER row must be routed through
the reviewed paper-only stop procedure; UNKNOWN evidence also blocks an
OBSERVED verdict.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
MARKER = b"1008"


@dataclass(frozen=True)
class Cursor:
    device: int
    inode: int
    offset: int
    pending: bytes = b""


def sample_log(path: Path, cursor: Cursor | None, *, sampled_at: datetime) -> tuple[dict, Cursor]:
    """Sample file identity/size/mtime and count only complete newly appended lines."""
    stat = path.stat()
    row = {
        "sampled_at_utc": sampled_at.astimezone(UTC).isoformat(),
        "log_path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "new_1008_lines": 0,
        "status": "BASELINE" if cursor is None else "OK",
    }
    if cursor is None:
        row["read_from_offset"] = stat.st_size
        return row, Cursor(stat.st_dev, stat.st_ino, stat.st_size)

    if (stat.st_dev, stat.st_ino) != (cursor.device, cursor.inode) or stat.st_size < cursor.offset:
        row["status"] = "UNKNOWN_ROTATED_OR_TRUNCATED"
        row["read_from_offset"] = cursor.offset
        return row, Cursor(stat.st_dev, stat.st_ino, stat.st_size)

    row["read_from_offset"] = cursor.offset
    with path.open("rb") as stream:
        stream.seek(cursor.offset)
        appended = stream.read(stat.st_size - cursor.offset)
        after = path.stat()
    if (after.st_dev, after.st_ino) != (stat.st_dev, stat.st_ino) or after.st_size < stat.st_size:
        row["status"] = "UNKNOWN_CHANGED_DURING_READ"
        return row, Cursor(after.st_dev, after.st_ino, after.st_size)

    chunks = (cursor.pending + appended).split(b"\n")
    row["new_1008_lines"] = sum(MARKER in line for line in chunks[:-1])
    if row["new_1008_lines"]:
        row["status"] = "STOP_TRIGGER"
    return row, Cursor(stat.st_dev, stat.st_ino, stat.st_size, chunks[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--end-utc", type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    end = args.end_utc
    if end.tzinfo is None:
        parser.error("--end-utc must include a UTC offset")
    now = datetime.now(UTC)
    if now.astimezone(ET).time() >= clock_time(7, 0):
        parser.error("start before 07:00 ET to retain full treatment coverage")

    cursor = None
    next_sample = time.monotonic()
    with args.output.open("x", encoding="utf-8") as output:
        while now < end:
            try:
                row, cursor = sample_log(args.gateway_log, cursor, sampled_at=now)
            except OSError as exc:
                row = {"sampled_at_utc": now.isoformat(), "status": "UNKNOWN_UNREADABLE", "error": str(exc)}
            output.write(json.dumps(row, sort_keys=True) + "\n")
            output.flush()
            if row["status"] == "STOP_TRIGGER":
                return 3
            if row["status"].startswith("UNKNOWN"):
                return 2
            next_sample += 1.0
            time.sleep(max(0.0, next_sample - time.monotonic()))
            now = datetime.now(UTC)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
