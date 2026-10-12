"""Summarize retained published outputs, NOT regenerate the aggregate method."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import statistics


def summarize(signals: str, leads: list) -> dict:
    rows = list(csv.reader(io.StringIO(signals)))
    if not rows or not isinstance(leads, list) or len(rows) != len(leads):
        raise ValueError("retained population mismatch")
    values = {}
    for signal in rows:
        if len(signal) != 4:
            raise ValueError("invalid retained row")
        if signal[1] not in {"momentum_30s", "momentum_60s"}:
            raise ValueError("unknown published ledger")
    for lead in leads:
        if not isinstance(lead, list) or len(lead) != 4 or lead[0] not in {
            "momentum_30s", "momentum_60s",
        }:
            raise ValueError("invalid retained lead row")
        value = lead[3]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("invalid published lead")
        values.setdefault(lead[0], []).append(value)
    signal_keys = [(row[1], row[0], row[2]) for row in rows]
    lead_keys = [tuple(row[:3]) for row in leads]
    if Counter(signal_keys) != Counter(lead_keys):
        raise ValueError("source-key population mismatch")
    ledgers = {}
    for code, samples in sorted(values.items()):
        positive = [value for value in samples if value > 0]
        ledgers[code] = dict(population=len(samples), positive=len(positive),
                             nonpositive=len(samples)-len(positive),
                             positive_only_median_s=statistics.median(positive) if positive else None,
                             all_population_median_s=statistics.median(samples))
    duplicate_keys = sum(count-1 for count in Counter(tuple(row[:3]) for row in leads).values())
    return dict(evidence_kind="RETAINED_PUBLISHED_OUTPUT_SUMMARY_NOT_GENERATION_REPLAY",
                population=len(rows), ordered_source_keys_match=signal_keys == lead_keys,
                source_key_multiplicities_match=True,
                ordered_source_key_mismatches=sum(a != b for a, b in zip(signal_keys, lead_keys, strict=True)),
                repeated_strategy_day_symbol_rows=duplicate_keys, ledgers=ledgers,
                original_generation_method="UNKNOWN_NOT_RECOVERED",
                aggregate_start_vs_end_availability="UNKNOWN_NOT_CORRECTED_WITHOUT_METHOD",
                actual_network_warning_or_ACK="UNMEASURED",
                note="Nonpositive is the published no-warning group, not independently proven live absence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("signals", type=Path)
    parser.add_argument("leads", type=Path)
    parser.add_argument("--tested-head")
    args = parser.parse_args()
    signal_bytes, lead_bytes = args.signals.read_bytes(), args.leads.read_bytes()
    result = summarize(signal_bytes.decode(), json.loads(lead_bytes))
    result["input_sha256"] = dict(signals=hashlib.sha256(signal_bytes).hexdigest(),
                                  leads=hashlib.sha256(lead_bytes).hexdigest())
    result["tested_head"] = args.tested_head or "UNKNOWN"
    print(json.dumps(result, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
