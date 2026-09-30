#!/usr/bin/env python3
"""Read-only summaries for the 2026-09-30 Option A inactive control."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
START = datetime(2026, 9, 30, 11, 0, tzinfo=UTC)
END = datetime(2026, 9, 30, 13, 40, tzinfo=UTC)
LOG_TIME = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})")
WATCH = re.compile(r"watchlist updated count=(\d+) sample=([^ ]*)")
PROBE = re.compile(r"\[V2-ATR-PROBE\] sym=([A-Z0-9.\-^]+) ts_ms=(\d+)")
TRACE = re.compile(
    r"^(\S+) load=([\d.]+),([\d.]+),([\d.]+) snap_last=(\S+) hb_last=(\S+)"
)


def iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def log_time(line: str) -> datetime | None:
    match = LOG_TIME.match(line)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[math.ceil(0.95 * len(ordered)) - 1], 4)


def display(value: datetime | None) -> str | None:
    return value.astimezone(ET).isoformat() if value else None


def summarize_trace(path: Path) -> dict:
    rows = []
    for line in path.open(encoding="utf-8"):
        match = TRACE.match(line)
        if not match:
            continue
        stamp = iso(match.group(1))
        if START <= stamp < END:
            rows.append((stamp, float(match.group(2)), match.group(5), match.group(6)))
    if not rows:
        return {"rows": 0, "error": "no in-window rows"}
    first, last = rows[0][0], rows[-1][0]
    seconds = {int(stamp.timestamp()) for stamp, *_ in rows}
    expected_observed = int(last.timestamp()) - int(first.timestamp()) + 1
    snapshots: list[datetime] = []
    seen_ids = set()
    for _, _, snapshot_id, _ in rows:
        if snapshot_id == "nil" or snapshot_id in seen_ids:
            continue
        seen_ids.add(snapshot_id)
        snapshots.append(datetime.fromtimestamp(int(snapshot_id.split("-", 1)[0]) / 1000, UTC))
    snapshots.sort()
    intervals = [(b - a).total_seconds() for a, b in zip(snapshots, snapshots[1:])]
    intervals_by_hour: dict[str, list[float]] = defaultdict(list)
    interval_ends = []
    for a, b in zip(snapshots, snapshots[1:]):
        value = (b - a).total_seconds()
        intervals_by_hour[str(b.astimezone(ET).hour)].append(value)
        interval_ends.append((b, value))
    valid_minutes = sorted({stamp.astimezone(ET).strftime("%H:%M") for stamp in snapshots})
    snapshots_by_hour = dict(sorted(Counter(minute[:2] for minute in valid_minutes).items()))
    rolling_by_hour: dict[str, list[float]] = defaultdict(list)
    rolling_all = []
    minute = first.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while minute < END:
        window = [
            value for endpoint, value in interval_ends
            if minute - timedelta(minutes=5) < endpoint <= minute
        ]
        if len(window) >= 20:
            value = p95(window)
            assert value is not None
            rolling_all.append(value)
            rolling_by_hour[str(minute.astimezone(ET).hour)].append(value)
        minute += timedelta(minutes=1)
    valid_by_hour = {hour: len(values) for hour, values in sorted(rolling_by_hour.items())}
    snapshot_reference = {}
    for hour in (7, 8, 9):
        key = str(hour)
        measured = intervals_by_hour.get(key, [])
        use_hour = snapshots_by_hour.get(f"{hour:02d}", 0) >= 20 and bool(measured)
        reference = p95(measured if use_hour else intervals)
        snapshot_reference[key] = {
            "source": "matching_hour" if use_hour else "whole_control_fallback",
            "control_p95_s": reference,
            "stop_strictly_above_s": max(10.0, 2 * reference) if reference is not None else None,
        }
    loads = [load for _, load, _, _ in rows]
    return {
        "source": str(path),
        "covered_from_et": display(first),
        "covered_to_et": display(last),
        "load_rows": len(rows),
        "load_unique_seconds": len(seconds),
        "load_expected_whole_control": int((END - START).total_seconds()),
        "load_expected_observed_span": expected_observed,
        "load_missing_observed_seconds": expected_observed - len(seconds),
        "load_max1": max(loads),
        "load_over_3_5_rows": sum(value > 3.5 for value in loads),
        "load_over_3_5_unique_seconds": len(
            {int(stamp.timestamp()) for stamp, load, _, _ in rows if load > 3.5}
        ),
        "snapshot_first_et": display(snapshots[0] if snapshots else None),
        "snapshot_last_et": display(snapshots[-1] if snapshots else None),
        "snapshot_distinct_ids": len(snapshots),
        "snapshot_nominal_expected_whole_control": int((END - START).total_seconds() / 5),
        "snapshot_nominal_expected_observed_span": math.ceil(expected_observed / 5),
        "snapshot_interval_count": len(intervals),
        "snapshot_interval_p95_s": p95(intervals),
        "snapshot_interval_max_s": max(intervals, default=None),
        "snapshot_rolling_5m_p95_count": len(rolling_all),
        "snapshot_rolling_5m_control_p95_s": p95(rolling_all),
        "snapshot_interval_p95_by_et_hour_s": {
            hour: p95(values) for hour, values in sorted(intervals_by_hour.items())
        },
        "snapshot_generated_minutes_by_et_hour": snapshots_by_hour,
        "snapshot_rolling_valid_minutes_by_et_hour": valid_by_hour,
        "snapshot_reference_by_et_hour": snapshot_reference,
    }


def heartbeat_events(paths: list[Path]) -> list[dict]:
    events: dict[str, dict] = {}
    for path in paths:
        for line in path.open(encoding="utf-8"):
            if not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("event_type") != "service_heartbeat":
                continue
            key = item.get("event_id")
            if isinstance(key, str):
                events[key] = item
    return sorted(events.values(), key=lambda item: item["produced_at"])


def summarize_heartbeats(paths: list[Path], trace_path: Path | None) -> dict:
    events = [
        item for item in heartbeat_events(paths)
        if item.get("source_service") == "market-data-gateway"
    ]
    control = [item for item in events if START <= iso(item["produced_at"]) < END]
    if not control:
        return {"events": 0, "error": "no gateway heartbeats in control"}
    produced = [iso(item["produced_at"]) for item in control]
    gaps = [(b - a).total_seconds() for a, b in zip(produced, produced[1:])]
    statuses = Counter(str(item.get("payload", {}).get("status")) for item in control)
    minutes = {stamp.astimezone(ET).strftime("%H:%M") for stamp in produced}
    ages = []
    if trace_path is not None:
        for line in trace_path.open(encoding="utf-8"):
            match = TRACE.match(line)
            if not match:
                continue
            sampled = iso(match.group(1))
            if not START <= sampled < END:
                continue
            index = bisect.bisect_right(produced, sampled) - 1
            if index >= 0 and control[index].get("payload", {}).get("status") == "healthy":
                ages.append((sampled - produced[index]).total_seconds())
    max_age = max(ages, default=None)
    return {
        "sources": [str(path) for path in paths],
        "first_et": display(produced[0]),
        "last_et": display(produced[-1]),
        "gateway_events": len(control),
        "nominal_expected_15s": int((END - START).total_seconds() / 15),
        "status_counts": dict(statuses),
        "interarrival_p95_s": p95(gaps),
        "interarrival_max_s": max(gaps, default=None),
        "valid_minutes": len(minutes),
        "valid_minutes_by_et_hour": dict(sorted(Counter(m[:2] for m in minutes).items())),
        "healthy_observer_age_samples": len(ages),
        "healthy_observer_age_max_s": max_age,
        "healthy_observer_age_p95_s": p95(ages),
        "heartbeat_age_stop_strictly_above_s": max(30.0, 2 * max_age) if max_age is not None else None,
    }


def summarize_v2(path: Path) -> dict:
    watchlist: set[str] | None = None
    minute_members: dict[datetime, set[str]] = {}
    minute_complete: dict[datetime, bool] = {}
    probes: dict[tuple[str, datetime], float] = {}
    probe_receipts: dict[tuple[str, datetime], datetime] = {}
    duplicates = 0
    markers = Counter()
    watch_updates = 0
    truncated_watch_updates = 0
    for line in path.open(encoding="utf-8", errors="replace"):
        stamp = log_time(line)
        if stamp is None or stamp >= END:
            continue
        update = WATCH.search(line)
        if update:
            watchlist = set(filter(None, update.group(2).split(",")))
            if START <= stamp:
                watch_updates += 1
                truncated_watch_updates += int(int(update.group(1)) != len(watchlist))
        if stamp < START:
            continue
        if any(tag in line for tag in ("[V2-DB-SEED", "[V2-STREAMER-DRAIN]", "[V2-REST-WARMED]")):
            markers[line.split("[V2-", 1)[1].split("]", 1)[0]] += 1
        if update:
            continue
        probe = PROBE.search(line)
        if not probe:
            continue
        symbol = probe.group(1)
        bar_start = datetime.fromtimestamp(int(probe.group(2)) / 1000, UTC)
        bar_close = bar_start + timedelta(minutes=1)
        if not START <= bar_close < END:
            continue
        key = symbol, bar_close
        if key in probes:
            duplicates += 1
            continue
        probes[key] = (stamp - bar_close).total_seconds()
        probe_receipts[key] = stamp
    # A watchlist update can occur inside a minute. Count the symbol if it was watched
    # at that minute's close; this is an active-watch upper bound, not guaranteed trades.
    watchlist = None
    active_at_start = None
    complete_at_start = False
    for line in path.open(encoding="utf-8", errors="replace"):
        stamp = log_time(line)
        if stamp is None or stamp >= END:
            continue
        if stamp >= START:
            break
        update = WATCH.search(line)
        if update:
            watchlist = set(filter(None, update.group(2).split(",")))
            complete_at_start = int(update.group(1)) == len(watchlist)
        active_at_start = sorted(watchlist) if watchlist is not None else None
    watchlist = set(active_at_start) if active_at_start is not None else None
    complete = complete_at_start
    updates = []
    for line in path.open(encoding="utf-8", errors="replace"):
        stamp = log_time(line)
        if stamp is None or stamp < START or stamp >= END:
            continue
        update = WATCH.search(line)
        if update:
            sample = set(filter(None, update.group(2).split(",")))
            updates.append((stamp, sample, int(update.group(1)) == len(sample)))
    minute = START
    index = 0
    while minute < END:
        while index < len(updates) and updates[index][0] < minute:
            watchlist = updates[index][1]
            complete = updates[index][2]
            index += 1
        minute_members[minute] = set(watchlist or ())
        minute_complete[minute] = complete
        minute += timedelta(minutes=1)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    by_symbol_hour: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_symbol_minute: dict[str, dict[datetime, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (symbol, bar_close), lag in probes.items():
        if symbol not in minute_members.get(bar_close, set()):
            continue
        by_symbol[symbol].append(lag)
        received = probe_receipts[(symbol, bar_close)]
        by_symbol_hour[symbol][received.astimezone(ET).hour].append(lag)
        by_symbol_minute[symbol][received.replace(second=0, microsecond=0)].append(lag)
    expected = Counter(symbol for members in minute_members.values() for symbol in members)
    rolling_by_symbol: dict[str, list[float]] = defaultdict(list)
    rolling_by_symbol_hour: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    minute = START
    while minute < END:
        for symbol in by_symbol_minute:
            window = [
                lag
                for offset in range(5)
                for lag in by_symbol_minute[symbol].get(minute - timedelta(minutes=offset), ())
            ]
            if len(window) < 3:
                continue
            value = p95(window)
            assert value is not None
            rolling_by_symbol[symbol].append(value)
            hour = minute.astimezone(ET).hour
            rolling_by_symbol_hour[symbol][hour].append(value)
        minute += timedelta(minutes=1)
    pooled_raw_hour: dict[int, list[float]] = defaultdict(list)
    for hourly in by_symbol_hour.values():
        for hour, values in hourly.items():
            pooled_raw_hour[hour].extend(values)
    pooled_raw_all = [lag for values in by_symbol.values() for lag in values]
    per_symbol = {}
    for symbol in sorted(set(expected) | set(by_symbol)):
        first_probe_close = min(
            (close for candidate, close in probes if candidate == symbol and candidate in minute_members.get(close, set())),
            default=None,
        )
        expected_after_first_probe = sum(
            first_probe_close is not None and close >= first_probe_close and symbol in members
            for close, members in minute_members.items()
        )
        references = {}
        for hour in (7, 8, 9):
            values = by_symbol_hour[symbol].get(hour, [])
            if len(values) >= 20:
                source, selected = "symbol_matching_hour", values
            elif by_symbol[symbol]:
                source, selected = "symbol_whole_control_fallback", by_symbol[symbol]
            elif len(pooled_raw_hour[hour]) >= 20:
                source, selected = "pooled_matching_hour_no_symbol_control", pooled_raw_hour[hour]
            else:
                source, selected = "pooled_whole_control_fallback", pooled_raw_all
            reference = p95(selected)
            references[str(hour)] = {
                "source": source,
                "control_p95_s": reference,
                "stop_strictly_above_s": max(5.0, 2 * reference) if reference is not None else None,
            }
        per_symbol[symbol] = {
            "eligible_live_probes": len(by_symbol[symbol]),
            "known_active_watch_minutes": expected[symbol],
            "expected_minutes_after_first_live_probe": expected_after_first_probe,
            "first_live_probe_bar_close_et": display(first_probe_close),
            "lag_p95_s": p95(by_symbol[symbol]),
            "lag_max_s": max(by_symbol[symbol], default=None),
            "rolling_5m_p95_count": len(rolling_by_symbol[symbol]),
            "rolling_5m_p95_s": p95(rolling_by_symbol[symbol]),
            "lag_p95_by_et_hour_s": {
                str(hour): p95(values) for hour, values in sorted(by_symbol_hour[symbol].items())
            },
            "rolling_valid_minutes_by_et_hour": {
                str(hour): len(values)
                for hour, values in sorted(rolling_by_symbol_hour[symbol].items())
            },
            "reference_by_et_hour": references,
        }
    return {
        "source": str(path),
        "watchlist_at_start": active_at_start,
        "watchlist_at_start_complete": complete_at_start,
        "watchlist_updates_in_window": watch_updates,
        "watchlist_truncated_updates_in_window": truncated_watch_updates,
        "watchlist_incomplete_minutes": sum(not complete for complete in minute_complete.values()),
        "probe_unique_total": sum(len(values) for values in by_symbol.values()),
        "probe_duplicate_total": duplicates,
        "seed_replay_warmup_markers": dict(markers),
        "per_symbol": per_symbol,
    }


def summarize_oms(path: Path) -> dict:
    direct = Counter()
    by_minute = defaultdict(Counter)
    first: datetime | None = None
    last: datetime | None = None
    timestamped = 0
    for line in path.open(encoding="utf-8", errors="replace"):
        stamp = log_time(line)
        if stamp is None or not START <= stamp < END:
            continue
        timestamped += 1
        first = first or stamp
        last = stamp
        kind = None
        if (
            ("[OMS-ABANDON-INTENT]" in line and "code=NO_FRESH_QUOTE" in line)
            or ("[OMS-BROKER-REJECT]" in line and "reason=NO_FRESH_QUOTE" in line)
        ):
            kind = "NO_FRESH_QUOTE"
        elif "no valid OMS market snapshot" in line:
            kind = "no_valid_market_snapshot"
        if kind:
            direct[kind] += 1
            by_minute[stamp.astimezone(ET).strftime("%H:%M")][kind] += 1
    return {
        "source": str(path),
        "first_timestamped_et": display(first),
        "last_timestamped_et": display(last),
        "timestamped_records_in_window": timestamped,
        "direct_refusals": dict(direct),
        "refusal_minutes": {minute: dict(counts) for minute, counts in sorted(by_minute.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("trace", "heartbeats", "v2", "oms"))
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    if args.mode == "trace":
        result = summarize_trace(args.paths[0])
    elif args.mode == "heartbeats":
        result = summarize_heartbeats(args.paths, args.trace)
    elif args.mode == "v2":
        result = summarize_v2(args.paths[0])
    else:
        result = summarize_oms(args.paths[0])
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
