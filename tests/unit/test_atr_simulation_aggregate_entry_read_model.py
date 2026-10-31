"""CONTROLLED pure read-port bytes; not actual173, DB or native proof."""

from copy import deepcopy
from collections import Counter
from dataclasses import FrozenInstanceError, replace
import hashlib

import pytest

from project_mai_tai.atr_simulation import aggregate_entry_publication as pub
from project_mai_tai.atr_simulation import aggregate_entry_read_model as read
from project_mai_tai.atr_simulation.aggregate_entry_contract import LABEL, MODE, MODEL, VERSION
from project_mai_tai.atr_simulation.aggregate_entry_display import render_aggregate_entry_table
from tests.unit.test_atr_simulation_aggregate_entry_publication import operands as operands, source, unknown_first


def independent(receipt, *, flagged=False, unknown=False):
    """Only declared metadata fixtures; no simulator or producer invocation."""
    receipt.update(version=pub.CONFIRMATION_ADAPTER_VERSION, cohort_policy=pub.INDEPENDENT_COHORT_POLICY,
                   baseline_policy_version=pub.CONFIRMATION_POLICY_VERSION)
    for case in receipt["cases"]:
        for row in case["account_rows"]:
            row.update(version=pub.CONFIRMATION_ADAPTER_VERSION, flags=[],
                same_account_overlap_order_ids=[], cohort_policy=pub.INDEPENDENT_COHORT_POLICY)
    receipt["day_evidence"][0]["session_end"] = dict(available_ns=1791849600000000000)
    if flagged:
        left = receipt["cases"][0]["account_rows"][0]
        right = receipt["cases"][1]["account_rows"][0]
        ns = left["recorded_aggregate"]["filled_ns"] + 500000000
        right["recorded_aggregate"]["filled_ns"] = ns
        right["aggregate_entry"]["at"]["event_ns"] = ns
        right["position"]["first_fill_ts"] = ns
        for row, peer in ((left, right), (right, left)):
            row["flags"] = ["SAME_ACCOUNT_OVERLAP"]
            row["same_account_overlap_order_ids"] = [peer["order_id"]]
    if unknown:
        unknown_first(receipt)
        receipt["cases"][0]["account_rows"][0]["issues"] = ["UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY"]
    def total(count, missing):
        return dict(legs=count, unknown_legs=missing,
            gross_pnl=None if missing else str(count * 50),
            known_component_gross_pnl=str((count-missing) * 50))
    overlap = 2 if flagged else 0
    receipt["independent_leg_totals"] = dict(all_legs=total(253, int(unknown)),
        excluding_same_account_overlap=total(253-overlap, int(unknown and not flagged)),
        same_account_overlap=total(overlap, int(unknown and flagged)))
    closes = {}
    for case in receipt["cases"]:
        for row in case["account_rows"]:
            start = row["recorded_aggregate"]["filled_ns"] // 60000000000 * 60000000000
            close = dict(bar_start_ns=start, available_ns=start+60000000000,
                close="10", line="9", state="long",
                source={**receipt["day_evidence"][0]["source"], "kind":"DERIVED",
                        "pointer":f"CONTROLLED:oracle-close-bar:{start}"})
            closes[start] = close
            row.update(baseline_policy_version=pub.CONFIRMATION_POLICY_VERSION,
                       entry_bar_close=close, confirmation_exit=None)
    receipt["day_evidence"][0]["all_bar_closes"] = [closes[key] for key in sorted(closes)]
    receipt["exit_reason_counts"] = dict(Counter(exit["reason"] for case in receipt["cases"]
        for row in case["account_rows"] for exit in row["exits"]))
    return receipt


def bound(receipt, producer):
    raw = pub.encode(receipt)
    sha = hashlib.sha256(raw).hexdigest()
    return read.prepare_aggregate_read_binding(raw,
        receipt_source=source("CONTROLLED:reviewed-final-receipt", sha), producer=producer,
        expected_receipt_sha256=sha, source_id="aggregate-entry:" + sha)


def port(binding):
    payload = pub.document(binding.publication_raw)
    run = {key: payload[key] for key in ("source_id", "input_sha256", "policy_version", "source_refs", "coverage")}
    run.update(result_sha256=pub.digest(payload["result_rows"]), created_at="CONTROLLED:DB timestamp")
    return dict(run=run, rows=payload["result_rows"], truncated=False, read_status="AVAILABLE")


def project(stored, binding, start="2026-09-22", end="2026-10-23"):
    return read.aggregate_entry_display_view(stored, binding=binding, start=start, end=end)


