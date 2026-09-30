"""Read-only lag census for raw ``redis-cli --raw XRANGE`` gateway dumps."""

from __future__ import annotations

import gzip
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ID = re.compile(r"^(\d{13})-\d+$")
ET = ZoneInfo("America/New_York")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def census(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    lag_by_mod: dict[int, list[float]] = defaultdict(list)
    lag_by_minute: dict[str, list[float]] = defaultdict(list)
    lag_all: list[float] = []
    per_symbol: dict[str, int] = defaultdict(int)
    first_id: int | None = None
    last_id: int | None = None
    missing_stamp = 0
    quote_count = 0
    trade_count = 0
    late: list[tuple[int, float]] = []
    event_ms: int | None = None
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            match = ID.match(line)
            if match:
                event_ms = int(match.group(1))
                first_id = event_ms if first_id is None else first_id
                last_id = event_ms
                continue
            if event_ms is None or not line.startswith("{"):
                continue
            event = json.loads(line)
            event_type = event.get("event_type")
            if event_type == "quote_tick":
                quote_count += 1
                continue
            if event_type != "trade_tick":
                continue
            trade_count += 1
            payload = event.get("payload", {})
            source_ms = payload.get("timestamp_ns")
            if not isinstance(source_ms, int) or source_ms <= 0:
                missing_stamp += 1
                continue
            lag = (event_ms - source_ms) / 1000
            stamp = datetime.fromtimestamp(event_ms / 1000, timezone.utc).astimezone(ET)
            key = stamp.strftime("%Y-%m-%d %H:%M")
            lag_all.append(lag)
            lag_by_mod[stamp.minute % 5].append(lag)
            lag_by_minute[key].append(lag)
            per_symbol[str(payload.get("symbol", ""))] += 1
            if lag > 2:
                late.append((event_ms, lag))

    def describe(values: list[float]) -> dict:
        return {
            "n": len(values), "p50_s": percentile(values, 0.5),
            "p95_s": percentile(values, 0.95),
            "p99_s": percentile(values, 0.99),
            "max_s": max(values) if values else None,
            "over_2s": sum(value > 2 for value in values),
        }

    stalls: list[dict] = []
    for published_ms, lag in late:
        if not stalls or published_ms - stalls[-1]["end_ms"] > 1500:
            stalls.append({"start_ms": published_ms, "end_ms": published_ms,
                           "max_s": lag, "n": 1})
        else:
            stalls[-1]["end_ms"] = published_ms
            stalls[-1]["max_s"] = max(stalls[-1]["max_s"], lag)
            stalls[-1]["n"] += 1
    return {
        "source": str(path),
        "first_utc": datetime.fromtimestamp(first_id / 1000, timezone.utc).isoformat()
        if first_id is not None else None,
        "last_utc": datetime.fromtimestamp(last_id / 1000, timezone.utc).isoformat()
        if last_id is not None else None,
        "trade_records": trade_count, "quote_records": quote_count,
        "trade_missing_source_stamp": missing_stamp,
        "all": describe(lag_all),
        "minute_mod_5": {str(index): describe(lag_by_mod[index]) for index in range(5)},
        "by_minute": {key: describe(values) for key, values in lag_by_minute.items()},
        "late_stalls": stalls,
        "late_symbols": sorted(per_symbol.items(), key=lambda item: item[1], reverse=True)[:20],
    }


if __name__ == "__main__":
    print(json.dumps(census(Path(sys.argv[1])), indent=2, sort_keys=True))
