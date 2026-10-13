from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from project_mai_tai.momwarn_health_contract import proposed_health_v2
from project_mai_tai.momwarn_cohort_lateness import CohortObservation, CompleteCohortPolicy
from project_mai_tai.momwarn_log_cursor import (
    LogReadRace, check_read_coverage, cursor_anchor, cursor_rewritten,
)


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
    v2_startup_pending: bool = False
    v2_startup_deadline_ns: int | None = None
    v2_cohort: dict | None = None


class WarningWatchdog:
    """Sticky A-only circuit breaker; duplicate OMS observations never count twice."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self._last_sync_seq: int | None = None
        self._slow_syncs = 0
        self._cohort = CompleteCohortPolicy()

    def cohort_metrics(self) -> dict:
        return self._cohort.metrics()

    def check(self, health: WarningHealth, *, publish_lag_seconds: float) -> str | None:
        if self.reason is not None:
            return self.reason
        numeric = (health.oms_sync_seconds, health.load_1m, publish_lag_seconds)
        lateness = health.v2_bar_lateness_seconds
        calibrated = health.v2_cohort is not None
        cohort_reason = None
        if calibrated:
            try:
                observation = CohortObservation.read(health.v2_cohort)
                if (observation.source != "strategy-state-isolated" or observation.observed_ns != health.observed_wall_ns
                        or health.v2_not_applicable or health.v2_startup_pending
                        or lateness != (observation.lateness_ns / 1e9 if observation.lateness_ns is not None else None)):
                    raise ValueError("CAL health envelope changed")
                cohort_reason = self._cohort.observe(observation)
            except (ValueError, TypeError):
                self.reason = "monitor_invalid"
                return self.reason
        if (lateness is None and not (health.v2_not_applicable or health.v2_startup_pending or calibrated)
                or health.v2_startup_pending and (
                    health.v2_not_applicable or lateness is not None
                    or type(health.v2_startup_deadline_ns) is not int
                    or health.v2_startup_deadline_ns <= 0)
                or lateness is not None and (not math.isfinite(lateness) or lateness < 0)
                or any(not math.isfinite(x) or x < 0 for x in numeric)):
            self.reason = "monitor_invalid"
        elif not 0 <= (time.time_ns() - health.observed_wall_ns) / 1e9 <= 5:
            self.reason = "monitor_stale"
        elif publish_lag_seconds > 2:
            self.reason = "publish_lag"
        elif cohort_reason:
            self.reason = cohort_reason
        elif not calibrated and lateness is not None and lateness > 5:
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
        if (self.reason is None and health.v2_startup_pending
                and time.time_ns() > health.v2_startup_deadline_ns):
            self.reason = "v2_startup_timeout"
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
        self._log_truncations: dict[str, int] = {}
        self._log_anchors: dict[str, bytes] = {}
        self._gateway_startup: dict | None = None

    def bind_gateway_start(self, instance_id: str, started_ns: int) -> None:
        if (self._gateway_startup is not None or not isinstance(instance_id, str)
                or not instance_id or type(started_ns) is not int or started_ns <= 0):
            raise ValueError("gateway startup baseline invalid/already bound")
        self._gateway_startup = dict(gateway_instance_id=instance_id,
                                    started_ns=started_ns, baseline_ns=started_ns,
                                    activation_ns=None, empty_seen=False, ready=None)

    def reset_window(self) -> None:
        # Prime live log offsets before the next A request, not across inactive nights.
        self._logs.clear()
        self._source = self._source_config = self._last_oms_evidence = None
        self._log_truncations.clear()
        self._log_anchors.clear()
        if self._gateway_startup is not None:
            self._gateway_startup.update(baseline_ns=time.time_ns(), activation_ns=None,
                                         empty_seen=False, ready=None)

    def log_observability(self) -> dict:
        counts = dict(self._log_truncations)
        if self._source is not None:
            for name, count in (("oms.log", self._source.oms.truncations),
                                ("schwab-1m-v2.log", self._source.truncations)):
                if count:
                    counts[name] = max(counts.get(name, 0), count)
        return {"copytruncate_counts": counts,
                "coverage_status": "GAP_UNOBSERVED_COPYTRUNCATE" if counts else (
                    "OBSERVED_CURSOR_READS_ONLY" if self._logs else "UNKNOWN_NOT_BASELINED")}

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

    def _has_live_error(self, name: str, text: bytes) -> bool:
        errors = [line for line in text.splitlines(keepends=True)
                  if re.search(rb"\bERROR\b", line)]
        if not errors:
            return False
        if name != "schwab-1m-v2.log" or self._source is None:
            return True
        pattern = (
            rb"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) ERROR "
            rb"project_mai_tai\.services\.schwab_1m_v2_bot \| "
            rb"\[V2-CONFIRMATION-EXIT-UNANSWERABLE\] [^\r\n]* "
            rb"reason=line_unproven\r?\n"
        )
        matches = [re.fullmatch(pattern, line) for line in errors]
        if any(match is None or len(re.findall(rb"\bERROR\b", line)) != 1
               for line, match in zip(errors, matches, strict=True)):
            return True
        # Only this closed-fill boot diagnostic is exempt. Identity, timestamp,
        # incomplete lines and every other error remain fail-closed.
        self._source._check_v2_process()
        proc = self._source.proc
        with self._open_regular(proc / str(self._source.config["v2_pid"]) / "stat") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return True
        fields = raw.decode().rsplit(")", 1)[1].split()
        with self._open_regular(proc / "stat") as stream:
            raw_boot = stream.read(65537)
        boot = re.search(rb"^btime (\d+)$", raw_boot, re.MULTILINE)
        if boot is None or len(raw_boot) > 65536:
            return True
        started_ns = int(boot[1]) * 1_000_000_000 + int(fields[19]) * 1_000_000_000 // os.sysconf("SC_CLK_TCK")
        for match in matches:
            try:
                stamp = datetime.strptime(match[1].decode(), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=self._source.zone)
            except ValueError:
                return True
            stamp_ns = int(stamp.timestamp()) * 1_000_000_000 + int(match[2]) * 1_000_000
            if not started_ns <= stamp_ns <= min(started_ns + 60_000_000_000, time.time_ns()):
                return True
        return False

    async def read_live(self, client, prefix: str) -> WarningHealth:
        # One budget for the entire sample, not one retry per file/source.
        try:
            return await self._read_live_once(client, prefix)
        except LogReadRace:
            return await self._read_live_once(client, prefix)

    async def _read_live_once(self, client, prefix: str) -> WarningHealth:
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
        measured = await asyncio.to_thread(self._source.read, state, **(
            {"startup": self._gateway_startup} if self._gateway_startup is not None else {}))
        return await asyncio.to_thread(self.read, measured=measured)

    def read(self, *, measured: dict | None = None) -> WarningHealth:
        data = self._read_file() if measured is None else measured
        if len(json.dumps(data, allow_nan=False).encode()) > self.MAX_HEALTH_BYTES:
            raise ValueError("health evidence exceeds bound")
        version = data.get("schema_version")
        not_applicable = False
        startup_pending = False
        cohort = None
        evidence = self._last_oms_evidence
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
            startup_pending = data["v2_applicability"] == "STARTUP_PENDING"
            cohort = data.get("v2_cohort")
            evidence = tuple(data[key] for key in (
                "oms_instance_id", "oms_sync_seq", "oms_sync_seconds", "oms_measured_wall_ns"))
            prior = self._last_oms_evidence
            if prior and (evidence[0] != prior[0] or evidence[1] == prior[1] and evidence != prior):
                raise ValueError("OMS instance/completion altered or restamped")
        elif version != 1:
            raise ValueError("unknown warning health schema")
        for name in ("observed_wall_ns", "oms_sync_seq"):
            if type(data.get(name)) is not int or data[name] < 0:
                raise ValueError(f"missing/invalid measured {name}")
        for name in ("oms_sync_seconds", "v2_bar_lateness_seconds"):
            value = data.get(name)
            if name == "v2_bar_lateness_seconds" and (not_applicable or startup_pending or cohort is not None) and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"missing/invalid measured {name}")
        new_error = False
        logs, anchors, truncations = dict(self._logs), dict(self._log_anchors), dict(self._log_truncations)
        for name in self.LOG_NAMES:
            with self._open_regular(self.log_directory / name) as source:
                info = os.fstat(source.fileno())
                previous = self._logs.get(name)
                if previous is None:
                    # Baseline before any A subscription, then examine every new byte.
                    logs[name] = (info.st_ino, info.st_size, b"")
                    anchors[name] = cursor_anchor(source.fileno(), info.st_size)
                    continue
                inode, offset, pending = previous
                if info.st_ino != inode:
                    raise ValueError("live log replaced; evidence unknown")
                if cursor_rewritten(source.fileno(), offset, self._log_anchors.get(name)):
                    truncations[name] = truncations.get(name, 0) + 1
                    offset, pending = 0, b""
                if info.st_size - offset > self.MAX_LOG_BYTES:
                    raise ValueError("live log read bound exceeded; evidence unknown")
                source.seek(offset)
                chunk = source.read(self.MAX_LOG_BYTES + 1)
                if len(chunk) > self.MAX_LOG_BYTES:
                    raise ValueError("live log read bound exceeded")
                covered = offset + len(chunk)
                check_read_coverage(source.fileno(), offset, covered, self.MAX_LOG_BYTES, name)
                text = pending + chunk
                if len(text) > self.MAX_LOG_BYTES:
                    raise ValueError("live log line bound exceeded")
                new_error |= self._has_live_error(name, text)
                tail = text.rsplit(b"\n", 1)[-1]
                logs[name] = (inode, covered, tail)
                anchors[name] = cursor_anchor(source.fileno(), covered)
        with self._open_regular(Path("/proc/meminfo")) as source:
            memory = source.read(8192).decode("ascii")
        available = re.search(r"^MemAvailable:\s+(\d+) kB$", memory, re.MULTILINE)
        if available is None:
            raise ValueError("RAM evidence unavailable")
        self._logs, self._log_anchors, self._log_truncations = logs, anchors, truncations
        self._last_oms_evidence = evidence
        return WarningHealth(
            observed_wall_ns=data["observed_wall_ns"], oms_sync_seq=data["oms_sync_seq"],
            oms_sync_seconds=data["oms_sync_seconds"],
            v2_bar_lateness_seconds=data["v2_bar_lateness_seconds"],
            load_1m=os.getloadavg()[0], ram_available_bytes=int(available[1]) * 1024,
            new_live_error=new_error,
            v2_not_applicable=not_applicable,
            v2_startup_pending=startup_pending,
            v2_startup_deadline_ns=data.get("v2_startup_deadline_ns") if startup_pending else None,
            v2_cohort=cohort,
        )
