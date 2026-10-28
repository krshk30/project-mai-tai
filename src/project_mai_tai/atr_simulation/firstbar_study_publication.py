"""Historical receipt -> SIM inbox/run/rows, NEVER a second model execution.

Main supplies the independently reviewed producer pin set and immutable receipt
hash. A receipt's self-reported pins alone confer no publication authority.
Unreconstructed cases stay in the durable case index, not fabricated trade rows.
"""

import base64
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


VERSION = "ATRSIM_HISTORICAL_PUBLICATION_V1"
LABEL = "replay, in-sample; underlying capture completeness UNKNOWN"
COVERAGE_SCOPE = "ALL_SUPPLIED_HASH_PINNED_STUDY_BYTES_0400_2000_NOT_MARKET_CAPTURE_COMPLETENESS"
MAX_RECEIPT_BYTES = 64 * 1024 * 1024
MAX_ROWS = 10000
CHUNK_BYTES = 1024 * 1024
PRODUCER_FILES_V1 = ("tools/atr_firstbar_study_replay.py", *(f"src/project_mai_tai/atr_simulation/{name}.py"
    for name in ("firstbar", "firstbar_exits", "firstbar_replay", "firstbar_reference",
        "firstbar_study", "firstbar_comparison", "firstbar_projection", "firstbar_binding",
        "types", "execution_eligibility", "exits")))
PRODUCER_FILES = (*PRODUCER_FILES_V1,
    "src/project_mai_tai/atr_simulation/firstbar_target_rule.py",
    "src/project_mai_tai/broker_adapters/target_price.py")
EXIT_POLICY_V2 = "C1_20261023_2129_A2_CEILING_FIRST_ELIGIBLE_STOP_PRINT"
IDENTITY_FIELDS = ("trading_day", "symbol", "broker", "account_id", "strategy", "flip_id", "segment_id")
WORLDS = {"FIRSTBAR", "FIRSTBAR_X3"}


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _sha(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        "historical_explicit_sha256_required")
    return value


def _json(raw):
    from .firstbar_cohort_job import _json as strict_json
    return strict_json(raw)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _identity(value):
    _require(isinstance(value, dict) and set(value) == set(IDENTITY_FIELDS)
        and all(isinstance(value[key], str) and value[key] for key in IDENTITY_FIELDS),
        "historical_exact_episode_identity_required")
    date.fromisoformat(value["trading_day"])
    return tuple(value[key] for key in IDENTITY_FIELDS)


def _study_span(day):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    bounds = []
    for hour in (4, 20):
        delta = datetime.combine(date.fromisoformat(day), time(hour),
            ZoneInfo("America/New_York")).astimezone(timezone.utc) - epoch
        bounds.append((delta.days * 86400 + delta.seconds) * 1_000_000_000)
    return tuple(bounds)


@dataclass(frozen=True)
class ProducerBinding:
    root: str
    reviewed_head: str
    # Explicit independently reviewed relative filenames and hashes, NOT receipt-derived.
    source_pins: tuple[tuple[str, str], ...]
    account_brokers: tuple[tuple[str, str], ...]
    strategy_code: str


@dataclass(frozen=True)
class HistoricalPublication:
    receipt_raw: bytes
    receipt_sha256: str
    publication_raw: bytes
    publication_sha256: str


def _producer(receipt, binding):
    _require(isinstance(binding, ProducerBinding), "historical_reviewed_producer_binding_required")
    _require(isinstance(binding.reviewed_head, str)
        and re.fullmatch(r"[0-9a-f]{40}", binding.reviewed_head) is not None,
        "historical_reviewed_producer_head_required")
    root = Path(binding.root).resolve(strict=True)
    _require(root.is_dir(), "historical_producer_root_required")
    expected = dict(binding.source_pins)
    policy = receipt.get("exit_policy_version")
    _require(policy in (None, EXIT_POLICY_V2), "historical_reviewed_producer_generation_required")
    members = PRODUCER_FILES_V1 if policy is None else PRODUCER_FILES
    _require(len(expected) == len(binding.source_pins) and set(expected) == set(members),
        "historical_exact_reviewed_producer_members_required")
    expected = {str(root / name): _sha(checksum) for name, checksum in expected.items()}
    _require(receipt.get("source_pins") == expected, "historical_producer_receipt_pin_mismatch")
    brokers = dict(binding.account_brokers)
    _require(len(brokers) == len(binding.account_brokers) and bool(brokers)
        and all(isinstance(key, str) and key and value in ("schwab", "webull")
                for key, value in brokers.items())
        and isinstance(binding.strategy_code, str) and bool(binding.strategy_code),
        "historical_explicit_account_and_strategy_binding_required")
    return brokers


