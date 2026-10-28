"""CONTROLLED result bytes; not actual historical outcomes or117 qualification."""

from copy import deepcopy
import json

import pytest

from tools.l12_original122_branch_adapter import C11229_INPUT_LIMITED, compare_c11229_input_limited


def row(case_id):
    return dict(case_id=case_id, original_record_sha256="a" * 64, decision="UNKNOWN",
        permission_granted=False, callback="UNRUN_MISSING_RESTORED_OPERANDS", branch_value=None,
        native_asof="UNKNOWN_NOT_STORED", same_entry_opportunity="UNKNOWN",
        missing_operands=["EXACT_PER_CASE_RESTORED_STATE_REFERENCES_AND_READBACK_NOT_BOUND"])


def emit(value):
    return json.dumps(value, separators=(",", ":")).encode()


def operands():
    values = {case: emit(row(case)) for case in C11229_INPUT_LIMITED}
    return values, deepcopy(values)


def test_exact_five_equal_bytes_are_unknown_not_PASS_and_do_not_claim117_caused():
    old, new = operands()
    seen, before = [], deepcopy((old, new))
    def guarded(case, old_bytes, new_bytes):
        seen.append(case)
        return old_bytes == old[case] and new_bytes == new[case]
    result = compare_c11229_input_limited(old, new, ruling_sha256="b" * 64, case_guard=guarded)
    assert set(seen) == C11229_INPUT_LIMITED and len(seen) == 5
    assert result["input_limited_case_count"] == 5
    assert result["retained_original_denominator"] == 122
    assert result["remaining_original_cases"] == 117
    assert result["remaining_117_caused"] == "NOT_ESTABLISHED_BY_THIS_CHECK"
    assert result["permission_granted"] is False
    assert all(value["disposition"] == "UNKNOWN_INPUT_LIMITED" and value["byte_identical"] is True
        and value["predicate_executed"] is False for value in result["cases"])
    assert (old, new) == before


@pytest.mark.parametrize("fault", ["missing", "extra", "foreign", "not_bytes", "oversize", "zero_ruling"])
def test_exact_five_membership_and_actual_bounded_bytes_required(fault):
    old, new = operands()
    case = sorted(old)[0]
    ruling = "b" * 64
    if fault == "missing":
        del new[case]
    elif fault == "extra":
        new["OTHER"] = new[case]
    elif fault == "foreign":
        new["OTHER"] = new.pop(case)
    elif fault == "not_bytes":
        old[case] = new[case] = new[case].decode()
    elif fault == "oversize":
        old[case] = new[case] = b" " * 65537
    else:
        ruling = "0" * 64
    with pytest.raises(ValueError):
        compare_c11229_input_limited(old, new, ruling_sha256=ruling, case_guard=lambda *_: True)


@pytest.mark.parametrize("fault", ["whitespace", "outcome"])
def test_semantic_equality_does_not_replace_byte_equality(fault):
    old, new = operands()
    case = sorted(old)[0]
    if fault == "whitespace":
        new[case] += b"\n"
    else:
        changed = row(case)
        changed["missing_operands"] = ["OTHER"]
        new[case] = emit(changed)
    with pytest.raises(ValueError, match="NEW_differs"):
        compare_c11229_input_limited(old, new, ruling_sha256="b" * 64, case_guard=lambda *_: True)


@pytest.mark.parametrize("field,value", [("decision", "PASS"), ("permission_granted", True),
    ("branch_value", True), ("callback", "REAL_FIRST_REST_PREDICATE_EXECUTED_LOCAL_BRANCH_ONLY"),
    ("case_id", "OTHER"), ("original_record_sha256", "0" * 64), ("missing_operands", []),
    ("native_asof", "FLAT"), ("same_entry_opportunity", "PROVEN"), ("callback", "UNRUN_FAKE")])
def test_equal_fabricated_outcomes_are_still_refused(field, value):
    old, new = operands()
    case = sorted(old)[0]
    changed = row(case)
    changed[field] = value
    old[case] = new[case] = emit(changed)
    with pytest.raises(ValueError):
        compare_c11229_input_limited(old, new, ruling_sha256="b" * 64, case_guard=lambda *_: True)


@pytest.mark.parametrize("raw", [b'{"case_id":"a","case_id":"b"}', b'{"value":NaN}', b"\xff"])
def test_ambiguous_nonfinite_or_non_UTF8_results_refuse(raw):
    old, new = operands()
    old[sorted(old)[0]] = new[sorted(old)[0]] = raw
    with pytest.raises((ValueError, UnicodeError)):
        compare_c11229_input_limited(old, new, ruling_sha256="b" * 64, case_guard=lambda *_: True)


def test_equality_without_independent_source_and_original_record_acceptance_refuses():
    old, new = operands()
    with pytest.raises(ValueError, match="artifact_binding"):
        compare_c11229_input_limited(old, new, ruling_sha256="b" * 64, case_guard=lambda *_: False)
