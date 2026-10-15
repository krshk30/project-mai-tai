"""Opt-in, bounded local /proc and new-log observations, never watchdog controls."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from pathlib import Path

from project_mai_tai.momwarn_measurements import NS, UNKNOWN

BOUND = 65_536
ERROR = re.compile(r"^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} )?ERROR(?:\s|:|$)")


def read_bounded(path: Path, bound: int = BOUND) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        data = os.read(fd, bound + 1)
        if len(data) > bound:
            raise ValueError("read bound exceeded")
        return data
    finally:
        os.close(fd)


class NewErrorProbe:
    """EOF baseline, then complete new lines only; rotation/read failure is UNKNOWN."""

    def __init__(self, paths: list[Path]) -> None:
        if len(paths) > 8:
            raise ValueError("at most eight log files")
        self.paths = paths
        self.states: dict[Path, tuple[tuple[int, int], int, bytes]] = {}

    def sample(self) -> tuple[int | str, str]:
        if not self.paths:
            return UNKNOWN, "UNKNOWN:no_logs"
        count, statuses = 0, []
        for path in self.paths:
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("not a regular log")
                identity = info.st_dev, info.st_ino
                prior = self.states.get(path)
                if prior is None or identity != prior[0] or info.st_size < prior[1]:
                    self.states[path] = identity, info.st_size, b""
                    statuses.append("UNKNOWN:baseline_or_rotation")
                    continue
                if info.st_size - prior[1] > BOUND:
                    self.states[path] = identity, info.st_size, b""
                    statuses.append("UNKNOWN:log_bound")
                    continue
                os.lseek(fd, prior[1], os.SEEK_SET)
                data = os.read(fd, BOUND + 1)
                if len(data) > BOUND:
                    raise ValueError("log bound")
                lines = (prior[2] + data).split(b"\n")
                if len(lines[-1]) > BOUND:
                    raise ValueError("partial line bound")
                count += sum(bool(ERROR.search(line.decode("utf-8"))) for line in lines[:-1])
                self.states[path] = identity, prior[1] + len(data), lines[-1]
            except (OSError, ValueError, UnicodeError) as exc:
                self.states.pop(path, None)
                statuses.append(f"UNKNOWN:log_read:{type(exc).__name__}")
            finally:
                if fd is not None:
                    os.close(fd)
        return (UNKNOWN, ";".join(statuses)) if statuses else (count, "OK")


class LocalProbe:
    """Process CPU is gateway-total; core CPU is system-wide, not gateway attribution."""

    def __init__(self, pid: int, logs: list[Path], *, proc: Path = Path("/proc"),
                 ticks_per_second: int | None = None) -> None:
        if type(pid) is not int or pid <= 0:
            raise ValueError("explicit gateway PID required")
        self.pid, self.proc = pid, proc
        self.hz = ticks_per_second or os.sysconf("SC_CLK_TCK")
        if self.hz <= 0:
            raise ValueError("invalid clock ticks")
        self.errors = NewErrorProbe(logs)
        self.baseline: tuple[int, int, int, dict] | None = None

    def sample(self, now_ns: int) -> dict:
        result = {"gateway_cpu_pct": UNKNOWN, "system_per_core_cpu_pct": UNKNOWN}
        status = {}
        try:
            raw = read_bounded(self.proc / str(self.pid) / "stat").decode()
            if int(raw.split("(", 1)[0]) != self.pid:
                raise ValueError("wrong process")
            fields = raw.rsplit(")", 1)[1].split()
            ticks, start = int(fields[11]) + int(fields[12]), int(fields[19])
            cores = {}
            for line in read_bounded(self.proc / "stat").decode().splitlines():
                name, *parts = line.split()
                if name.startswith("cpu") and name[3:].isdigit():
                    values = [int(part) for part in parts[:8]]
                    if len(values) < 4 or any(item < 0 for item in values):
                        raise ValueError("invalid core ticks")
                    cores[name] = (sum(values), values[3] + (values[4] if len(values) > 4 else 0))
            if not cores or min(ticks, start) < 0:
                raise ValueError("missing CPU evidence")
            prior = self.baseline
            if prior and prior[2] == start and now_ns > prior[0] and ticks >= prior[1]:
                result["gateway_cpu_pct"] = (ticks - prior[1]) / self.hz / (
                    (now_ns - prior[0]) / NS
                ) * 100
                measured = {}
                for core, (total, idle) in cores.items():
                    old = prior[3].get(core)
                    dt, di = (total - old[0], idle - old[1]) if old else (0, -1)
                    measured[core] = (dt - di) / dt * 100 if dt > 0 and 0 <= di <= dt else UNKNOWN
                result["system_per_core_cpu_pct"] = measured
                status["cpu"] = "OK"
            else:
                status["cpu"] = "UNKNOWN:baseline_or_pid_reset"
            self.baseline = now_ns, ticks, start, cores
        except (OSError, ValueError, IndexError, UnicodeError) as exc:
            self.baseline = None
            status["cpu"] = f"UNKNOWN:proc_read:{type(exc).__name__}"
        result["new_error_count"], status["logs"] = self.errors.sample()
        result["probe_status"] = status
        return result

    async def sample_offloop(self, now_ns: int) -> dict:
        return await asyncio.to_thread(self.sample, now_ns)