def _manifest(files, descriptor, seed):
    manifest = files.document(descriptor)
    _require(isinstance(manifest, list), "historical_supplied_file_manifest_array_required")
    expected = {(item.reference_key[0], item.reference_key[1]) for item in seed.cases}
    indexed = {}
    for item in manifest:
        _require(isinstance(item, dict), "historical_manifest_row_required")
        key = item.get("symbol"), item.get("day")
        evidence = item.get("evidence")
        _require(key in expected and key not in indexed and item.get("complete") is True
            and item.get("gaps") == [] and item.get("history_label") == LABEL
            and isinstance(evidence, dict) and evidence.get("scope") == "ALL_SUPPLIED_FILE_BYTES_RECORDS_ONLY"
            and evidence.get("underlying_market_capture_complete") is None
            and evidence.get("producer_completion_evidence") == "UNKNOWN"
            and evidence.get("live_coverage_unchanged") is True,
            "historical_study_envelope_not_native_coverage_required")
        _sha(item.get("sha256"))
        _require(all(type(item.get(field)) is int and item[field] > 0
            for field in ("bytes", "records", "start_ns", "end_ns"))
            and item["start_ns"] < item["end_ns"]
            and isinstance(evidence.get("artifact"), str) and bool(evidence["artifact"]),
            "historical_manifest_hash_count_and_clock_required")
        _require((item["start_ns"], item["end_ns"]) == _study_span(item["day"]),
            "historical_exact_study_0400_2000_required")
        indexed[key] = item
    _require(set(indexed) == expected, "historical_manifest_all_selected_symbol_days_required")
    return indexed


def _tapes(receipt, declared, manifest_ref):
    tapes = receipt.get("tape_inputs")
    _require(isinstance(tapes, list), "historical_receipt_tape_index_required")
    seen, sources = set(), []
    for item in tapes:
        _require(isinstance(item, dict), "historical_receipt_tape_mapping_required")
        key = item.get("symbol"), item.get("day")
        _require(key in declared and key not in seen, "historical_unexpected_or_duplicate_tape")
        seen.add(key)
        expected, coverage = declared[key], item.get("coverage")
        _require(item.get("sha256") == expected["sha256"] and isinstance(coverage, dict)
            and coverage.get("start_ns") == expected["start_ns"]
            and coverage.get("end_ns") == expected["end_ns"]
            and coverage.get("complete") is True and coverage.get("gaps") == [],
            "historical_tape_envelope_mismatch")
        refs = coverage.get("sources")
        _require(isinstance(refs, list) and len(refs) == 2 and all(isinstance(ref, dict) for ref in refs)
            and refs[0].get("sha256") == expected["sha256"]
            and refs[0].get("artifact") == expected["evidence"]["artifact"]
            and refs[0].get("kind") == "RETAINED" and refs[0].get("pointer") == "$"
            and refs[1].get("sha256") == manifest_ref["sha256"]
            and refs[1].get("artifact") == manifest_ref["artifact"]
            and refs[1].get("pointer") == f"$[{tuple(declared).index(key)}]",
            "historical_tape_actual_source_and_manifest_required")
        sources.append(refs[0])
    # Missing tape processing is retained UNKNOWN, never inferred to be no-buy.
    return sorted(set(declared) - seen), sources


