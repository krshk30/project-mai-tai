"""Non-operational health prototype; G1 v2 contract/source review required before launch."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("input receipt and output health file must be distinct")
    parser.error("NOT READY: reviewed G1 health v2 contract and actual applicable v2 source required")


if __name__ == "__main__":
    main()
