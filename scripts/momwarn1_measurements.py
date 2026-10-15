"""Offline MOMWARN1 JSONL -> ten-second and per-stock CSV; no live mode."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from project_mai_tai.momwarn_measurements import DETAIL_FIELDS, SAMPLE_FIELDS, replay, write_csv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    parser.add_argument("--evidence-kind", required=True, choices=("CONTROLLED", "RECORDED"))
    parser.add_argument("--samples", required=True, type=Path)
    parser.add_argument("--entries", required=True, type=Path)
    args = parser.parse_args()
    if len({path.resolve() for path in (args.input, args.samples, args.entries)}) != 3:
        parser.error("input and output paths must be distinct")
    with args.input.open(encoding="utf-8") as handle:
        samples, entries = replay(handle, args.day, record_kind=args.evidence_kind)
    # Exclusive creation protects retained evidence; both must be absent before writing.
    if args.samples.exists() or args.entries.exists():
        parser.error("output evidence already exists")
    with args.samples.open("x", encoding="utf-8", newline="") as handle:
        write_csv(handle, samples, SAMPLE_FIELDS)
    with args.entries.open("x", encoding="utf-8", newline="") as handle:
        write_csv(handle, entries, DETAIL_FIELDS)


if __name__ == "__main__":
    main()