def _case(case, expected, brokers, strategy, original_legs):
    """Mechanical canonical result/row membership checks, not an execution oracle."""
    from .firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_FILL_POLICY, FIRSTBAR_RESULT_VERSION
    from .repository import digest, json_value
    from .service_contract import canonical_position_fields

    _require(isinstance(case, dict) and case.get("reference_key") == list(expected.reference_key)
        and case.get("reference") == json_value(expected) and case.get("history_label") == LABEL,
        "historical_exact_selected_case_reference_required")
    for name in ("leg_bindings", "reconstruction_issues", "assumptions", "comparisons", "results", "store_rows"):
        _require(isinstance(case.get(name), list), "historical_case_sequences_required")
    _require(all(isinstance(item, str) for name in ("reconstruction_issues", "assumptions")
        for item in case[name]), "historical_case_issue_text_required")
    legs = {}
    for leg in case["leg_bindings"]:
        _require(isinstance(leg, dict) and isinstance(leg.get("account"), str)
            and isinstance(leg.get("order_id"), str) and bool(leg["order_id"])
            and leg.get("symbol") == expected.reference_key[0], "historical_actual_leg_identity_required")
        key = leg["account"], leg["order_id"]
        _require(key not in legs, "historical_duplicate_original_account_order")
        _require(leg == original_legs.get(key), "historical_original_buy_leg_bytes_changed")
        legs[key] = leg
    results, identities = {}, {}
    for result in case["results"]:
        _require(isinstance(result, dict), "historical_raw_result_required")
        identity = result.get("identity")
        identity_key = _identity(identity)
        mode = result.get("mode")
        _require(isinstance(mode, str) and mode in WORLDS, "historical_result_world_required")
        key = identity_key, mode
        _require(key not in results, "historical_duplicate_raw_result")
        order = identity["flip_id"].removeprefix("REFERENCE_FILL_MINUTE:")
        _require(identity["symbol"] == expected.reference_key[0]
            and identity["trading_day"] == expected.reference_key[1]
            and identity["account_id"] in brokers and identity["broker"] == brokers[identity["account_id"]]
            and identity["strategy"] == strategy
            and identity["flip_id"] == "REFERENCE_FILL_MINUTE:" + order
            and identity["segment_id"] == "REFERENCE_SEGMENT_PROXY:" + order
            and (identity["account_id"], order) in legs,
            "historical_result_original_leg_route_or_proxy_identity_mismatch")
        results[key], identities[identity_key] = result, identity
    _require(set(results) == {(identity, world) for identity in identities for world in WORLDS},
        "historical_both_worlds_for_each_actual_input_required")
    if identities:
        _require({(value["account_id"], value["flip_id"].removeprefix("REFERENCE_FILL_MINUTE:"))
            for value in identities.values()} == set(legs), "historical_all_reconstructed_legs_required")
        _require(not case["reconstruction_issues"], "historical_reconstruction_issues_with_input_rows")
    else:
        _require(not case["store_rows"] and not case["comparisons"] and bool(case["reconstruction_issues"]),
            "historical_missing_inputs_must_remain_explicit_unknown")
    comparisons = {}
    for comparison in case["comparisons"]:
        _require(isinstance(comparison, dict) and comparison.get("reference_key") == list(expected.reference_key)
            and comparison.get("mode") in WORLDS
            and comparison.get("status") in {"UNKNOWN", "DIFFERENT", "MATCH_COMPARED_FIELDS_ONLY"}
            and comparison.get("scope") == "PROVIDED_REFERENCE_FIELDS_NOT_NATIVE_ACCEPTANCE",
            "historical_comparison_key_world_required")
        mode = comparison["mode"]
        _require(mode not in comparisons and isinstance(comparison.get("bindings"), list)
            and len(comparison["bindings"]) == len(identities)
            and {_identity(item) for item in comparison["bindings"]} == set(identities),
            "historical_comparison_full_identity_membership_required")
        comparisons[mode] = comparison
    _require(set(comparisons) == (WORLDS if identities else set()), "historical_both_comparisons_required")
    rows, seen = [], set()
    for row in case["store_rows"]:
        _require(isinstance(row, dict), "historical_canonical_store_row_required")
        key = _identity(row.get("identity")), row.get("mode")
        _require(key in results and key not in seen, "historical_store_row_membership_mismatch")
        raw = results[key]
        _require(all(row.get(field) == value for field, value in raw.items() if field != "gross_pnl")
            and row.get("engine_gross_pnl") == raw.get("gross_pnl")
            and row.get("gross_pnl") == (raw.get("gross_pnl")
                if raw.get("decision") == "TRADE_CLOSED" and not raw.get("issues") else None),
            "historical_canonical_row_changed_raw_result")
        _require(row.get("version") == FIRSTBAR_RESULT_VERSION and row.get("model") == FIRSTBAR_FILL_POLICY
            and row.get("engine_dependency_head") == FIRSTBAR_ENGINE_HEAD
            and row.get("population_label") == LABEL and row.get("qualification") == "UNQUALIFIED_REPLAY"
            and row.get("live_totals_included") is False,
            "historical_canonical_row_source_contract_mismatch")
        expected_key = digest((row["identity"], row["mode"], row["model"]))
        _require(row.get("outcome_id") == expected_key
            and canonical_position_fields(row) == canonical_position_fields(row["position"]),
            "historical_canonical_position_or_outcome_key_mismatch")
        positive = bool(row.get("fills"))
        kind = ("TRADE" if positive else "SKIP" if row["decision"].startswith("SKIPPED_")
            and not row["issues"] else "UNKNOWN")
        _require(row.get("outcome_kind") == kind
            and row.get("trade_id") == (expected_key if positive else None),
            "historical_filled_entry_must_not_disappear")
        seen.add(key)
        rows.append(row)
    _require(seen == set(results), "historical_all_raw_results_need_canonical_rows")
    return rows, {"reference_key": list(expected.reference_key),
        "status": "CANONICAL_ROWS_PRESENT_NOT_ACCEPTANCE" if identities else "UNKNOWN_NO_CANONICAL_INPUTS",
        "identities": list(identities.values()), "canonical_rows": len(rows),
        "reconstruction_issues": case["reconstruction_issues"], "assumptions": case["assumptions"],
        "comparisons": case["comparisons"]}


