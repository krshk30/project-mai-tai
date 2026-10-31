"""CONTROLLED source contracts only: no actual 14-day or native qualification.

All numeric windows below are explicitly synthetic candidate operands, NOT
selected operational thresholds. Actual-origin metadata tests exercise parsing
only; no fixture is a delivered historical sample or C1 approval receipt.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta
import json

import pytest

from ops.smartalert1.system import CLASSES, SCHEMA, evaluate_system
from ops.smartalert1.system_replay import replay_system

AS_OF = "2026-10-28T13:00:00+00:00"
START = "2026-10-14T13:01:00+00:00"
END = "2026-10-28T13:01:00+00:00"


def controlled_calendar(first, last, *, fall=False):
    """Explicit CONTROLLED calendar operands, not a fetched historical calendar."""
    rows = []
    day = first
    while day <= last:
        following = day + timedelta(days=1)
        offset = "-05:00" if fall and day >= date(2026, 11, 2) else "-04:00"
        next_offset = "-05:00" if fall and following >= date(2026, 11, 2) else "-04:00"
        rows.append(dict(day=day.isoformat(), start=day.isoformat() + "T00:00:00" + offset,
                         end=following.isoformat() + "T00:00:00" + next_offset))
        day = following
    return rows


def frame(class_name="B_service_down", reading=None):
    subject = dict(account=None, broker="system", bot=None, symbol=None,
                   service="oms-risk", subject_id="service:oms", execution_mode="live", manual=False)
    evidence = dict(schema_version=SCHEMA, source_id="controlled:health", source_kind="controlled_health",
        source_hash="a" * 64, source_epoch="controlled_epoch", observed_at=AS_OF,
        event_at=AS_OF, received_at=AS_OF, coverage_start="2026-10-28T12:59:00+00:00",
        coverage_end=AS_OF, complete=True, overflow=False, read_error=None,
        evidence_ids=["event:1"], subject=subject,
        payload={"checks": [dict(**{"class": class_name}, condition_epoch="transition:1",
            transition_evidence_id="event:1", first_seen=AS_OF, affirmative_refs=["event:1"],
            reading=reading if reading is not None else {"active": False})]})
    # Candidate values are arguments to the test, never production defaults.
    window = dict(max_age_seconds="30", max_sample_seconds="60", restart_count_limit=3,
        near_expiry_seconds="3600", max_silence_seconds="60", max_quote_age_seconds="5",
        max_tick_lag_seconds="1", max_sync_age_seconds="30", count_window_seconds="60", error_count_limit=0)
    expected = dict(subject, source_epoch="controlled_epoch", session="PM",
                    expected_running=True, work_expected=True, maintenance_active=False,
                    expectation_ref="controlled:historical_profile",
                    valid_from="2026-10-28T12:00:00+00:00", valid_until="2026-10-28T14:00:00+00:00")
    policy = dict(schema_version=SCHEMA, policyversion="controlled_candidates_v1", enabled=True,
        mode="CANDIDATE_REPLAY", qualified=False, approval_ref=None, calibration_ref="controlled:windows",
        windows={name: dict(window) for name in CLASSES},
        expectations={"version": "controlled_profile", "subjects": {"service:oms": expected},
            "calendar_ref": "controlled:calendar",
            "et_calendar": controlled_calendar(date(2026, 10, 14), date(2026, 10, 28))})
    return evidence, policy


def own(evidence, policy, class_name="B_service_down"):
    return next(f for f in evaluate_system(evidence, policy, as_of=AS_OF) if f["class"] == class_name)


READINGS = (
    ("B_service_down", {"active": False}, {"active": True}),
    ("B_restart_loop", {"before": 1, "after": 5, "sample_started_at": "2026-10-28T12:59:30+00:00", "sample_ended_at": AS_OF},
        {"before": 1, "after": 1, "sample_started_at": "2026-10-28T12:59:30+00:00", "sample_ended_at": AS_OF}),
    ("B_guard_STOP", {"stop_present": True, "guard_owns_live": True}, {"stop_present": False, "guard_owns_live": True}),
    ("B_token_dead_or_near_expiry", {"dead": True, "refresh_expires_at": None},
        {"dead": False, "refresh_expires_at": "2026-11-01T13:00:00+00:00"}),
    ("B_scanner_silent", {"progress_complete": True, "last_cycle_at": "2026-10-28T12:58:00+00:00"},
        {"progress_complete": True, "last_cycle_at": AS_OF}),
    ("B_quote_stale", {"quote_scope_complete": True, "subscribed": True, "quote_at": "2026-10-28T12:59:50+00:00"},
        {"quote_scope_complete": True, "subscribed": True, "quote_at": AS_OF}),
    ("B_OMS_stall", {"progress_complete": True, "tick_lag_seconds": "2", "sync_completed_at": AS_OF, "sync_outcome": "ok"},
        {"progress_complete": True, "tick_lag_seconds": "0", "sync_completed_at": AS_OF, "sync_outcome": "ok"}),
    ("B_DB_write_error", {"scope_complete": True, "error_count": 1, "error_kind": "db_write_failure", "window_start": "2026-10-28T12:59:00+00:00", "window_end": AS_OF},
        {"scope_complete": True, "error_count": 0, "error_kind": "none", "window_start": "2026-10-28T12:59:00+00:00", "window_end": AS_OF}),
    ("B_broker_API_error", {"scope_complete": True, "error_count": 1, "error_kind": "api_failure", "window_start": "2026-10-28T12:59:00+00:00", "window_end": AS_OF},
        {"scope_complete": True, "error_count": 1, "error_kind": "legal_refusal", "window_start": "2026-10-28T12:59:00+00:00", "window_end": AS_OF}),
    ("B_checker_blindness", {"source_readable": False, "proof_expired": False, "scope_complete": True},
        {"source_readable": True, "proof_expired": False, "scope_complete": True}),
)


@pytest.mark.parametrize("class_name,bad,good", READINGS, ids=[r[0] for r in READINGS])
def test_each_system_class_controlled_risk_and_affirmative_health(class_name, bad, good):
    evidence, policy = frame(class_name, bad)
    before = deepcopy((evidence, policy))
    finding = own(evidence, policy, class_name)
    assert finding["status"] == "TRUE" and not finding["paging_eligible"]
    assert finding["qualification"]["reason"] == "candidate_only_not_historical_qualification"
    assert (evidence, policy) == before
    evidence["payload"]["checks"][0]["reading"] = good
    assert own(evidence, policy, class_name)["status"] == "FALSE"


def test_off_and_default_off_do_not_consume_evidence():
    class Unreadable:
        def __iter__(self):
            raise AssertionError("OFF inspected input")
    assert evaluate_system(Unreadable(), {}, as_of="bad") == []
    assert evaluate_system(Unreadable(), {"enabled": True, "mode": "OFF"}, as_of="bad") == []


@pytest.mark.parametrize("mode", ["REPORT_ONLY", "QUALIFIED_LIVE"])
def test_unqualified_non_candidate_policy_never_selects_threshold_or_pages(mode):
    evidence, policy = frame()
    policy["mode"] = mode
    findings = evaluate_system(evidence, policy, as_of=AS_OF)
    assert own(evidence, policy)["status"] == "UNKNOWN"
    assert any(f["class"] == "B_checker_blindness" for f in findings)
    assert not any(f["paging_eligible"] for f in findings)


def test_qualified_live_authorization_operands_are_required_and_paper_manual_do_not_page():
    evidence, policy = frame()
    policy.update(mode="QUALIFIED_LIVE", qualified=True,
                  approval_ref="controlled:approval_shape_only", calibration_ref="controlled:calibration_shape_only")
    assert own(evidence, policy)["paging_eligible"]
    for field, value in (("manual", True), ("execution_mode", "paper")):
        copy = deepcopy(evidence)
        copy["subject"][field] = value
        expectation = deepcopy(policy)
        expectation["expectations"]["subjects"]["service:oms"][field] = value
        if field == "manual":
            expectation["expectations"]["subjects"]["service:oms"]["manual_proof_refs"] = ["event:1"]
        assert not own(copy, expectation)["paging_eligible"]
    policy["approval_ref"] = None
    assert own(evidence, policy)["status"] == "UNKNOWN"


@pytest.mark.parametrize("field,value", [
    ("complete", False), ("complete", "true"), ("overflow", True), ("read_error", "unreadable"),
    ("observed_at", "2026-10-28T13:00:01+00:00"), ("event_at", "2026-10-28T13:00:01+00:00"),
    ("observed_at", "2026-10-28T13:00:00"), ("source_hash", "not_a_hash"),
    ("evidence_ids", ["event:1", "event:1"]), ("evidence_ids", [str(n) for n in range(65)]),
])
def test_malformed_or_partial_envelope_cannot_prove_health(field, value):
    evidence, policy = frame(reading={"active": True})
    evidence[field] = value
    findings = evaluate_system(evidence, policy, as_of=AS_OF)
    assert findings and all(f["status"] == "UNKNOWN" for f in findings)
    assert all(not f["paging_eligible"] for f in findings)


def test_expired_outer_proof_never_clears_and_does_not_renew_inner_timestamp():
    evidence, policy = frame(reading={"active": True})
    findings = evaluate_system(evidence, policy, as_of="2026-10-28T13:00:31+00:00")
    finding = next(f for f in findings if f["class"] == "B_service_down")
    assert finding["status"] == "UNKNOWN" and finding["freshness"]["status"] == "STALE"
    assert finding["last_seen"] == AS_OF and finding["first_seen"] is None


@pytest.mark.parametrize("mutation", ["no_transition", "no_first_seen", "future_first_seen", "no_affirmative", "wrong_epoch", "wrong_broker"])
def test_identity_and_recovery_require_actual_causal_operands(mutation):
    evidence, policy = frame(reading={"active": True})
    check = evidence["payload"]["checks"][0]
    if mutation == "no_transition":
        check["transition_evidence_id"] = "other"
    elif mutation == "no_first_seen":
        check["first_seen"] = None
    elif mutation == "future_first_seen":
        check["first_seen"] = "2026-10-28T13:00:01+00:00"
    elif mutation == "no_affirmative":
        check["affirmative_refs"] = []
    elif mutation == "wrong_epoch":
        policy["expectations"]["subjects"]["service:oms"]["source_epoch"] = "foreign"
    else:
        policy["expectations"]["subjects"]["service:oms"]["broker"] = "webull"
    assert all(f["status"] == "UNKNOWN" for f in evaluate_system(evidence, policy, as_of=AS_OF))


@pytest.mark.parametrize("field", ["expected_running", "maintenance_active"])
def test_positive_schedule_or_maintenance_can_make_clean_stop_expected(field):
    evidence, policy = frame()
    policy["expectations"]["subjects"]["service:oms"][field] = field == "maintenance_active"
    assert own(evidence, policy)["status"] == "FALSE"


def test_quiet_scanner_is_not_derived_from_no_confirmations_and_heartbeat_is_not_oms_progress():
    evidence, policy = frame("B_scanner_silent", {"confirmed_events": 0})
    assert own(evidence, policy, "B_scanner_silent")["status"] == "UNKNOWN"
    evidence, policy = frame("B_OMS_stall", {"heartbeat_healthy": True})
    assert own(evidence, policy, "B_OMS_stall")["status"] == "UNKNOWN"


@pytest.mark.parametrize("class_name,reading", [
    ("B_restart_loop", {"before": 5, "after": 0, "sample_started_at": "2026-10-28T12:59:30+00:00", "sample_ended_at": AS_OF}),
    ("B_service_down", {"active": "false"}),
    ("B_quote_stale", {"quote_scope_complete": True, "subscribed": True, "quote_at": "2026-10-28T13:00:01+00:00"}),
    ("B_OMS_stall", {"progress_complete": True, "tick_lag_seconds": "NaN", "sync_completed_at": AS_OF, "sync_outcome": "ok"}),
])
def test_counter_reset_future_quote_and_nonfinite_values_are_unknown(class_name, reading):
    evidence, policy = frame(class_name, reading)
    assert own(evidence, policy, class_name)["status"] == "UNKNOWN"


def test_repeated_evaluation_retains_epoch_first_seen_and_records_only_verified_action():
    evidence, policy = frame()
    check = evidence["payload"]["checks"][0]
    check["action"] = {"code": "existing_retry_logged", "evidence_ids": ["event:1"], "observed_at": AS_OF}
    first = own(evidence, policy)
    again = next(f for f in evaluate_system(evidence, policy, as_of="2026-10-28T13:00:01+00:00") if f["class"] == "B_service_down")
    assert first["incident_key"] == again["incident_key"]
    assert first["first_seen"] == again["first_seen"] == AS_OF
    assert again["last_seen"] == AS_OF and again["action_taken"]["code"] == "existing_retry_logged"
    check["action"]["evidence_ids"] = ["foreign"]
    assert own(evidence, policy)["action_taken"] == "UNKNOWN"


def test_resource_overflow_refuses_without_trim_and_unknown_does_not_echo_private_payload():
    evidence, policy = frame()
    evidence["payload"]["untrusted"] = "x" * 1048576
    findings = evaluate_system(evidence, policy, as_of=AS_OF)
    assert len(findings) == 1 and findings[0]["reason_code"] == "frame_overflow"
    assert len(json.dumps(findings)) < 524288
    evidence, policy = frame()
    policy["calibration_ref"] = "https://private.example/topic"
    output = json.dumps(evaluate_system(evidence, policy, as_of=AS_OF))
    assert "private.example" not in output


def observation(evidence, origin="CONTROLLED"):
    return dict(record_type="observation", origin=origin, as_of=AS_OF, evidence=evidence)


def summary(result, class_name="B_service_down"):
    return next(c for c in result["classes"] if c["class"] == class_name)


def test_replay_exact_336h_partial_et_days_and_controlled_samples_insufficient():
    evidence, policy = frame()
    result = replay_system([observation(evidence)], policy, window_start=START, window_end=END)
    item = summary(result)
    assert item["counts"] == {"TRUE": 1, "FALSE": 0, "UNKNOWN": 0}
    assert item["would_page_incidents"] == 1 and item["controlled_observations"] == 1
    assert item["calibration"] is None and item["sample_status"] == "INSUFFICIENT"
    assert not result["paging_eligible"] and not result["classifications_use_labels"]
    assert result["et_buckets"][0]["partial"] and result["et_buckets"][-1]["partial"]
    elapsed = sum((datetime.fromisoformat(d["end"]) - datetime.fromisoformat(d["start"])).total_seconds()
                  for d in result["et_buckets"])
    assert elapsed == 336 * 3600


def test_replay_labels_never_change_earlier_verdict_and_wrong_refs_do_not_match():
    evidence, policy = frame()
    finding = own(evidence, policy)
    label = dict(record_type="label", origin="ACTUAL_RETAINED", incident_key=finding["incident_key"],
        status="FALSE", evidence_ids=["other"], label_asof="2026-10-28T13:00:30+00:00",
        adjudicated_by="controlled:reviewer_shape_only", actionability="informational")
    # A single actual-origin grammar control, NOT an actual historical sample.
    result = replay_system([observation(evidence, "ACTUAL_RETAINED"), label], policy,
                           window_start=START, window_end=END)
    assert summary(result)["counts"]["TRUE"] == 1
    assert summary(result)["label_missing"] == 1 and summary(result)["calibration"] is None
    label["evidence_ids"] = ["event:1"]
    matched = replay_system([observation(evidence, "ACTUAL_RETAINED"), label], policy,
                            window_start=START, window_end=END)
    assert summary(matched)["counts"]["TRUE"] == 1
    assert summary(matched)["calibration"] == {"TP": 0, "FP": 1, "TN": 0, "FN": 0}
    assert summary(matched)["sample_status"] == "INSUFFICIENT"


def test_replay_no_coverage_or_controlled_coverage_cannot_claim_14days():
    evidence, policy = frame()
    coverage = dict(record_type="coverage", origin="CONTROLLED", **{"class": "B_service_down"},
                    start=START, end=END, complete=True, overflow=False, evidence_ids=["controlled:coverage"])
    result = replay_system([coverage, observation(evidence)], policy, window_start=START, window_end=END)
    assert summary(result)["coverage"] == "INSUFFICIENT"
    assert summary(result)["incomplete_coverage_records"] == 1
    assert all(c["sample_status"] == "INSUFFICIENT" for c in result["classes"])


@pytest.mark.parametrize("failure", ["short_window", "future_observation", "future_label", "unordered", "duplicate_label"])
def test_replay_refuses_unbound_clocks_or_duplicate_adjudication(failure):
    evidence, policy = frame()
    record = observation(evidence)
    records = [record]
    end = END
    if failure == "short_window":
        end = AS_OF
    elif failure == "future_observation":
        record["as_of"] = "2026-10-28T13:02:00+00:00"
    elif failure == "unordered":
        earlier = deepcopy(record)
        earlier["as_of"] = "2026-10-28T12:59:59+00:00"
        records.append(earlier)
    else:
        label = dict(record_type="label", origin="CONTROLLED", incident_key=own(evidence, policy)["incident_key"],
            status="TRUE", evidence_ids=["event:1"], label_asof="2026-10-28T13:02:00+00:00" if failure == "future_label" else AS_OF,
            adjudicated_by="controlled:reviewer", actionability="actionable")
        records.append(label)
        if failure == "duplicate_label":
            records.append(deepcopy(label))
    result = replay_system(records, policy, window_start=START, window_end=end)
    assert result["status"] == "UNKNOWN" and not result["coverage_complete"]


def test_replay_record_cap_explicit_and_dst_window_not_14_dates():
    evidence, policy = frame()
    small_record = dict(record_type="coverage", origin="CONTROLLED", **{"class": "B_service_down"},
        start=START, end=END, complete=True, overflow=False, evidence_ids=["controlled:coverage"])
    overflow = replay_system((small_record for _ in range(1001)), policy, window_start=START, window_end=END)
    assert overflow["status"] == "UNKNOWN" and overflow["reason_code"] == "replay_frame_record_overflow"
    policy["expectations"]["et_calendar"] = controlled_calendar(date(2026, 10, 29), date(2026, 11, 12), fall=True)
    dst = replay_system([], policy, window_start="2026-10-29T13:00:00+00:00", window_end="2026-11-12T13:00:00+00:00")
    assert dst["elapsed_hours"] == 336
    spans = [(datetime.fromisoformat(d["end"]) - datetime.fromisoformat(d["start"])).total_seconds()
             for d in dst["et_buckets"]]
    assert 25 * 3600 in spans and sum(spans) == 336 * 3600


@pytest.mark.parametrize("mutation", ["absent", "gap", "offset_overlap"])
def test_calendar_is_a_bound_operand_not_a_guessed_timezone(mutation):
    _, policy = frame()
    if mutation == "absent":
        policy["expectations"].pop("et_calendar")
    elif mutation == "gap":
        policy["expectations"]["et_calendar"].pop(2)
    else:
        policy["expectations"]["et_calendar"][1]["start"] = "2026-10-15T00:00:00-05:00"
    result = replay_system([], policy, window_start=START, window_end=END)
    assert result["status"] == "UNKNOWN" and result["qualification"] == "INSUFFICIENT"


def test_on_evaluator_and_replayer_do_not_read_files_or_wallclock(monkeypatch):
    evidence, policy = frame()

    def forbidden(*args, **kwargs):
        raise AssertionError("pure source function attempted IO or wallclock")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("time.time", forbidden)
    assert own(evidence, policy)["status"] == "TRUE"
    result = replay_system([observation(evidence)], policy, window_start=START, window_end=END)
    assert summary(result)["counts"]["TRUE"] == 1 and not result["paging_eligible"]


@pytest.mark.parametrize("field,value", [("valid_until", AS_OF),
    ("valid_from", "2026-10-28T13:00:01+00:00")])
def test_stale_or_future_schedule_cannot_suppress_a_service_failure(field, value):
    evidence, policy = frame()
    expected = policy["expectations"]["subjects"]["service:oms"]
    expected.update(maintenance_active=True)
    expected[field] = value
    findings = evaluate_system(evidence, policy, as_of=AS_OF)
    assert all(f["status"] == "UNKNOWN" for f in findings)
    assert not any(f["status"] == "FALSE" for f in findings)


def canonical_frame_bytes(policy, records):
    return len(json.dumps({"policy": policy, "records": records}, allow_nan=False,
                          ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def test_aggregate_two_valid_records_overflow_before_second_evaluation(monkeypatch):
    from ops.smartalert1 import system_replay as replay_module

    evidence, policy = frame()
    evidence["payload"]["padding"] = "x" * 600000
    record = observation(evidence)
    assert canonical_frame_bytes(policy, [record]) < 1048576
    assert canonical_frame_bytes(policy, [record, record]) > 1048576
    calls = []
    actual = replay_module.evaluate_system

    def counted(*args, **kwargs):
        calls.append(1)
        return actual(*args, **kwargs)

    def supplied_once():
        yield record
        yield deepcopy(record)
        raise AssertionError("overflow consumed beyond the offending second record")

    monkeypatch.setattr(replay_module, "evaluate_system", counted)
    result = replay_module.replay_system(supplied_once(), policy, window_start=START, window_end=END)
    assert calls == [1]
    assert result["status"] == "UNKNOWN" and result["reason_code"] == "replay_frame_byte_overflow"
    assert result["records_seen"] == 2 and result["qualification"] == "INSUFFICIENT"
    assert not result["coverage_complete"] and "classes" not in result


@pytest.mark.parametrize("spare", [0, 1])
def test_aggregate_exact_boundary_includes_policy_and_json_shell(spare):
    evidence, policy = frame()
    evidence["payload"]["padding"] = ""
    record = observation(evidence)
    evidence["payload"]["padding"] = "x" * (1048576 - spare - canonical_frame_bytes(policy, [record]))
    assert canonical_frame_bytes(policy, [record]) == 1048576 - spare
    result = replay_system(iter([record]), policy, window_start=START, window_end=END)
    assert result["status"] == "REPLAY_RECORDED_NOT_QUALIFICATION"
    assert result["records_seen"] == 1 and result["input_frame_bytes"] == 1048576 - spare
    assert summary(result)["counts"]["TRUE"] == 1 and not result["paging_eligible"]


def test_policy_byte_reservation_refuses_before_touching_iterator():
    _, policy = frame()
    policy["padding"] = ""
    policy_bytes = len(json.dumps(policy, allow_nan=False, ensure_ascii=True,
                                 separators=(",", ":")).encode("utf-8"))
    policy["padding"] = "x" * (1048576 - policy_bytes)
    assert canonical_frame_bytes(policy, []) > 1048576

    class Unconsumed:
        def __iter__(self):
            raise AssertionError("oversized policy frame consumed records")

    result = replay_system(Unconsumed(), policy, window_start=START, window_end=END)
    assert result["status"] == "UNKNOWN" and result["reason_code"] == "replay_frame_byte_overflow"
    assert result["records_seen"] == 0


def test_finite_generator_is_consumed_once_with_exact_separator_bytes():
    evidence, policy = frame()
    records = [observation(evidence) for _ in range(3)]

    class Once:
        def __init__(self):
            self.iterations = 0
            self.index = 0

        def __iter__(self):
            self.iterations += 1
            assert self.iterations == 1
            return self

        def __next__(self):
            if self.index == len(records):
                raise StopIteration
            value = records[self.index]
            self.index += 1
            return value

    source = Once()
    result = replay_system(source, policy, window_start=START, window_end=END)
    assert source.iterations == 1 and source.index == 3
    assert result["records_seen"] == 3 and result["input_frame_bytes"] == canonical_frame_bytes(policy, records)
    assert summary(result)["counts"]["TRUE"] == 3
