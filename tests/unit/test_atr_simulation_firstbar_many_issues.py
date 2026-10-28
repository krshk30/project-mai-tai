"""Canonical receipt projection stress; not new engine or economic acceptance."""

from copy import deepcopy
from dataclasses import replace

import pytest

from project_mai_tai.atr_simulation.firstbar_binding import (
    FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION, load_firstbar_engine,
)
from project_mai_tai.atr_simulation.firstbar_display import firstbar_tables
from project_mai_tai.atr_simulation.firstbar_projection import firstbar_result_row
from project_mai_tai.atr_simulation.firstbar_projection import FIRSTBAR_SEED_LABEL
from project_mai_tai.atr_simulation.firstbar_read_model import firstbar_display_view
from project_mai_tai.atr_simulation.firstbar_ui_reader import SEED_BINDING
from tests.unit.test_atr_simulation_firstbar_store_binding import _item


def _stored_with_issues(extra_count):
    engine, api = load_firstbar_engine()
    result = engine.simulate_many((_item(unknown_after_fill=True),),
        fill_policy=api.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)[0]
    assert result.fills and result.issues and result.exits
    # Preserve the real filled-UNKNOWN receipt, then supply explicitly
    # CONTROLLED canonical issue multiplicity to exercise presentation only.
    issues = tuple(dict.fromkeys((*result.issues,
        *(f"CONTROLLED_UNKNOWN_{index:02}" for index in range(extra_count)))))
    result = replace(result, issues=issues,
        exits=tuple(replace(exit, issues=issues) for exit in result.exits))
    row = firstbar_result_row(result)
    stored = {"run": {"source_id": "CONTROLLED-many-issues",
        "policy_version": FIRSTBAR_RESULT_VERSION,
        "coverage": {"engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
            "engine_contract_version": FIRSTBAR_RESULT_VERSION, "complete": None}},
        "rows": [row], "consumers": [], "truncated": False,
        "read_status": "AVAILABLE", "read_issues": []}
    return stored, issues


def test_canonical_filled_unknown_issue_union_is_unique_without_losing_trade_or_raw_facts():
    stored, issues = _stored_with_issues(18)
    before = deepcopy(stored)
    assert len(issues) <= 32
    view = firstbar_display_view(stored, source_id="CONTROLLED-many-issues")
    assert stored == before
    assert view["outcomes"][0]["issues"] == list(issues)
    table = firstbar_tables(view)[0]
    assert len(table["rows"]) == 1
    assert table["rows"][0]["status"] == "UNKNOWN"
    assert table["rows"][0]["accepted_quantity"] > 0
    assert table["rows"][0]["row"]["pnl_label"] == "UNKNOWN"
    assert table["totals"]["complete_requested_window"] is False


def test_more_than_32_unique_issues_still_refuses_instead_of_weakening_display_bound():
    stored, issues = _stored_with_issues(33)
    assert len(issues) > 32
    with pytest.raises(ValueError):
        firstbar_display_view(stored, source_id="CONTROLLED-many-issues")


def test_stored_seed_display_uses_exact_c1_label_without_qualifying_totals_or_capture():
    stored, _ = _stored_with_issues(18)
    # CONTROLLED metadata shape only; not an actual byte-validated seed claim.
    stored["run"]["coverage"]["reference_seed_binding"] = deepcopy(SEED_BINDING)
    stored["run"]["source_refs"] = [{"sha256": SEED_BINDING[field]} for field in
        ("csv_sha256", "split_sha256", "selector_sha256")]
    before = deepcopy(stored)
    view = firstbar_display_view(stored, source_id="CONTROLLED-many-issues")
    assert stored == before
    assert view["source"]["population_label"] == FIRSTBAR_SEED_LABEL
    assert view["source"]["qualification"] == "UNQUALIFIED_REPLAY"
    assert all(row["population_label"] == FIRSTBAR_SEED_LABEL for row in view["outcomes"])
    assert all(table["source"]["population_label"] == FIRSTBAR_SEED_LABEL for table in firstbar_tables(view))
    assert view["coverage_status"] == "UNKNOWN"
    assert all(total["complete_requested_window"] is False for total in view["totals"].values())


def test_canonical_seed_row_label_is_separate_from_economic_qualification():
    engine, api = load_firstbar_engine()
    result = engine.simulate_many((_item(unknown_after_fill=True),),
        fill_policy=api.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)[0]
    row = firstbar_result_row(result, population_label=FIRSTBAR_SEED_LABEL)
    assert row["population_label"] == "replay, in-sample; underlying capture completeness UNKNOWN"
    assert row["qualification"] == "UNQUALIFIED_REPLAY"
    assert row["fills"] and row["gross_pnl"] is None