def prepare_historical_publication(receipt_source, *, evidence_root, reference_root,
        coverage_manifest_source, producer):
    """Bounded metadata/result verification only; no tape replay, DB or engine call."""
    from .firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION
    from .firstbar_cohort_job import EvidenceFiles, _reference_seed
    from .firstbar_read_model import firstbar_display_view
    from .firstbar_reference import bind_reference_seed, ReferenceSeedManifest
    from .firstbar_study import parse_study_buy_legs
    from .repository import json_value
    from .types import EvidenceKind, SourceRef

    files = EvidenceFiles(evidence_root)
    _require(isinstance(receipt_source, dict) and receipt_source.get("pointer") == "$"
        and receipt_source.get("kind") == "RETAINED", "historical_retained_receipt_source_required")
    path = Path(receipt_source["artifact"])
    path = (path if path.is_absolute() else files.root / path).resolve(strict=True)
    _require(path.is_relative_to(files.root) and path.is_file(), "historical_receipt_outside_evidence_root")
    with path.open("rb") as source:
        raw = source.read(MAX_RECEIPT_BYTES + 1)
    checksum = _sha(receipt_source.get("sha256"))
    _require(len(raw) <= MAX_RECEIPT_BYTES and hashlib.sha256(raw).hexdigest() == checksum,
        "historical_bounded_receipt_hash_mismatch")
    receipt = _json(raw)
    _require(isinstance(receipt, dict) and receipt.get("phase") == "COMPLETE_FILE_REPLAY"
        and receipt.get("case_count") == 173 and type(receipt["case_count"]) is int
        and receipt.get("history_label") == LABEL and receipt.get("coverage_scope") == COVERAGE_SCOPE,
        "historical_complete_seed_receipt_contract_required")
    brokers = _producer(receipt, producer)
    descriptor, refs, _ = _reference_seed(files, {"root": reference_root})
    manifest = ReferenceSeedManifest("replay, in-sample; reviewed CSV173", 173, *refs)
    seed = bind_reference_seed(files.read({**json_value(refs[0]), "pointer": "$"}),
        files.read({**json_value(refs[1]), "pointer": "$"}), manifest=manifest)
    _require(receipt.get("reference_manifest") == json_value(seed.manifest)
        and receipt.get("reference_exclusions") == json_value(seed.reference_exclusions)
        and receipt.get("provided_sums_not_replay") == json_value({"A": seed.provided_a_sum, "X3": seed.provided_x3_sum}),
        "historical_manifest_or_seven_exclusions_changed")
    declared = _manifest(files, coverage_manifest_source, seed)
    missing_tapes, tape_refs = _tapes(receipt, declared, coverage_manifest_source)
    actual_sources = []
    original_legs = None
    for name in ("buy_source", "execution_policy_source"):
        ref = receipt.get(name)
        _require(isinstance(ref, dict), "historical_buy_and_policy_sources_required")
        source_raw = files.read({**ref, "pointer": "$"})
        actual_sources.append(ref)
        if name == "buy_source":
            _require(ref["sha256"] == "a7c719d0a0a7015ec8062ad18483de71dacd2ca1abfe2eebee440a43d125b750",
                "historical_reviewed_original_buy_file_required")
            source = SourceRef(ref["artifact"], ref["sha256"], ref["pointer"],
                EvidenceKind(ref["kind"]), ref.get("asof_ns"))
            original_legs = {(leg.account, leg.order_id): json_value(leg)
                for leg in parse_study_buy_legs(source_raw, source)}
    cases = receipt.get("cases")
    _require(isinstance(cases, list) and len(cases) == 173
        and [item.get("reference_key") for item in cases if isinstance(item, dict)]
            == [list(item.reference_key) for item in seed.cases],
        "historical_all173_in_exact_reference_order_required")
    rows, case_index, seen = [], [], set()
    for case, expected in zip(cases, seed.cases):
        selected, entry = _case(case, expected, brokers, producer.strategy_code, original_legs)
        for row in selected:
            key = _identity(row["identity"]), row["mode"]
            _require(key not in seen, "historical_episode_reused_across_reference_cases")
            seen.add(key)
        rows.extend(selected)
        case_index.append(entry)
    _require(len(rows) <= MAX_ROWS, "historical_bounded_store_rows_required")
    source_id = "firstbar-study:" + checksum
    coverage = {"complete": None, "gaps": [], "native_asof": "UNKNOWN",
        "underlying_capture_completeness": "UNKNOWN", "population_label": LABEL,
        "engine_dependency_head": FIRSTBAR_ENGINE_HEAD, "engine_contract_version": FIRSTBAR_RESULT_VERSION,
        "qualification": "UNQUALIFIED_REPLAY", "reference_seed_binding": descriptor,
        "historical_study": {"version": VERSION, "receipt_sha256": checksum,
            "producer_reviewed_head": producer.reviewed_head, "source_pins": receipt["source_pins"],
            "selected_cases": case_index, "reference_exclusions": receipt["reference_exclusions"],
            "missing_processed_symbol_days": missing_tapes, "global_pnl_qualified": False,
            "raw_receipt_stream": "atrsim-study-raw:" + checksum,
            "raw_receipt_bytes": len(raw), "chunk_bytes": CHUNK_BYTES,
            "raw_receipt_chunks": (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES}}
    # Exercise the existing canonical view/row shape guards, not a new model.
    firstbar_display_view({"run": {"source_id": source_id, "policy_version": FIRSTBAR_RESULT_VERSION,
        "coverage": coverage}, "rows": rows, "consumers": [], "truncated": False,
        "read_status": "AVAILABLE", "read_issues": []}, source_id=source_id)
    source_refs = [*json_value(refs), receipt_source, coverage_manifest_source, *tape_refs, *actual_sources]
    document = {"version": VERSION, "source_id": source_id, "input_sha256": checksum,
        "policy_version": FIRSTBAR_RESULT_VERSION, "coverage": coverage,
        "source_refs": source_refs, "result_rows": rows}
    publication = _encode(document)
    return HistoricalPublication(raw, checksum, publication, hashlib.sha256(publication).hexdigest())


def publish_historical_publication(repository, prepared):
    """Append raw inbox first, then existing atomic run/rows publication.

Both calls are independently restart-idempotent; they are NOT one transaction.
Unknown commit errors propagate. No automatic retry, cleanup deletion or engine.
"""
    _require(isinstance(prepared, HistoricalPublication)
        and hashlib.sha256(prepared.receipt_raw).hexdigest() == prepared.receipt_sha256
        and hashlib.sha256(prepared.publication_raw).hexdigest() == prepared.publication_sha256,
        "historical_immutable_prepared_publication_required")
    document = _json(prepared.publication_raw)
    _require(document.get("version") == VERSION and document.get("input_sha256") == prepared.receipt_sha256
        and document.get("source_id") == "firstbar-study:" + prepared.receipt_sha256,
        "historical_prepared_source_identity_mismatch")
    stream = "atrsim-study-raw:" + prepared.receipt_sha256
    consumer = "atrsim-study:" + prepared.receipt_sha256
    events = [{"stream": stream, "stream_id": str(offset // CHUNK_BYTES), "fields": {
        "version": VERSION, "receipt_sha256": prepared.receipt_sha256, "offset": offset,
        "encoding": "base64", "bytes_base64": base64.b64encode(
            prepared.receipt_raw[offset:offset + CHUNK_BYTES]).decode("ascii")}}
        for offset in range(0, len(prepared.receipt_raw), CHUNK_BYTES)]
    prior = repository.checkpoint(consumer)
    repository.capture(consumer, prior["revision"] if prior else 0, events,
        {stream: prepared.receipt_sha256}, issues=["HISTORICAL_STUDY_NOT_NATIVE_CAPTURE"])
    inserted = repository.publish(**{key: document[key] for key in
        ("source_id", "input_sha256", "policy_version", "source_refs", "coverage", "result_rows")})
    study = document["coverage"].get("historical_study", {})
    return {"source_id": document["source_id"], "inserted": inserted,
        "canonical_rows": len(document["result_rows"]),
        "reference_cases": len(study.get("selected_cases", [])),
        "reference_exclusions": len(study.get("reference_exclusions", [])),
        "receipt_sha256": prepared.receipt_sha256,
        "history_label": LABEL, "qualification": "UNQUALIFIED_REPLAY",
        "fresh_database_readback": "UNRUN", "global_pnl_qualified": False}
