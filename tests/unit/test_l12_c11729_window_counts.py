"""Exact C11729 counting kernel, CONTROLLED windows; no actual OLD log claim."""

from copy import deepcopy

import pytest

from tools.l12_recorded_boot_timeline import (
    FLEET_FILES, FLEET_NAMES, ConfirmationSeedTracker, RecordedSessionMap)


ANCHOR = 1793001600000
CONFIRM = ANCHOR + 3 * 3600000 + 1851000
OFFSET_NS = 259200000 * 1000000


def inputs():
    mapping = RecordedSessionMap(target_day="2026-10-26", ruling_sha256="a" * 64,
        chronology_review_sha256="b" * 64)
    records = []
    for identity, kind, stamp in (("1", "CONFIRM", CONFIRM), ("2", "FADE", CONFIRM + 1200000),
                                  ("3", "CONFIRM", CONFIRM + 1260000)):
        records.append(dict(event_id=identity, event_type=kind, symbol="YMAT",
            mapped_ms=stamp, mapped_ns=stamp * 1000000,
            original_ms=stamp - 259200000, original_ns=stamp * 1000000 - OFFSET_NS,
            raw_ref="CONTROLLED_SCANNER:" + identity))
    return dict(version="FRIDAY18_PER_CONFIRM_V1", names=list(FLEET_NAMES), source_sha256=dict(FLEET_FILES),
        mapping=mapping.receipt(), chronology={name: records if name == "YMAT" else [] for name in FLEET_NAMES},
        confirmations=[records[0], records[2]],
        first_confirm_by_name={name: records[0] if name == "YMAT" else None for name in FLEET_NAMES},
        missing_confirm_names=[name for name in FLEET_NAMES if name != "YMAT"])


def value():
    result = ConfirmationSeedTracker(inputs(), limit_bars=None, limit_metric=None, limit_review_sha256=None)
    result.bind_c11729(ruling_sha256="c" * 64, old_window_review=None)
    return result


def old_review(count=1, ready="READY"):
    fleet = inputs()
    records = fleet["chronology"]["YMAT"]
    windows = {}
    for record, end in ((records[0], records[1]["original_ns"]), (records[2], 1792800000000 * 1000000)):
        windows[record["event_id"]] = dict(original_confirm_ns=record["original_ns"], original_end_ns=end,
            original_first_ready_ns=record["original_ns"] + 9000000000 if ready == "READY" else None,
            not_ready_closed_minutes=count, ready_status=ready, raw_line_references=["CONTROLLED_LOG:1"])
    return dict(scope="INDEPENDENT_LIVE_OLD_LOG_PER_CONFIRM_WINDOWS", raw_log_sha256="d" * 64,
        review_sha256="e" * 64, scanner_sha256=FLEET_FILES["scanner_confirmed_events_2026-10-23.csv"], windows=windows)


def not_ready(result, number=0, *, clock_extra_ms=0):
    bar = CONFIRM // 60000 * 60000 + number * 60000
    result.observe_return_reason(symbol="YMAT", reason="track_line_not_ready",
        clock_ns=(bar + 63000 + clock_extra_ms) * 1000000, closed_bar_ms=bar)


def ready(result, now):
    result.observe(dict(status="OBSERVED_SEED_SAMPLE_NOT_FULL_TIMELINE_QUALIFICATION", observed_ms=now,
        symbols=[dict(symbol="YMAT", seed_complete=True, history_anchor_ms=ANCHOR,
            history_completed_ns=now * 1000000, line_buy_ready=True)]))


