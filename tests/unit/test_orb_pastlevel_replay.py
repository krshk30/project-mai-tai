"""Offline real-input price/shape comparator; main-only, never a native fill replay.

See analysis/ORBPASTLEVEL1-REPLAY-V2-INPUT-REQUEST-20261021.md. This deliberately does not
invent a broker simulator or reconstruct absent OMS/lifecycle state.
"""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from project_mai_tai.strategy_core.orb_pastlevel import POLICY, select_entry


SOURCE_PATHS = (
    "src/project_mai_tai/broker_adapters/protocols.py",
    "src/project_mai_tai/broker_adapters/schwab.py",
    "src/project_mai_tai/oms/service.py",
    "src/project_mai_tai/oms/orb_pastlevel.py",
    "src/project_mai_tai/orb_country_route.py",
    "src/project_mai_tai/orb_schwab_order_route.py",
    "src/project_mai_tai/strategy_core/orb_schwab_bracket.py",
    "src/project_mai_tai/strategy_core/orb_pastlevel.py",
    "tests/unit/test_orb_pastlevel_replay.py",
)
EXPECTED_SYMBOLS = {"MI", "JAGX", "FLYE", "GIXI", "NXL", "XHG", "BIYA", "AIXI",
                    "OLOX", "SXTC", "AMOD"}


def stamp(value):
    # datetime silently truncates fractional digits beyond microseconds.
    # Keep raw Massive nanoseconds in the packet; do not silently round a boundary.
    fraction = value.partition(".")[2]
    digits = ""
    for char in fraction:
        if not char.isdigit():
            break
        digits += char
    if len(digits) > 6 and any(char != "0" for char in digits[6:]):
        raise ValueError("timestamp_submicrosecond_requires_reviewed_clock_adapter")
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("timezone_missing")
    return at


def rows(packet, manifest, name):
    spec = manifest["files"][name]
    path = (packet / spec["path"]).resolve()
    if not path.is_relative_to(packet.resolve()):
        raise ValueError("packet_path_escape")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
        raise ValueError(f"hash_mismatch:{name}")
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def conditional_touches(decision, selected, trades, time_basis="strict-arrival"):
    """Descriptive tape touches conditional on an assumed decision-time entry."""
    if not decision.get("capture_end") or not decision.get("capture_start"):
        return {"verdict": "UNKNOWN_capture_window_missing"}
    start, end = stamp(decision["capture_start"]), stamp(decision["capture_end"])
    entered = stamp(decision["decision_at"])
    if not start <= entered <= end or decision.get("capture_complete") != "true":
        return {"verdict": "UNKNOWN_capture_coverage"}
    touches, uncertain = [], []
    for trade in trades:
        if trade["plan_id"] != decision["plan_id"]:
            continue
        if trade["symbol"] != decision["symbol"]:
            uncertain.append(trade["trade_id"])
            continue
        at = stamp(trade["event_at"])
        if not entered <= at <= end:
            continue
        if trade.get("correction_of") or trade.get("cancel_of"):
            uncertain.append(trade["trade_id"])
            continue
        if time_basis == "strict-arrival":
            if not trade.get("received_at"):
                uncertain.append(trade["trade_id"])
                continue
            available = stamp(trade["received_at"])
            if available < at:
                uncertain.append(trade["trade_id"])
                continue
            if available > end:
                continue
            at = max(entered, available)
        if trade["eligible"] not in {"true", "false"}:
            uncertain.append(trade["trade_id"])
            continue
        if trade["eligible"] == "false":
            continue
        price = Decimal(trade["price"])
        if not price.is_finite() or price <= 0:
            uncertain.append(trade["trade_id"])
            continue
        kind = ("target" if price >= Decimal(selected["bracket_target_price"]) else
                "protective" if price <= Decimal(selected["bracket_stop_price"]) else None)
        if kind:
            touches.append((at, kind, trade["trade_id"]))
    touches.sort()
    first = [item for item in touches if item[0] == touches[0][0]] if touches else []
    return {"verdict": "UNKNOWN_trade_provenance_or_eligibility" if uncertain else "DESCRIPTIVE_ONLY",
            "time_basis": time_basis,
            "assumption": "decision_time_entry_NOT_proven_even_for_marketable_LIMIT",
            "first_touch": [(at.isoformat(), kind, identity) for at, kind, identity in first],
            "same_time_target_stop_ambiguous": len({item[1] for item in first}) > 1,
            "uncertain_trade_ids": uncertain, "native_fill": "NOT_INFERRED"}


