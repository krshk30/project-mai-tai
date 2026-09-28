#!/usr/bin/env python3
"""Classify v2 arms without consulting outcomes or changing trading behavior.

Input is a chronological sequence of v2 log lines. Files must be passed in
rotation order; same-timestamp lines retain file order. The trip/Fill join is a
separate stage so arm labels can be frozen before outcomes are examined.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Iterator


_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[,.](\d{1,6}))?")
_ARM = re.compile(r"\[V2-CW-ARM\]\s+(\S+)\s+armed\s+bar_ts=(\d+)")
_PLACE = re.compile(r"\[V2-RESTING-(?:PLACE|EH-ARM)\]\s+(\S+)\s+slot=(\S+)")
_DISARM = re.compile(r"\[V2-CW-DISARM\]\s+(\S+)")
_SEED_CAP = re.compile(
    r"\[V2-CW-SEED-CAP\]\s+(\S+)\s+reconstructed.*arm_bar_ts=(\d+)"
)
_DB_SEED_GAP = re.compile(r"\[V2-DB-SEED-GAP\]\s+(\S+)\s+dropped\s+")


@dataclass(frozen=True)
class Arm:
    symbol: str
    emitted_at: datetime
    bar_ts_ms: int
    source_line: int
    age_s: float
    classification: str
    seed_capped: bool = False


@dataclass(frozen=True)
class Placement:
    symbol: str
    emitted_at: datetime
    slot: str
    source_line: int
    arm_line: int | None
    join_method: str


def _time(line: str) -> datetime | None:
    match = _TS.match(line)
    if match is None:
        return None
    micros = (match.group(2) or "0").ljust(6, "0")
    return datetime.strptime(
        f"{match.group(1)}.{micros}", "%Y-%m-%d %H:%M:%S.%f"
    ).replace(tzinfo=UTC)


def classify_age(emitted_at: datetime, bar_ts_ms: int) -> tuple[float, str]:
    age_s = emitted_at.timestamp() - bar_ts_ms / 1000
    if age_s < 0:
        return age_s, "UNKNOWN"
    if age_s <= 120:
        return age_s, "LIVE"
    if age_s > 300:
        return age_s, "REBUILT"
    return age_s, "UNKNOWN"


def parse_events(lines: Iterable[str]) -> tuple[list[Arm], list[Placement]]:
    """Preserve causal read order, including equal-timestamp seed/cap lines."""

    arms: list[Arm] = []
    places: list[Placement] = []
    active: dict[str, int] = {}
    seed_gap_at: dict[str, datetime] = {}
    last_at: datetime | None = None
    for line_number, line in enumerate(lines, 1):
        at = _time(line)
        if at is None:
            continue
        if last_at is not None and at < last_at:
            raise ValueError(f"log lines out of time order at source line {line_number}")
        last_at = at
        if match := _DB_SEED_GAP.search(line):
            seed_gap_at[match.group(1)] = at
        if match := _ARM.search(line):
            symbol, raw_bar_ts = match.groups()
            bar_ts_ms = int(raw_bar_ts)
            age_s, label = classify_age(at, bar_ts_ms)
            # A preceding same-second seed marker is evidence, not a text sort tie.
            seed_at = seed_gap_at.get(symbol)
            if seed_at is not None and 0 <= (at - seed_at).total_seconds() <= 1:
                label = "REBUILT"
            arms.append(Arm(symbol, at, bar_ts_ms, line_number, age_s, label))
            active[symbol] = len(arms) - 1
        if match := _SEED_CAP.search(line):
            symbol, raw_bar_ts = match.groups()
            candidates = [
                index for index, arm in enumerate(arms)
                if arm.symbol == symbol and arm.bar_ts_ms == int(raw_bar_ts)
                and arm.source_line < line_number
            ]
            if candidates:
                index = candidates[-1]
                arms[index] = replace(arms[index], seed_capped=True, classification="REBUILT")
        if match := _DISARM.search(line):
            active.pop(match.group(1), None)
        if match := _PLACE.search(line):
            symbol, slot = match.groups()
            index = active.get(symbol)
            arm = arms[index] if index is not None else None
            places.append(
                Placement(
                    symbol, at, slot, line_number,
                    arm.source_line if arm else None,
                    "inferred" if arm else "UNKNOWN",
                )
            )
    return arms, places


def direct_arm(
    arms: Iterable[Arm], *, symbol: str, arm_bar_ts_ms: int
) -> Arm | None:
    matches = [
        arm for arm in arms
        if arm.symbol == symbol and arm.bar_ts_ms == arm_bar_ts_ms
    ]
    return matches[0] if len(matches) == 1 else None


def _read_lines(paths: Iterable[Path]) -> Iterator[str]:
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as source:
            yield from source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v2-log", type=Path, action="append", required=True)
    parser.add_argument("--arms-csv", type=Path, required=True)
    parser.add_argument("--placements-csv", type=Path, required=True)
    args = parser.parse_args()
    arms, places = parse_events(_read_lines(args.v2_log))
    for path, fields, records in (
        (
            args.arms_csv,
            ("symbol", "emitted_at", "bar_ts_ms", "source_line", "age_s", "classification", "seed_capped"),
            arms,
        ),
        (
            args.placements_csv,
            ("symbol", "emitted_at", "slot", "source_line", "arm_line", "join_method"),
            places,
        ),
    ):
        with path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for record in records:
                writer.writerow({field: getattr(record, field) for field in fields})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
