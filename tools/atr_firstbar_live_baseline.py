"""Main-only file replay of actual aggregate entries under the SAME exit policy.

Execution requires explicit C1-approved aggregate assumption. No app, provider,
DB, reference strategy, FIRSTBAR entry simulation or existing-model rerun.
"""

import argparse
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys


FAC9_SHA = "fac9c0fc0efd00f15a00776f869eafb28b4010f47c57dfc86e0a105357c93e2d"
BUY_SHA = "a7c719d0a0a7015ec8062ad18483de71dacd2ca1abfe2eebee440a43d125b750"
ASSUMPTION = "COLLAPSE_RECORDED_ORDER_TO_ONE_ENTRY_AT_RECORDED_FILLED_NS"
REVIEW_CASE_KEYS = (
    ("TCRT", "2026-10-13", "08:48:54"), ("FLYE", "2026-10-13", "11:44:07"),
    ("IPDN", "2026-10-06", "12:14:47"), ("VEEA", "2026-10-09", "14:16:31"),
    ("GLND", "2026-10-14", "07:04:09"), ("GLND", "2026-10-14", "09:50:01"),
    ("LNKS", "2026-10-12", "07:02:55"), ("PFAI", "2026-10-13", "09:59:45"),
    ("GLND", "2026-10-14", "07:07:56"),
)
REQUIRED_PINS = {
    "tools/atr_firstbar_live_baseline.py",
    "src/project_mai_tai/__init__.py",
    "src/project_mai_tai/atr_simulation/__init__.py",
    "src/project_mai_tai/backtest/__init__.py",
    "src/project_mai_tai/backtest/atr_oracle.py",
    "src/project_mai_tai/momentum_paper/conditions.py",
    "src/project_mai_tai/broker_adapters/target_price.py",
    *(f"src/project_mai_tai/atr_simulation/{name}.py" for name in (
        "firstbar_live_baseline", "firstbar", "firstbar_exits", "firstbar_target_rule",
        "firstbar_study", "firstbar_replay", "firstbar_reference", "types", "exits",
        "execution_eligibility")),
}


