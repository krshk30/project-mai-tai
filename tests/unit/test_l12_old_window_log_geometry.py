"""CONTROLLED OLD text geometry; no private DB, actual replay, or native claims."""

import pytest

from tools.l12_recorded_boot_timeline import (
    _aware_ns, old_window_candidates_from_records, parse_old_log_lines,
    split_delivered_old_fleet_log,
)


def line(clock, body):
    return f"2026-10-23 {clock} INFO controlled | {body}\n"


def returned(clock, reason, bar=1792755420000):
    return line(clock, "[V2-RESTING-RETURN] symbol=YMAT opportunity_id=0 "
        f"bar_ms={bar} slot=first reason={reason}")


def fleet():
    confirm = dict(event_id="CONTROLLED:1", symbol="YMAT", event_type="CONFIRM",
        original_ns=_aware_ns("2026-10-23T11:37:11+00:00"))
    fade = dict(event_id="CONTROLLED:2", symbol="YMAT", event_type="FADE",
        original_ns=_aware_ns("2026-10-23T12:00:00+00:00"))
    return dict(confirmations=[confirm], chronology={"YMAT": [confirm, fade]},
        missing_confirm_names=["AARD"])


def result(text):
    return old_window_candidates_from_records(parse_old_log_lines(text.encode()), fleet(),
        review_sha256="a" * 64)


def window(value):
    return value["baseline_candidate"]["windows"]["CONTROLLED:1"]


def test_empty_roll_and_scanner_confirm_do_not_create_membership_or_readiness():
    value = result(line("08:00:04,633", "watchlist updated count=0 sample= warmed=0"))
    assert value["scanner_denominator"] == 18
    assert value["details"][0]["actual_V2_membership"].startswith("UNKNOWN")
    assert value["details"][0]["scanner_CONFIRM_is_not_V2_membership"] is True
    assert window(value)["ready_status"] == "UNKNOWN"
    assert window(value)["original_first_ready_ns"] is None
    assert window(value)["not_ready_closed_minutes"] == 0
    assert value["permission_granted"] is False


def test_prior_boot_population_is_retained_not_current_fleet_membership():
    value = result(line("05:46:38,265", "watchlist updated count=3 sample=DKI,VIVK,YHC warmed=0"))
    assert len(value["prior_session_context"]) == 1
    assert value["prior_session_context"][0]["sampled_names"] == ["DKI", "VIVK", "YHC"]
    assert value["details"][0]["actual_V2_membership"].startswith("UNKNOWN")


@pytest.mark.parametrize("reason", ["rearm_no_pending_episode", "track_line_not_ready"])
def test_not_ready_and_rearm_are_not_positive_readiness_witnesses(reason):
    value = result(returned("11:38:02,752", reason))
    assert window(value)["ready_status"] == "UNKNOWN"
    assert window(value)["not_ready_closed_minutes"] == int(reason == "track_line_not_ready")


def test_duplicate_returns_count_one_closed_bar_not_two_quotes():
    value = result(returned("11:38:02,752", "track_line_not_ready")
        + returned("11:38:03,000", "track_line_not_ready"))
    assert window(value)["not_ready_closed_minutes"] == 1
    assert value["details"][0]["not_ready_records"][0]["ordinal"] == 1


def test_positive_return_carries_exact_clock_and_raw_pointer_not_last_not_ready():
    value = result(returned("11:38:02,752", "track_line_not_ready")
        + returned("11:45:01,005", "track_long_no_new_rest", 1792755840000))
    assert window(value)["ready_status"] == "READY"
    assert window(value)["original_first_ready_ns"] == _aware_ns("2026-10-23T11:45:01.005+00:00")
    assert window(value)["raw_line_references"][-1].endswith(":line:2")
    assert value["native_availability"] == "UNKNOWN"


def test_positive_membership_is_separate_from_line_readiness():
    value = result(line("11:37:11,688", "watchlist updated count=1 sample=YMAT warmed=1"))
    assert value["details"][0]["actual_V2_membership"] == "OBSERVED_V2_SAMPLE_MEMBER"
    assert window(value)["ready_status"] == "UNKNOWN"


def test_truncated_membership_sample_absence_is_not_exclusion_proof():
    value = result(line("11:37:11,688", "watchlist updated count=6 sample=AARD,AIOK,AIXI,BKYI,BOLT warmed=6"))
    assert value["membership_records"][0]["sample_is_complete"] is False
    assert value["details"][0]["actual_V2_membership"].startswith("UNKNOWN")


def test_positive_return_after_window_is_not_used_for_prior_confirm():
    value = result(returned("12:01:02,000", "track_long_no_new_rest", 1792756800000))
    assert window(value)["ready_status"] == "UNKNOWN"


def test_readd_has_own_baseline_not_whole_day_or_previous_ready():
    inputs = fleet()
    inputs["chronology"]["YMAT"].append(dict(event_id="CONTROLLED:3", symbol="YMAT",
        event_type="CONFIRM", original_ns=_aware_ns("2026-10-23T12:05:00+00:00")))
    inputs["confirmations"].append(inputs["chronology"]["YMAT"][-1])
    records = parse_old_log_lines(returned("11:45:01,005", "track_long_no_new_rest",
        1792755840000).encode())
    value = old_window_candidates_from_records(records, inputs, review_sha256="a" * 64)
    assert window(value)["ready_status"] == "READY"
    later = value["baseline_candidate"]["windows"]["CONTROLLED:3"]
    assert later["ready_status"] == "UNKNOWN"
    assert later["not_ready_closed_minutes"] == 0


def test_midminute_confirm_does_not_count_preceding_closed_minute():
    value = result(returned("11:37:20,000", "track_line_not_ready", 1792755360000))
    assert window(value)["not_ready_closed_minutes"] == 0
    assert window(value)["ready_status"] == "UNKNOWN"


@pytest.mark.parametrize("body", [
    "[V2-RESTING-RETURN] broken",
    "watchlist updated count=2 sample=YMAT warmed=1",
    "watchlist updated count=2 sample=YMAT,YMAT warmed=1",
])
def test_malformed_known_records_stop_instead_of_silent_omission(body):
    with pytest.raises(ValueError):
        parse_old_log_lines(line("11:38:02,000", body).encode())


def test_unclosed_bar_refuses_comparison():
    with pytest.raises(ValueError, match="OLD_closed_bar_clock_contradiction"):
        result(returned("11:38:02,000", "track_line_not_ready", 1792755480000))


def test_actual_delivery_entry_rejects_controlled_bytes_without_parsing_as_actual():
    with pytest.raises(ValueError, match="exact_delivered_OLD_log_SHA_required"):
        split_delivered_old_fleet_log(b"CONTROLLED not actual", fleet(), review_sha256="a" * 64)
