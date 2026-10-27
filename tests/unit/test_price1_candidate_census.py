"""Census arithmetic/contract controls, synthetic inputs only. Author UNRUN."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from tools import price1_candidate_counts as census
from tools.price1_candidate_compare import compare


START = 1791793200000000000 + 9 * 86400 * 1000000000  # Synthetic 10-21 04:20 ET, NOT retained delivery.
HEAD = "a" * 40


def header():
    return {"contract": "PRICE1-CANDIDATES-v4", "session_date": "2026-10-21",
        "initial_state": "empty_before_session_prepare", "prior_closes": {"TEST": ".90"},
        "condition_version": "synthetic-control", "coverage_started_ms": START // 1000000 - 60000,
        "owner_cap": 16, "max_window_symbols": 20000,
        "complete_for_both_owner_sets": False, "provenance": "SYNTHETIC unit control, NOT a capture",
        "clock_basis": {"kind": "declared_counterfactual", "contract_id": "SYNTHETIC-CONTROL-v1",
            "assignment_rule": "fixed test fixture clocks only", "delivery_scope": "synthetic unit rows only"},
        "study_start_ns": START, "study_end_ns": START + 3 * census.MINUTE_NS,
        "validation_window": {"session_date": None, "gateway_end": "18:00",
                              "detection_end": "18:00", "guard_end": "18:10"}}


def write_input(tmp_path, rows=()):
    path = tmp_path / "synthetic.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in (header(), *rows)) + "\n")
    return path, sha256(path.read_bytes()).hexdigest()


def result(tmp_path):
    path, digest = write_input(tmp_path)
    return census.summarize(path, declared_head=HEAD, expected_input_sha256=digest)


@pytest.mark.parametrize("offset,expected", [(-1, "2026-10-21T08:19Z"),
    (0, "2026-10-21T08:20Z"), (1, "2026-10-21T08:20Z")])
def test_integer_minute_boundary_never_rounds_last_ns_forward(offset, expected):
    assert census.minute_key(START + offset) == expected


@pytest.mark.parametrize("value", [-1, True, 1.0, None])
def test_observation_time_requires_real_integer_ns(value):
    with pytest.raises(ValueError, match="integer"):
        census.minute_key(value)


def test_missing_minute_is_unknown_while_observed_zero_is_a_real_sample():
    minutes = {}
    census.sample_minute(minutes, observed=START, candidates={}, desired=set(), active=set())
    grid = census.include_missing_minutes(minutes, START, START + 3 * census.MINUTE_NS)
    assert len(grid) == 3
    assert grid[census.minute_key(START)]["candidate_max"] == 0
    assert grid[census.minute_key(START)]["samples"] == 1
    assert grid[census.minute_key(START + census.MINUTE_NS)] is None
    assert grid[census.minute_key(START + 2 * census.MINUTE_NS)] is None


@pytest.mark.parametrize("reason,expected", [("warning_cap_full", 1),
    ("warning_owner_cap_full", 1), ("drop_cooldown", 0)])
def test_both_actual_cap_refusal_reasons_are_visible_per_minute(reason, expected):
    minutes = {}
    census.sample_minute(minutes, observed=START, candidates={"A": None}, desired={"A", "B"},
        active={"B"}, outcome=SimpleNamespace(status="REFUSED", reason=reason))
    sample = minutes[census.minute_key(START)]
    assert sample["candidate_max"] == 1 and sample["desired_owner_max"] == 2
    assert sample["active_path_max"] == 1 and sample["cap_refusals"] == expected
    assert sample["outcomes"] == {"REFUSED:" + reason: 1}


def test_aggregation_reports_over_cap_evidence_rather_than_clamping_it():
    minutes = {}
    names = {f"T{index}" for index in range(17)}
    census.sample_minute(minutes, observed=START, candidates=names, desired=names, active=set())
    assert minutes[census.minute_key(START)]["candidate_max"] == 17
    assert minutes[census.minute_key(START)]["desired_owner_max"] == 17


@pytest.mark.parametrize("payload", [b'{"a":1,"a":2}\n', b'{"nested":{"a":1,"a":2}}\n'])
def test_duplicate_header_or_nested_fields_are_not_silently_overwritten(payload):
    with pytest.raises(ValueError, match="duplicate"):
        list(census.read_records(BytesIO(payload)))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_does_not_become_input_evidence(value):
    with pytest.raises(ValueError, match="nonfinite"):
        list(census.read_records(BytesIO(('{"value":' + value + '}\n').encode())))


@pytest.mark.parametrize("payload", [b'[]\n', b'1\n', b'null\n'])
def test_event_and_manifest_lines_must_be_objects(payload):
    with pytest.raises(ValueError, match="object"):
        list(census.read_records(BytesIO(payload)))


def test_normalized_line_limit_rejects_oversize_before_decoding():
    with pytest.raises(ValueError, match="oversized"):
        list(census.read_records(BytesIO(b"{}\n" + b" " * census.MAX_LINE_BYTES + b"{}\n")))


def test_priorclose_manifest_has_separate_bounded_capacity():
    payload = b'{"padding":"' + b"x" * census.MAX_LINE_BYTES + b'"}\n'
    rows = list(census.read_records(BytesIO(payload)))
    assert len(rows) == 1 and len(rows[0][1]["padding"]) == census.MAX_LINE_BYTES


def test_manifest_limit_also_rejects_oversize_before_decoding():
    with pytest.raises(ValueError, match="oversized"):
        list(census.read_records(BytesIO(b" " * census.MAX_HEADER_BYTES + b"{}\n")))


@pytest.mark.parametrize("field,value", [("study_start_ns", START + 1),
    ("study_end_ns", START + 1), ("study_end_ns", START),
    ("study_end_ns", START + 1441 * census.MINUTE_NS),
    ("study_start_ns", True), ("study_start_ns", START - 1440 * census.MINUTE_NS)])
def test_declared_study_bounds_cannot_hide_partial_or_wrong_day_minutes(field, value):
    item = header()
    item[field] = value
    with pytest.raises(ValueError):
        census.study_window(item)


def test_retained_window_settings_must_be_present_not_replaced_by_defaults():
    item = header()
    del item["validation_window"]
    with pytest.raises(KeyError, match="validation_window"):
        census.study_window(item)


def test_empty_retained_input_is_unknown_not_three_zero_candidate_minutes(tmp_path):
    output = result(tmp_path)
    assert len(output["minutes"]) == len(output["unknown_minutes"]) == 3
    assert output["maximum"] is None and output["desired_owner_maximum"] is None
    assert output["coverage"] == "UNKNOWN_PARTIAL" and output["acceptance"].startswith("UNKNOWN")
    assert output["cap"] == 16


def test_input_identity_mismatch_refuses_before_replay_model(tmp_path, monkeypatch):
    path, _ = write_input(tmp_path)
    monkeypatch.setattr(census, "MomentumWarningConsumer", lambda **kwargs: pytest.fail("model constructed"))
    with pytest.raises(ValueError, match="SHA"):
        census.summarize(path, declared_head=HEAD, expected_input_sha256="0" * 64)


def test_wrong_source_root_cannot_borrow_a_different_heads_consumer(tmp_path):
    path, digest = write_input(tmp_path)
    with pytest.raises(ValueError, match="source root mismatch"):
        census.summarize(path, declared_head=HEAD, expected_input_sha256=digest, expected_source_root=tmp_path)


def test_replayed_bytes_are_rehashed_not_only_the_preflight_file(tmp_path, monkeypatch):
    path, digest = write_input(tmp_path)
    original = census.read_records

    def changed(source, *, checksum=None):
        yield from original(source, checksum=checksum)
        checksum.update(b"changed-after-preflight")

    monkeypatch.setattr(census, "read_records", changed)
    with pytest.raises(ValueError, match="changed after"):
        census.summarize(path, declared_head=HEAD, expected_input_sha256=digest)


def test_built_current_price_candidate_uses_real_consumer_without_priorclose_dollar_gate(tmp_path):
    end_ns = START + 1000000000
    payload = {"version": 1, "event_type": "aggregate", "symbol": "TEST",
        "start_ms": START // 1000000, "end_ms": end_ns // 1000000,
        "open": ".90", "high": "1.20", "low": ".90", "close": "1.20", "volume": 100,
        "received_utc_ns": end_ns, "published_utc_ns": end_ns,
        "gateway_instance_id": "synthetic", "generation": 1, "sequence": 1,
        "dropped_total": 0, "provider_kind": "massive_ws_a"}
    path, digest = write_input(tmp_path, [{"kind": "aggregate", "observed_ns": end_ns,
                                         "legacy_active": [], "payload": payload}])
    output = census.summarize(path, declared_head=HEAD, expected_input_sha256=digest)
    sample = output["minutes"][census.minute_key(START)]
    assert sample["candidate_max"] == sample["desired_owner_max"] == 1
    assert sample["outcomes"] == {"ADMITTED:observed_warning": 1}
    assert output["unknown_minutes"] == [census.minute_key(START + index * census.MINUTE_NS) for index in (1, 2)]
    assert output["coverage"] == "UNKNOWN_PARTIAL"


def test_comparison_keeps_numeric_cap_pressure_and_unknown_acceptance(tmp_path):
    old = result(tmp_path)
    new = deepcopy(old)
    new["declared_head"] = "b" * 40
    for output, count, refusals in ((old, 4, 0), (new, 16, 3)):
        output["minutes"][census.minute_key(START)] = {"candidate_max": count,
            "desired_owner_max": count, "cap_refusals": refusals, "samples": 1}
        output["maximum"] = output["desired_owner_maximum"] = count
        output["cap_refusals"] = refusals
    comparison = compare(old, new)
    assert comparison["old_max"] == 4 and comparison["new_max"] == 16
    assert comparison["minutes"][0]["delta_max"] == 12
    assert comparison["minutes"][0]["delta_cap_refusals"] == 3
    assert comparison["acceptance"].startswith("UNKNOWN")


@pytest.mark.parametrize("field,value", [("input_sha256", "mismatch"), ("cap", 15),
    ("study_start_ns", START - census.MINUTE_NS), ("study_end_ns", START + census.MINUTE_NS),
    ("validation_window", {}), ("clock_basis", {}), ("initial_state", "different"),
    ("session_date", "2026-10-22")])
def test_census_compare_refuses_input_window_or_configuration_drift(tmp_path, field, value):
    old = result(tmp_path)
    new = deepcopy(old)
    new[field] = value
    with pytest.raises(ValueError):
        compare(old, new)


def test_minutes_missing_in_both_inputs_remain_explicit_unknown(tmp_path):
    old = result(tmp_path)
    new = deepcopy(old)
    comparison = compare(old, new)
    assert len(comparison["unknown_minutes"]) == 3
    assert all(row["delta_max"] is None and row["scope"] == "UNKNOWN_MISSING_MINUTE"
               for row in comparison["minutes"])


def test_retained_clock_contract_needs_explicit_receipt_and_observation_provenance():
    item = header()
    item["clock_basis"] = {"kind": "retained_consumer_delivery",
        "receipt_provenance": "synthetic positive parser control", "observation_provenance": "same control"}
    basis, scope = census.clock_contract(item)
    assert basis["kind"] == "retained_consumer_delivery" and "not verified" in scope


def test_declared_historical_clock_contract_is_not_called_native_delivery():
    item = header()
    item["initial_state"] = "declared_cold_start"
    _, scope = census.clock_contract(item)
    assert "NOT native/local-arrival/as-known" in scope


@pytest.mark.parametrize("kind", ["sip_is_arrival", None])
def test_undeclared_clock_assignment_is_rejected(kind):
    item = header()
    item["clock_basis"]["kind"] = kind
    with pytest.raises(ValueError, match="unknown clock"):
        census.clock_contract(item)


def test_missing_clock_basis_cannot_use_a_default():
    item = header()
    del item["clock_basis"]
    with pytest.raises(KeyError, match="clock_basis"):
        census.clock_contract(item)


@pytest.mark.parametrize("field", ["contract_id", "assignment_rule", "delivery_scope"])
def test_historical_study_requires_each_predeclared_assignment_operand(field):
    item = header()
    del item["clock_basis"][field]
    with pytest.raises(ValueError, match="required"):
        census.clock_contract(item)


def test_native_delivery_cannot_borrow_declared_cold_start_as_actual_state():
    item = header()
    item["clock_basis"] = {"kind": "retained_consumer_delivery",
        "receipt_provenance": "synthetic control", "observation_provenance": "synthetic control"}
    item["initial_state"] = "declared_cold_start"
    with pytest.raises(ValueError, match="cold-start guess"):
        census.clock_contract(item)


def test_complete_historical_input_still_never_becomes_native_or_accepted(tmp_path):
    path, _ = write_input(tmp_path)
    item = header()
    item["complete_for_both_owner_sets"] = True
    path.write_text(json.dumps(item) + "\n")
    output = census.summarize(path, declared_head=HEAD, expected_input_sha256=sha256(path.read_bytes()).hexdigest())
    assert output["coverage"] == "DECLARED_COUNTERFACTUAL_INPUT_COMPLETE"
    assert "NOT native/local-arrival/as-known" in output["scope"]
    assert output["acceptance"].startswith("UNKNOWN") and len(output["unknown_minutes"]) == 3


@pytest.mark.parametrize("day,offset", [("2026-10-21", 0), ("2026-10-22", 86400 * 1000000000)])
def test_both_replacement_sessions_bind_their_explicit_window(tmp_path, day, offset):
    item = header()
    item["session_date"] = day
    item["study_start_ns"] += offset
    item["study_end_ns"] += offset
    item["coverage_started_ms"] += offset // 1000000
    item["validation_window"]["session_date"] = day
    path = tmp_path / "replacement.jsonl"
    path.write_text(json.dumps(item) + "\n")
    output = census.summarize(path, declared_head=HEAD, expected_input_sha256=sha256(path.read_bytes()).hexdigest())
    assert output["session_date"] == day
    assert output["contract"] == "PRICE1-CENSUS-RESULT-v4"
    assert all(key.startswith(day) for key in output["minutes"])
    assert len(output["unknown_minutes"]) == 3
    assert output["maximum"] is None and output["required_names"] == {"POM": 0}
    assert output["acceptance"].startswith("UNKNOWN")


@pytest.mark.parametrize("day", ["2026-10-12", "2026-10-20", "2026-10-23"])
def test_withdrawn_or_undeclared_sessions_cannot_be_silently_relabelled(day):
    item = header()
    item["session_date"] = day
    with pytest.raises(ValueError, match="replacement census session"):
        census.study_window(item)


def test_declared_22_cannot_use_21_bounds():
    item = header()
    item["session_date"] = "2026-10-22"
    with pytest.raises(ValueError, match="explicitly declared ET session"):
        census.study_window(item)


def test_validation_window_other_session_is_not_defaulted():
    item = header()
    item["validation_window"]["session_date"] = "2026-10-22"
    with pytest.raises(ValueError, match="wrong retained validation session"):
        census.study_window(item)


def test_old_v3_contract_is_not_accepted_as_corrected_input(tmp_path):
    item = header()
    item["contract"] = "PRICE1-CANDIDATES-v3"
    path = tmp_path / "old-contract.jsonl"
    path.write_text(json.dumps(item) + "\n")
    with pytest.raises(ValueError, match="corrected sessions require v4"):
        census.summarize(path, declared_head=HEAD, expected_input_sha256=sha256(path.read_bytes()).hexdigest())


def test_receipt_after_study_end_is_not_backdated_to_fill_a_quiet_minute(tmp_path):
    path, digest = write_input(tmp_path, [{"kind": "heartbeat",
        "observed_ns": START + 3 * census.MINUTE_NS + 1000000000, "legacy_active": []}])
    output = census.summarize(path, declared_head=HEAD, expected_input_sha256=digest)
    assert len(output["unknown_minutes"]) == 3
    assert all(value is None for value in output["minutes"].values())
    assert output["maximum"] is None
