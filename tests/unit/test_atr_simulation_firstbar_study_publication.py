"""New CONTROLLED bridge guards; not actual173/PG/native/economic acceptance."""

import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from project_mai_tai.atr_simulation.firstbar_binding import load_firstbar_engine
from project_mai_tai.atr_simulation.firstbar_comparison import compare_firstbar_case
from project_mai_tai.atr_simulation.firstbar_projection import firstbar_result_row
from project_mai_tai.atr_simulation.firstbar_reference import ReferenceSeedCase
from project_mai_tai.atr_simulation.firstbar_study_publication import (
    EXIT_POLICY_V2, LABEL, PRODUCER_FILES, VERSION, HistoricalPublication, ProducerBinding,
    _case, _encode, _json, _producer, _study_span, publish_historical_publication,
    _manifest, _tapes,
    prepare_historical_publication,
)
from project_mai_tai.atr_simulation.models import inputs, metadata, rows, runs
from project_mai_tai.atr_simulation.repository import SimulationRepository, json_value
from tests.unit.test_atr_simulation_firstbar_store_binding import SOURCE, _item


def _case_fixture():
    item = _item(unknown_after_fill=True)
    identity = replace(item.episode.identity, flip_id="REFERENCE_FILL_MINUTE:CONTROLLED-order",
        segment_id="REFERENCE_SEGMENT_PROXY:CONTROLLED-order")
    item = replace(item, episode=replace(item.episode, identity=identity))
    engine, api = load_firstbar_engine()
    results = engine.simulate_many((item,), fill_policy=api.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
    expected = ReferenceSeedCase((identity.symbol, identity.trading_day, "10:00:00"), SOURCE, SOURCE,
        (("sym", identity.symbol), ("date", identity.trading_day), ("fill_et", "10:00:00"), ("qty", "196")),
        (("qty", 196),))
    leg = {"account": identity.account_id, "order_id": "CONTROLLED-order", "symbol": identity.symbol,
        "quantity": 196, "source": json_value(SOURCE)}
    case = {"reference_key": list(expected.reference_key), "reference": json_value(expected),
        "history_label": LABEL, "leg_bindings": [leg], "reconstruction_issues": [],
        "assumptions": ["CONTROLLED mapping, not native watch proof"],
        "results": json_value(results),
        "store_rows": [firstbar_result_row(result, population_label=LABEL) for result in results],
        "comparisons": [json_value(compare_firstbar_case(expected,
            tuple(result for result in results if result.mode is mode), mode=mode, bindings=(identity,)))
            for mode in api.FirstBarMode]}
    originals = {(identity.account_id, "CONTROLLED-order"): deepcopy(leg)}
    return case, expected, {identity.account_id: identity.broker}, identity.strategy, originals


def test_actual_canonical_filled_unknown_results_keep_two_worlds_and_unknown_economics():
    case, expected, brokers, strategy, originals = _case_fixture()
    before = deepcopy(case)
    result, index = _case(case, expected, brokers, strategy, originals)
    assert case == before
    assert len(result) == 2 and index["canonical_rows"] == 2
    assert all(row["outcome_kind"] == "TRADE" and row["fills"] for row in result)
    assert all(row["gross_pnl"] is None and row["population_label"] == LABEL for row in result)


@pytest.mark.parametrize("fault", ["missing_world", "duplicate_result", "missing_row", "duplicate_row",
    "row_changed_result", "gross_pnl_invented", "position_changed", "filled_erased", "wrong_route",
    "wrong_strategy", "wrong_reference", "wrong_original_leg", "missing_comparison", "duplicate_comparison",
    "comparison_dropped_account", "reconstruction_issue_with_results", "wrong_label", "false_comparison_acceptance"])
def test_canonical_case_membership_or_raw_receipt_mutation_is_refused(fault):
    case, expected, brokers, strategy, originals = _case_fixture()
    if fault == "missing_world":
        case["results"].pop()
    elif fault == "duplicate_result":
        case["results"].append(deepcopy(case["results"][0]))
    elif fault == "missing_row":
        case["store_rows"].pop()
    elif fault == "duplicate_row":
        case["store_rows"].append(deepcopy(case["store_rows"][0]))
    elif fault == "row_changed_result":
        case["store_rows"][0]["issues"].append("CONTROLLED mutation")
    elif fault == "gross_pnl_invented":
        case["store_rows"][0]["gross_pnl"] = "999.99"
    elif fault == "position_changed":
        case["store_rows"][0]["filled_qty"] += 1
    elif fault == "filled_erased":
        case["store_rows"][0]["outcome_kind"] = "UNKNOWN"
    elif fault == "wrong_route":
        brokers = {key: "webull" for key in brokers}
    elif fault == "wrong_strategy":
        strategy = "CONTROLLED-other-strategy"
    elif fault == "wrong_reference":
        case["reference_key"][-1] = "10:00:01"
    elif fault == "wrong_original_leg":
        case["leg_bindings"][0]["quantity"] += 1
    elif fault == "missing_comparison":
        case["comparisons"].pop()
    elif fault == "duplicate_comparison":
        case["comparisons"].append(deepcopy(case["comparisons"][0]))
    elif fault == "comparison_dropped_account":
        case["comparisons"][0]["bindings"] = []
    elif fault == "reconstruction_issue_with_results":
        case["reconstruction_issues"] = ["CONTROLLED_UNRECONSTRUCTED"]
    elif fault == "false_comparison_acceptance":
        case["comparisons"][0]["status"] = "NATIVE_PNL_ACCEPTED"
    else:
        case["store_rows"][0]["population_label"] = "COMPLETE_NATIVE"
    with pytest.raises(ValueError):
        _case(case, expected, brokers, strategy, originals)


def test_missing_canonical_inputs_remain_indexed_unknown_without_fake_account_row():
    case, expected, brokers, strategy, originals = _case_fixture()
    case.update(results=[], store_rows=[], comparisons=[], reconstruction_issues=["UNKNOWN_RETAINED_STUDY_TAPE"])
    selected, index = _case(case, expected, brokers, strategy, originals)
    assert selected == [] and index["status"] == "UNKNOWN_NO_CANONICAL_INPUTS"
    assert index["reference_key"] == list(expected.reference_key)


def test_empty_case_without_unknown_evidence_is_not_an_affirmative_never_buy():
    case, expected, brokers, strategy, originals = _case_fixture()
    case.update(results=[], store_rows=[], comparisons=[], reconstruction_issues=[])
    with pytest.raises(ValueError, match="explicit_unknown"):
        _case(case, expected, brokers, strategy, originals)


@pytest.mark.parametrize("fault", ["missing_member", "wrong_hash", "root_mix", "self_pins_only", "duplicate_member"])
def test_producer_requires_independently_pinned_complete_exact_root(tmp_path, fault):
    pins = tuple((name, "a" * 64) for name in PRODUCER_FILES)
    binding = ProducerBinding(str(tmp_path), "b" * 40, pins, (("CONTROLLED-account", "schwab"),), "ATR")
    receipt = {"exit_policy_version": EXIT_POLICY_V2,
               "source_pins": {str(tmp_path / name): checksum for name, checksum in pins}}
    assert _producer(receipt, binding) == {"CONTROLLED-account": "schwab"}
    if fault == "missing_member":
        receipt["source_pins"].pop(next(iter(receipt["source_pins"])))
    elif fault == "wrong_hash":
        receipt["source_pins"][next(iter(receipt["source_pins"]))] = "c" * 64
    elif fault == "root_mix":
        key = next(iter(receipt["source_pins"]))
        receipt["source_pins"][key.replace(str(tmp_path), "/CONTROLLED-other-root")] = receipt["source_pins"].pop(key)
    elif fault == "self_pins_only":
        binding = replace(binding, source_pins=())
    else:
        binding = replace(binding, source_pins=(*pins, pins[0]))
    with pytest.raises(ValueError):
        _producer(receipt, binding)


def _prepared_controlled():
    case, expected, brokers, strategy, originals = _case_fixture()
    selected, _ = _case(case, expected, brokers, strategy, originals)
    raw = _encode({"CONTROLLED": "raw receipt bytes; NOT real173", "cases": [case]})
    checksum = hashlib.sha256(raw).hexdigest()
    document = {"version": VERSION, "source_id": "firstbar-study:" + checksum,
        "input_sha256": checksum, "policy_version": selected[0]["version"],
        "coverage": {"complete": None, "population_label": LABEL,
            "CONTROLLED": "storage transaction mechanics only, NOT seed metadata acceptance"},
        "source_refs": [json_value(SOURCE)], "result_rows": selected}
    publication = _encode(document)
    return HistoricalPublication(raw, checksum, publication, hashlib.sha256(publication).hexdigest())


def test_actual_sqlite_repository_retains_raw_bytes_and_rows_on_identical_reentry(tmp_path):
    engine = sa.create_engine("sqlite:///" + str(tmp_path / "CONTROLLED-sim.sqlite"))
    metadata.create_all(engine)
    repository = SimulationRepository(engine)
    prepared = _prepared_controlled()
    try:
        first = publish_historical_publication(repository, prepared)
        second = publish_historical_publication(repository, prepared)
        assert first["inserted"] is True and second["inserted"] is False
        assert first["fresh_database_readback"] == "UNRUN"
        assert first["reference_cases"] == first["reference_exclusions"] == 0
        with engine.connect() as connection:
            assert connection.execute(sa.select(sa.func.count()).select_from(rows)).scalar_one() == 2
            assert connection.execute(sa.select(sa.func.count()).select_from(runs)).scalar_one() == 1
            chunks = connection.execute(sa.select(inputs.c.fields).order_by(inputs.c.stream_id)).scalars().all()
        recovered = b"".join(base64.b64decode(item["bytes_base64"]) for item in
            sorted(chunks, key=lambda item: item["offset"]))
        assert recovered == prepared.receipt_raw
        assert hashlib.sha256(recovered).hexdigest() == first["receipt_sha256"]
    finally:
        engine.dispose()


def test_changed_prepared_bytes_refuse_before_any_repository_method():
    prepared = replace(_prepared_controlled(), publication_raw=b"CONTROLLED corrupted")
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("repository reached before immutable hash guard: " + name)
    with pytest.raises(ValueError, match="immutable_prepared"):
        publish_historical_publication(Forbidden(), prepared)


def test_study_bounds_use_0400_2000_et_exact_integers_and_preserve_dst():
    winter = _study_span("2026-10-23")
    after_dst = _study_span("2026-11-03")
    assert winter[1] - winter[0] == after_dst[1] - after_dst[0] == 16 * 3600 * 1_000_000_000
    assert winter[0] % (86400 * 1_000_000_000) == 8 * 3600 * 1_000_000_000
    assert after_dst[0] % (86400 * 1_000_000_000) == 9 * 3600 * 1_000_000_000


def test_duplicate_and_nonfinite_receipt_json_refuse():
    for raw in (b'{"x":1,"x":2}', b'{"x":NaN}'):
        with pytest.raises(ValueError):
            _json(raw)


def _manifest_fixture(tmp_path):
    from project_mai_tai.atr_simulation.firstbar_cohort_job import EvidenceFiles
    start, end = _study_span("2026-10-23")
    item = {"symbol": "X", "day": "2026-10-23", "complete": True, "gaps": [],
        "history_label": LABEL, "sha256": "a" * 64, "bytes": 20, "records": 1,
        "start_ns": start, "end_ns": end, "evidence": {"artifact": "CONTROLLED.t",
            "scope": "ALL_SUPPLIED_FILE_BYTES_RECORDS_ONLY", "underlying_market_capture_complete": None,
            "producer_completion_evidence": "UNKNOWN", "live_coverage_unchanged": True}}
    _, expected, _, _, _ = _case_fixture()
    # Private single-case metadata guard only; public prepare requires actual173.
    seed = SimpleNamespace(cases=(expected,))
    return EvidenceFiles(tmp_path), item, seed


@pytest.mark.parametrize("fault", ["native_complete", "producer_complete", "live_changed", "wrong_scope",
    "wrong_label", "wrong_window", "duplicate", "missing"])
def test_supplied_study_envelope_cannot_promote_capture_or_drop_members(tmp_path, fault):
    files, item, seed = _manifest_fixture(tmp_path)
    content = [item]
    if fault == "native_complete":
        item["evidence"]["underlying_market_capture_complete"] = True
    elif fault == "producer_complete":
        item["evidence"]["producer_completion_evidence"] = True
    elif fault == "live_changed":
        item["evidence"]["live_coverage_unchanged"] = False
    elif fault == "wrong_scope":
        item["evidence"]["scope"] = "WHOLE_MARKET"
    elif fault == "wrong_label":
        item["history_label"] = "COMPLETE_CAPTURE"
    elif fault == "wrong_window":
        item["start_ns"] += 1
    elif fault == "duplicate":
        content.append(deepcopy(item))
    else:
        content = []
    raw = _encode(content)
    (tmp_path / "manifest.json").write_bytes(raw)
    descriptor = {"artifact": "manifest.json", "sha256": hashlib.sha256(raw).hexdigest(),
        "pointer": "$", "kind": "CONTROLLED"}
    with pytest.raises(ValueError):
        _manifest(files, descriptor, seed)


def test_missing_processed_tape_stays_unknown_without_invented_rows(tmp_path):
    _, item, _ = _manifest_fixture(tmp_path)
    missing, refs = _tapes({"tape_inputs": []}, {("X", "2026-10-23"): item},
        {"artifact": "CONTROLLED-manifest", "sha256": "b" * 64})
    assert missing == [("X", "2026-10-23")] and refs == []


@pytest.mark.parametrize("fault", [None, "wrong_hash", "wrong_pointer", "duplicate", "native_source_kind"])
def test_manifest_and_actual_tape_references_are_bound_without_replaying_tape(tmp_path, fault):
    files, item, seed = _manifest_fixture(tmp_path)
    raw = _encode([item])
    (tmp_path / "manifest.json").write_bytes(raw)
    descriptor = {"artifact": "manifest.json", "sha256": hashlib.sha256(raw).hexdigest(),
        "pointer": "$", "kind": "CONTROLLED"}
    declared = _manifest(files, descriptor, seed)
    tape = {"symbol": "X", "day": "2026-10-23", "sha256": item["sha256"], "coverage": {
        "start_ns": item["start_ns"], "end_ns": item["end_ns"], "complete": True, "gaps": [],
        "sources": [{"artifact": "CONTROLLED.t", "sha256": item["sha256"], "pointer": "$", "kind": "RETAINED"},
            {**descriptor, "pointer": "$[0]"}]}}
    receipt = {"tape_inputs": [tape]}
    if fault == "wrong_hash":
        tape["sha256"] = "c" * 64
    elif fault == "wrong_pointer":
        tape["coverage"]["sources"][1]["pointer"] = "$[1]"
    elif fault == "duplicate":
        receipt["tape_inputs"].append(deepcopy(tape))
    elif fault == "native_source_kind":
        tape["coverage"]["sources"][0]["kind"] = "NATIVE"
    if fault is None:
        missing, refs = _tapes(receipt, declared, descriptor)
        assert missing == [] and refs[0]["sha256"] == item["sha256"]
    else:
        with pytest.raises(ValueError):
            _tapes(receipt, declared, descriptor)


@pytest.mark.parametrize("fault", ["wrong_hash", "partial_receipt", "boolean_count"])
def test_public_prepare_refuses_unfinished_or_changed_receipt_before_seed_loading(tmp_path, fault):
    from project_mai_tai.atr_simulation.firstbar_study_publication import COVERAGE_SCOPE
    receipt = {"phase": "COMPLETE_FILE_REPLAY", "case_count": 173,
        "history_label": LABEL, "coverage_scope": COVERAGE_SCOPE}
    if fault == "partial_receipt":
        receipt["phase"] = "STARTED"
    elif fault == "boolean_count":
        receipt["case_count"] = True
    raw = _encode(receipt)
    path = tmp_path / "CONTROLLED-receipt.json"
    path.write_bytes(raw)
    source = {"artifact": str(path), "sha256": "f" * 64 if fault == "wrong_hash"
        else hashlib.sha256(raw).hexdigest(), "pointer": "$", "kind": "RETAINED"}
    with pytest.raises(ValueError):
        prepare_historical_publication(source, evidence_root=tmp_path,
            reference_root="UNAVAILABLE_UNREACHED", coverage_manifest_source=None, producer=None)
    assert path.read_bytes() == raw
