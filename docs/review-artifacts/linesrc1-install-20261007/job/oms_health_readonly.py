"""[codex] Fresh new-OMS published health only while v2 is deliberately stopped."""
import argparse
from datetime import datetime, timezone
import json
import sys
from urllib.request import urlopen

import release_policy as p


class MeasuredBlock(p.Stop):
    pass


def prove(overview, start, now):
    p.need(isinstance(overview.get("errors"), list) and not overview["errors"], "overview errors/unreadable")
    rows = [row for row in overview["services"] if row.get("service_name") == "oms"]
    p.need(len(rows) == 1, "OMS heartbeat population ambiguous")
    row = rows[0]
    stamp = p.moment(row.get("observed_at_raw"))
    p.need(start <= stamp <= now and (now - stamp).total_seconds() <= 120,
           "new OMS heartbeat stale/old/future")
    if (row.get("raw_status", row.get("status")) != "healthy"
            or row.get("effective_status", row.get("status")) != "healthy"):
        raise MeasuredBlock("OMS not healthy")
    return dict(rc=0, observed_at_utc=stamp.isoformat(), service="oms", scope="new OMS only; strict flat separately unchanged")


def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--start", required=True)
        args = parser.parse_args()
        with urlopen("http://127.0.0.1:8100/api/overview", timeout=20) as response:
            raw = response.read(2_000_001)
        p.need(len(raw) <= 2_000_000, "overview exceeds2MB")
        print(json.dumps(prove(json.loads(raw), p.moment(args.start), datetime.now(timezone.utc)), sort_keys=True))
        return 0
    except MeasuredBlock:
        print(json.dumps(dict(rc=1, verdict="BLOCK", service="oms"), sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps(dict(rc=2, verdict="UNKNOWN", error_type=type(exc).__name__), sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
