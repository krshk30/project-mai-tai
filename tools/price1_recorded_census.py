"""Offline delivered-log census receipt; no application imports or model replay."""

from __future__ import annotations

import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from tools.price1_log_census import normalize


INPUTS = {
    "2026-10-21": ("momentum-paper_2026-10-21.log",
                   "152f46d2477c778ab88a845e5427bd32e5856621cc689968230072a7f886ff1a"),
    "2026-10-22": ("momentum-paper_2026-10-22_to_1552.log",
                   "a241c662279b05b1b0de87f35bab35e104650c91977dd8611873017d774a9eda"),
}
UNKNOWN = "UNKNOWN_MISSING_BAR_CLOSE_FULL_PRIORS_OWNER_STATE_AND_COMPETING_STREAM"


def check_manifest(packet: Path) -> dict:
    with (packet / "SHA256SUMS").open("rb") as handle:
        raw = handle.read(4097)
    if len(raw) > 4096:
        raise ValueError("manifest_bound_exceeded")
    entries = {}
    for line in raw.decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[1] in entries:
            raise ValueError("malformed_or_duplicate_manifest_entry")
        entries[parts[1]] = parts[0]
    if entries != dict(INPUTS.values()):
        raise ValueError("delivered_manifest_differs_from_pinned_inputs")
    return {"path": str((packet / "SHA256SUMS").resolve()), "sha256": sha256(raw).hexdigest()}


def day_receipt(result: dict) -> dict:
    minutes = []
    for row in result["minutes"]:
        minutes.append({
            "minute": row["minute"], "transition_line_refs": row["transition_lines"],
            "observed_log_candidate_peak": row["recorded_transition_set_peak"],
            "observed_log_peak_symbols": row["recorded_peak_symbols"],
            "observed_log_end_symbols": row["recorded_transition_set_end"],
            "observed_log_peak_minus_16": row["recorded_peak_minus_16"],
            "old_prior_gate_candidate_count": None, "new_close_gate_candidate_count": None,
            "newly_eligible_symbols": None, "newly_eligible_POM": "UNKNOWN_BAR_CLOSE_MISSING",
            "potential_expansion_ADD_symbols": row["potential_gate_expansion_ADD_symbols"],
            "count_scope": "RECORDED_TRANSITION_LEDGER_NOT_OWNER_UNION_OR_EITHER_MODELED_LEG",
            "old_new_count_status": UNKNOWN,
        })
    pom = [row for row in result["transitions"] if row["action"] == "ADD" and row["symbol"] == "POM"]
    return {
        "input": result["input"], "input_sha256": result["sha256"],
        "bytes": result["bytes"], "lines": result["lines"],
        "first_log_at": result["first_log_at"], "last_log_at": result["last_log_at"],
        "transition_counts": result["transition_counts"], "boundaries": result["boundaries"],
        "unparsed_line_refs": result["unparsed_line_refs"], "anomalies": result["anomalies"],
        "observed_log_candidate_max": result["recorded_transition_set_peak"],
        "observed_log_max_minus_16": result["recorded_transition_set_peak"] - 16,
        "old_prior_gate_candidate_max": None, "new_close_gate_candidate_max": None,
        "old_prior_gate_max_vs_16": "UNKNOWN", "new_close_gate_max_vs_16": "UNKNOWN",
        "newly_eligible_symbols": None, "newly_eligible_POM": "UNKNOWN_BAR_CLOSE_MISSING",
        "observed_POM_ADD_count": len(pom), "observed_POM_ADD_records": pom,
        "prior_close_source": result["prior_close_source"], "declared_prior_closes": result["prior_closes"],
        "old_new_count_status": UNKNOWN, "per_minute": minutes,
    }


def collect(packet: Path, *, log_zone: str, timezone_source: str,
            pom_prior_close: Decimal, prior_source: str) -> dict:
    if not timezone_source.strip() or not prior_source.strip():
        raise ValueError("explicit_timezone_and_prior_provenance_required")
    manifest = check_manifest(packet)
    days = {}
    for day, (filename, digest) in INPUTS.items():
        result = normalize(packet / filename, expected_sha=digest, log_zone=log_zone,
                           prior_closes={"POM": pom_prior_close}, prior_source=prior_source)
        if result["first_log_at"][:10] != day or result["last_log_at"][:10] != day:
            raise ValueError("pinned_log_day_mismatch")
        days[day] = day_receipt(result)
    here = Path(__file__).resolve()
    return {
        "contract": "PRICE1-DELIVERED-LOG-CENSUS-v1", "manifest": manifest,
        "source_sha256": {path.name: sha256(path.read_bytes()).hexdigest()
                          for path in (here, here.with_name("price1_log_census.py"))},
        "declared_log_timezone": log_zone, "timezone_source": timezone_source,
        "clock_scope": "LOG_TIMESTAMP_DECLARATION_NOT_NATIVE_A_T_DELIVERY_OR_AS_KNOWN_PREFIX",
        "withdrawn_session": "2026-10-12", "days": days,
        "acceptance": "LOG_INVENTORY_ONLY_COUNTERFACTUAL_AND_OWNER_CAP_UNKNOWN",
        "model_executed": False, "provider_access": False,
        "coverage": "DELIVERED_TEXT_ONLY_NOT_RECOVERED_1S_BARS_OR_T_OR_COMPLETE_OWNER_STATE",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--log-zone", required=True)
    parser.add_argument("--timezone-source", required=True)
    parser.add_argument("--pom-prior-close", type=Decimal, required=True)
    parser.add_argument("--prior-source", required=True)
    args = parser.parse_args()
    print(json.dumps(collect(args.input_dir, log_zone=args.log_zone,
        timezone_source=args.timezone_source, pom_prior_close=args.pom_prior_close,
        prior_source=args.prior_source), sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
