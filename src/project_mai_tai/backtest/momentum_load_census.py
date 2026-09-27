"""Read-only process attribution before a Momentum throughput replay.

The census never changes the frozen replay threshold or runs a replay. It
records short-lived host load and process CPU around the five-minute cron
boundaries that coincided with the prior Step 2 aborts.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class ProcessSample:
    pid: int
    ppid: int
    comm: str
    state: str
    cpu_ticks: int


def parse_proc_stat(raw: str) -> ProcessSample:
    opening = raw.find("(")
    closing = raw.rfind(")")
    if opening < 0 or closing <= opening:
        raise ValueError("invalid /proc stat process name")
    pid = int(raw[:opening].strip())
    fields = raw[closing + 1 :].split()
    if len(fields) < 13:
        raise ValueError("incomplete /proc stat fields")
    return ProcessSample(
        pid=pid,
        ppid=int(fields[1]),
        comm=raw[opening + 1 : closing],
        state=fields[0],
        cpu_ticks=int(fields[11]) + int(fields[12]),
    )


def scan_processes(proc_root: Path = Path("/proc")) -> dict[int, ProcessSample]:
    samples: dict[int, ProcessSample] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            sample = parse_proc_stat((entry / "stat").read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue
        samples[sample.pid] = sample
    return samples


def safe_process_label(pid: int, comm: str, proc_root: Path = Path("/proc")) -> str:
    """Log a script basename, never arguments or credential-bearing commands."""

    try:
        argv = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return comm
    if len(argv) > 1:
        script = os.fsdecode(argv[1])
        if script.startswith("/home/trader/") and script.endswith((".py", ".sh")):
            return f"{comm}:{Path(script).name}"
    return comm


def read_load(proc_root: Path = Path("/proc")) -> tuple[float, float, int, int]:
    fields = (proc_root / "loadavg").read_text(encoding="utf-8").split()
    runnable, total = fields[3].split("/")
    return float(fields[0]), float(fields[1]), int(runnable), int(total)


def process_cpu_rows(
    previous: Mapping[int, ProcessSample],
    current: Mapping[int, ProcessSample],
    *,
    elapsed_seconds: float,
    ticks_per_second: int,
    label_reader: Callable[[int, str], str] = safe_process_label,
    top_n: int = 15,
) -> list[dict[str, float | int | str]]:
    ranked: list[tuple[int, ProcessSample]] = []
    for pid, sample in current.items():
        prior = previous.get(pid)
        if prior is None or prior.comm != sample.comm:
            continue
        ticks = sample.cpu_ticks - prior.cpu_ticks
        if ticks > 0:
            ranked.append((ticks, sample))
    ranked.sort(key=lambda row: row[0], reverse=True)
    return [
        {
            "pid": sample.pid,
            "ppid": sample.ppid,
            "label": label_reader(sample.pid, sample.comm),
            "state": sample.state,
            "cpu_pct_one_cpu": round(
                ticks / ticks_per_second / elapsed_seconds * 100, 2
            ),
            "cpu_seconds": round(ticks / ticks_per_second, 3),
        }
        for ticks, sample in ranked[:top_n]
    ]


def collect_census(
    *,
    duration_seconds: float,
    interval_seconds: float,
    samples_path: Path,
    summary_path: Path,
    proc_root: Path = Path("/proc"),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    if duration_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("census duration and interval must be positive")
    samples_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
    previous = scan_processes(proc_root)
    started = previous_at = monotonic()
    max_load = 0.0
    over_limit = 0
    count = 0
    observed_cpu: Counter[str] = Counter()
    with samples_path.open("w", encoding="utf-8") as handle:
        while monotonic() - started < duration_seconds:
            sleep(interval_seconds)
            sampled_at = monotonic()
            current = scan_processes(proc_root)
            load_1m, load_5m, runnable, tasks = read_load(proc_root)
            rows = process_cpu_rows(
                previous,
                current,
                elapsed_seconds=max(sampled_at - previous_at, 0.001),
                ticks_per_second=ticks_per_second,
                label_reader=lambda pid, comm: safe_process_label(pid, comm, proc_root),
            )
            for row in rows:
                observed_cpu[str(row["label"])] += float(row["cpu_seconds"])
            max_load = max(max_load, load_1m)
            over_limit += load_1m > 3.5
            count += 1
            handle.write(
                json.dumps(
                    {
                        "sampled_at_utc": datetime.now(UTC).isoformat(),
                        "load_1m": load_1m,
                        "load_5m": load_5m,
                        "runnable": runnable,
                        "tasks": tasks,
                        "uninterruptible_tasks_seen": sum(
                            row.state == "D" for row in current.values()
                        ),
                        "top_processes": rows,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            handle.flush()
            previous, previous_at = current, sampled_at
    summary: dict[str, object] = {
        "samples": count,
        "max_load_1m": max_load,
        "samples_above_frozen_3_5_limit": over_limit,
        "top_observed_cpu_seconds": [
            {"label": label, "cpu_seconds": round(seconds, 3)}
            for label, seconds in observed_cpu.most_common(15)
        ],
        "raw_samples": str(samples_path),
        "limitations": (
            "A one-second process census can miss processes that start and exit "
            "between samples; CPU association is diagnostic, not causal proof."
        ),
    }
    temporary = summary_path.with_name(f".{summary_path.name}.tmp")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(summary_path)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=900)
    parser.add_argument("--interval-seconds", type=float, default=1)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            collect_census(
                duration_seconds=args.duration_seconds,
                interval_seconds=args.interval_seconds,
                samples_path=args.samples,
                summary_path=args.summary,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
