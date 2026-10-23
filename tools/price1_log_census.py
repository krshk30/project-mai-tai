"""Read-only recorded warning-transition inventory, NOT a warning-model replay."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) ")
TRANSITION = re.compile(
    r"\[MOMENTUM-WARNING\] action=(ADD|DROP) symbol=([A-Z0-9._-]+) "
    r"trigger_price=(\S+) trigger_price_source=aggregate_high_at_add "
    r"status=(ADMITTED|DROPPED) reason=(\S+)\s*$"
)
MAX_BYTES = 2 * 1024 * 1024
MAX_LINE = 65536


def normalize(path: Path, *, expected_sha: str, log_zone: str,
              prior_closes: dict[str, Decimal] | None = None,
              prior_source: str | None = None) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
        raise ValueError("expected_sha256_required")
    priors = prior_closes or {}
    if priors and not prior_source:
        raise ValueError("prior_close_provenance_required")
    if any(not value.is_finite() or value <= 0 for value in priors.values()):
        raise ValueError("invalid_prior_close")
    zone = ZoneInfo(log_zone)
    digest = hashlib.sha256()
    records = []
    unparsed = []
    total = 0
    first = last = None
    with path.open("rb") as handle:
        for line_number, raw in enumerate(iter(lambda: handle.readline(MAX_LINE + 1), b""), 1):
            total += len(raw)
            if len(raw) > MAX_LINE or total > MAX_BYTES:
                raise ValueError("input_bound_exceeded")
            digest.update(raw)
            text = raw.decode("utf-8")
            stamp = STAMP.match(text)
            if stamp is None:
                unparsed.append(line_number)
                continue
            clock = datetime.strptime(stamp[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=zone)
            if last is not None and (clock < last or clock.date() != first.date()):
                raise ValueError("nonmonotonic_or_multiday_log")
            first = clock if first is None else first
            last = clock
            row = {"line": line_number, "at": clock, "kind": "other"}
            if "[MOMENTUM-PAPER] starting paper observer;" in text:
                row["kind"] = "start"
            elif "[MOMENTUM-PAPER-SESSION] ready " in text:
                row["kind"] = "session"
            elif "[MOMENTUM-PAPER-STOP] signal received;" in text:
                row["kind"] = "stop"
            elif "[MOMENTUM-WARNING] action=" in text:
                match = TRANSITION.search(text)
                if match is None or (match[1], match[4]) not in {
                    ("ADD", "ADMITTED"), ("DROP", "DROPPED"),
                }:
                    raise ValueError(f"malformed_transition_line_{line_number}")
                row.update(kind=match[1], symbol=match[2], high=match[3], reason=match[5])
            records.append(row)
    actual_sha = digest.hexdigest()
    if actual_sha != expected_sha:
        raise ValueError("input_sha256_mismatch")
    if first is None:
        raise ValueError("no_timestamped_records")

    active = set()
    minutes = []
    transitions = []
    boundaries = []
    anomalies = []
    counts = Counter()
    index = 0
    epoch = 0
    minute = first.replace(second=0, microsecond=0)
    while minute <= last:
        end = minute + timedelta(minutes=1)
        peak = len(active)
        peak_symbols = sorted(active)
        refs = []
        potential = set()
        while index < len(records) and records[index]["at"] < end:
            record = records[index]
            index += 1
            kind = record["kind"]
            if kind in {"start", "session", "stop"}:
                # A logging boundary is not a recovered persisted/RAM checkpoint.
                active.clear()
                epoch += 1
                boundaries.append({"line": record["line"], "kind": kind,
                                   "at": record["at"].isoformat(), "epoch": epoch})
            if kind not in {"ADD", "DROP"}:
                continue
            counts[kind] += 1
            symbol = record["symbol"]
            refs.append(record["line"])
            if kind == "ADD":
                if symbol in active:
                    anomalies.append({"line": record["line"], "reason": "duplicate_ADD"})
                active.add(symbol)
            else:
                if symbol not in active:
                    anomalies.append({"line": record["line"], "reason": "orphan_DROP"})
                active.discard(symbol)
            if len(active) > peak:
                peak, peak_symbols = len(active), sorted(active)
            prior = priors.get(symbol)
            old = None if prior is None else prior >= Decimal("1")
            # This is the ADD high, never the required close or a DROP-time quote.
            new = None
            if kind == "ADD" and record["high"] != "UNKNOWN":
                high = Decimal(record["high"])
                if not high.is_finite() or high <= 0:
                    raise ValueError("invalid_aggregate_high")
                if high < Decimal("1"):
                    new = False
            if kind == "ADD" and old is False and new is None:
                potential.add(symbol)
            transitions.append({
                "line": record["line"], "at": record["at"].isoformat(), "epoch": epoch,
                "action": kind, "symbol": symbol, "aggregate_high_at_ADD": record["high"],
                "reason": record["reason"],
                "old_prior_gate_at_ADD": old if kind == "ADD" else None,
                "new_close_gate_at_ADD": new,
                "old_full_admission": None, "new_full_admission": None,
            })
        minutes.append({
            "minute": minute.isoformat(), "transition_lines": refs,
            "recorded_transition_set_peak": peak, "recorded_peak_symbols": peak_symbols,
            "recorded_transition_set_end": sorted(active),
            "recorded_peak_minus_16": peak - 16,
            "old_candidate_count": None, "new_candidate_count": None,
            "newly_entering_symbols": None,
            "potential_gate_expansion_ADD_symbols": sorted(potential),
            "coverage": "LOG_LEDGER_ONLY_NOT_COMPLETE_OWNER_OR_COUNTERFACTUAL",
        })
        minute = end
    return {
        "contract": "PRICE1-LOG-CENSUS-v1", "input": str(path.resolve()),
        "sha256": actual_sha, "bytes": total, "lines": line_number,
        "declared_log_timezone": log_zone,
        "timezone_proof": "CALLER_DECLARED_NOT_NATIVE_CLOCK_ATTESTATION",
        "first_log_at": first.isoformat(), "last_log_at": last.isoformat(),
        "prior_close_source": prior_source,
        "prior_closes": {key: str(value) for key, value in sorted(priors.items())},
        "transition_counts": dict(counts), "boundaries": boundaries,
        "unparsed_line_refs": unparsed, "anomalies": anomalies,
        "recorded_transition_set_peak": max(row["recorded_transition_set_peak"] for row in minutes),
        "old_candidate_max": None, "new_candidate_max": None,
        "old_vs_16": "UNKNOWN", "new_vs_16": "UNKNOWN",
        "acceptance": "UNKNOWN_MISSING_BAR_CLOSE_PRIOR_UNIVERSE_DELIVERY_AND_OWNER_STATE",
        "transitions": transitions, "minutes": minutes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--log-zone", required=True)
    parser.add_argument("--prior-close", action="append", default=[], metavar="SYMBOL=PRICE")
    parser.add_argument("--prior-source")
    args = parser.parse_args()
    priors = {}
    for item in args.prior_close:
        symbol, price = item.split("=", 1)
        if not re.fullmatch(r"[A-Z0-9._-]+", symbol) or symbol in priors:
            raise ValueError("invalid_or_duplicate_prior_symbol")
        priors[symbol] = Decimal(price)
    result = normalize(args.input, expected_sha=args.sha256, log_zone=args.log_zone,
                       prior_closes=priors, prior_source=args.prior_source)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
