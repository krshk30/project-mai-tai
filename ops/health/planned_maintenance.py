#!/usr/bin/env python3
"""Bounded install-window marker; never suppress live-money or monitor-error findings."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

DEFAULT_MARKER = Path("/run/project-mai-tai/planned-maintenance.json")
MAX_TTL = 1800
SERVICES = frozenset({
    "control", "market-capture", "market-data", "oms", "orb-schwab",
    "reconciler", "schwab-1m-v2", "strategy", "momentum-paper",
})


def active_services(path: Path, now: float) -> frozenset[str]:
    try:
        if path.is_symlink() or path.stat().st_mode & 0o022 or path.stat().st_size > 4096:
            return frozenset()
        data = json.loads(path.read_text(encoding="utf-8"))
        start, end = float(data["started_at"]), float(data["expires_at"])
        services = frozenset(data["services"])
        if not (0 < end - start <= MAX_TTL and start <= now < end):
            return frozenset()
        if not services or not services <= SERVICES or not data["reason"].strip():
            return frozenset()
        return services
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return frozenset()


def planned_finding(fingerprint: str, services: frozenset[str]) -> bool:
    for service in services:
        if fingerprint == f"fleet-runtime:service-runtime:{service}:inactive":
            return True
        if fingerprint == f"fleet-runtime:{service}-heartbeat":
            return True
    return False


def filter_findings(marker: Path, state: Path, now: float, lines: list[str]) -> list[str]:
    services = active_services(marker, now)
    # Preserve one local start/end line; never mark suppressed findings as delivered.
    current = ""
    if services:
        current = marker.read_text(encoding="utf-8")
    prior = state.read_text(encoding="utf-8") if state.exists() else ""
    if prior != current:
        with state.with_suffix(".log").open("a", encoding="utf-8") as log:
            if prior:
                log.write(f"{now:.0f} maintenance ended (removed, expired, invalid, or replaced)\n")
            if current:
                log.write(f"{now:.0f} maintenance started {current.strip()}\n")
        state.write_text(current, encoding="utf-8")
    return [line for line in lines if not planned_finding(line.split("|", 1)[0], services)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "end", "filter"))
    parser.add_argument("--marker", type=Path, default=DEFAULT_MARKER)
    parser.add_argument("--ttl", type=int, default=900)
    parser.add_argument("--reason")
    parser.add_argument("--service", action="append", choices=sorted(SERVICES))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--findings", type=Path)
    args = parser.parse_args()
    now = time.time()
    if args.action == "start":
        if not (1 <= args.ttl <= MAX_TTL) or not args.reason or not args.reason.strip() or not args.service:
            parser.error("start requires reason, service scope and TTL of 1..1800 seconds")
        args.marker.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create prevents an overlapping install silently extending a live marker.
        fd = os.open(args.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"started_at": now, "expires_at": now + args.ttl,
                       "services": sorted(set(args.service)), "reason": args.reason}, out)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        print("maintenance started; expiry is mandatory and cannot be renewed implicitly")
    elif args.action == "end":
        args.marker.unlink(missing_ok=True)
        print("maintenance ended; normal paging restored")
    else:
        if args.state is None or args.findings is None:
            parser.error("filter requires state and findings")
        lines = args.findings.read_text(encoding="utf-8").splitlines()
        for line in filter_findings(args.marker, args.state, now, lines):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
