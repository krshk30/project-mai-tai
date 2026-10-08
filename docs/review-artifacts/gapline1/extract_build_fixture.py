"""Extract retained candles and hold identities; never contact a live system."""

import argparse
import hashlib
import json
from pathlib import Path


def extract(path):
    raw = path.read_bytes()
    pull = json.loads(raw)
    return {
        "as_of_utc": pull["as_of_utc"],
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "sources": pull["sources"],
        "sessions": {
            key: [[row["bar_time"], *[row[field] for field in
                   ("open_price", "high_price", "low_price", "close_price", "volume")]]
                  for row in rows]
            for key, rows in pull["sessions"].items()
        },
        "holds": [
            {
                "symbol": case["hold"]["symbol"],
                "detected_ms": case["hold"]["ms"],
                "kind": "recovery" if "reason=recovery_gap" in case["hold"]["line"] else "initial",
                "source": case["hold"]["path"] + ":" + str(case["hold"]["line_number"]),
                "prior": case["prior"]["line"] if case["prior"] else None,
                "resume_ms": case["resume"]["ms"] if case["resume"] else None,
            }
            for case in pull["holds"]
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    args = parser.parse_args()
    print(json.dumps(extract(args.raw), separators=(",", ":")))
