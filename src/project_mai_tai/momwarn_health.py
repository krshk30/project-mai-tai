"""NON-OPERATIONAL health v1 prototype; cadence rule superseded by the v2 proposal.

Retained controlled loop/atomic-write components are not a reviewed live producer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from project_mai_tai.momwarn_measurement_probes import read_bounded
from project_mai_tai.momwarn_measurements import NS, integer, nonnegative

HEALTH_BOUND = 8192
OMS_END = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) INFO .*"
    r"\[OMS-BROKER-SYNC-PASS\] id=(\d+) phase=end outcome=(ok|failed|cancelled) "
    r"duration_ms=(\d+(?:\.\d+)?)\b"
)


def parse_oms_end(line: str, *, verified_timezone: str, instance_id: str) -> dict:
    """Original log timestamp + monotonic duration; never log mtime or read time."""
    match = OMS_END.search(line)
    if not match or not instance_id:
        raise ValueError("not a measured OMS end receipt")
    stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=ZoneInfo(verified_timezone)
    )
    return {
        "oms_instance_id": instance_id, "oms_sync_seq": int(match[3]),
        "oms_sync_seconds": float(match[5]) / 1000,
        "oms_measured_wall_ns": int(stamp.timestamp()) * NS + int(match[2]) * 1_000_000,
        "oms_source": "OMS-BROKER-SYNC-PASS.phase=end", "oms_coverage": "complete",
        "oms_outcome": match[4],
    }


def read_health_receipt(path: Path) -> dict:
    receipt = json.loads(read_bounded(path, HEALTH_BOUND))
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be an object")
    return receipt


class HealthPublisher:
    def __init__(self, output: Path) -> None:
        if not output.is_absolute() or output.name in {"", ".", ".."}:
            raise ValueError("absolute runtime output required")
        self.output = output
        self.binding: tuple[str, str] | None = None
        self.last_seq: int | None = None
        self.last_observed: int | None = None
        self.last_oms: tuple[float, int] | None = None
        self.last_status = "UNKNOWN:not_sampled"

    def measured_payload(self, receipt: dict, *, now_ns: int) -> dict:
        if not isinstance(receipt, dict):
            raise ValueError("missing measured object")
        if (
            type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or receipt.get("receipt_kind") != "measured" or receipt.get("clock_status") != "verified"
            or receipt.get("oms_source") != "OMS-BROKER-SYNC-PASS.phase=end"
            or receipt.get("oms_coverage") != "complete"
            or receipt.get("v2_source") != "completed_bar_due_monitor"
            or receipt.get("v2_coverage") != "complete_current_due"
        ):
            raise ValueError("missing measured provenance/coverage")
        stamps = [receipt.get(field) for field in (
            "observed_wall_ns", "oms_measured_wall_ns", "v2_measured_wall_ns"
        )]
        if not integer(now_ns) or any(
            not integer(stamp) or stamp == 0 or not 0 <= now_ns - stamp <= 5 * NS
            for stamp in stamps
        ):
            raise ValueError("stale/future/missing measurement")
        observed = min(stamps)
        if self.last_observed is not None and observed < self.last_observed:
            raise ValueError("measurement clock regressed")
        binding = receipt.get("oms_instance_id"), receipt.get("v2_instance_id")
        if any(not isinstance(item, str) or not item for item in binding):
            raise ValueError("missing source instance")
        if self.binding is not None and binding != self.binding:
            raise ValueError("source instance changed; review setup")
        seq = receipt.get("oms_sync_seq")
        if not integer(seq) or seq == 0 or self.last_seq is not None and seq not in (
            self.last_seq, self.last_seq + 1
        ):
            raise ValueError("OMS sequence missing/skipped/regressed")
        duration, lateness = receipt.get("oms_sync_seconds"), receipt.get("v2_bar_lateness_seconds")
        if not nonnegative(duration) or not nonnegative(lateness):
            raise ValueError("missing/invalid measured values")
        if seq == self.last_seq and (duration, receipt["oms_measured_wall_ns"]) != self.last_oms:
            raise ValueError("unchanged OMS sequence was altered/restamped")
        due, completed = receipt.get("v2_due_wall_ns"), receipt.get("v2_completed_wall_ns")
        v2_now = receipt["v2_measured_wall_ns"]
        if not integer(due) or due == 0 or due > v2_now:
            raise ValueError("missing/future completed-bar due evidence")
        if completed is not None and (not integer(completed) or not due <= completed <= v2_now):
            raise ValueError("invalid completed-bar observation")
        expected = ((v2_now if completed is None else completed) - due) / NS
        if abs(expected - lateness) > .000001:
            raise ValueError("lateness does not match actual due/completion receipt")
        return {
            "schema_version": 1, "observed_wall_ns": observed,
            "oms_sync_seq": seq, "oms_sync_seconds": duration,
            "v2_bar_lateness_seconds": lateness,
            "provenance": {field: receipt[field] for field in (
                "oms_instance_id", "v2_instance_id", "oms_measured_wall_ns",
                "v2_measured_wall_ns", "oms_source", "v2_source", "v2_coverage",
                "v2_due_wall_ns", "v2_completed_wall_ns",
            )},
        }

    def publish_once(self, receipt: dict | None, *, now_ns: int, failure: str = "read_failure") -> str:
        try:
            if receipt is None:
                raise ValueError(failure)
            payload = self.measured_payload(receipt, now_ns=now_ns)
            status = "OK"
        except (ValueError, TypeError, OverflowError) as exc:
            payload = {"schema_version": 1, "status": "UNKNOWN", "reason": str(exc)[:200]}
            status = "UNKNOWN:" + str(exc)[:200]
        # UNKNOWN deliberately lacks required values: G1 fails closed, without fake zeros.
        self._atomic_replace(payload)
        if status == "OK":
            provenance = payload["provenance"]
            self.binding = provenance["oms_instance_id"], provenance["v2_instance_id"]
            self.last_seq, self.last_observed = payload["oms_sync_seq"], payload["observed_wall_ns"]
            self.last_oms = payload["oms_sync_seconds"], provenance["oms_measured_wall_ns"]
        self.last_status = status
        return status

    def _atomic_replace(self, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > HEALTH_BOUND:
            raise ValueError("health output bound")
        parent = self.output.parent
        if parent.resolve() != parent:
            raise ValueError("symlink runtime directory")
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = ".momwarn-health-" + os.urandom(12).hex()
        fd = None
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("runtime directory must be publisher-owned and not writable by others")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = None
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.output.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)


async def run_health_publisher(
    reader: Callable[[], dict], publisher: HealthPublisher, stop: asyncio.Event,
    *, wall_ns: Callable[[], int] = time.time_ns, monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Code only: explicitly invoked after review. At most one off-loop operation in flight."""
    def cycle() -> None:
        dispatched = monotonic()
        try:
            receipt = reader()
            failure = "read_bound_exceeded"
            if monotonic() - dispatched > 1:
                receipt = None
        except Exception as exc:
            receipt, failure = None, f"read_failure:{type(exc).__name__}"
        publisher.publish_once(receipt, now_ns=wall_ns(), failure=failure)

    while not stop.is_set():
        started = monotonic()
        try:
            await asyncio.to_thread(cycle)
        except (OSError, ValueError) as exc:
            publisher.last_status = f"UNKNOWN:write_failure:{type(exc).__name__}"
        # No detached timeouts: a blocked worker cannot spawn more workers or late writes.
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(.001, 1 - (monotonic() - started)))
        except TimeoutError:
            pass
