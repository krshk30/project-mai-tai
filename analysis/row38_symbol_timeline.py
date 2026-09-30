"""Print one symbol's gateway events near a mirror decision from a raw stream dump."""

import gzip
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


path, symbol, start, end = sys.argv[1:]
start_ms = int(datetime.fromisoformat(start).timestamp() * 1000)
end_ms = int(datetime.fromisoformat(end).timestamp() * 1000)
opener = gzip.open if Path(path).suffix == ".gz" else open
event_ms = None
with opener(path, "rt", encoding="utf-8") as stream:
    for raw in stream:
        line = raw.strip()
        if re.fullmatch(r"\d{13}-\d+", line):
            event_ms = int(line.split("-", 1)[0])
            continue
        if event_ms is None or not start_ms <= event_ms <= end_ms or not line.startswith("{"):
            continue
        event = json.loads(line)
        payload = event.get("payload", {})
        if payload.get("symbol") != symbol:
            continue
        published = datetime.fromtimestamp(event_ms / 1000, timezone.utc).isoformat()
        print(published, event.get("event_type"), payload)
