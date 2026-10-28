"""CONTROLLED34 reporting fixtures, not original input/runtime witnesses."""

from copy import deepcopy

import pytest

from tools.l12_recorded_boot_timeline import (
    FLEET_NAMES, SOURCE_ANCHOR_MS, RecordedSessionMap, classify_c11229_individual_blocks,
)


def operands(reason="STALE_BAR_OVER_180_SECONDS"):
    mapping = RecordedSessionMap(target_day="2026-10-26", ruling_sha256="a" * 64,
        chronology_review_sha256="b" * 64)
    start = SOURCE_ANCHOR_MS * 1000000
    end = start + 16 * 3600 * 1000000000
    windows = [dict(window=dict(event_id=str(i), symbol=FLEET_NAMES[i % len(FLEET_NAMES)],
        original_ns=start, original_end_ns=end), outcome="FAIL_NEW_OVER_10",
        NEW_not_ready_closed_minutes=11, applied_not_ready_limit=10) for i in range(34)]
    report = dict(version="C11859_RETIMED_BOOT_WINDOWS_V3", seed_denominator=18,
        window_denominator=34, permission_granted=False, windows=windows)
    mapped_start, mapped_end = mapping.market_ns(start), mapping.market_ns(end)
    excluded = mapped_start + 240 * 1000000000
    inputs = {"STALE_BAR_OVER_180_SECONDS": dict(bar_ms=mapped_start // 1000000,
        observed_ms=excluded // 1000000), "HISTORY_READ_ERROR": dict(exception_type="ValueError"),
        "UNPROVEN_PRODUCER": dict(producer_proven=False)}
    evidence = dict(version="C11229_REAL_INPUT_INDIVIDUAL_BLOCK_V1", event_id="0",
        symbol=windows[0]["window"]["symbol"], exclusion_clock_ns=excluded,
        window_start_ns=mapped_start, window_end_ns=mapped_end, observed_through_ns=mapped_end,
        reason=reason, input=inputs[reason], input_origin="ACTUAL_RETAINED_INPUT_OR_REAL_READER_RESULT",
        input_reference="CONTROLLED_REFERENCE_FOR_GUARD_UNIT_ONLY", input_sha256="c" * 64,
        witness_sha256="d" * 64, review_sha256="e" * 64, actual_exclusion=True,
        symbol_unknown=True, symbol_entry_blocked=True, entry_count=0, rest_fleet_released=True)
    return report, evidence, mapping


def classify(report, evidence, mapping, **kwargs):
    return classify_c11229_individual_blocks(report, evidence, mapping=mapping, ruling_sha256="f" * 64,
        report_guard=kwargs.get("report_guard", lambda *_: True),
        witness_guard=kwargs.get("witness_guard", lambda *_: True))


@pytest.mark.parametrize("reason", ["STALE_BAR_OVER_180_SECONDS", "HISTORY_READ_ERROR", "UNPROVEN_PRODUCER"])
def test_exact_real_reason_with_guards_is_separate_block_not_seed_PASS(reason):
    report, evidence, mapping = operands(reason)
    before = deepcopy((report, evidence))
    result = classify(report, [evidence], mapping)
    assert result["window_denominator"] == 34 and result["seed_denominator"] == 18
    assert result["expected_individual_blocks"] == ["0"]
    assert result["windows"][0]["outcome"] == "EXPECTED_INDIVIDUAL_BLOCK"
    assert result["windows"][0]["original_seed_outcome"] == "FAIL_NEW_OVER_10"
    assert result["windows"][0]["seed_PASS_claimed"] is False
    assert result["windows"][0]["symbol_admission_granted"] is False
    assert result["windows"][1]["outcome"] == "FAIL_NEW_OVER_10"
    assert len(result["new_gate_failures"]) == 33
    assert result["original_seed_report"] == report and (report, evidence) == before


@pytest.mark.parametrize("field,value", [("actual_exclusion", False), ("symbol_unknown", False),
    ("symbol_entry_blocked", False), ("rest_fleet_released", False), ("entry_count", 1),
    ("entry_count", False), ("input_origin", "CONTROLLED_COUNTERCASE_NOT_REAL34"),
    ("event_id", "OTHER"), ("symbol", "OTHER"), ("review_sha256", "0" * 64)])
def test_unjustified_or_unbound_block_cannot_get_expected_disposition(field, value):
    report, evidence, mapping = operands()
    evidence[field] = value
    with pytest.raises(ValueError):
        classify(report, [evidence], mapping)


@pytest.mark.parametrize("age", [180000, 179999, -1])
def test_age_at_or_under180_seconds_is_not_the_stale_exception(age):
    report, evidence, mapping = operands()
    evidence["input"]["bar_ms"] = evidence["input"]["observed_ms"] - age
    with pytest.raises(ValueError, match="strictly_over180"):
        classify(report, [evidence], mapping)


@pytest.mark.parametrize("fault", ["short_coverage", "wrong_start", "wrong_end", "outside_window",
    "duplicate", "missing_window", "extra_window", "zero_ruling"])
def test_no_partial_window_evidence_or_changed_denominator(fault):
    report, evidence, mapping = operands()
    witnesses = [evidence]
    if fault == "short_coverage":
        evidence["observed_through_ns"] -= 1
    elif fault == "wrong_start":
        evidence["window_start_ns"] += 1
    elif fault == "wrong_end":
        evidence["window_end_ns"] -= 1
    elif fault == "outside_window":
        evidence["exclusion_clock_ns"] = evidence["window_end_ns"]
    elif fault == "duplicate":
        witnesses.append(deepcopy(evidence))
    elif fault == "missing_window":
        report["windows"].pop()
    elif fault == "extra_window":
        report["windows"].append(deepcopy(report["windows"][0]))
    else:
        with pytest.raises(ValueError):
            classify_c11229_individual_blocks(report, witnesses, mapping=mapping, ruling_sha256="0" * 64,
                report_guard=lambda *_: True, witness_guard=lambda *_: True)
        return
    with pytest.raises(ValueError):
        classify(report, witnesses, mapping)


@pytest.mark.parametrize("guard", ["report_guard", "witness_guard"])
def test_independent_report_and_real_input_guards_required(guard):
    report, evidence, mapping = operands()
    with pytest.raises(ValueError):
        classify(report, [evidence], mapping, **{guard: lambda *_: False})


@pytest.mark.parametrize("reason,input_value", [("UNPROVEN_PRODUCER", {"producer_proven": True}),
    ("HISTORY_READ_ERROR", {"exception_type": "raw secret text"}), ("OTHER", {})])
def test_only_the_three_literal_supported_reason_operands(reason, input_value):
    report, evidence, mapping = operands()
    evidence.update(reason=reason, input=input_value)
    with pytest.raises(ValueError):
        classify(report, [evidence], mapping)


def test_no_dispositions_preserves_all_original_failures():
    report, _, mapping = operands()
    result = classify(report, [], mapping)
    assert result["expected_individual_blocks"] == []
    assert len(result["new_gate_failures"]) == 34
    assert result["other_seed_limits_unchanged"] is True


def test_complete_run_guard_receives_witness_set_and_refuses_omitted_actual_exclusion():
    report, _, mapping = operands()
    observed = []
    def complete_run(actual_report, actual_witnesses):
        observed.append((actual_report, actual_witnesses))
        return {row["event_id"] for row in actual_witnesses} == {"0"}
    with pytest.raises(ValueError, match="source_report_unbound"):
        classify(report, [], mapping, report_guard=complete_run)
    assert observed == [(report, [])]
