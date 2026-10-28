"""First-flip diagnostic accounting only; no real restored boot permission."""

from copy import deepcopy

import pytest

from tools.l12_recorded_boot_driver import retain_first_flip


def observation():
    return dict(symbol="AAA", clock_ns=123, flip="BUY", returned_BUY_draft=False,
        actual_return_reasons=[dict(reason="retry_segment_mismatch")],
        actual_strategy_admissions=[dict(allowed=False, slot="first")],
        flip_owner_opportunity_id=1792742400000, status="NO_BUY_DRAFT_ACTUAL_GUARD_REASONS_OR_UNKNOWN")


def test_refused_first_flip_is_retained_without_inventing_age_or_permission():
    first, row = {"AAA": "UNKNOWN_NO_BUY_FLIP_OBSERVED"}, observation()
    before = deepcopy(row)
    retain_first_flip(first, row)
    assert first["AAA"]["returned_BUY_draft"] is False
    assert first["AAA"]["actual_return_reasons"] == [{"reason": "retry_segment_mismatch"}]
    assert first["AAA"]["native_admission"] == "UNKNOWN_NOT_INFERRED_FROM_STRATEGY_RETURN"
    assert first["AAA"]["durable_row_causal_attribution"].startswith("UNKNOWN_")
    assert first["AAA"]["permission_granted"] is False
    assert row == before


@pytest.mark.parametrize("later", ["successful_draft", "different_refusal", "raised"])
def test_later_success_refusal_or_error_cannot_replace_actual_first_flip(later):
    first = {"AAA": "UNKNOWN_NO_BUY_FLIP_OBSERVED"}
    retain_first_flip(first, observation())
    before = deepcopy(first)
    second = observation()
    second.update(clock_ns=124, status=later, returned_BUY_draft=later == "successful_draft")
    retain_first_flip(first, second)
    assert first == before


@pytest.mark.parametrize("signal", [None, "SELL", ""])
def test_absent_buy_signal_remains_unknown_not_zero_entry_block_PASS(signal):
    first, row = {"AAA": "UNKNOWN_NO_BUY_FLIP_OBSERVED"}, observation()
    row["flip"] = signal
    retain_first_flip(first, row)
    assert first["AAA"] == "UNKNOWN_NO_BUY_FLIP_OBSERVED"


def test_actual_evaluation_error_stays_error_not_entry_refusal():
    first, row = {"AAA": "UNKNOWN_NO_BUY_FLIP_OBSERVED"}, observation()
    row.update(status="ACTUAL_EVALUATION_RAISED_NOT_REFUSAL_OR_PASS", exception_type="ValueError")
    retain_first_flip(first, row)
    assert first["AAA"]["exception_type"] == "ValueError"
    assert first["AAA"]["status"] == "ACTUAL_EVALUATION_RAISED_NOT_REFUSAL_OR_PASS"


def test_foreign_symbol_not_silently_added_or_dropped_from_population():
    with pytest.raises(ValueError, match="outside_recorded_population"):
        retain_first_flip({"BBB": "UNKNOWN_NO_BUY_FLIP_OBSERVED"}, observation())


def test_saved_refusal_cannot_change_when_a_later_observation_mutates_its_lists():
    first, row = {"AAA": "UNKNOWN_NO_BUY_FLIP_OBSERVED"}, observation()
    retain_first_flip(first, row)
    row["actual_return_reasons"].clear()
    assert first["AAA"]["actual_return_reasons"] == [{"reason": "retry_segment_mismatch"}]
