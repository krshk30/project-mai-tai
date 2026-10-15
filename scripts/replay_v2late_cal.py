"""Local retained-file rule replay only. No services, provider or Redis operations."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from project_mai_tai.momwarn_cohort_lateness import replay_events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--through", default="10:45")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.capture.read_bytes()
    if len(raw) > 64 * 1024**2:
        raise ValueError("capture byte bound")
    capture = json.loads(raw)
    zone = ZoneInfo("America/New_York")
    start = int(datetime.fromisoformat(args.day+"T04:00:00").replace(tzinfo=zone).timestamp())*10**9
    end = int(datetime.fromisoformat(args.day+"T"+args.through+":00").replace(tzinfo=zone).timestamp())*10**9
    result = replay_events(capture, first_due_ns=start, last_due_ns=end)
    result.update(capture_sha256=hashlib.sha256(raw).hexdigest(), capture_rows=len(capture["rows"]),
                  capture_as_of_ns=capture["as_of_ns"])
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"trips":result["trips"], "rows":len(result["rows"]),
                      "counts":{status:sum(row["status"] == status for row in result["rows"])
                                for status in sorted({row["status"] for row in result["rows"]})},
                      "capture_sha256":result["capture_sha256"]}))


if __name__ == "__main__":
    main()