def compare(packet: Path, time_basis: str):
    manifest_bytes = (packet / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["schema"] != "ORBPASTLEVEL1-input-v2":
        raise ValueError("schema_mismatch")
    if manifest.get("quote_binding") not in {"actual-consumed", "decision-second-counterfactual"}:
        raise ValueError("quote_binding_missing")
    if not manifest.get("exporter_identity") or not manifest.get("census_receipt"):
        raise ValueError("export_provenance_missing")
    if not isinstance(manifest.get("missing_inputs"), list):
        raise ValueError("missing_input_inventory_required")
    if (type(manifest.get("quote_max_age_ms")) is not int
            or manifest["quote_max_age_ms"] <= 0):
        raise ValueError("quote_age_setting_invalid")
    if time_basis not in manifest["declared_time_bases"]:
        raise ValueError("undeclared_time_basis")
    plans = manifest["plans"]
    plan_ids = [p["plan_id"] for p in plans]
    if len(set(plan_ids)) != len(plan_ids):
        raise ValueError("duplicate_plan_identity")
    by_plan = {p["plan_id"]: p for p in plans}
    for plan in plans:
        if (not plan["plan_id"] or not plan["decision_ids"]
                or plan["session_date"] < "2026-10-01"):
            raise ValueError("original_plan_identity_or_decisions_missing")
    decisions = rows(packet, manifest, "decisions")
    quotes = rows(packet, manifest, "quotes")
    broker_events = rows(packet, manifest, "broker_events")
    trades = rows(packet, manifest, "trades")
    quote_ids = [q["quote_id"] for q in quotes]
    if len(set(quote_ids)) != len(quote_ids):
        raise ValueError("duplicate_quote_identity")
    by_quote = {q["quote_id"]: q for q in quotes}
    for name, records in (("quote", quotes), ("broker_event", broker_events), ("trade", trades)):
        for record in records:
            if record["plan_id"] not in by_plan:
                raise ValueError(f"unbound_{name}_plan")
    trade_ids = [row["trade_id"] for row in trades]
    if len(set(trade_ids)) != len(trade_ids):
        raise ValueError("duplicate_trade_identity")
    results, reasons = [], Counter()
    seen = set()
    source_hashes = {}
    root = Path(__file__).resolve().parents[2]
    if not set(SOURCE_PATHS).issubset(manifest["candidate_source_sha256"]):
        raise ValueError("candidate_source_inventory_missing")
    declared = {key: p["plan_id"] for p in plans for key in p["decision_ids"]}
    if len(declared) != sum(len(p["decision_ids"]) for p in plans):
        raise ValueError("duplicate_declared_decision")
    for name, expected in manifest["candidate_source_sha256"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("source_path_escape")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"source_mismatch:{name}")
        source_hashes[name] = actual
    for decision in decisions:
        key = decision["decision_id"]
        if key in seen or declared.get(key) != decision["plan_id"]:
            raise ValueError("duplicate_or_unbound_decision")
        seen.add(key)
        plan = by_plan[decision["plan_id"]]
        if (decision["symbol"] != plan["symbol"]
                or decision["session_date"] != plan["session_date"]):
            raise ValueError("decision_plan_identity_mismatch")
        result = {"plan_id": decision["plan_id"], "decision_id": key,
                  "native_execution": "NOT_INFERRED", "lifecycle_authority": "NOT_REPLAYED",
                  "gate_replay": "NOT_REPLAYED", "observed_order_type": decision["observed_order_type"],
                  "observed_limit_price": decision["observed_limit_price"]}
        try:
            now = stamp(decision["decision_at"])
            if now.astimezone(ZoneInfo("America/New_York")).date().isoformat() != plan["session_date"]:
                raise ValueError("decision_session_date_mismatch")
            quote = by_quote[decision["quote_id"]]
            if quote["plan_id"] != decision["plan_id"] or quote["symbol"] != decision["symbol"]:
                raise ValueError("quote_plan_identity_mismatch")
            if (quote.get("provider") != "Massive" or not quote.get("source_event_id")
                    or not quote.get("source_timestamp") or not quote.get("raw_record_reference")):
                raise ValueError("Massive_quote_provenance_UNKNOWN")
            available = quote["available_at"] if time_basis == "strict-arrival" else quote["producer_at"]
            if not available:
                raise ValueError("quote_availability_UNKNOWN")
            if stamp(available) > now:
                raise ValueError("quote_not_yet_available")
            if time_basis == "strict-arrival" and stamp(available) < stamp(quote["producer_at"]):
                raise ValueError("quote_arrival_before_event_UNKNOWN")
            md = select_entry(Decimal(decision["level"]),
                {"ask": quote["ask"], "received_at": stamp(quote["producer_at"])}, now,
                max_age_ms=manifest["quote_max_age_ms"])
            result.update(selection=md, outcome="price_shape_selected_not_permission_to_send",
                          order_type_changed=md["order_type"] != decision["observed_order_type"],
                          limit_changed=Decimal(md["limit_price"]) != Decimal(decision["observed_limit_price"])
                          if decision["observed_limit_price"] else "UNKNOWN",
                          conditional_tape_study=conditional_touches(decision, md, trades, time_basis))
        except (ValueError, KeyError, ArithmeticError) as exc:
            result.update(outcome="NO_SEND_OR_UNKNOWN", reason=str(exc))
            reasons[str(exc)] += 1
        results.append(result)
    missing = {p["plan_id"]: sorted(set(p["decision_ids"]) - seen) for p in plans}
    missing = {key: value for key, value in missing.items() if value}
    undeclared = sorted(seen - {key for p in plans for key in p["decision_ids"]})
    complete = (manifest.get("complete_original11_census") is True and len(plans) == 11
                and {p["symbol"] for p in plans} == EXPECTED_SYMBOLS
                and not missing and not undeclared and not manifest["missing_inputs"])
    unknown = [r["decision_id"] for r in results if r["outcome"] == "NO_SEND_OR_UNKNOWN"]
    return {"policy": POLICY, "packet_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "quote_binding": manifest["quote_binding"],
            "acceptance": "NOT_ESTABLISHED_PRICE_STUDY_ONLY",
            "missing_inputs": manifest["missing_inputs"], "unknown_or_no_send_decisions": unknown,
            "time_basis": time_basis, "candidate_source_sha256": source_hashes,
            "original11_coverage": "DECLARED_COMPLETE_NOT_NATIVE_ACCEPTANCE" if complete else "UNKNOWN",
            "missing_decisions": missing, "undeclared_decisions": undeclared,
            "plan_count": len(plans), "decision_count": len(results), "reasons": dict(reasons),
            "broker_event_rows": len(broker_events), "trade_rows": len(trades),
            "native_fills_target_stop_outcomes": "NOT_REPLAYED",
            "actual_broker_events_baseline_unmodified": broker_events,
            "actual_quote_rows_unmodified": quotes,
            "actual_decision_rows_unmodified": decisions,
            "OMS_gates_lifecycle_and_wire_awaits": "REQUIRE_SEPARATE_CONTROLLED_REPLAY",
            "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("packet", type=Path)
    parser.add_argument("--time-basis", choices=("strict-arrival", "declared-event-time-study"),
                        default="strict-arrival")
    args = parser.parse_args()
    print(json.dumps(compare(args.packet, args.time_basis), indent=2, sort_keys=True))