def test_external173_253_binding_formats_new_mode_without_native_or_realized_claim(operands):
    receipt, producer = operands
    binding = bound(independent(receipt, flagged=True), producer)
    stored = port(binding)
    original = deepcopy(stored)
    view = project(stored, binding)
    assert stored == original
    assert len(view["rows"]) == 253 and view["expected_study_cases"] == 173
    assert view["expected_study_rows"] == 253 and view["expected_requested_rows"] == 253
    assert view["complete_requested_window"] is True
    assert (view["mode"], view["model"], view["version"]) == (MODE, MODEL, VERSION)
    assert view["history_label"] == LABEL and view["known_pnl_label"] == "$+12,650.00"
    assert view["global_pnl_qualified"] is False and view["actual_realized_pnl_included"] is False
    assert view["native_execution_qualified"] is False
    assert view["underlying_capture_completeness"] == "UNKNOWN"
    assert view["unknown_count"] == 0
    row = next(row for row in view["rows"] if row["identity"]["account_id"] == "A")
    assert row["quantity"] == "100" and row["entry_price"] == "$10"
    assert row["exit_price"] == "$10.50" and row["pnl_pct_label"] == "+5.0%"
    assert row["entry_time"].endswith("-04:00")
    assert row["partial_fill_timing"] == "UNKNOWN" and row["raw_baseline_result"]["recorded_aggregate"]
    flagged = [row for row in view["rows"] if row["flags"]]
    assert len(flagged) == 2 and all(row["pnl_label_known"] for row in flagged)
    assert all("SAME_ACCOUNT_OVERLAP" in row["summary"] for row in flagged)
    totals = view["full_study_totals"]
    assert totals["scope"] == "FULL_BOUND_STUDY_NOT_PAGE_OR_REALIZED_PNL"
    # Publication sums this fixture's "50.00" row amounts without stripping scale.
    assert totals["independent_leg_totals"]["same_account_overlap"]["gross_pnl"] == "100.00"
    assert totals["independent_leg_totals"]["excluding_same_account_overlap"]["gross_pnl"] == "12550.00"
    rendered = render_aggregate_entry_table(view, render_row=lambda row: "<tr>CONTROLLED</tr>", column_header="")
    assert "not actual realized" in rendered and rendered.count("<tr>CONTROLLED</tr>") == 25


def test_genuine_unknown_with_saved_exit_stays_null_in_rows_full_total_and_bucket(operands):
    receipt, producer = operands
    binding = bound(independent(receipt, flagged=True, unknown=True), producer)
    view = project(port(binding), binding)
    assert view["unknown_count"] == 1
    unknown = next(row for row in view["rows"] if not row["pnl_label_known"])
    assert unknown["pnl"] is None and unknown["pnl_label"] == unknown["pnl_pct_label"] == "UNKNOWN"
    assert unknown["exit_time"] == unknown["exit_price"] == "UNKNOWN"
    assert unknown["raw_baseline_result"]["exits"][0]["price"] == "10.50"
    assert unknown["flags"] == ["SAME_ACCOUNT_OVERLAP"]
    assert view["known_page_gross_pnl"] == "12600.00"
    totals = view["full_study_totals"]["independent_leg_totals"]
    assert totals["all_legs"]["gross_pnl"] is None
    assert totals["same_account_overlap"]["gross_pnl"] is None
    assert totals["all_legs"]["known_component_gross_pnl"] == "12600.00"
    assert view["global_pnl_qualified"] is False


def test_truncated_page_keeps_page_subtotal_separate_from_full_three_totals(operands):
    receipt, producer = operands
    binding = bound(independent(receipt), producer)
    stored = port(binding)
    stored.update(rows=stored["rows"][:1], truncated=True)
    view = project(stored, binding)
    assert view["known_pnl_label"] == "$+50.00" and view["returned_rows"] == 1
    assert view["complete_requested_window"] is False and view["expected_requested_rows"] == 253
    assert view["full_study_totals"]["independent_leg_totals"]["all_legs"]["gross_pnl"] == "12650.00"


def test_requested_day_subsets_expected_index_not_full253_or_global_total(operands):
    receipt, producer = operands
    binding = bound(independent(receipt), producer)
    stored = port(binding)
    assert len(project(stored, binding, "2026-10-12", "2026-10-12")["rows"]) == 253
    stored["rows"] = []
    empty = project(stored, binding, "2026-10-13", "2026-10-13")
    assert empty["complete_requested_window"] is True and empty["expected_requested_rows"] == 0
    assert empty["known_pnl_label"] == "UNKNOWN" and empty["known_page_gross_pnl"] is None


@pytest.mark.parametrize("status", ("UNKNOWN", "UNAVAILABLE"))
def test_explicit_absent_storage_returns_unknown_not_zero_or_trades(operands, status):
    receipt, producer = operands
    binding = bound(independent(receipt), producer)
    view = project(dict(run=None, rows=[], truncated=False, read_status=status), binding)
    assert view["read_status"] == status and view["rows"] == []
    assert view["known_pnl_label"] == "UNKNOWN" and view["full_study_totals"] is None
    assert view["complete_requested_window"] is False
    render_aggregate_entry_table(view, render_row=lambda row: "UNREACHABLE", column_header="")


