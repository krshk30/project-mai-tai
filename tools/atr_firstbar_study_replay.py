"""Offline supplied-file runner. Main-only execution; no provider/DB/live IO."""

import argparse
from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys

from project_mai_tai.atr_simulation.execution_eligibility import policy_from_payload
from project_mai_tai.atr_simulation.firstbar import FirstBarMode
from project_mai_tai.atr_simulation.firstbar_comparison import compare_firstbar_case
from project_mai_tai.atr_simulation.firstbar_reference import bind_reference_seed, reviewed_173_manifest
from project_mai_tai.atr_simulation.firstbar_projection import FIRSTBAR_SEED_LABEL, firstbar_result_row
from project_mai_tai.atr_simulation.firstbar_replay import FirstBarFillPolicy, FirstBarReplayEngine
from project_mai_tai.atr_simulation.firstbar_target_rule import EXIT_POLICY_VERSION, target_rule_source_pin
from project_mai_tai.atr_simulation.firstbar_study import (
    StudyTapePacket, reconstruct_study_cases, _boundary,
)
from project_mai_tai.atr_simulation.types import Coverage, EvidenceKind, SourceRef


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_input_json_key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("nonfinite_input_json_number")
    return json.loads(raw, parse_float=Decimal, object_pairs_hook=pairs, parse_constant=nonfinite)


def _source(path, raw, pointer="$"):
    return SourceRef(str(path), hashlib.sha256(raw).hexdigest(), pointer, EvidenceKind.RETAINED)


