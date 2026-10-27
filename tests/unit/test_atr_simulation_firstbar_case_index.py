"""CONTROLLED index/page metadata on real173 keys, NOT actual replay results."""

from copy import deepcopy
import csv
from dataclasses import FrozenInstanceError
from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path

import pytest

from project_mai_tai.atr_simulation.firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION
from project_mai_tai.atr_simulation.firstbar_case_index import (
    LABEL, ORDERED_KEYS_SHA256, historical_study_case_index, render_historical_study_case_index,
)
from project_mai_tai.atr_simulation.firstbar_ui_reader import SEED_BINDING


START, END = date(2026, 9, 22), date(2026, 10, 23)
SOURCE = "firstbar-study:" + "a" * 64


def _keys():
    # Exact delivered CSV metadata fixture, including original CRLF bytes.
    path = Path(__file__).resolve().parents[1] / "fixtures/atr_simulation/firstbar_reference_173_20261023.csv"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "6987ae93121fc35dc09d38d099933fd93aa932a5e48245b6dbaab82e17381f25"
    keys = [(row["sym"], row["date"], row["fill_et"]) for row in csv.DictReader(io.StringIO(raw.decode()))]
    assert hashlib.sha256(json.dumps(keys, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest() == ORDERED_KEYS_SHA256
    return keys


def _stored(*, with_inputs=True):
    selected, rows = [], []
    for index, key in enumerate(_keys()):
        # Signal0 has1 account leg, signal1 has2; remaining171 have UNKNOWN inputs.
        n = (index + 1) if with_inputs and index < 2 else 0
        identities = [{"trading_day": key[1], "symbol": key[0], "broker": "schwab",
            "account_id": f"CONTROLLED-account-{number}", "strategy": "schwab_1m_v2",
            "flip_id": f"CONTROLLED-flip-{index}", "segment_id": f"CONTROLLED-segment-{index}"}
            for number in range(n)]
        comparisons = []
        for world in ("FIRSTBAR", "FIRSTBAR_X3"):
            if n:
                status = ("UNKNOWN" if (index == 0 and world == "FIRSTBAR")
                    or (index == 1 and world == "FIRSTBAR_X3") else
                    "DIFFERENT" if index == 0 else "MATCH_COMPARED_FIELDS_ONLY")
                comparisons.append({"reference_key": list(key), "mode": world,
                    "status": status, "bindings": deepcopy(identities),
                    "issues": ["CONTROLLED_UNKNOWN_COMPARISON_EVIDENCE"] if status == "UNKNOWN" else []})
            rows.extend({"identity": deepcopy(identity), "mode": world,
                "engine_dependency_head": FIRSTBAR_ENGINE_HEAD, "version": FIRSTBAR_RESULT_VERSION,
                "population_label": LABEL} for identity in identities)
        selected.append({"reference_key": list(key), "status": "CANONICAL_ROWS_PRESENT_NOT_ACCEPTANCE"
            if n else "UNKNOWN_NO_CANONICAL_INPUTS", "identities": identities,
            "canonical_rows": n * 2, "reconstruction_issues": [] if n else ["CONTROLLED_UNKNOWN_INPUT"],
            "comparisons": comparisons})
    exclusions = [{"reference_key": ["CONTROLLED", "2026-09-22", f"19:00:{number:02}"],
        "reasons": ["CONTROLLED_REFERENCE_EXCLUSION_NOT_STRATEGY_SKIP"]} for number in range(7)]
    return {"run": {"source_id": SOURCE, "policy_version": FIRSTBAR_RESULT_VERSION,
        "source_refs": [{"artifact": "CONTROLLED-stored-source-reference", "sha256": SEED_BINDING[name]}
            for name in ("csv_sha256", "split_sha256", "selector_sha256")],
        "coverage": {"engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
            "engine_contract_version": FIRSTBAR_RESULT_VERSION, "population_label": LABEL,
            "underlying_capture_completeness": "UNKNOWN",
            "reference_seed_binding": deepcopy(SEED_BINDING),
            "historical_study": {"version": "ATRSIM_HISTORICAL_PUBLICATION_V1",
                "global_pnl_qualified": False, "receipt_sha256": "a" * 64,
                "selected_cases": selected, "reference_exclusions": exclusions}}},
        "rows": rows, "truncated": False, "read_status": "AVAILABLE" if rows else "NO_ROWS_UNKNOWN"}


def _summary(stored, start=START, end=END):
    return historical_study_case_index(stored, source_id=SOURCE, requested_start=start, requested_end=end)


def test_real_reference_keys_keep173_signals_distinct_from3_legs_and6_world_results():
    stored = _stored()
    before = deepcopy(stored)
    summary = _summary(stored)
    assert stored == before
    assert summary.status == "BOUND_REFERENCE_INDEX"
    assert summary.selected_seed_signals == summary.requested_window_signals == 173
    assert summary.seed_signals_without_inputs == summary.window_signals_without_inputs == 171
    assert summary.reconstructed_seed_account_legs == 3
    assert all(world.expected_result_rows_in_window == world.observed_result_rows_in_page == 3 for world in summary.worlds)
    assert all(world.page_membership_status == "EXACT_DECLARED_RESULT_ROWS_ONLY" for world in summary.worlds)
    assert all(world.unknown_signal_evidence_in_window == 172 for world in summary.worlds)
    assert summary.worlds[0].matched_reference_fields_only_in_window == 1
    assert summary.worlds[1].different_signals_in_window == 1
    assert "FIRSTBAR: CONTROLLED_UNKNOWN_COMPARISON_EVIDENCE" in summary.cases[0].issues
    assert len(summary.exclusions) == 7
    assert summary.global_pnl_qualified is False
    assert summary.economic_qualification == "UNQUALIFIED_REPLAY"


def test_all_no_input_signals_remain173_unknowns_not_a_zero_trade_or_pnl_claim():
    summary = _summary(_stored(with_inputs=False))
    assert summary.selected_seed_signals == summary.seed_signals_without_inputs == 173
    assert len(summary.cases) == 173 and all(case.reconstructed_account_legs == 0 for case in summary.cases)
    assert all(world.unknown_signal_evidence_in_window == 173 for world in summary.worlds)
    html = render_historical_study_case_index(summary)
    assert "173" in html and "UNKNOWN_NO_CANONICAL_INPUTS" in html
    assert "not an affirmed no-buy or zero P&amp;L" in html
    assert "$0" not in html and "<th>Entry" not in html


def test_date_window_filters_counts_without_losing_the_full_seed_denominator():
    stored = _stored()
    chosen = date(2026, 10, 23)
    stored["rows"] = []  # CONTROLLED input cases above belong to September22.
    summary = _summary(stored, chosen, chosen)
    expected = sum(key[1] == chosen.isoformat() for key in _keys())
    assert summary.selected_seed_signals == len(summary.cases) == 173
    assert summary.requested_window_signals == summary.window_signals_without_inputs == expected
    assert summary.reconstructed_window_account_legs == 0


@pytest.mark.parametrize("fault", ["missing_index", "short_index", "duplicate_key", "reordered", "wrong_source_ref",
    "wrong_engine", "wrong_source", "missing_exclusion", "overlap_exclusion", "duplicate_identity",
    "boolean_result_count", "missing_comparison", "unknown_without_issue", "unknown_with_comparison",
    "duplicate_comparison", "wrong_identity_day", "global_pnl_upgrade", "seed_descriptor_missing",
    "wrong_contract", "capture_promoted", "wrong_population_label"])
def test_malformed_or_unbound_index_reports_unknown_denominator_not173_or_zero(fault):
    stored = _stored()
    coverage = stored["run"]["coverage"]
    study = coverage["historical_study"]
    cases = study["selected_cases"]
    if fault == "missing_index":
        coverage.pop("historical_study")
    elif fault == "short_index":
        cases.pop()
    elif fault == "duplicate_key":
        cases[-1]["reference_key"] = cases[0]["reference_key"]
    elif fault == "reordered":
        cases[0], cases[1] = cases[1], cases[0]
    elif fault == "wrong_source_ref":
        stored["run"]["source_refs"].pop()
    elif fault == "wrong_engine":
        coverage["engine_dependency_head"] = "CONTROLLED-other"
    elif fault == "wrong_source":
        stored["run"]["source_id"] = "CONTROLLED-other"
    elif fault == "missing_exclusion":
        study["reference_exclusions"].pop()
    elif fault == "overlap_exclusion":
        study["reference_exclusions"][0]["reference_key"] = cases[0]["reference_key"]
    elif fault == "duplicate_identity":
        cases[1]["identities"][0] = cases[0]["identities"][0]
    elif fault == "boolean_result_count":
        cases[0]["canonical_rows"] = True
    elif fault == "missing_comparison":
        cases[0]["comparisons"].pop()
    elif fault == "unknown_without_issue":
        cases[-1]["reconstruction_issues"] = []
    elif fault == "unknown_with_comparison":
        cases[-1]["comparisons"] = cases[0]["comparisons"]
    elif fault == "duplicate_comparison":
        cases[0]["comparisons"][1] = cases[0]["comparisons"][0]
    elif fault == "wrong_identity_day":
        cases[0]["identities"][0]["trading_day"] = "2026-10-23"
    elif fault == "global_pnl_upgrade":
        study["global_pnl_qualified"] = True
    elif fault == "wrong_contract":
        coverage["engine_contract_version"] = "CONTROLLED-other-contract"
    elif fault == "capture_promoted":
        coverage["underlying_capture_completeness"] = True
    elif fault == "wrong_population_label":
        coverage["population_label"] = "NATIVE_LIVE"
    else:
        coverage.pop("reference_seed_binding")
    summary = _summary(stored)
    assert summary.status == "UNKNOWN" and summary.selected_seed_signals is None
    assert summary.reconstructed_seed_account_legs is None
    assert "Signal denominator UNKNOWN" in render_historical_study_case_index(summary)


@pytest.mark.parametrize("fault", ["missing_result", "duplicate_result", "truncated", "unbound_result", "unavailable_response"])
def test_page_problem_preserves_bound173_index_without_whole_world_or_pnl_claim(fault):
    stored = _stored()
    if fault == "missing_result":
        stored["rows"].pop()
    elif fault == "duplicate_result":
        stored["rows"].append(deepcopy(stored["rows"][0]))
    elif fault == "truncated":
        stored["truncated"] = True
    elif fault == "unavailable_response":
        stored["read_status"] = "UNAVAILABLE"
    else:
        stored["rows"][0]["identity"]["account_id"] = "CONTROLLED-unbound"
    summary = _summary(stored)
    assert summary.selected_seed_signals == 173 and len(summary.cases) == 173
    assert any(world.page_membership_status != "EXACT_DECLARED_RESULT_ROWS_ONLY" for world in summary.worlds)
    assert summary.global_pnl_qualified is False


def test_footer_escapes_input_issues_and_exclusions_without_mutating_raw_index():
    stored = _stored()
    stored["run"]["coverage"]["historical_study"]["selected_cases"][-1]["reconstruction_issues"] = ["<script>bad</script>"]
    stored["run"]["coverage"]["historical_study"]["reference_exclusions"][0]["reasons"] = ["<img src=x onerror=bad>"]
    before = deepcopy(stored)
    summary = _summary(stored)
    html = render_historical_study_case_index(summary)
    assert "&lt;script&gt;bad&lt;/script&gt;" in html and "<script>" not in html
    assert "&lt;img src=x onerror=bad&gt;" in html and "<img" not in html
    assert "Reference exclusions: 7 (not affirmative strategy skips)" in html
    assert stored == before
    with pytest.raises(FrozenInstanceError):
        summary.status = "NATIVE_GO"


@pytest.mark.parametrize("stored", [None, [], {"run": None}])
def test_unavailable_storage_is_unknown_not_an_empty_seed(stored):
    summary = _summary(stored)
    assert summary.selected_seed_signals is None and not summary.cases


@pytest.mark.parametrize("start,end", [(datetime(2026, 9, 22), END), (END, START),
    (date(2026, 9, 21), END), (START, date(2026, 10, 24))])
def test_invalid_or_widened_scope_is_refused(start, end):
    with pytest.raises(ValueError):
        _summary(_stored(), start, end)


def test_renderer_requires_the_detached_typed_summary_not_unvalidated_raw_mapping():
    with pytest.raises(ValueError, match="typed_historical"):
        render_historical_study_case_index({"selected_seed_signals": 173})
