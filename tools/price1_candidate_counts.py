"""MAIN-only retained-input warning-owner count study; never a provider/client.

Run this identical external driver separately with each pinned head's PYTHONPATH.
No baseline source mutation or answer-driven tuning. See the source handoff for
the exact JSONL contract and coverage/ownership counterfactual limitations.
"""

import argparse
from collections import Counter
from datetime import UTC, date, datetime, time
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from project_mai_tai.momwarn_validation_window import MomentumValidationWindow
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import AggregateObservation, CoverageEvidence


MINUTE_NS = 60_000_000_000
MAX_LINE_BYTES = 262144
MAX_HEADER_BYTES = 2097152
STUDY_DATES = {date(2026, 10, 21), date(2026, 10, 22)}


def declared_session(header):
    session = date.fromisoformat(header["session_date"])
    if session not in STUDY_DATES:
        raise ValueError("replacement census session must be 2026-10-21 or 2026-10-22")
    return session


def minute_key(observed_ns):
    if type(observed_ns) is not int or observed_ns < 0:
        raise ValueError("minute needs nonnegative integer observation ns")
    # Integer quotient prevents the final ns of a minute rounding into the next.
    return datetime.fromtimestamp((observed_ns // MINUTE_NS) * 60, UTC).strftime("%Y-%m-%dT%H:%MZ")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"nonfinite JSON: {value}")


def read_records(source, *, checksum=None):
    line_number = 0
    while True:
        # The manifest carries all prior closes; individual A/T events do not.
        limit = MAX_HEADER_BYTES if line_number == 0 else MAX_LINE_BYTES
        line = source.readline(limit + 1)
        if not line:
            return
        line_number += 1
        if len(line) > limit:
            raise ValueError(f"oversized normalized event at line {line_number}")
        if checksum is not None:
            checksum.update(line)
        row = json.loads(line, object_pairs_hook=unique_object,
                         parse_constant=reject_constant)
        if not isinstance(row, dict):
            raise ValueError(f"JSON object required at line {line_number}")
        yield line_number, row


def study_window(header):
    session = declared_session(header)
    start, end = header["study_start_ns"], header["study_end_ns"]
    if (type(start) is not int or type(end) is not int or start < 0 or not start < end
            or start % MINUTE_NS or end % MINUTE_NS or end - start > 1440 * MINUTE_NS):
        raise ValueError("study bounds must be ordered whole-minute integer ns within one day")
    et = ZoneInfo("America/New_York")
    for point in (start, end - 1):
        if datetime.fromtimestamp(point // 1000000000, UTC).astimezone(et).date() != session:
            raise ValueError("study bounds must cover the explicitly declared ET session day only")
    settings = header["validation_window"]
    window = MomentumValidationWindow(
        session_date=date.fromisoformat(settings["session_date"]) if settings["session_date"] else None,
        gateway_end=time.fromisoformat(settings["gateway_end"]),
        detection_end=time.fromisoformat(settings["detection_end"]),
        guard_end=time.fromisoformat(settings["guard_end"]))
    if window.session_date not in (None, session):
        raise ValueError("wrong retained validation session")
    return start, end, window


def clock_contract(header):
    basis = header["clock_basis"]
    if not isinstance(basis, dict):
        raise ValueError("clock basis must be an explicit provenance object")
    kind = basis.get("kind")
    if kind == "retained_consumer_delivery":
        required = ("receipt_provenance", "observation_provenance")
        if header["initial_state"] != "empty_before_session_prepare":
            raise ValueError("retained delivery requires the actual empty session prefix, not a cold-start guess")
        scope = "retained-delivery counterfactual study; native clocks DECLARED, not verified; not live qualification"
    elif kind == "declared_counterfactual":
        required = ("contract_id", "assignment_rule", "delivery_scope")
        if header["initial_state"] not in {"empty_before_session_prepare", "declared_cold_start"}:
            raise ValueError("counterfactual initial state must be declared explicitly")
        scope = "DECLARED historical/counterfactual clock study; NOT native/local-arrival/as-known evidence"
    else:
        raise ValueError("unknown clock basis; never infer receipt clocks from SIP/event time")
    if any(not isinstance(basis.get(key), str) or not basis[key].strip() for key in required):
        raise ValueError("clock contract lacks required provenance/assignment fields")
    return basis, scope


def sample_minute(minutes, *, observed, candidates, desired, active, outcome=None):
    entry = minutes.setdefault(minute_key(observed), {
        "candidate_max": 0, "desired_owner_max": 0, "active_path_max": 0,
        "last_candidates": [], "samples": 0, "cap_refusals": 0, "outcomes": {}})
    entry["candidate_max"] = max(entry["candidate_max"], len(candidates))
    entry["desired_owner_max"] = max(entry["desired_owner_max"], len(desired))
    entry["active_path_max"] = max(entry["active_path_max"], len(active))
    entry["last_candidates"] = sorted(candidates)
    entry["samples"] += 1
    if outcome is not None:
        key = outcome.status + ":" + outcome.reason
        entry["outcomes"][key] = entry["outcomes"].get(key, 0) + 1
        entry["cap_refusals"] += int(outcome.reason in {"warning_cap_full", "warning_owner_cap_full"})


def include_missing_minutes(minutes, start, end):
    # Missing delivery/heartbeat samples are UNKNOWN, not manufactured zeroes.
    return {minute_key(point): minutes.get(minute_key(point)) for point in range(start, end, MINUTE_NS)}


def coverage(row):
    return None if row is None else CoverageEvidence(
        tuple(row["epoch"]), row["start_ms"], row["through_ms"], row["dropped_total"],
        row["continuous"], row["lineage"])


def source_identity(expected_source_root):
    bindings = {}
    root = Path(expected_source_root).resolve() if expected_source_root is not None else None
    for name in ("engine", "warning_consumer"):
        module_path = Path(sys.modules[f"project_mai_tai.momentum_paper.{name}"].__file__).resolve()
        if root is not None and module_path != root / "src/project_mai_tai/momentum_paper" / (name + ".py"):
            raise ValueError(f"imported source root mismatch: {name}")
        bindings[name] = {"path": str(module_path), "sha256": sha256(module_path.read_bytes()).hexdigest()}
    return bindings


def summarize(path, *, declared_head, expected_input_sha256, expected_source_root=None):
    if not re.fullmatch(r"[0-9a-f]{40}", declared_head):
        raise ValueError("declared head must be a full lowercase Git SHA, independently pinned by main")
    bindings = source_identity(expected_source_root)
    checksum = sha256()
    with path.open("rb") as raw:
        for chunk in iter(lambda: raw.read(1048576), b""):
            checksum.update(chunk)
    digest = checksum.hexdigest()
    if digest != expected_input_sha256:
        raise ValueError("input SHA differs from predeclared packet identity")
    minutes, outcomes, detections = {}, Counter(), Counter()
    previous = -1
    read_checksum = sha256()
    with path.open("rb") as source:
        records = read_records(source, checksum=read_checksum)
        _, header = next(records)
        if header["contract"] != "PRICE1-CANDIDATES-v4":
            raise ValueError("wrong input contract; corrected sessions require v4")
        session = declared_session(header)
        clock_basis, scope = clock_contract(header)
        if header["owner_cap"] != 16:
            raise ValueError("study must preserve the production 16-owner cap")
        if type(header["complete_for_both_owner_sets"]) is not bool:
            raise ValueError("completeness must be an explicit boolean, not a truthy string")
        if type(header["max_window_symbols"]) is not int or header["max_window_symbols"] < 16:
            raise ValueError("retained window capacity must be an integer at least 16")
        start, end, window = study_window(header)
        model = MomentumWarningConsumer(prior_closes={k: Decimal(v) for k, v in header["prior_closes"].items()},
            condition_version=header["condition_version"], coverage_started_ms=header["coverage_started_ms"],
            owner_cap=header["owner_cap"], max_window_symbols=header["max_window_symbols"],
            validation_window=window)
        for line_number, row in records:
            observed = row["observed_ns"]
            if type(observed) is not int or observed < previous:
                raise ValueError(f"nonmonotonic receive order at line {line_number}")
            previous = observed
            active = set(row["legacy_active"])
            kind = row["kind"]
            emitted = ()
            outcome = None
            if kind == "aggregate":
                outcome, emitted = model.observe_aggregate(AggregateObservation.from_payload(row["payload"]),
                    observed_ns=observed, legacy_active=active)
            elif kind == "trade":
                trade = dict(row["trade"])
                trade["price"] = Decimal(trade["price"])
                trade["conditions"] = tuple(trade["conditions"])
                emitted = model.observe_trade(TradePrint(**trade), sip_timestamp_ns=row["sip_timestamp_ns"],
                    observed_ns=observed, raw_coverage=coverage(row["raw_coverage"]))
            elif kind == "complete_read":
                model.complete_read(row["message_id"], observed_ns=observed)
            elif kind == "invalidate_transport":
                model.invalidate_transport(row["reason"])
            elif kind == "heartbeat":
                emitted = model.engine.advance_clock(observed // 1000000)
                model.expire_quiet(observed_ms=observed // 1000000, legacy_active=active)
            else:
                raise ValueError(f"unsupported retained event at line {line_number}: {kind}")
            if not start <= observed < end:
                continue  # Consume the real prefix, but do not count it as study-window samples.
            if outcome is not None:
                outcomes[outcome.status + ":" + outcome.reason] += 1
            for record in emitted:
                if record.event_type == "DETECTED":
                    detections[record.symbol] += 1
            sample_minute(minutes, observed=observed, candidates=model.candidates._candidates,
                desired=model.desired_symbols(active), active=active | model.engine.active_symbols, outcome=outcome)
    if read_checksum.hexdigest() != expected_input_sha256:
        raise ValueError("replayed input bytes changed after the preflight SHA check")
    grid = include_missing_minutes(minutes, start, end)
    complete = header["complete_for_both_owner_sets"] is True
    coverage_status = "UNKNOWN_PARTIAL"
    if complete:
        coverage_status = ("DECLARED_COMPLETE" if clock_basis["kind"] == "retained_consumer_delivery"
                           else "DECLARED_COUNTERFACTUAL_INPUT_COMPLETE")
    return {"contract": "PRICE1-CENSUS-RESULT-v4", "session_date": session.isoformat(),
        "declared_head": declared_head, "input_sha256": digest,
        "source_bindings": bindings, "source_root_pinned": expected_source_root is not None,
        "study_start_ns": start, "study_end_ns": end, "validation_window": header["validation_window"],
        "scope": scope, "clock_basis": clock_basis, "initial_state": header["initial_state"],
        "acceptance": "UNKNOWN_UNTIL_MAIN_VERIFIES_RETAINED_INPUT_AND_ORIGINAL_CASES",
        "detections_scope": "observed retained events only; missing delivery is not zero detections",
        "coverage": coverage_status,
        "provenance": header["provenance"], "cap": 16, "minutes": grid,
        "unknown_minutes": [minute for minute, item in grid.items() if item is None],
        "maximum": max((item["candidate_max"] for item in minutes.values()), default=None),
        "desired_owner_maximum": max((item["desired_owner_max"] for item in minutes.values()), default=None),
        "cap_refusals": sum(item["cap_refusals"] for item in minutes.values()),
        "outcomes": dict(outcomes), "detections": dict(detections),
        "required_names": {"POM": detections["POM"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--declared-head", required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--expected-source-root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.input, declared_head=args.declared_head,
        expected_input_sha256=args.expected_input_sha256, expected_source_root=args.expected_source_root), sort_keys=True))


if __name__ == "__main__":
    main()