def _encode(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def canonical_study_rows(results):
    """Preserve actual typed results for later SIM storage; never rerun a model."""
    return [firstbar_result_row(result, population_label=FIRSTBAR_SEED_LABEL) for result in results]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--account-brokers", required=True, help="Explicit JSON object, no guessed route")
    parser.add_argument("--strategy-code", required=True)
    parser.add_argument("--coverage-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    from project_mai_tai.atr_simulation import firstbar_binding, firstbar_replay
    if firstbar_binding.FIRSTBAR_RESULT_VERSION != firstbar_replay.FIRSTBAR_REPLAY_VERSION:
        raise ValueError("reviewed_successor_projection_binding_required_before_replay")
    output = args.output.resolve()
    if not output.is_relative_to(Path("/private/tmp")) or output.exists():
        raise ValueError("fresh_private_output_path_required")
    root = args.reference_root.resolve()
    manifest = reviewed_173_manifest(str(root))
    selector = (root / "four/agg4.py").read_bytes()
    if hashlib.sha256(selector).hexdigest() != manifest.selector_source.sha256:
        raise ValueError("actual_reference_selector_source_mismatch")
    seed = bind_reference_seed((root / "four_columns.csv").read_bytes(),
                               (root / "four/raw_split.json").read_bytes(), manifest=manifest)
    policy_raw = args.policy_file.read_bytes()
    if hashlib.sha256(policy_raw).hexdigest() != args.policy_sha256:
        raise ValueError("explicit_policy_pin_mismatch")
    policy = policy_from_payload(_json(policy_raw), _source(args.policy_file, policy_raw))
    brokers = _json(args.account_brokers)
    if not isinstance(brokers, dict) or any(not isinstance(key, str) or not key
            or value not in ("webull", "schwab") for key, value in brokers.items()):
        raise ValueError("explicit_account_to_supported_broker_map_required")
    coverage_rows, coverage_source = {}, None
    if args.coverage_manifest is not None:
        coverage_raw = args.coverage_manifest.read_bytes()
        coverage_source = _source(args.coverage_manifest, coverage_raw)
        for index, row in enumerate(_json(coverage_raw)):
            key = row["symbol"], row["day"]
            if key in coverage_rows:
                raise ValueError("duplicate_tape_coverage_identity")
            coverage_rows[key] = row, replace(coverage_source, pointer=f"$[{index}]")
    buy_path = root / "fbh/buys.psv"
    buy_bytes = buy_path.read_bytes()
    buy_source = _source(buy_path, buy_bytes)
    if buy_source.sha256 != "a7c719d0a0a7015ec8062ad18483de71dacd2ca1abfe2eebee440a43d125b750":
        raise ValueError("reviewed_original_buy_leg_source_required")
    case_outputs, tape_inputs = {}, []
    for symbol in sorted({case.reference_key[0] for case in seed.cases}):
        if re.fullmatch(r"[A-Z][A-Z0-9.]{0,15}", symbol) is None:
            raise ValueError("safe_reference_symbol_required")
        selected = tuple(case for case in seed.cases if case.reference_key[0] == symbol)
        packets = []
        for day in sorted({case.reference_key[1] for case in selected}):
            start, end = _boundary(day, 4), _boundary(day, 20)
            path = root / "fbh/mtape" / f"{symbol}_{day}.t"
            if not path.is_file():
                continue  # Reconstruction emits an explicit UNKNOWN per affected case.
            raw = path.read_bytes()
            source = _source(path, raw)
            declaration = coverage_rows.get((symbol, day))
            complete, gaps, sources = None, ("UNKNOWN_RETAINED_STUDY_CAPTURE_COVERAGE",), (source,)
            if declaration is not None:
                row, declaration_source = declaration
                if row["sha256"] != source.sha256:
                    raise ValueError("coverage_tape_hash_mismatch")
                if row["complete"] is not None and type(row["complete"]) is not bool:
                    raise ValueError("tristate_coverage_required")
                if (type(row["start_ns"]) is not int or type(row["end_ns"]) is not int
                        or row["end_ns"] < row["start_ns"]
                        or not isinstance(row["gaps"], list)
                        or any(not isinstance(gap, str) for gap in row["gaps"])):
                    raise ValueError("explicit_coverage_bounds_and_gaps_required")
                if row["complete"] is True and not row.get("evidence"):
                    raise ValueError("coverage_evidence_reference_required")
                start, end = row["start_ns"], row["end_ns"]
                complete, gaps, sources = row["complete"], tuple(row["gaps"]), (source, declaration_source)
            coverage = Coverage(start, end, complete, gaps, sources)
            packets.append(StudyTapePacket(symbol, day, raw, source, coverage))
            tape_inputs.append({"symbol": symbol, "day": day, "sha256": source.sha256,
                                "coverage": asdict(coverage)})
        reconstructed = reconstruct_study_cases(replace(seed, cases=selected), buy_bytes,
            buy_source, tuple(packets), execution_policy=policy, account_brokers=brokers,
            strategy_code=args.strategy_code, live_by_order={})
        # All dates for this symbol share a slot/volume simulation. An earlier
        # unresolved position is not silently reset by a new day or seed row.
        cohort = tuple(item for case in reconstructed for item in case.inputs)
        results = FirstBarReplayEngine().simulate_many(cohort,
            fill_policy=FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
        by_reference = {case.reference_key: case for case in selected}
        for case in reconstructed:
            identities = tuple(item.episode.identity for item in case.inputs)
            comparisons = []
            for mode in FirstBarMode:
                bound = tuple(result for result in results
                              if result.mode is mode and result.identity in identities)
                if identities:
                    comparisons.append(asdict(compare_firstbar_case(by_reference[case.reference_key],
                        bound, mode=mode, bindings=identities)))
            case_outputs[case.reference_key] = dict(reference_key=case.reference_key,
                history_label="replay, in-sample; underlying capture completeness UNKNOWN",
                reference=asdict(by_reference[case.reference_key]),
                leg_bindings=[asdict(leg) for leg in case.legs],
                reconstruction_issues=case.issues, assumptions=case.assumptions,
                comparisons=comparisons,
                store_rows=canonical_study_rows(tuple(result for result in results if result.identity in identities)),
                results=[asdict(result) for result in results if result.identity in identities])
    source_pins = {str(Path(__file__).resolve()): hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    for suffix in ("firstbar", "firstbar_exits", "firstbar_replay", "firstbar_reference",
                   "firstbar_study", "firstbar_comparison", "firstbar_projection", "firstbar_binding",
                   "types", "execution_eligibility", "exits", "firstbar_target_rule"):
        path = Path(sys.modules["project_mai_tai.atr_simulation." + suffix].__file__).resolve()
        source_pins[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    target_path, target_sha = target_rule_source_pin()
    source_pins[target_path] = target_sha
    receipt = dict(phase="COMPLETE_FILE_REPLAY", case_count=len(seed.cases), source_pins=source_pins,
        exit_policy_version=EXIT_POLICY_VERSION,
        history_label="replay, in-sample; underlying capture completeness UNKNOWN",
        coverage_scope="ALL_SUPPLIED_HASH_PINNED_STUDY_BYTES_0400_2000_NOT_MARKET_CAPTURE_COMPLETENESS",
        scope="RECONSTRUCTED_IN_SAMPLE_SIM_NOT_NATIVE_OR_INSTALL_ACCEPTANCE",
        clock_label="reconstructed SIP clock; source-export millisecond precision/file order",
        execution_policy_label="ex-post SIM policy, snapshot retrieved ~10-05, retrieval time UNKNOWN",
        opening_latency_note="Operator-provided live opening note ~107ms p50/~1s p99; "
                             "SIP study omits receipt latency and is not native timing proof",
        execution_policy_source=asdict(policy.source),
        provided_sums_not_replay={"A": seed.provided_a_sum, "X3": seed.provided_x3_sum},
        reference_manifest=asdict(seed.manifest),
        reference_exclusions=[asdict(row) for row in seed.reference_exclusions],
        buy_source=asdict(buy_source), tape_inputs=tape_inputs,
        cases=[case_outputs[case.reference_key] for case in seed.cases],
        actual_individual_live_facts="UNKNOWN; reference reported LIVE fields preserved, not fabricated fills",
        native_arrival_and_queue="UNKNOWN; no native calls, quotes or provider IDs synthesized")
    with output.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, default=_encode, allow_nan=False, sort_keys=True)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

