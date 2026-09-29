#!/usr/bin/env python3
"""Read-only B11 resting-buy provenance and fill-outcome study.

The caller supplies immutable CSV snapshots and rotated v2 logs. This script
does not connect to a broker or database and never estimates missing exits.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FIRST_DAY = datetime(2026, 9, 1, 8, tzinfo=UTC)
ACTIVATE_993 = datetime(2026, 9, 16, 23, 43, 44, tzinfo=UTC)
ACTIVATE_1049 = datetime(2026, 9, 28, 22, 42, 25, tzinfo=UTC)
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.](\d{3})")
PROBE = re.compile(
    r"\[V2-ATR-PROBE\] sym=(\S+) ts_ms=(\d+).*?trail=([\d.]+) "
    r"state=(\w+) age=(\d+) .*?flip=(\w+)"
)
PLACE = re.compile(
    r"\[V2-RESTING-(PLACE|EH-ARM)\] (\S+) slot=(\S+) "
    r"line=([\d.]+) trigger=([\d.]+)"
)
LEGACY_PLACE = re.compile(
    r"\[V2-RESTING-(PLACE|EH-ARM)\] (\S+) slot=(\S+) "
    r"(?:stop=|soft-rest at level=)([\d.]+)"
)
WARM = re.compile(r"\[V2-(?:REST|STREAMER)-WARMED\].*?for (\S+) ")
SEED = re.compile(r"\[V2-(?:DB-SEED[^\]]*|CW-SEED-CAP|STREAMER-DRAIN)\].*?\b([A-Z]{2,6})\b")
WATCH = re.compile(r"schwab_1m_v2 watchlist updated count=\d+ sample=([A-Z,]+)")
SEGMENT_BOUND = re.compile(r"\[V2-FANOUT-SEGMENT-BOUND\] (\S+).*?segment_id=(\d+)")
SLOT_BOUND = re.compile(r"\[V2-FANOUT-SLOT-BOUND\] (\S+).*?slot_id=([0-9a-f-]{36})")


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def log_time(line: str) -> datetime | None:
    match = TS.match(line)
    return dt(f"{match[1]}.{match[2]}+00:00") if match else None


def session_day(at: datetime) -> str:
    et = at.astimezone(ET)
    if et.hour < 4:
        et -= timedelta(days=1)
    return et.date().isoformat()


def era(at: datetime) -> str:
    if at < ACTIVATE_993:
        return "pre_993"
    if at < ACTIVATE_1049:
        return "post_993_pre_1049"
    return "post_1049"


@dataclass(frozen=True)
class Probe:
    emitted: datetime
    bar: datetime
    trail: Decimal
    state: str
    age: int
    flip: str
    source_line: int

    @property
    def latency_s(self) -> float:
        return (self.emitted - self.bar).total_seconds()


@dataclass(frozen=True)
class Placement:
    symbol: str
    at: datetime
    slot: str
    line: Decimal
    trigger: Decimal
    kind: str
    source_line: int
    short_segment: tuple[Probe, ...]
    latest_warm: datetime | None
    latest_seed: datetime | None
    watch_sample: datetime | None
    segment_id: str = ""
    slot_id: str = ""


def _logs(paths: list[Path]):
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as source:
            yield from source


def parse_logs(paths: list[Path], symbols: set[str], cutoff: datetime) -> tuple[list[Placement], dict[str, object]]:
    placements: list[Placement] = []
    segment: dict[str, list[Probe]] = defaultdict(list)
    warmed: dict[str, datetime] = {}
    seeded: dict[str, datetime] = {}
    sampled: dict[str, datetime] = {}
    counts = {"arms": 0, "placements": 0, "probe": 0}
    era_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"arms": 0, "placements": 0})
    last_time: datetime | None = None
    for number, line in enumerate(_logs(paths), 1):
        at = log_time(line)
        if at is None or at < FIRST_DAY or at > cutoff:
            continue
        if last_time is not None and at < last_time:
            raise ValueError(f"v2 logs out of write order at line {number}")
        last_time = at
        if "[V2-CW-ARM]" in line:
            counts["arms"] += 1
            era_counts[era(at)]["arms"] += 1
        if match := WARM.search(line):
            if match[1] in symbols:
                warmed[match[1]] = at
        if match := SEED.search(line):
            if match[1] in symbols:
                seeded[match[1]] = at
        if match := WATCH.search(line):
            for symbol in match[1].split(","):
                if symbol in symbols:
                    sampled[symbol] = at
        if match := PROBE.search(line):
            symbol = match[1]
            if symbol not in symbols:
                continue
            counts["probe"] += 1
            probe = Probe(
                at, datetime.fromtimestamp(int(match[2]) / 1000, UTC),
                Decimal(match[3]), match[4], int(match[5]), match[6], number,
            )
            if probe.state != "short":
                segment.pop(symbol, None)
            elif probe.age == 0:
                segment[symbol] = [probe]
            elif symbol in segment:
                segment[symbol].append(probe)
        if match := SEGMENT_BOUND.search(line):
            if placements and placements[-1].symbol == match[1] and timedelta() <= at - placements[-1].at <= timedelta(seconds=1):
                placements[-1] = replace(placements[-1], segment_id=match[2])
        if match := SLOT_BOUND.search(line):
            if placements and placements[-1].symbol == match[1] and timedelta() <= at - placements[-1].at <= timedelta(seconds=1):
                placements[-1] = replace(placements[-1], slot_id=match[2])
        match = PLACE.search(line)
        legacy = None if match else LEGACY_PLACE.search(line)
        if match or legacy:
            match = match or legacy
            symbol = match[2]
            counts["placements"] += 1
            era_counts[era(at)]["placements"] += 1
            if symbol not in symbols:
                continue
            placements.append(
                Placement(
                    symbol, at, match[3], Decimal(match[4]),
                    Decimal(match[5]) if legacy is None else Decimal(match[4]),
                    match[1], number, tuple(segment.get(symbol, ())),
                    warmed.get(symbol), seeded.get(symbol), sampled.get(symbol),
                )
            )
    return placements, {**counts, "by_era": dict(era_counts)}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def scanner_windows(rows: list[dict[str, str]], symbols: set[str]) -> dict[tuple[str, str], list[tuple[datetime, datetime | None]]]:
    events: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["symbol"] in symbols:
            events[(row["trade_date"], row["symbol"])].append(row)
    windows: dict[tuple[str, str], list[tuple[datetime, datetime | None]]] = {}
    for key, rows_for_key in events.items():
        result: list[tuple[datetime, datetime | None]] = []
        start: datetime | None = None
        for row in rows_for_key:
            at = dt(row["event_at"])
            if row["event_type"] == "CONFIRM" and start is None:
                start = at
            elif row["event_type"] in {"FADE", "RETENTION_DROP"} and start is not None:
                result.append((start, at))
                start = None
        if start is not None:
            result.append((start, None))
        windows[key] = result
    return windows


def classify_place(
    place: Placement,
    windows: dict[tuple[str, str], list[tuple[datetime, datetime | None]]],
) -> tuple[str, str, str]:
    probes = place.short_segment
    if not probes or probes[0].age != 0 or probes[0].flip != "SELL":
        return "UNKNOWN", "UNKNOWN", "short_flip_not_observed"
    if not {0, 1, 2}.issubset({probe.age for probe in probes}):
        return "UNKNOWN", "UNKNOWN", "three_short_bars_incomplete"
    if not probes[-1].trail.quantize(Decimal("0.0001")) == place.line:
        return "UNKNOWN", "UNKNOWN", "trail_line_mismatch"
    first_three = [next(probe for probe in probes if probe.age == age) for age in (0, 1, 2)]
    if len({probe.bar for probe in first_three}) != 3:
        return "UNKNOWN", "UNKNOWN", "short_bar_identity_ambiguous"
    evidence = [*first_three, probes[-1]]
    key = (session_day(place.at), place.symbol)
    day_windows = windows.get(key, ())
    covering = [
        (index, window) for index, window in enumerate(day_windows)
        if window[0] <= place.at and (window[1] is None or place.at < window[1])
    ]
    if any(probe.latency_s > 300 for probe in evidence):
        if len(covering) == 1 and any(probe.bar < covering[0][1][0] for probe in evidence):
            return "REBUILT", "same_session_readd" if covering[0][0] else "pre_watch", "historical_short_or_trail_bar"
        return "REBUILT", "UNKNOWN", "historical_short_or_trail_bar"
    if any(probe.latency_s < 0 or probe.latency_s > 120 for probe in evidence):
        return "UNKNOWN", "UNKNOWN", "bar_latency_uncertain"
    if len(covering) != 1:
        return "UNKNOWN", "UNKNOWN", "scanner_membership_ambiguous"
    watch_start = covering[0][1][0]
    if any(probe.bar < watch_start for probe in evidence):
        return "REBUILT", "same_session_readd" if covering[0][0] else "pre_watch", "short_or_trail_before_confirm"
    if place.latest_seed is not None and probes[0].emitted <= place.latest_seed <= place.at:
        return "UNKNOWN", "UNKNOWN", "seed_during_short_segment"
    if place.latest_warm is None or not (watch_start <= place.latest_warm < probes[0].bar):
        return "UNKNOWN", "UNKNOWN", "warmup_after_short_or_unverified"
    if place.watch_sample is None or not (watch_start <= place.watch_sample <= place.at):
        return "UNKNOWN", "UNKNOWN", "bot_watch_membership_unverified"
    return "LIVE", "", "live_short_and_trail_after_warmup"


def match_place(fill: dict[str, str], places: list[Placement]) -> tuple[Placement | None, str]:
    metadata = json.loads(fill["intent_payload"] or "{}").get("metadata", {})
    if metadata.get("cw_entry_slot") != "first":
        return None, "not_first_slot"
    try:
        trigger = Decimal(str(metadata["entry_price"]))
    except (KeyError, ValueError):
        return None, "entry_trigger_missing"
    intent_at = dt(fill["intent_at"]) if fill["intent_at"] else dt(fill["filled_at"])
    # A Webull mirrored intent can be consumed minutes after placement. The
    # exact trigger and, when logged, segment identity are stronger than lag.
    max_age = timedelta(minutes=30)
    segment_id = str(metadata.get("fanout_segment_id") or "")
    slot_id = str(metadata.get("fanout_slot_id") or "")
    matches = [
        place for place in places
        if place.symbol == fill["symbol"] and place.slot == "first"
        and place.trigger == trigger and timedelta() <= intent_at - place.at <= max_age
        and (not place.segment_id or not segment_id or place.segment_id == segment_id)
        and (not place.slot_id or not slot_id or place.slot_id == slot_id)
    ]
    near = [place for place in matches if intent_at - place.at <= timedelta(seconds=60)]
    if len(near) == 1:
        return near[0], "exact_trigger_identity_near"
    if len(near) > 1:
        return None, f"near_placement_matches_{len(near)}"
    if len(matches) == 1:
        return matches[0], "exact_trigger_identity_delayed"
    if len(matches) != 1:
        return None, f"placement_matches_{len(matches)}"
    raise AssertionError("unreachable placement count")


def pair_fills(rows: list[dict[str, str]]) -> dict[str, tuple[Decimal, Decimal, list[str]]]:
    """Only fully paired, non-overlapping same-session buys receive outcomes."""
    queue: dict[tuple[str, str, str], deque[tuple[dict[str, str], Decimal]]] = defaultdict(deque)
    sold: dict[str, list[tuple[Decimal, Decimal, str]]] = defaultdict(list)
    overlapping: set[tuple[str, str, str]] = set()
    for row in sorted(rows, key=lambda row: (dt(row["filled_at"]), row["fill_id"])):
        key = session_day(dt(row["filled_at"])), row["account"], row["symbol"]
        qty = Decimal(row["quantity"])
        if row["side"] == "buy":
            if queue[key]:
                overlapping.add(key)
            queue[key].append((row, qty))
        elif row["side"] == "sell":
            while qty > 0 and queue[key]:
                buy, remaining = queue[key][0]
                used = min(qty, remaining)
                sold[buy["fill_id"]].append((used, Decimal(row["price"]), row["fill_id"]))
                qty -= used
                remaining -= used
                if remaining:
                    queue[key][0] = buy, remaining
                else:
                    queue[key].popleft()
    result = {}
    for row in rows:
        if row["side"] != "buy":
            continue
        if (session_day(dt(row["filled_at"])), row["account"], row["symbol"]) in overlapping:
            continue
        exits = sold.get(row["fill_id"], [])
        buy_qty = Decimal(row["quantity"])
        sell_qty = sum((qty for qty, _, _ in exits), Decimal(0))
        if sell_qty == buy_qty and buy_qty > 0:
            sell_vwap = sum((qty * price for qty, price, _ in exits), Decimal(0)) / sell_qty
            result[row["fill_id"]] = buy_qty, sell_vwap, [fill_id for _, _, fill_id in exits]
    return result


def overlapping_sessions(rows: list[dict[str, str]]) -> set[tuple[str, str, str]]:
    balance: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    ambiguous: set[tuple[str, str, str]] = set()
    for row in sorted(rows, key=lambda row: (dt(row["filled_at"]), row["fill_id"])):
        key = session_day(dt(row["filled_at"])), row["account"], row["symbol"]
        qty = Decimal(row["quantity"])
        if row["side"] == "buy":
            if balance[key] > 0:
                ambiguous.add(key)
            balance[key] += qty
        elif row["side"] == "sell":
            balance[key] -= qty
    return ambiguous


def managed_row(fill: dict[str, str], rows: list[dict[str, str]]) -> tuple[str, str]:
    fill_at = dt(fill["filled_at"])
    candidates = [
        row for row in rows
        if row["account"] == fill["account"] and row["symbol"] == fill["symbol"]
        and timedelta(seconds=-2) <= dt(row["entry_time"]) - fill_at <= timedelta(seconds=60)
        and Decimal(row["entry_price"]) == Decimal(fill["price"])
    ]
    if len(candidates) == 1:
        return candidates[0]["row_id"], "exact_price_time_account"
    return "", f"managed_row_matches_{len(candidates)}"


def summarize(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["era"], row["account"], row["classification"]].append(row)
    summaries = []
    for (which_era, account, label), group in sorted(grouped.items()):
        scored = [(row["symbol"], Decimal(row["return_pct"])) for row in group if row["return_pct"]]
        values = [value for _, value in scored]
        by_name = {}
        for symbol in sorted({name for name, _ in scored}):
            remaining = [value for name, value in scored if name != symbol]
            if remaining:
                by_name[symbol] = (float(median(remaining)), float(sum(remaining)))
        summaries.append({
            "era": which_era, "account": account, "classification": label,
            "filled_buy_legs": len(group), "rows_with_outcomes": len(values),
            "winners": sum(value > 0 for value in values),
            "median_pct": float(median(values)) if values else None,
            "sum_pct": float(sum(values)) if values else None,
            "drop_one_by_name": by_name,
        })
    return summaries


def breakdown(rows: list[dict[str, str]], dimension: str) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["account"], row["classification"], row[dimension]].append(row)
    result = []
    for (account, label, value), group in sorted(grouped.items()):
        returns = [Decimal(row["return_pct"]) for row in group if row["return_pct"]]
        result.append({
            "account": account, "classification": label, dimension: value,
            "filled_buy_legs": len(group), "rows_with_outcomes": len(returns),
            "winners": sum(item > 0 for item in returns),
            "median_pct": float(median(returns)) if returns else None,
            "sum_pct": float(sum(returns)) if returns else None,
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--fills", type=Path, required=True)
    parser.add_argument("--managed", type=Path, required=True)
    parser.add_argument("--scanner", type=Path, required=True)
    parser.add_argument("--stages", type=Path, required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--trips", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    cutoff = dt(args.cutoff)
    fills = read_csv(args.fills)
    managed = read_csv(args.managed)
    symbols = {row["symbol"] for row in fills if row["side"] == "buy"}
    log_paths = sorted(args.logs.glob("schwab-1m-v2.log-202609*.gz"))
    log_paths.extend([args.logs / "schwab-1m-v2.log-20260928", args.logs / "schwab-1m-v2.log"])
    places, counts = parse_logs(log_paths, symbols, cutoff)
    by_symbol: dict[str, list[Placement]] = defaultdict(list)
    for place in places:
        by_symbol[place.symbol].append(place)
    windows = scanner_windows(read_csv(args.scanner), symbols)
    exits = pair_fills(fills)
    overlaps = overlapping_sessions(fills)
    stages = read_csv(args.stages)
    managed_by_id = {row["row_id"]: row for row in managed}
    fills_by_id = {row["fill_id"]: row for row in fills}
    trips = []
    for fill in fills:
        if fill["side"] != "buy":
            continue
        metadata = json.loads(fill["intent_payload"] or "{}").get("metadata", {})
        place, join = match_place(fill, by_symbol.get(fill["symbol"], []))
        if metadata.get("cw_entry_slot") != "first":
            label, subclass, reason = "NOT_FIRST", "", "reclaim_or_other_entry"
        else:
            label, subclass, reason = classify_place(place, windows) if place else ("UNKNOWN", "UNKNOWN", join)
        row_id, row_join = managed_row(fill, managed)
        outcome = exits.get(fill["fill_id"])
        return_pct = ""
        sell_ids = ""
        sell_vwap = ""
        outcome_exclusion = ""
        if outcome and not row_id:
            outcome_exclusion = row_join
        elif not outcome:
            outcome_exclusion = "exit_fill_missing_or_ambiguous"
        elif (
            managed_by_id[row_id]["status"] != "closed"
            or Decimal(managed_by_id[row_id]["current_quantity"]) != 0
            or any(
                dt(fills_by_id[sell_id]["filled_at"]) > dt(managed_by_id[row_id]["updated_at"])
                for sell_id in outcome[2]
            )
        ):
            outcome_exclusion = "managed_row_not_closed_or_sell_after_close"
        else:
            sell_vwap = str(outcome[1])
            sell_ids = ";".join(outcome[2])
            return_pct = str((outcome[1] / Decimal(fill["price"]) - 1) * 100)
        trips.append({
            "row_id": row_id, "row_join": row_join, "intent_id": fill["intent_id"],
            "order_id": fill["order_id"], "buy_fill_id": fill["fill_id"],
            "sell_fill_ids": sell_ids, "account": fill["account"],
            "symbol": fill["symbol"], "entry_at": fill["filled_at"],
            "placement_at": place.at.isoformat() if place else "",
            "placement_line": place.source_line if place else "",
            "join_method": join, "classification": label, "subclass": subclass,
            "classification_reason": reason, "era": era(dt(fill["filled_at"])),
            "entry_slot": metadata.get("cw_entry_slot", ""),
            "holdout": "2026-09-22" <= session_day(dt(fill["filled_at"])) <= "2026-09-26",
            "session_day": session_day(dt(fill["filled_at"])),
            "hour_et": dt(fill["filled_at"]).astimezone(ET).strftime("%H"),
            "segment_id": metadata.get("fanout_segment_id", ""),
            "placement_segment_id": place.segment_id if place else "",
            "slot_id": metadata.get("fanout_slot_id", ""),
            "placement_slot_id": place.slot_id if place else "",
            "trigger": metadata.get("entry_price", ""),
            "short_flip_bar": place.short_segment[0].bar.isoformat() if place and place.short_segment else "",
            "short_flip_emitted": place.short_segment[0].emitted.isoformat() if place and place.short_segment else "",
            "third_short_bar": place.short_segment[2].bar.isoformat() if place and len(place.short_segment) > 2 else "",
            "trail_bar": place.short_segment[-1].bar.isoformat() if place and place.short_segment else "",
            "warm_at": place.latest_warm.isoformat() if place and place.latest_warm else "",
            "seed_at": place.latest_seed.isoformat() if place and place.latest_seed else "",
            "watch_sample_at": place.watch_sample.isoformat() if place and place.watch_sample else "",
            "buy_qty": fill["quantity"], "buy_vwap": fill["price"],
            "sell_vwap": sell_vwap, "return_pct": return_pct,
            "outcome_exclusion": outcome_exclusion,
        })
    with args.trips.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(trips[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(trips)
    args.summary.write_text(
        json.dumps({"cutoff": cutoff.isoformat(), "log_counts": counts,
                    "managed_rows": len(managed), "buy_fills": len(trips),
                    "first_slot_buy_fills": sum(row["entry_slot"] == "first" for row in trips),
                    "placement_matches": sum(row["join_method"].startswith("exact_trigger_identity_") for row in trips),
                    "fully_paired_exit_fills": len(exits),
                    "managed_row_matches": sum(bool(row["row_id"]) for row in trips),
                    "overlapping_account_symbol_sessions": [list(key) for key in sorted(overlaps)],
                    "outcomes": sum(bool(row["return_pct"]) for row in trips),
                    "entry_stages": stages,
                    "groups": summarize(trips),
                    "holdout": breakdown(trips, "holdout"),
                    "hour_et": breakdown(trips, "hour_et"),
                    "subclass": breakdown(trips, "subclass"),
                    "unknown_reasons": dict(sorted(
                        ((reason, sum(row["classification_reason"] == reason for row in trips))
                         for reason in {row["classification_reason"] for row in trips if row["classification"] == "UNKNOWN"}),
                        key=lambda item: (-item[1], item[0]),
                    ))}, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
