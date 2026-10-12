from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from project_mai_tai.momwarn_health_contract import proposed_health_v2


@dataclass(frozen=True)
class WarningHealth:
    observed_wall_ns: int
    oms_sync_seq: int
    oms_sync_seconds: float
    v2_bar_lateness_seconds: float | None
    load_1m: float
    ram_available_bytes: int
    new_live_error: bool = False
    v2_not_applicable: bool = False


class WarningWatchdog:
    """Sticky A-only circuit breaker; duplicate OMS observations never count twice."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self._last_sync_seq: int | None = None
        self._slow_syncs = 0

    def check(self, health: WarningHealth, *, publish_lag_seconds: float) -> str | None:
        if self.reason is not None:
            return self.reason
        numeric = (health.oms_sync_seconds, health.load_1m, publish_lag_seconds)
        lateness = health.v2_bar_lateness_seconds
        if (lateness is None and not health.v2_not_applicable
                or lateness is not None and (not math.isfinite(lateness) or lateness < 0)
                or any(not math.isfinite(x) or x < 0 for x in numeric)):
            self.reason = "monitor_invalid"
        elif not 0 <= (time.time_ns() - health.observed_wall_ns) / 1e9 <= 5:
            self.reason = "monitor_stale"
        elif publish_lag_seconds > 2:
            self.reason = "publish_lag"
        elif lateness is not None and lateness > 5:
            self.reason = "v2_bar_late"
        elif health.load_1m > 6:
            self.reason = "load"
        elif health.ram_available_bytes < 3 * 1024**3:
            self.reason = "ram"
        elif health.new_live_error:
            self.reason = "live_error"
        elif self._last_sync_seq is not None and health.oms_sync_seq < self._last_sync_seq:
            self.reason = "monitor_sync_regressed"
        elif self._last_sync_seq is not None and health.oms_sync_seq > self._last_sync_seq + 1:
            self.reason = "monitor_sync_gap"
        elif health.oms_sync_seq != self._last_sync_seq:
            self._last_sync_seq = health.oms_sync_seq
            if health.oms_sync_seconds > 5:
                self._slow_syncs += 1
            if self._slow_syncs >= 2:
                self.reason = "oms_sync_slow_twice"
        return self.reason


class WarningHealthReader:
    """Bounded regular-file reads, called off-loop. No DB, HTTP or service control.

    G3 supplies fresh measured OMS duration/sequence and actual v2 completed-bar
    lateness in an atomically replaced JSON file. Missing evidence disables A.
    Linux MemAvailable measures memory that can be allocated without swapping.
    """

    MAX_HEALTH_BYTES = 8192
    MAX_LOG_BYTES = 65536
    LOG_NAMES = ("oms.log", "strategy.log", "schwab-1m-v2.log", "orb-schwab.log")

    def __init__(self, health_path: str, log_directory: str):
        self.health_path = Path(health_path)
        self.log_directory = Path(log_directory)
        self._logs: dict[str, tuple[int, int, bytes]] = {}
        self._source = None
        self._source_config = None
        self._last_oms_evidence = None

    def reset_window(self) -> None:
        # Prime live log offsets before the next A request, not across inactive nights.
        self._logs.clear()
        self._source = self._source_config = self._last_oms_evidence = None

    @staticmethod
    def _open_regular(path: Path):
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError(f"monitor source is not a regular file: {path}")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    def _read_file(self) -> dict:
        with self._open_regular(self.health_path) as source:
            raw = source.read(self.MAX_HEALTH_BYTES + 1)
        if len(raw) > self.MAX_HEALTH_BYTES:
            raise ValueError("health evidence exceeds bound")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("health input must be an object")
        return data

    async def read_live(self, client, prefix: str) -> WarningHealth:
        """Only configured sources use the existing A-owned Redis client.

        Legacy direct reads remain compatible for controlled component tests.
        Live setup must explicitly bind real source instances/expectations, not restamp an
        external placeholder. This method does not create a connection.
        """
        from project_mai_tai.momwarn_live_health import (
            LiveWarningHealthSource, MAX_STATE_ROWS, newest_v2_state,
        )

        config = await asyncio.to_thread(self._read_file)
        if config.get("source_config_version") != 1:
            raise ValueError("live source setup required; external receipt is not live proof")
        if self._source_config is not None and config != self._source_config:
            raise ValueError("live source configuration changed; review/rebaseline required")
        if self._source is None:
            self._source = LiveWarningHealthSource(config, self.log_directory)
            self._source_config = dict(config)
        entries = await client.xrevrange(f"{prefix}:strategy-state-isolated", count=MAX_STATE_ROWS)
        state = newest_v2_state(entries)
        measured = await asyncio.to_thread(self._source.read, state)
        return await asyncio.to_thread(self.read, measured=measured)

    def read(self, *, measured: dict | None = None) -> WarningHealth:
        data = self._read_file() if measured is None else measured
        if len(json.dumps(data, allow_nan=False).encode()) > self.MAX_HEALTH_BYTES:
            raise ValueError("health evidence exceeds bound")
        version = data.get("schema_version")
        not_applicable = False
        if version == 2:
            provenance = data.get("v2_applicability_provenance", {})
            if not isinstance(provenance, dict):
                raise ValueError("invalid v2 applicability provenance")
            source = {**data, **{key: provenance[key] for key in (
                "v2_source_lane", "v2_expectation_kind", "v2_expected_start_et",
                "v2_watchlist_source", "v2_watchlist_snapshot_ns", "v2_watchlist_complete",
                "v2_watchlist",
                "v2_first_bar_start_et", "v2_bar_interval_seconds", "v2_first_bar_source_ref",
            ) if key in provenance}}
            data = proposed_health_v2(source, now_ns=time.time_ns())
            not_applicable = data["v2_applicability"] == "NOT_APPLICABLE"
            evidence = tuple(data[key] for key in (
                "oms_instance_id", "oms_sync_seq", "oms_sync_seconds", "oms_measured_wall_ns"))
            prior = self._last_oms_evidence
            if prior and (evidence[0] != prior[0] or evidence[1] == prior[1] and evidence != prior):
                raise ValueError("OMS instance/completion altered or restamped")
            self._last_oms_evidence = evidence
        elif version != 1:
            raise ValueError("unknown warning health schema")
        for name in ("observed_wall_ns", "oms_sync_seq"):
            if type(data.get(name)) is not int or data[name] < 0:
                raise ValueError(f"missing/invalid measured {name}")
        for name in ("oms_sync_seconds", "v2_bar_lateness_seconds"):
            value = data.get(name)
            if name == "v2_bar_lateness_seconds" and not_applicable and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"missing/invalid measured {name}")
        new_error = False
        for name in self.LOG_NAMES:
            with self._open_regular(self.log_directory / name) as source:
                info = os.fstat(source.fileno())
                previous = self._logs.get(name)
                if previous is None:
                    # Baseline before any A subscription, then examine every new byte.
                    self._logs[name] = (info.st_ino, info.st_size, b"")
                    continue
                inode, offset, pending = previous
                if info.st_ino != inode or info.st_size < offset:
                    raise ValueError("live log rotated/truncated; evidence unknown")
                if info.st_size - offset > self.MAX_LOG_BYTES:
                    raise ValueError("live log read bound exceeded; evidence unknown")
                source.seek(offset)
                chunk = source.read(self.MAX_LOG_BYTES + 1)
                if len(chunk) > self.MAX_LOG_BYTES:
                    raise ValueError("live log read bound exceeded")
                text = pending + chunk
                if len(text) > self.MAX_LOG_BYTES:
                    raise ValueError("live log line bound exceeded")
                new_error |= re.search(rb"\bERROR\b", text) is not None
                tail = text.rsplit(b"\n", 1)[-1]
                self._logs[name] = (inode, offset + len(chunk), tail)
        with self._open_regular(Path("/proc/meminfo")) as source:
            memory = source.read(8192).decode("ascii")
        available = re.search(r"^MemAvailable:\s+(\d+) kB$", memory, re.MULTILINE)
        if available is None:
            raise ValueError("RAM evidence unavailable")
        return WarningHealth(
            observed_wall_ns=data["observed_wall_ns"], oms_sync_seq=data["oms_sync_seq"],
            oms_sync_seconds=data["oms_sync_seconds"],
            v2_bar_lateness_seconds=data["v2_bar_lateness_seconds"],
            load_1m=os.getloadavg()[0], ram_available_bytes=int(available[1]) * 1024,
            new_live_error=new_error,
            v2_not_applicable=not_applicable,
        )
