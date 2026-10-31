"""CONTROLLED revised policy; no actual OLD parse, APP, PG, or fleet execution."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from tools.l12_recorded_boot_timeline import (
    C11810_CARRIED, C11810SeedTracker, FLEET_NAMES,
    SOURCE_ANCHOR_MS, SOURCE_END_NS, RecordedSessionMap,
    _aware_ms, _aware_ns, c11810_candidate_from_records, c11810_old_window_candidate,
    c11810_window_geometry, parse_old_log_lines,
)


def fleet():
    mapping = RecordedSessionMap(target_day="2026-10-26", ruling_sha256="a" * 64,
        chronology_review_sha256="b" * 64)
    history, chronology, confirmations = {}, {}, []
    for name in FLEET_NAMES:
        history[name] = [dict(stamp=SOURCE_ANCHOR_MS + 60000,
            raw_ref="CONTROLLED_BAR:" + name, raw_sha256="d" * 64)]
        events = []
        if name not in C11810_CARRIED:
            events.append(dict(event_id=name + ":CONFIRM", symbol=name, event_type="CONFIRM",
                original_ns=_aware_ns("2026-10-23T11:37:11+00:00"), raw_ref="CONTROLLED_CONFIRM:" + name))
        events.append(dict(event_id=name + ":FADE", symbol=name, event_type="FADE",
            original_ns=_aware_ns("2026-10-23T11:40:00+00:00"), raw_ref="CONTROLLED_FADE:" + name))
        if name == "YMAT":
            events.append(dict(event_id="YMAT:READD", symbol=name, event_type="CONFIRM",
                original_ns=_aware_ns("2026-10-23T12:05:00+00:00"), raw_ref="CONTROLLED_READD:YMAT"))
        chronology[name] = events
        confirmations.extend(row for row in events if row["event_type"] == "CONFIRM")
    return dict(names=list(FLEET_NAMES), chronology=chronology, confirmations=confirmations,
        file_rows=history, missing_confirm_names=sorted(C11810_CARRIED), mapping=mapping.receipt())


def line(clock, body):
    return f"2026-10-23 {clock} INFO controlled | {body}\n"


def returns(clock, reason, bar="2026-10-23T11:37:00+00:00"):
    return line(clock, "[V2-RESTING-RETURN] symbol=YMAT opportunity_id=0 "
        f"bar_ms={_aware_ms(bar)} slot=first reason={reason}")


def candidate(inputs=None, raw=""):
    return c11810_candidate_from_records(fleet() if inputs is None else inputs,
        parse_old_log_lines(raw.encode()), ruling_sha256="c" * 64)


def tracker(inputs=None, raw=""):
    inputs = fleet() if inputs is None else inputs
    value = candidate(inputs, raw)
    review = dict(version="C11810_INDEPENDENT_WINDOW_REVIEW_V1",
        candidate_sha256=sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        ruling_sha256="c" * 64, review_sha256="e" * 64)
    return C11810SeedTracker(inputs, value, policy_review=review)


def find(value, name="YMAT", identity=None):
    windows = value.windows if isinstance(value, C11810SeedTracker) else value["windows"]
    return next(row for row in windows if row["symbol"] == name
        and (identity is None or row["event_id"] == identity))


def observe(value, name, clock, *, seed=True, ready=True):
    at = value.mapping.market_ms(_aware_ms(clock))
    value.observe(dict(status="OBSERVED_SEED_SAMPLE_NOT_FULL_TIMELINE_QUALIFICATION", observed_ms=at,
        symbols=[dict(symbol=name, seed_complete=seed, line_buy_ready=ready,
            history_anchor_ms=value.mapping.market_ms(SOURCE_ANCHOR_MS), history_completed_ns=at * 1000000)]))


def reason(value, name, bar):
    original = _aware_ms(bar)
    at = value.mapping.market_ms(original)
    value.observe_return_reason(symbol=name, reason="track_line_not_ready",
        clock_ns=(at + 63000) * 1000000, closed_bar_ms=at)


def test_exact_seven_carried_are_boot_windows_not_invented_confirms():
    inputs = fleet()
    original = deepcopy(inputs["chronology"])
    value = candidate(inputs)
    carried = [row for row in value["windows"] if row["kind"] == "CARRIED_IN_BOOT"]
    assert {row["symbol"] for row in carried} == C11810_CARRIED
    assert all(row["original_ns"] == SOURCE_ANCHOR_MS * 1000000 for row in carried)
    assert inputs["chronology"] == original
    assert len(value["windows"]) == len(inputs["confirmations"]) + 7
    assert value["seed_denominator"] == 18 and value["permission_granted"] is False


def test_fade_does_not_cut_window_without_positive_old_drop():
    value = candidate()
    first = find(value)
    assert first["original_end_ns"] == _aware_ns("2026-10-23T12:05:00+00:00")
    assert first["recorded_fades"][0]["original_ns"] == _aware_ns("2026-10-23T11:40:00+00:00")
    assert first["old_actual_drop"] is None
    assert find(value, "AARD")["original_end_ns"] == SOURCE_END_NS


def test_complete_old_drop_records_both_fade_and_later_observed_drop_clocks():
    raw = line("11:37:11,688", "watchlist updated count=1 sample=YMAT warmed=1")
    raw += line("11:40:00,100", "watchlist updated count=0 sample= warmed=0")
    first = find(candidate(raw=raw))
    assert first["original_end_ns"] == _aware_ns("2026-10-23T11:40:00.100+00:00")
    assert first["uninterrupted_end_ns"] == _aware_ns("2026-10-23T12:05:00+00:00")
    assert first["old_actual_drop"]["present_line"] == 1
    assert first["old_actual_drop"]["absent_membership_record"]["count"] == 0


@pytest.mark.parametrize("prior,following", [
    ("count=0 sample= warmed=0", "count=0 sample= warmed=0"),
    ("count=6 sample=AARD,AIOK,AIXI,BKYI,BOLT warmed=6", "count=0 sample= warmed=0"),
    ("count=1 sample=YMAT warmed=1", "count=6 sample=AARD,AIOK,AIXI,BKYI,BOLT warmed=6"),
    ("count=1 sample=YMAT warmed=1", "count=1 sample=YMAT warmed=1"),
])
def test_absence_or_partial_sample_is_not_proof_of_actual_old_drop(prior, following):
    raw = line("11:37:11,688", "watchlist updated " + prior)
    raw += line("11:40:00,100", "watchlist updated " + following)
    assert find(candidate(raw=raw))["old_actual_drop"] is None


def test_cannot_search_past_first_contradictory_membership_for_passing_boundary():
    raw = line("11:37:11,688", "watchlist updated count=1 sample=YMAT warmed=1")
    raw += line("11:40:00,100", "watchlist updated count=1 sample=YMAT warmed=1")
    raw += line("11:41:00,100", "watchlist updated count=0 sample= warmed=0")
    assert find(candidate(raw=raw))["old_actual_drop"] is None


def test_no_ready_line_remains_unknown_not_ready_at_zero():
    old = find(candidate())["OLD"]
    assert old["not_ready_closed_minutes"] == 0
    assert old["first_observed_ready_return"] is None
    assert old["ready_status"] == "NO_OBSERVED_READY_UNKNOWN_NOT_ZERO_READY"


def test_old_return_after_fade_still_belongs_to_prior_window():
    value = candidate(raw=returns("11:45:01,005", "track_long_no_new_rest",
        "2026-10-23T11:44:00+00:00"))
    assert find(value)["OLD"]["first_observed_ready_return"]["ordinal"] == 1


def test_new_after_fade_return_counted_before_next_confirm_and_readd_separate():
    value = tracker()
    reason(value, "YMAT", "2026-10-23T11:44:00+00:00")
    reason(value, "YMAT", "2026-10-23T12:06:00+00:00")
    windows = value.report()["per_name"]["YMAT"]
    assert windows[0]["NEW_not_ready_closed_minutes"] == 1
    assert windows[1]["NEW_not_ready_closed_minutes"] == 1


def test_carried_first_bar_missing_is_unknown_not_default_04_or_zero():
    inputs = fleet()
    inputs["file_rows"]["AARD"] = []
    value = tracker(inputs)
    observe(value, "AARD", "2026-10-23T04:03:00+00:00")
    row = value.report()["per_name"]["AARD"][0]
    assert row["window"]["count_start_original_ns"] is None
    assert row["outcome"] == "UNKNOWN_FIRST_SESSION_BAR"


def test_carried_seed_elapsed_after_first_recorded_closed_bar_over_ten_fails():
    value = tracker()
    observe(value, "AARD", "2026-10-23T08:13:00+00:00")
    row = value.report()["per_name"]["AARD"][0]
    assert row["seed_closed_minutes_after_count_anchor"] == 11
    assert row["outcome"] == "FAIL_CARRIED_SEED_OVER_10_AFTER_FIRST_BAR"


def test_early_seed_before_first_bar_is_not_clamped_to_zero_minutes():
    value = tracker()
    observe(value, "AARD", "2026-10-23T08:00:00+00:00")
    row = value.report()["per_name"]["AARD"][0]
    assert row["seed_closed_minutes_after_count_anchor"] is None
    assert row["outcome"] == "UNKNOWN_SEED_BEFORE_FIRST_RECORDED_BAR_NOT_ZERO"


def test_new_over_ten_not_ready_is_fail_even_old_ready_unobserved():
    value = tracker()
    for index in range(11):
        stamp = value.mapping.market_ms(SOURCE_ANCHOR_MS + 60000 + index * 60000)
        value.observe_return_reason(symbol="AARD", reason="track_line_not_ready",
            clock_ns=(stamp + 63000) * 1000000, closed_bar_ms=stamp)
    assert value.report()["per_name"]["AARD"][0]["outcome"] == "FAIL_NEW_OVER_10"


def test_duplicate_same_closed_bar_not_counted_twice():
    value = tracker()
    reason(value, "YMAT", "2026-10-23T11:38:00+00:00")
    reason(value, "YMAT", "2026-10-23T11:38:00+00:00")
    assert value.report()["per_name"]["YMAT"][0]["NEW_not_ready_closed_minutes"] == 1


def test_policy_review_cannot_cover_different_candidate_bytes():
    value = tracker()
    changed = deepcopy(value.candidate)
    changed["windows"] = []
    with pytest.raises(ValueError, match="explicit_C11810_independent"):
        C11810SeedTracker(value.fleet, changed, policy_review=value.policy_review)


def test_reviewed_candidate_still_requires_every_name_and_window():
    value = tracker()
    changed = deepcopy(value.candidate)
    changed["windows"] = []
    review = deepcopy(value.policy_review)
    review["candidate_sha256"] = sha256(json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(ValueError, match="no_0_of_0"):
        C11810SeedTracker(value.fleet, changed, policy_review=review)


def test_actual_old_entry_refuses_controlled_text_without_claiming_it_is_delivery():
    with pytest.raises(ValueError, match="exact_delivered_OLD_log_SHA"):
        c11810_old_window_candidate(b"CONTROLLED", fleet(), ruling_sha256="c" * 64)
    assert candidate()["raw_log_sha256"] is None
    assert candidate()["record_origin"] == "CONTROLLED_GEOMETRY_NOT_VALIDATED_LOG_DELIVERY"


def test_seven_carried_membership_cannot_be_silently_changed():
    inputs = fleet()
    inputs["missing_confirm_names"] = []
    with pytest.raises(ValueError, match="seven_reviewed_carried"):
        c11810_window_geometry(inputs, [], ruling_sha256="c" * 64)


def test_positive_ready_witness_and_actual_new_ready_are_required_for_comparison():
    value = tracker(raw=returns("11:45:01,005", "track_long_no_new_rest",
        "2026-10-23T11:44:00+00:00"))
    observe(value, "YMAT", "2026-10-23T11:45:02+00:00")
    row = value.report()["per_name"]["YMAT"][0]
    assert row["outcome"] == "OBSERVED_WITHIN_LIMITS_NOT_INSTALL_PERMISSION"
    assert value.report()["permission_granted"] is False


def test_slower_than_old_fails_even_below_ten():
    value = tracker(raw=returns("11:38:02,000", "track_line_not_ready")
        + returns("11:42:02,000", "track_long_no_new_rest", "2026-10-23T11:41:00+00:00"))
    reason(value, "YMAT", "2026-10-23T11:38:00+00:00")
    reason(value, "YMAT", "2026-10-23T11:39:00+00:00")
    assert value.report()["per_name"]["YMAT"][0]["outcome"] == "FAIL_NEW_SLOWER_THAN_OLD"


def test_midminute_confirm_excludes_only_preceding_closed_minute():
    value = tracker()
    reason(value, "YMAT", "2026-10-23T11:36:00+00:00")
    reason(value, "YMAT", "2026-10-23T11:37:00+00:00")
    assert value.report()["per_name"]["YMAT"][0]["NEW_not_ready_closed_minutes"] == 1


def test_seed_complete_does_not_override_actual_line_not_ready():
    value = tracker()
    observe(value, "YMAT", "2026-10-23T11:45:02+00:00", ready=False)
    row = value.report()["per_name"]["YMAT"][0]
    assert row["first_seed"] is not None and row["first_ready"] is None
    assert row["outcome"] == "UNKNOWN_SEED_OR_READY_UNOBSERVED"


def test_extra_actual_observation_requires_review_not_population_shrink():
    value = tracker()
    with pytest.raises(ValueError, match="not_silently_dropped"):
        observe(value, "FOREIGN", "2026-10-23T11:45:02+00:00")
