"""Summarize bounded log evidence without pretending position rollups are REST counts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("oms", type=Path)
    parser.add_argument("market_data", type=Path)
    args = parser.parse_args()
    oms = args.oms.read_text()
    market_data = args.market_data.read_text()
    rollups = []
    for line in oms.splitlines():
        if "[BROKER-SYNC-CENSUS]" not in line:
            continue
        match = re.search(r"live:schwab_1m_v2: ok=(\d+) failed=(\d+)", line)
        if match:
            rollups.append({"at": line[:23], "ok": int(match[1]), "failed": int(match[2])})
    print(json.dumps({
        "scope": "bounded partial-day box log inspection, not complete REST instrumentation",
        "oms_capture": {
            "path": str(args.oms), "sha256": hashlib.sha256(oms.encode()).hexdigest(),
            "lines": len(oms.splitlines()), "source_byte_cap": 3000000,
            "filtered_line_cap": 400, "first_rollup": rollups[0] if rollups else None,
            "last_rollup": rollups[-1] if rollups else None,
        },
        "market_data_capture": {
            "path": str(args.market_data), "sha256": hashlib.sha256(market_data.encode()).hexdigest(),
            "lines": len(market_data.splitlines()), "source_tail_line_cap": 4000,
        },
        "schwab_position_rollups": len(rollups),
        "max_ok_in_five_minute_rollup": max((r["ok"] for r in rollups), default=None),
        "position_rollup_is_total_rest_census": False,
        "actual_peak_schwab_rest_calls_per_minute": None,
        "actual_peak_status": "UNMEASURED",
        "primary_documented_limit_per_minute": None,
        "primary_limit_status": "UNPROVEN: public Schwab portal gave no readable limit document",
        "primary_source_attempted": "https://developer.schwab.com/products/trader-api--individual",
        "extra_24_calls_per_minute_proven_safe": False,
        "limitations": [
            "Successful HTTP attempts are not individually logged by the adapter transport.",
            "Position rollups exclude other endpoints, retries and other app processes.",
            "No authenticated portal, tokens, broker requests or production writes were used.",
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
