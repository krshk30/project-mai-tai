"""Analyze a local JSONL log export; this script never connects to production."""
from __future__ import annotations

import argparse
import bisect
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKER = re.compile(r"\[(V2-[\w-]+)\]\s+(?:(?:sym|symbol)=)?([A-Z0-9.]+)(?:\s|$)")
FIELDS = re.compile(r"([a-z_]+)=([^\s]+)")


def analyze(path: Path, start: datetime, end: datetime) -> dict:
    symbols = defaultdict(list)
    rejects = 0
    for raw in path.open():
        row = json.loads(raw)
        line = row.get("text", "")
        found = MARKER.search(line)
        if not found:
            continue
        try:
            at = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
        except ValueError:
            rejects += 1
            continue
        if not start <= at <= end:
            continue
        row.update(at=at, marker=found[1], symbol=found[2], fields=dict(FIELDS.findall(line)))
        symbols[found[2]].append(row)
    pairs, unpaired = [], []
    cancels_by_day = Counter()
    for symbol, events in symbols.items():
        events.sort(key=lambda row: (row["at"], row["line"]))
        places = [row for row in events if row["marker"] == "V2-RESTING-PLACE"]
        place_times = [row["at"] for row in places]
        times = [row["at"] for row in events]
        for row in events:
            if row["marker"] != "V2-RESTING-CANCEL" or row["fields"].get("reason") != "reprice":
                continue
            cancels_by_day[row["at"].astimezone(ET).date().isoformat()] += 1
            index = bisect.bisect_right(place_times, row["at"])
            if index == len(places):
                unpaired.append({"symbol": symbol, "at": row["at"], "path": row["path"], "line": row["line"]})
                continue
            placed = places[index]
            seconds = (placed["at"] - row["at"]).total_seconds()
            between = events[bisect.bisect_right(times, row["at"]):bisect.bisect_left(times, placed["at"])]
            probes = [e for e in between if e["marker"] == "V2-ATR-PROBE"]
            # These are observations, not inferred exclusive causes. Historical flag/floor
            # values and silent early-return state are not reconstructed by this parser.
            observations = Counter()
            evidence = []
            if seconds >= 90:
                for event in between:
                    fields = event["fields"]
                    if event["marker"] == "V2-STOP-ASK-PRICE-CHECK" and fields.get("held") == "1":
                        observations["explicit_stop_ask_" + fields.get("reason", "unknown")] += 1
                        evidence.append(event)
                    elif event["marker"] == "V2-FLIP-OWNER-ADMISSION" and fields.get("allowed") == "0":
                        observations["explicit_admission_" + fields.get("reason", "unknown")] += 1
                        evidence.append(event)
                for event in probes:
                    fields = event["fields"]
                    if fields.get("flip", "none").lower() not in {"none", "null"}:
                        observations["intervening_flip_" + fields["flip"]] += 1
                    if fields.get("state") == "short" and float(fields.get("vol", "inf")) <= 10000:
                        observations["short_bar_volume_at_most_10000"] += 1
                    if fields.get("state") == "short" and int(fields.get("age", "999")) < 3:
                        observations["short_age_under_3"] += 1
                    evidence.append(event)
            pairs.append({
                "symbol": symbol, "cancel_et": row["at"].astimezone(ET),
                "place_et": placed["at"].astimezone(ET), "seconds": seconds,
                "same_day": row["at"].astimezone(ET).date() == placed["at"].astimezone(ET).date(),
                "cancel_path": row["path"], "cancel_line": row["line"],
                "place_path": placed["path"], "place_line": placed["line"],
                "observations": observations,
                "evidence": [{k: e[k] for k in ("path", "line", "text")} for e in evidence],
            })
    same_day = [p for p in pairs if p["same_day"]]
    values = [p["seconds"] for p in same_day]
    long = [p for p in same_day if p["seconds"] >= 90]
    return {
        "start": start, "end": end, "cancel_count": sum(cancels_by_day.values()),
        "cancels_by_day": dict(sorted(cancels_by_day.items())),
        "pairs": len(pairs), "same_day_pairs": len(same_day), "cross_day_pairs": len(pairs) - len(same_day),
        "unpaired": unpaired, "parse_rejects": rejects,
        "same_day_seconds": {"min": min(values, default=None), "median": statistics.median(values) if values else None,
                             "max": max(values, default=None), "under_10": sum(v < 10 for v in values),
                             "under_90": sum(v < 90 for v in values),
                             "90_to_150": sum(90 <= v < 150 for v in values),
                             "150_plus": sum(v >= 150 for v in values)},
        "long_pairs": long,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--start", default="2026-09-18T00:00:00-04:00")
    parser.add_argument("--end", default="2026-10-02T15:25:00-04:00")
    args = parser.parse_args()
    print(json.dumps(analyze(args.input, datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)), default=str, indent=2))