def test_unique_actual_closed_bar_returns_not_elapsed_time_or_quote_count():
    result = value()
    not_ready(result)
    not_ready(result, clock_extra_ms=1000)
    row = result.report()["per_confirm_windows"][0]
    assert row["actual_not_ready_closed_minutes"] == 1
    assert row["actual_returns"][0]["clock_ns"] == (CONFIRM // 60000 * 60000 + 63000) * 1000000
    assert row["outcome"] == "UNKNOWN_OLD_WINDOW_LOG_UNBOUND"


def test_midminute_confirm_excludes_preceding_closed_bar_only_not_later_minutes():
    result = value()
    bar = CONFIRM // 60000 * 60000 - 60000
    result.observe_return_reason(symbol="YMAT", reason="track_line_not_ready",
        clock_ns=(CONFIRM + 1000) * 1000000, closed_bar_ms=bar)
    not_ready(result)
    not_ready(result, 1)
    assert result.report()["per_confirm_windows"][0]["actual_not_ready_closed_minutes"] == 2


def test_any_new_window_over_ten_fails_even_with_OLD_log_missing():
    result = value()
    for index in range(11):
        not_ready(result, index)
    assert result.report()["per_confirm_windows"][0]["outcome"] == "FAIL_NEW_WINDOW_OVER_10"


def test_new_slower_than_same_OLD_window_fails_even_under_ten():
    result = value()
    result.bind_c11729(ruling_sha256="c" * 64, old_window_review=old_review(count=1))
    not_ready(result)
    not_ready(result, 1)
    assert result.report()["per_confirm_windows"][0]["outcome"] == "FAIL_NEW_SLOWER_THAN_OLD"


def test_equal_OLD_count_and_actual_line_ready_observation_still_not_install_permission():
    result = value()
    result.bind_c11729(ruling_sha256="c" * 64, old_window_review=old_review(count=1))
    not_ready(result)
    ready(result, CONFIRM + 9000)
    report = result.report()
    assert report["per_confirm_windows"][0]["outcome"] == "MEASURED_WITHIN_BOTH_LIMITS_NOT_INSTALL_GO"
    assert report["permission_granted"] is False and report["seed_denominator"] == 18


@pytest.mark.parametrize("status,outcome", [("NEVER_READY", "NEVER_READY_LIKE_FOR_LIKE_LABEL_NOT_AUTOMATIC_PASS"),
    ("UNKNOWN", "UNKNOWN_OLD_WINDOW_READY_UNPROVEN")])
def test_OLD_never_ready_or_unknown_is_not_silently_ready(status, outcome):
    result = value()
    result.bind_c11729(ruling_sha256="c" * 64, old_window_review=old_review(ready=status))
    assert result.report()["per_confirm_windows"][0]["outcome"] == outcome


@pytest.mark.parametrize("fault", ["whole_day", "missing_window", "window_end", "confirm", "bool_count", "ready_from_silence"])
def test_baseline_must_bind_every_exact_source_window_and_actual_ready_clock(fault):
    result, review = value(), deepcopy(old_review())
    if fault == "whole_day":
        review["scope"] = "WHOLE_DAY_YMAT37_BKYI12"
    elif fault == "missing_window":
        review["windows"].pop("3")
    elif fault == "window_end":
        review["windows"]["1"]["original_end_ns"] += 60000000000
    elif fault == "confirm":
        review["windows"]["1"]["original_confirm_ns"] += 1
    elif fault == "bool_count":
        review["windows"]["1"]["not_ready_closed_minutes"] = True
    else:
        review["windows"]["1"]["original_first_ready_ns"] = None
    with pytest.raises(ValueError):
        result.bind_c11729(ruling_sha256="c" * 64, old_window_review=review)


def test_other_return_reason_is_not_a_line_not_ready_count():
    result = value()
    result.observe_return_reason(symbol="YMAT", reason="track_entries_held",
        clock_ns=CONFIRM * 1000000, closed_bar_ms=CONFIRM // 60000 * 60000 - 60000)
    assert result.report()["per_confirm_windows"][0]["actual_not_ready_closed_minutes"] == 0


def test_no_count_after_FADE_and_readd_has_separate_window_identity():
    result = value()
    at = CONFIRM + 1230000
    bar = at // 60000 * 60000 - 60000
    result.observe_return_reason(symbol="YMAT", reason="track_line_not_ready", clock_ns=at * 1000000, closed_bar_ms=bar)
    assert all(row["actual_not_ready_closed_minutes"] == 0 for row in result.report()["per_confirm_windows"])
