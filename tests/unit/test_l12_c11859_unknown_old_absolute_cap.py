"""C1 explicit1859 criterion; CONTROLLED not actual fleet or boot execution."""

import pytest

from tests.unit.test_l12_c11810_carried_windows import observe, returns, tracker
from tools.l12_recorded_boot_timeline import SOURCE_ANCHOR_MS


def reasons(value, name, count):
    first = value.mapping.market_ms(SOURCE_ANCHOR_MS + (3 * 60 + 37) * 60000)
    for index in range(count):
        bar = first + index * 60000
        value.observe_return_reason(symbol=name, reason="track_line_not_ready",
            clock_ns=(bar + 63000) * 1000000, closed_bar_ms=bar)


@pytest.mark.parametrize("count", [0, 1, 10, 11])
def test_unknown_old_zero_is_not_a_limit_but_absolute_ten_still_is(count):
    value = tracker()
    reasons(value, "YMAT", count)
    observe(value, "YMAT", "2026-10-23T11:59:00+00:00")
    row = value.report()["per_name"]["YMAT"][0]
    assert row["window"]["OLD"]["not_ready_closed_minutes"] == 0
    assert row["applied_not_ready_limit"] == 10
    assert row["OLD_comparison"] == "UNKNOWN_NO_POSITIVE_OLD_READY_WITNESS"
    assert row["outcome"] == ("FAIL_NEW_OVER_10" if count == 11 else
        "OBSERVED_WITHIN_ABSOLUTE_CAP_OLD_COMPARISON_UNKNOWN_NOT_INSTALL_PERMISSION")


def test_unknown_old_nonzero_count_also_does_not_become_a_comparison_limit():
    value = tracker(raw=returns("11:38:02,000", "track_line_not_ready"))
    reasons(value, "YMAT", 3)
    observe(value, "YMAT", "2026-10-23T11:59:00+00:00")
    row = value.report()["per_name"]["YMAT"][0]
    assert row["window"]["OLD"]["not_ready_closed_minutes"] == 1
    assert row["outcome"] == "OBSERVED_WITHIN_ABSOLUTE_CAP_OLD_COMPARISON_UNKNOWN_NOT_INSTALL_PERMISSION"


@pytest.mark.parametrize("count,failed", [(0, False), (1, True)])
def test_positive_old_ready_with_zero_count_still_enforces_actual_zero(count, failed):
    value = tracker(raw=returns("11:42:02,000", "track_long_no_new_rest", "2026-10-23T11:41:00+00:00"))
    reasons(value, "YMAT", count)
    observe(value, "YMAT", "2026-10-23T11:59:00+00:00")
    row = value.report()["per_name"]["YMAT"][0]
    assert row["applied_not_ready_limit"] == 0
    assert row["outcome"] == ("FAIL_NEW_SLOWER_THAN_OLD" if failed else
        "OBSERVED_WITHIN_LIMITS_NOT_INSTALL_PERMISSION")


def test_missing_new_seed_ready_does_not_pass_from_zero_count_below_absolute_cap():
    value = tracker()
    row = value.report()["per_name"]["YMAT"][0]
    assert row["NEW_not_ready_closed_minutes"] == 0
    assert row["outcome"] == "UNKNOWN_SEED_OR_READY_UNOBSERVED"
    assert row["window"]["event_id"] in value.report()["new_gate_unresolved"]


def test_ymat_bolt_context_is_not_used_as_a_favorable_limit():
    value = tracker()
    reasons(value, "YMAT", 3)
    reasons(value, "BOLT", 2)
    report = value.report()
    context = report["count_context_not_gate"]
    assert context["YMAT"]["OLD_C11859_context_minutes"] == 30
    assert context["BOLT"]["OLD_C11859_context_minutes"] == 10
    assert context["YMAT"]["NEW_sum_window_not_ready_minutes"] == 3
    assert context["BOLT"]["NEW_sum_window_not_ready_minutes"] == 2
    assert report["hard_cap_closed_minutes"] == 10
    assert report["permission_granted"] is False


def test_every_window_is_retained_with_positive_and_unknown_old_counts_partitioned():
    value = tracker(raw=returns("11:42:02,000", "track_long_no_new_rest", "2026-10-23T11:41:00+00:00"))
    report = value.report()
    assert report["window_denominator"] == len(value.candidate["windows"])
    assert report["positive_OLD_comparison_windows"] == 1
    assert report["UNKNOWN_OLD_comparison_windows"] == report["window_denominator"] - 1
    assert report["seed_denominator"] == 18
    assert report["version"] == "C11859_RETIMED_BOOT_WINDOWS_V3"