@pytest.mark.parametrize("fault", (
    "source", "sha", "policy", "coverage", "refs", "result-sha", "duplicate", "missing",
    "outcome", "row-money", "row-position", "row-source", "row-native", "row-overlap", "legacy-mode",
    "wrong-model", "row-version", "unknown-zero", "outside-day", "missing-status", "bad-truncated",
    "unavailable-with-run", "unavailable-with-rows", "missing-run", "too-many"))
def test_foreign_mutated_unbound_partial_or_legacy_port_fails_closed(operands, fault):
    receipt, producer = operands
    binding = bound(independent(receipt, flagged=True, unknown=True), producer)
    stored = port(binding)
    row = stored["rows"][0]
    if fault == "source":
        stored["run"]["source_id"] = "DB-self-attestation"
    elif fault == "sha":
        stored["run"]["input_sha256"] = "f" * 64
    elif fault == "policy":
        stored["run"]["policy_version"] = "LIVE"
    elif fault == "coverage":
        stored["run"]["coverage"]["aggregate_entry_study"]["unknown_account_legs"] = 0
    elif fault == "refs":
        stored["run"]["source_refs"] = []
    elif fault == "result-sha":
        stored["run"]["result_sha256"] = "f" * 64
    elif fault == "duplicate":
        stored["rows"].append(deepcopy(row))
    elif fault == "missing":
        stored["rows"].pop()
    elif fault == "outcome":
        row["outcome_id"] = "FOREIGN"
    elif fault == "row-money":
        row["gross_pnl"] = "999"
    elif fault == "row-position":
        row["filled_qty"] = 1
    elif fault == "row-source":
        row["source_pins"] = {}
    elif fault == "row-native":
        row["native_execution_qualified"] = True
    elif fault == "row-overlap":
        row["flags"] = []
    elif fault == "legacy-mode":
        row["mode"] = "LIVE"
    elif fault == "wrong-model":
        row["model"] = "ACTUAL"
    elif fault == "row-version":
        row["version"] = "LEGACY"
    elif fault == "unknown-zero":
        row.update(decision="MODELED_BASELINE_CLOSED", gross_pnl="0", issues=[])
    elif fault == "outside-day":
        with pytest.raises(ValueError, match="outside_window"):
            project(stored, binding, "2026-10-13", "2026-10-13")
        return
    elif fault == "missing-status":
        del stored["read_status"]
    elif fault == "bad-truncated":
        stored["truncated"] = 0
    elif fault == "unavailable-with-run":
        stored.update(read_status="UNAVAILABLE", rows=[])
    elif fault == "unavailable-with-rows":
        stored.update(read_status="UNKNOWN", run=None)
    elif fault == "missing-run":
        stored["run"] = None
    else:
        stored["rows"] = [row] * 10001
    with pytest.raises(ValueError):
        project(stored, binding)


@pytest.mark.parametrize("start,end", (("2026-09-21", "2026-10-23"), ("2026-09-22", "2026-10-24"),
    ("2026-10-13", "2026-10-12"), ("20261012", "2026-10-12"), (None, "2026-10-12")))
def test_strict32_calendar_days_never_expands_to_arbitrary_history(operands, start, end):
    receipt, producer = operands
    binding = bound(independent(receipt), producer)
    with pytest.raises(ValueError):
        project(port(binding), binding, start, end)


@pytest.mark.parametrize("fault", ("hash", "source-id", "foreign-head", "missing-pin", "v1"))
def test_database_own_metadata_cannot_supply_missing_external_operand(operands, fault):
    receipt, producer = operands
    independent(receipt)
    if fault == "foreign-head":
        producer = replace(producer, reviewed_head="b" * 40)
    elif fault == "missing-pin":
        producer = replace(producer, source_pins=producer.source_pins[:-1])
    elif fault == "v1":
        receipt["version"] = pub.ADAPTER_VERSION
    raw = pub.encode(receipt)
    sha = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        read.prepare_aggregate_read_binding(raw, receipt_source=source("CONTROLLED:receipt", sha),
            producer=producer, expected_receipt_sha256="f" * 64 if fault == "hash" else sha,
            source_id="foreign" if fault == "source-id" else "aggregate-entry:" + sha)


def test_prepared_expected_bytes_are_immutable_and_mutation_detected(operands):
    receipt, producer = operands
    binding = bound(independent(receipt), producer)
    with pytest.raises(FrozenInstanceError):
        binding.source_id = "foreign"
    with pytest.raises(ValueError, match="trusted_binding"):
        project(port(binding), replace(binding, publication_raw=binding.publication_raw + b" "))


def test_same_payloads_in_other_read_order_have_same_canonical_view(operands):
    receipt, producer = operands
    binding = bound(independent(receipt), producer)
    stored = port(binding)
    original = project(stored, binding)
    stored["rows"].reverse()
    assert project(stored, binding) == original