def json_data(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_input_key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("finite_input_required")
    return json.loads(raw, parse_float=Decimal, object_pairs_hook=pairs, parse_constant=nonfinite)


def pinned(path, expected):
    raw = Path(path).read_bytes()
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("exact_input_bytes_sha_required")
    return raw


def verify_source_manifest(manifest, root):
    if (set(manifest["source_pins"]) != REQUIRED_PINS
            or not re.fullmatch(r"[0-9a-f]{40}", manifest["reviewed_head"])):
        raise ValueError("explicit_exact_baseline_source_generation_required")
    result = {}
    for name, sha in manifest["source_pins"].items():
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("contained_baseline_source_required")
        pinned(path, sha)
        result[str(path)] = sha
    return result


def plain(value):
    return json.loads(json.dumps(value, default=str))


def verify_loaded_sources(root):
    """PYTHONPATH must not substitute another checkout after disk-pin review."""
    for name in REQUIRED_PINS:
        if not name.startswith("src/") or name.endswith("broker_adapters/target_price.py"):
            continue  # Standalone target leaf is verified by its sealed loader.
        if name.endswith("momentum_paper/conditions.py"):
            module_name = "_atr_sim_exact_momentum_condition_classifier"
        else:
            module_name = name[4:-3].replace("/", ".").removesuffix(".__init__")
        module = sys.modules.get(module_name)
        if module is None or Path(module.__file__).resolve() != (root / name).resolve():
            raise ValueError("actual_loaded_baseline_module_outside_reviewed_source_generation")


def bind_actual_legs(seed_keys, receipt, parsed_legs):
    """Literal fac9 original-leg joins; never its simulated fills or entry plans."""
    if [tuple(case["reference_key"]) for case in receipt["cases"]] != list(seed_keys):
        raise ValueError("exact_ordered_seed_receipt_join_required")
    by_order = {(leg.account, leg.order_id): leg for leg in parsed_legs}
    if len(by_order) != len(parsed_legs):
        raise ValueError("duplicate_original_order_source")
    selected, seen = {}, set()
    for case in receipt["cases"]:
        key = tuple(case["reference_key"])
        legs = []
        for original in case["leg_bindings"]:
            identity = original["account"], original["order_id"]
            if identity in seen or identity not in by_order:
                raise ValueError("unique_bound_original_account_order_required")
            leg = by_order[identity]
            if plain(asdict(leg)) != original:
                raise ValueError("original_aggregate_leg_fields_or_source_mismatch")
            if leg.symbol != key[0]:
                raise ValueError("original_leg_symbol_mismatch")
            seen.add(identity)
            legs.append(leg)
        if not legs:
            raise ValueError("original_case_all_account_legs_required")
        selected[key] = tuple(legs)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reference-root", "fac9-receipt", "policy-file", "policy-sha256",
                 "source-manifest", "source-manifest-sha256", "account-brokers", "strategy-code", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--aggregate-entry-assumption", choices=(ASSUMPTION,), required=True)
    parser.add_argument("--authority-artifact", required=True)
    parser.add_argument("--authority-sha256", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    manifest = json_data(pinned(args.source_manifest, args.source_manifest_sha256))
    source_pins = verify_source_manifest(manifest, root)
    authority = pinned(args.authority_artifact, args.authority_sha256)
    if not authority.strip():
        raise ValueError("explicit_reviewed_C1_aggregate_authority_required")
    output = Path(args.output).resolve()
    if not output.is_relative_to(Path("/private/tmp")) or output.exists():
        raise ValueError("fresh_private_output_required")

    # Load pure project modules only AFTER the independent source generation is
    # verified. No application imports or normal broker package imports.
    from project_mai_tai.atr_simulation.execution_eligibility import policy_from_payload
    from project_mai_tai.atr_simulation.firstbar_live_baseline import (
        AggregateBaselineInput, BASELINE_LABEL, BASELINE_VARIANT, BASELINE_VERSION,
        BASELINE_POLICY_VERSION, COHORT_POLICY, ENTRY_MODEL, HISTORY_LABEL, TIE_POLICY, independent_leg_totals,
        prepare_baseline_day, simulate_aggregate_cohort,
    )
    from project_mai_tai.atr_simulation.firstbar_reference import bind_reference_seed, reviewed_173_manifest
    from project_mai_tai.atr_simulation.firstbar_study import StudyTapePacket, parse_study_buy_legs
    from project_mai_tai.atr_simulation.firstbar_target_rule import EXIT_POLICY_VERSION
    from project_mai_tai.atr_simulation.types import Coverage, EvidenceKind, FlipIdentity, SourceRef

    def source(path, raw, pointer="$"):
        return SourceRef(str(Path(path).resolve()), hashlib.sha256(raw).hexdigest(), pointer, EvidenceKind.RETAINED)

    study = Path(args.reference_root).resolve(strict=True)
    seed_manifest = reviewed_173_manifest(str(study))
    pinned(study / "four/agg4.py", seed_manifest.selector_source.sha256)
    seed = bind_reference_seed(pinned(study / "four_columns.csv", seed_manifest.csv_source.sha256),
        pinned(study / "four/raw_split.json", seed_manifest.split_source.sha256), manifest=seed_manifest)
    receipt = json_data(pinned(args.fac9_receipt, FAC9_SHA))
    if receipt["exit_policy_version"] != EXIT_POLICY_VERSION:
        raise ValueError("same_corrected_exit_policy_required")
    buy_path = study / "fbh/buys.psv"
    buy_raw = pinned(buy_path, BUY_SHA)
    buy_source = source(buy_path, buy_raw)
    selected = bind_actual_legs(tuple(case.reference_key for case in seed.cases), receipt,
                               parse_study_buy_legs(buy_raw, buy_source))
    policy_raw = pinned(args.policy_file, args.policy_sha256)
    if args.policy_sha256 != receipt["execution_policy_source"]["sha256"]:
        raise ValueError("same_approved_condition_snapshot_required")
    policy = policy_from_payload(json_data(policy_raw), source(args.policy_file, policy_raw))
    verify_loaded_sources(root)
    brokers = json_data(args.account_brokers)
    actual_accounts = {leg.account for legs in selected.values() for leg in legs}
    if (not isinstance(brokers, dict) or set(brokers) != actual_accounts
            or any(value not in ("schwab", "webull") for value in brokers.values()) or not args.strategy_code):
        raise ValueError("exact_explicit_account_broker_strategy_binding_required")
    for case in receipt["cases"]:
        for result in case["results"]:
            identity = result["identity"]
            if brokers[identity["account_id"]] != identity["broker"] or identity["strategy"] != args.strategy_code:
                raise ValueError("same_fac9_account_route_strategy_required")
    tape_index = {}
    for row in receipt["tape_inputs"]:
        key = row["symbol"], row["day"]
        if key in tape_index:
            raise ValueError("unique_fac9_tape_index_required")
        tape_index[key] = row
    expected_days = {key[:2] for key in selected}
    if set(tape_index) != expected_days:
        raise ValueError("exact_selected_day_tape_denominator_required")
    results, day_evidence = {}, []
    for symbol, day in sorted(expected_days):
        if re.fullmatch(r"[A-Z][A-Z0-9.]{0,15}", symbol) is None or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
            raise ValueError("safe_selected_day_required")
        row = tape_index[symbol, day]
        path = (study / "fbh/mtape" / f"{symbol}_{day}.t").resolve(strict=True)
        if not path.is_relative_to(study / "fbh/mtape"):
            raise ValueError("contained_selected_tape_required")
        raw = pinned(path, row["sha256"])
        coverage = row["coverage"]
        declared_sources = tuple(SourceRef(**value) for value in coverage["sources"])
        if sum(s.artifact == str(path) and s.sha256 == row["sha256"] for s in declared_sources) != 1:
            raise ValueError("exact_fac9_tape_coverage_binding_required")
        cov = Coverage(coverage["start_ns"], coverage["end_ns"], coverage["complete"],
                       tuple(coverage["gaps"]), declared_sources)
        packet = StudyTapePacket(symbol, day, raw, source(path, raw), cov)
        prepared = prepare_baseline_day(packet, policy)
        items = []
        for key, legs in selected.items():
            if key[:2] != (symbol, day):
                continue
            for leg in legs:
                identity = FlipIdentity(day, symbol, brokers[leg.account], leg.account, args.strategy_code,
                    "RECORDED_AGGREGATE_ORDER:" + leg.order_id, "ACTUAL_ENTRY_BASELINE:" + leg.order_id)
                items.append(AggregateBaselineInput(key, identity, leg, prepared, args.aggregate_entry_assumption))
        for result in simulate_aggregate_cohort(tuple(items)):
            results.setdefault(result.reference_key, []).append(result)
        day_evidence.append(dict(symbol=symbol, day=day, source=asdict(packet.source),
            coverage=asdict(cov), issues=prepared.issues, all_day_sell_flips=[asdict(e) for e in prepared.sell_flips],
            all_day_flip_events=[dict(side=side, boundary=asdict(e)) for side, e in prepared.all_flips],
            all_bar_closes=[asdict(close) for close in prepared.bar_closes],
            session_end=asdict(prepared.session_end)))
    cases = []
    for case in seed.cases:
        bound = tuple(results[case.reference_key])
        unknown = any(result.modeled_gross_pnl is None for result in bound)
        cases.append(dict(reference_key=case.reference_key, label=BASELINE_LABEL,
            history_label=HISTORY_LABEL, variant=BASELINE_VARIANT, entry_model=ENTRY_MODEL,
            clock_label="reconstructed SIP clock; source-export millisecond precision/file order",
            account_rows=[asdict(result) for result in bound],
            modeled_gross_pnl=None if unknown else sum((r.modeled_gross_pnl for r in bound), Decimal(0)),
            decision="UNKNOWN" if unknown else "MODELED_BASELINE_CLOSED"))
    verify_source_manifest(manifest, root)
    verify_loaded_sources(root)
    all_results = tuple(result for values in results.values() for result in values)
    totals = independent_leg_totals(all_results)
    payload = dict(version=BASELINE_VERSION, variant=BASELINE_VARIANT, entry_model=ENTRY_MODEL,
        cohort_policy=COHORT_POLICY, independent_leg_totals=totals,
        baseline_policy_version=BASELINE_POLICY_VERSION,
        exit_reason_counts=dict(Counter(exit.reason.value for result in all_results for exit in result.exits)),
        same_account_overlap_definition="ALL_LEGS_PARTICIPATING_IN_SAME_ACCOUNT_SYMBOL_STRATEGY_OVERLAP_INCLUSIVE_BOUNDARY",
        label=BASELINE_LABEL, history_label=HISTORY_LABEL, exit_policy_version=EXIT_POLICY_VERSION,
        clock_label="reconstructed SIP clock; source-export millisecond precision/file order",
        opening_latency_note="Operator-provided live opening note ~107ms p50/~1s p99; SIP replay omits receipt latency",
        execution_policy_label="ex-post SIM policy, snapshot retrieved ~10-05, retrieval time UNKNOWN",
        assumption=args.aggregate_entry_assumption, same_clock_policy=TIE_POLICY,
        authority=dict(artifact=args.authority_artifact, sha256=args.authority_sha256),
        reviewed_head=manifest["reviewed_head"], source_pins=source_pins,
        source_manifest_sha256=args.source_manifest_sha256, fac9_input_sha256=FAC9_SHA,
        seed_manifest=asdict(seed_manifest), reference_exclusions=[asdict(e) for e in seed.reference_exclusions],
        case_count=len(cases), account_leg_count=sum(len(c["account_rows"]) for c in cases), cases=cases,
        total_modeled_gross_pnl=None if any(c["modeled_gross_pnl"] is None for c in cases)
            else sum((c["modeled_gross_pnl"] for c in cases), Decimal(0)),
        known_component_gross_pnl=sum((r.modeled_gross_pnl for values in results.values() for r in values if r.modeled_gross_pnl is not None), Decimal(0)),
        unknown_account_legs=sum(r.modeled_gross_pnl is None for values in results.values() for r in values),
        review_cases=[case for case in cases if tuple(case["reference_key"]) in REVIEW_CASE_KEYS],
        buy_source=asdict(buy_source), day_evidence=day_evidence, policy_source=asdict(policy.source),
        actual_realized_live_pnl="SEPARATE_ACTUAL_FILL_LEDGER_NOT_THIS_MODELED_EXIT_BASELINE",
        native_individual_partial_timing="UNKNOWN", original_model_reexecuted=False,
        storage_binding="NEW_TYPED_VARIANT; NOT LIVE/ACTUAL OR PRINT_VOLUME_ENTRY_ENUM")
    raw = (json.dumps(payload, default=str, allow_nan=False, sort_keys=True) + "\n").encode()
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(raw)
    print(json.dumps(dict(output=str(output), sha256=hashlib.sha256(raw).hexdigest(),
        case_count=len(cases), total_modeled_gross_pnl=payload["total_modeled_gross_pnl"],
        unknown_account_legs=payload["unknown_account_legs"], independent_leg_totals=totals), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
