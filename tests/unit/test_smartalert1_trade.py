"""SOURCE controls only. All operands/windows are synthetic, not replay samples.

No APP/provider import, fixture acquisition, DB, process, transport or live clock.
The explicit 60s/300s candidate windows below are TEST VALUES, not alert policy.
Main alone may execute this new file after source review under its existing fence.
"""

from copy import deepcopy
import json

import pytest

from ops.smartalert1.trade import CLASSES, evaluate_trade
from ops.smartalert1.trade_replay import replay_trade

AS_OF = "2026-10-28T14:00:00Z"
START = "2026-10-14T14:01:50.286791Z"
END = "2026-10-28T14:01:50.286791Z"


def policy(mode="CANDIDATE_REPLAY"):
    return {"schema_version": "smartalert1.v1", "policyversion": "synthetic-policy-1",
        "enabled": True, "mode": mode, "qualified": False, "approval_ref": None,
        "calibration_ref": None,
        "windows": {cls: {"freshness_seconds": 300, "wait_seconds": 60, "qualified": False}
                    for cls in CLASSES},
        "expectations": {"version": "synthetic-expectations-1", "source_head": "a" * 40,
            "settings_ref": "synthetic-settings", "routing_ref": "synthetic-routes",
            "calendar_ref": "synthetic-calendar", "legal_holds": ["approved_no_chase"],
            "routes": {bot + "|" + session: ["schwab", "webull"]
                for bot in ("schwab_1m_v2", "orb_schwab", "momentum_warn_live")
                for session in ("PM", "RTH", "AH")}}}


def operands(cls, risk=True):
    common = {"identity_complete": True}
    values = {
        "A1": {"owned_quantity": "10", "native_protected_quantity": "0" if risk else "10",
            "software_protected_quantity": "0", "book_complete": True,
            "ownership_complete": True, "coverage_disjoint": True},
        "A2": {"opportunity_complete": True, "outcome_complete": True, "entry_eligible": True,
            "trigger_reached": True, "legal_hold": "none", "matched_order_ids": [] if risk else ["order-1"],
            "matched_fill_ids": [], "software_rest_waiting_cross": False},
        "A3": {"refusal_proven": True, "admission_and_route_complete": True,
            "refusal_code": "unexpected_guard", "legal_hold": "none" if risk else "approved_no_chase",
            "legal_hold_proven": True, "admission_allowed": True, "quote_request_complete": True,
            "unexpected_refusal_proven": True},
        "A4": {"book_complete": True, "ownership_complete": True, "native_owned_quantity": "10",
            "bot_owned_quantity": "0" if risk else "10", "unowned_order_ids": []},
        "A5": {"exit_signal_proven": True, "exit_outcomes_complete": True,
            "required_exit_quantity": "10", "matched_sell_filled_quantity": "0" if risk else "10",
            "reprotected_remainder_quantity": "0", "allocation_complete": True,
            "matched_sell_fill_ids": [] if risk else ["fill-1"]},
        "A7": {"entry_membership_complete": True, "gate_history_complete": True,
            "legal_hold": "none", "entry_eligible": True, "blocked": risk,
            "block_reason": "entry_history_gap" if risk else "none", "recovery_proven": not risk},
    }
    values["A6"] = deepcopy(values["A2"])
    return {**common, **values[cls]}


def envelope(cls="A1", risk=True):
    return {"schema_version": "smartalert1.v1", "source_id": "synthetic-proof-frame",
        "source_kind": "synthetic_control", "source_hash": "b" * 64, "source_epoch": "session-1",
        "observed_at": AS_OF, "event_at": "2026-10-28T13:59:00Z", "received_at": AS_OF,
        "coverage_start": "2026-10-28T13:58:00Z", "coverage_end": AS_OF,
        "complete": True, "overflow": False, "read_error": False,
        "evidence_ids": ["proof-1", "order-1", "fill-1"],
        "subject": {"account": "logical-account-1", "broker": "webull", "bot": "schwab_1m_v2",
            "symbol": "POM", "service": "oms", "subject_id": "episode-1",
            "execution_mode": "live", "manual": False},
        "payload": {"expectation_ref": "synthetic-expectations-1", "source_head": "a" * 40,
            "session": "RTH", "checks": [{"class": cls, "condition_epoch": "condition-1",
                "first_seen": "2026-10-28T13:58:30Z", "action_taken": "none_recorded",
                "operands": operands(cls, risk)}]}}


def one(evidence, selected=None, as_of=AS_OF):
    findings = evaluate_trade(evidence, selected if selected is not None else policy(), as_of=as_of)
    assert len(findings) == 1
    return findings[0]


def observation(evidence=None, as_of=AS_OF):
    return {"kind": "observation", "as_of": as_of, "evidence": evidence or envelope()}


def replay(records, selected=None, start=START, end=END):
    return replay_trade(records, selected if selected is not None else policy(), window_start=start, window_end=end)


def label(evidence, status="TRUE", origin="synthetic_control"):
    return {"kind": "label", "incident_key": one(evidence)["incident_key"], "status": status,
            "evidence_ids": ["proof-1"], "label_asof": END,
            "adjudicated_by": "synthetic-review", "origin": origin}


def attach_envelope():
    frame = envelope("A4")
    subject = frame["subject"]
    subject["subject_id"] = "attach-intent-1"
    identity = {key: subject[key] for key in ("account", "bot", "broker", "symbol")}
    base = "schwab_1m_v2-POM-protect-0123456789ab"
    attempt = {"intent_id": subject["subject_id"], "base": base, "quantity": "10",
               "phase": "accepted", "cancel_requested": False, "combo_id": "synthetic-combo"}
    data = {"identity_complete": True, "assertion": "intent_without_broker_order",
        "cohort_complete": True, "entry_owned_proven": True, "attempts_complete": True,
        "linked_order_ids": [], "linked_orders_bound": True,
        "intent": {**identity, "id": subject["subject_id"], "side": "sell", "intent_type": "close",
            "reason": "WBATTACH1_PROTECTION", "status": "accepted", "quantity": "10",
            "metadata": {"entry_order_id": "entry-1", "managed_row_id": "managed-1",
                "webull_protect_base_client_order_id": base,
                "webull_protect_target_client_order_id": base[:39] + "T",
                "webull_protect_stop_client_order_id": base[:39] + "S",
                "webull_protect_combo_id": "synthetic-combo"}},
        "entry": {**identity, "id": "entry-1", "side": "buy", "client_order_id": "atr-entry-1",
            "broker_order_id": "synthetic-native-entry", "journal": {"entry_id": "entry-1",
                "managed_id": "managed-1", "attempts": [attempt], "exit_phase": None}},
        "managed": {**identity, "id": "managed-1", "entry_order_id": "entry-1",
                    "entry_client_order_id": "atr-entry-1"}}
    frame["payload"]["checks"][0]["operands"] = data
    return frame


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("risk", [True, False])
def test_each_trade_class_positive_or_affirmative_negative(cls, risk):
    finding = one(envelope(cls, risk))
    assert finding["status"] == ("TRUE" if risk else "FALSE")
    assert finding["paging_eligible"] is False


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("bot", ["schwab_1m_v2", "orb_schwab", "momentum_warn_live"])
@pytest.mark.parametrize("session", ["PM", "RTH", "AH"])
def test_explicit_synthetic_route_and_session_identity(broker, bot, session):
    frame = envelope()
    frame["subject"].update(broker=broker, bot=bot)
    frame["payload"]["session"] = session
    assert one(frame)["status"] == "TRUE"
    # These are synthetic route expectations, never proof a real bot may trade this session.


@pytest.mark.parametrize("field,value", [("complete", False), ("overflow", True),
                                        ("read_error", True), ("complete", 1)])
def test_incomplete_envelope_never_clean(field, value):
    frame = envelope()
    frame[field] = value
    finding = one(frame)
    assert finding["status"] == "UNKNOWN" and finding["paging_eligible"] is False


@pytest.mark.parametrize("value", [False, 1, None, "true"])
def test_positive_identity_attestation_required(value):
    frame = envelope()
    frame["payload"]["checks"][0]["operands"]["identity_complete"] = value
    assert one(frame)["status"] == "UNKNOWN"


@pytest.mark.parametrize("value", [10, True, "NaN", "Infinity", "-1", "1e2", ""])
def test_quantities_require_finite_exact_decimal_strings(value):
    frame = envelope()
    frame["payload"]["checks"][0]["operands"]["owned_quantity"] = value
    assert one(frame)["status"] == "UNKNOWN"


def test_software_protection_requires_live_owner_and_allowed_policy():
    frame = envelope()
    data = frame["payload"]["checks"][0]["operands"]
    data.update(software_protected_quantity="10", software_owner_live=True, software_policy_allowed=True)
    assert one(frame)["status"] == "FALSE"
    data["software_owner_live"] = False
    assert one(frame)["status"] == "UNKNOWN"


def test_oco_quantities_cannot_be_double_counted():
    frame = envelope()
    data = frame["payload"]["checks"][0]["operands"]
    data.update(native_protected_quantity="10", software_protected_quantity="10",
                software_owner_live=True, software_policy_allowed=True)
    assert one(frame)["reason_code"] == "protection_quantity_ambiguous"


def test_exact_decimal_low_digit_is_not_rounded_into_false_recovery():
    frame = envelope()
    data = frame["payload"]["checks"][0]["operands"]
    data.update(owned_quantity="99999999999999999999.000000000000000001",
                native_protected_quantity="99999999999999999999.000000000000000000")
    assert one(frame)["status"] == "TRUE"


def test_huge_numeric_window_is_unknown_not_an_exception():
    selected = policy()
    selected["windows"]["A1"]["freshness_seconds"] = 1 << 2000
    assert one(envelope(), selected)["status"] == "UNKNOWN"


@pytest.mark.parametrize("cls", ["A2", "A6", "A7"])
def test_positive_legal_hold_is_not_missed_trade(cls):
    frame = envelope(cls)
    frame["payload"]["checks"][0]["operands"].update(
        legal_hold="approved_no_chase", legal_hold_proven=True)
    assert one(frame)["status"] == "FALSE"


def test_untriggered_rest_is_not_forced_buy_and_contradiction_unknown():
    frame = envelope("A2")
    data = frame["payload"]["checks"][0]["operands"]
    data.update(trigger_reached=False, software_rest_waiting_cross=True)
    assert one(frame)["status"] == "FALSE"
    data["trigger_reached"] = True
    assert one(frame)["reason_code"] == "software_rest_cross_contradiction"


def test_partial_exit_reprotected_remainder_is_positive_recovery():
    frame = envelope("A5")
    data = frame["payload"]["checks"][0]["operands"]
    data.update(matched_sell_filled_quantity="4", reprotected_remainder_quantity="6",
                matched_sell_fill_ids=["fill-1"])
    assert one(frame)["status"] == "FALSE"
    data["matched_sell_fill_ids"] = []
    assert one(frame)["status"] == "UNKNOWN"


def test_held_exit_observation_without_entry_membership_is_unknown():
    frame = envelope("A7")
    frame["payload"]["checks"][0]["operands"]["entry_membership_complete"] = False
    assert one(frame)["status"] == "UNKNOWN"


def test_missing_windows_and_unqualified_report_only_are_unknown():
    selected = policy()
    selected["windows"] = {}
    assert one(envelope(), selected)["reason_code"] == "window_unknown"
    assert one(envelope(), policy("REPORT_ONLY"))["reason_code"] == "window_unqualified"


def test_even_qualified_live_is_delivery_ineligible_in_shadow():
    selected = policy("QUALIFIED_LIVE")
    assert one(envelope(), selected)["paging_eligible"] is False
    selected.update(qualified=True, approval_ref="synthetic-approval", calibration_ref="synthetic-calibration")
    selected["windows"]["A1"]["qualified"] = True
    assert one(envelope(), selected)["paging_eligible"] is False
    assert one(envelope(), selected)["qualification"]["delivery_mode"] == "SHADOW"
    selected["mode"] = "CANDIDATE_REPLAY"
    assert one(envelope(), selected)["paging_eligible"] is False


def test_off_does_not_mutate_or_evaluate_frames():
    selected = policy()
    selected["enabled"] = False
    frame = {"opaque": object()}
    assert evaluate_trade(frame, selected, as_of="not-a-clock") == []


def test_paper_excluded_manual_requires_positive_allocation():
    frame = envelope()
    frame["subject"]["execution_mode"] = "paper"
    assert evaluate_trade(frame, policy(), as_of=AS_OF) == []
    frame["subject"].update(execution_mode="live", manual=True)
    assert one(frame)["status"] == "UNKNOWN"
    frame["payload"]["checks"][0]["operands"]["manual_allocation_proven"] = True
    result = one(frame)
    assert result["status"] == "FALSE" and result["paging_eligible"] is False
    frame["subject"]["bot"] = None  # A manual holding must not invent a bot row.
    result = one(frame)
    assert result["status"] == "FALSE" and result["bot"] is None and result["manual"] is True


@pytest.mark.parametrize("field,value", [("observed_at", "2026-10-28T15:00:00Z"),
                                        ("event_at", "2026-10-28T14:01:00Z"),
                                        ("received_at", "2026-10-28T13:00:00Z"),
                                        ("observed_at", "2026-10-28T14:00:00")])
def test_future_reversed_and_naive_clocks_unknown(field, value):
    frame = envelope()
    frame[field] = value
    assert one(frame)["status"] == "UNKNOWN"


def test_stale_source_never_clears_and_first_seen_not_retimed():
    frame = envelope(risk=False)
    result = one(frame, as_of="2026-10-28T14:10:00Z")
    assert result["status"] == "UNKNOWN" and result["freshness"]["status"] == "STALE"
    current = one(frame)
    assert current["first_seen"] == "2026-10-28T13:58:30Z"
    assert current["last_seen"] == AS_OF


def test_keys_stable_across_pid_observation_and_input_unchanged():
    frame, selected = envelope(), policy()
    before = deepcopy((frame, selected))
    first = one(frame, selected)
    second = one(frame, selected, as_of="2026-10-28T14:01:00Z")
    assert first["incident_key"] == second["incident_key"]
    assert (frame, selected) == before


def test_wrong_epoch_or_route_is_unknown_not_clean():
    frame = envelope()
    frame["payload"]["source_head"] = "c" * 40
    assert one(frame)["status"] == "UNKNOWN"
    frame = envelope()
    frame["payload"]["session"] = "CLOSED"
    assert one(frame)["reason_code"] == "route_expectation_unknown"


def test_source_payload_and_raw_exception_never_echoed():
    frame = envelope()
    frame["payload"]["private"] = "SECRET_TOKEN_https://private.invalid/secret"
    frame["payload"]["checks"][0]["operands"]["owned_quantity"] = "SECRET_EXCEPTION"
    text = json.dumps(one(frame))
    assert "SECRET" not in text and "private.invalid" not in text


def test_input_and_output_overflow_return_unknown_instead_of_partial_clean():
    frame = envelope()
    frame["payload"]["oversize"] = "x" * 1048577
    assert one(frame)["status"] == "UNKNOWN"
    frame = envelope()
    template = frame["payload"]["checks"][0]
    frame["payload"]["checks"] = [{**template, "condition_epoch": "condition-" + str(i)} for i in range(600)]
    result = one(frame)
    assert result["reason_code"] == "output_resource_overflow"


def test_duplicate_condition_identity_is_unknown():
    frame = envelope()
    frame["payload"]["checks"] *= 2
    assert one(frame)["reason_code"] == "duplicate_condition_identity"


def test_exact_wbattach_accepted_journal_is_lifecycle_false_only():
    frame = attach_envelope()
    assert one(frame)["reason_code"] == "positive_journaled_attach_no_bo_by_design"
    frame["payload"]["checks"].append(envelope()["payload"]["checks"][0])
    findings = evaluate_trade(frame, policy(), as_of=AS_OF)
    assert {value["class"]: value["status"] for value in findings} == {"A4": "FALSE", "A1": "TRUE"}


@pytest.mark.parametrize("damage", ["reason_only", "missing_combo", "wrong_combo", "wrong_entry",
    "wrong_managed", "wrong_account", "wrong_broker", "wrong_bot", "wrong_symbol", "wrong_base",
    "wrong_children", "duplicate_attempt", "duplicate_base", "unknown_ack", "submitting", "cancel",
    "exit", "foreign_attempt", "quantity", "unowned", "partial", "bad_bool"])
def test_wbattach_exception_never_blindly_excludes_reason_or_ambiguous_ack(damage):
    frame = attach_envelope()
    data = frame["payload"]["checks"][0]["operands"]
    intent, entry = data["intent"], data["entry"]
    attempt = entry["journal"]["attempts"][0]
    if damage == "reason_only":
        data["entry"] = None
    elif damage == "missing_combo":
        intent["metadata"].pop("webull_protect_combo_id")
    elif damage == "wrong_combo":
        attempt["combo_id"] = "different"
    elif damage == "wrong_entry":
        intent["metadata"]["entry_order_id"] = "different"
    elif damage == "wrong_managed":
        data["managed"]["entry_order_id"] = "different"
    elif damage in {"wrong_account", "wrong_broker", "wrong_bot", "wrong_symbol"}:
        field = damage.removeprefix("wrong_")
        entry[field] = "different"
    elif damage == "wrong_base":
        attempt["base"] = "different"
    elif damage == "wrong_children":
        intent["metadata"]["webull_protect_target_client_order_id"] = "different"
    elif damage == "duplicate_attempt":
        entry["journal"]["attempts"].append(deepcopy(attempt))
    elif damage == "duplicate_base":
        entry["journal"]["attempts"].append({**attempt, "intent_id": "different"})
    elif damage in {"unknown_ack", "submitting"}:
        attempt["phase"] = "unknown" if damage == "unknown_ack" else "submitting"
    elif damage == "cancel":
        attempt["cancel_requested"] = True
    elif damage == "exit":
        entry["journal"]["exit_phase"] = "exiting"
    elif damage == "foreign_attempt":
        entry["journal"]["attempts"].append({**attempt, "intent_id": "other", "base": "other-base"})
    elif damage == "quantity":
        attempt["quantity"] = "9"
    elif damage == "unowned":
        data["entry_owned_proven"] = False
    elif damage == "partial":
        data["attempts_complete"] = False
    else:
        attempt["cancel_requested"] = 0
    assert one(frame)["status"] != "FALSE"


def test_wbattach_terminal_and_bound_order_do_not_clear_wrong_identity():
    frame = attach_envelope()
    data = frame["payload"]["checks"][0]["operands"]
    data["intent"]["status"] = "filled"
    assert one(frame)["status"] == "FALSE"
    data["intent"]["account"] = "different"
    assert one(frame)["status"] == "TRUE"
    frame = attach_envelope()
    data = frame["payload"]["checks"][0]["operands"]
    data["linked_order_ids"] = ["order-1"]
    assert one(frame)["status"] == "FALSE"
    data["linked_orders_bound"] = False
    assert one(frame)["status"] == "UNKNOWN"


@pytest.mark.parametrize("status", [None, [], {}, 1, "unrecognized_state"])
def test_wbattach_malformed_status_is_unknown_not_crash_or_false_clear(status):
    frame = attach_envelope()
    frame["payload"]["checks"][0]["operands"]["intent"]["status"] = status
    assert one(frame)["status"] == "UNKNOWN"


def test_replay_empty_bad_window_and_nonchronological_refuse():
    assert replay([])["status"] == "INSUFFICIENT"
    assert replay([observation()], start="2026-10-15T14:01:50.286791Z")["reason_code"] == "replay_window_not_336_elapsed_hours"
    rows = [observation(as_of="2026-10-28T14:01:00Z"), observation()]
    assert replay(rows)["reason_code"] == "replay_chronology_unknown"


def test_replay_repeat_dedupe_unknown_labels_are_null_not_zero():
    output = replay([observation(), observation()])
    assert output["observations"] == 2 and output["counts"]["TRUE"] == 2
    assert output["unique_incidents"] == output["would_page_incidents"] == 1
    assert output["calibration"]["TP"] is None and output["calibration"]["label_missing"] == 1
    assert output["historical_coverage"] == "UNKNOWN" and output["paging_eligible"] is False


def test_replay_no_future_recovery_operand_leak_or_true_erasure():
    first, recovered = envelope(), envelope(risk=False)
    recovered["observed_at"] = recovered["received_at"] = recovered["coverage_end"] = "2026-10-28T14:01:00Z"
    output = replay([observation(first), observation(recovered, "2026-10-28T14:01:00Z"), label(first)])
    assert output["counts"]["TRUE"] == output["counts"]["FALSE"] == 1
    assert output["calibration"]["TP"] == 1 and output["calibration"]["FN"] == 0
    assert output["would_page_incidents"] == 1
    assert output["samples"]["A1"]["status"] == "INSUFFICIENT"


def test_replay_conflicting_label_and_first_seen_reset_do_not_claim_clean():
    frame = envelope()
    output = replay([observation(frame), label(frame), label(frame, "FALSE")])
    assert output["calibration"]["label_conflicts"] == 1 and output["calibration"]["TP"] is None
    changed = deepcopy(frame)
    changed["payload"]["checks"][0]["first_seen"] = "2026-10-28T13:59:00Z"
    assert replay([observation(frame), observation(changed)])["reason_code"] == "condition_first_seen_changed"


def test_replay_future_evidence_unknown_and_unlabeled_samples_insufficient():
    frame = envelope()
    frame["event_at"] = "2026-10-28T14:01:00Z"
    output = replay([observation(frame)])
    assert output["counts"]["UNKNOWN"] == 1 and output["would_page_incidents"] == 0
    assert all(value["status"] == "INSUFFICIENT" for value in output["samples"].values())


def test_replay_et_boundary_and_input_immutability():
    rows, selected = [observation()], policy()
    before = deepcopy((rows, selected))
    output = replay(rows, selected)
    assert output["elapsed_hours"] == 336 and output["partial_boundary_days"] == {"first": True, "last": True}
    assert output["et_start"].endswith("-04:00")
    assert (rows, selected) == before


def test_replay_off_does_not_consume_input_stream():
    def forbidden():
        raise AssertionError("OFF consumed records")
        yield
    selected = policy()
    selected["enabled"] = False
    assert replay(forbidden(), selected)["status"] == "OFF"


def shadow_stops_frame():
    """Synthetic held-B operands, never a historical hold-time reconstruction."""
    frame = envelope("A1")
    frame["subject"]["subject_id"] = "position-1"
    identity = {key: frame["subject"][key] for key in ("account", "broker", "bot", "symbol")}
    frame["payload"]["checks"][0]["operands"] = {
        "identity_complete": True, "assertion": "stops_armed_shadow", "cohort_complete": True,
        "position": {**identity, "id": "position-1", "quantity": "10",
                     "opened_at": "2026-10-28T13:50:00Z"},
        "armed_stop": None,
        "managed": {**identity, "id": "managed-1", "status": "open", "current_quantity": "10",
                    "entry_order_id": "entry-1", "entry_client_order_id": "entry-client-1"},
        "entry": {**identity, "id": "entry-1", "client_order_id": "entry-client-1",
                  "side": "buy", "broker_order_id": "native-entry-1",
                  "protection_state": "resting", "pair_base": "synthetic-pair",
                  "exit_phase": None},
        "native_negative": None,
    }
    selected = policy()
    selected["windows"]["A1"]["settle_seconds"] = 120  # existing guard, synthetic input only
    return frame, selected


def test_shadow_stops_positive_persisted_evidence_cannot_grant_paging():
    frame, selected = shadow_stops_frame()
    result = one(frame, selected)
    assert result["status"] == "FALSE"
    assert result["reason_code"] == "stops_shadow_persisted_attachment_evidence_only"
    assert result["paging_eligible"] is False and result["qualification"]["shadow_only"] is True
    assert "WORKING" not in result["reason_code"]


@pytest.mark.parametrize("state", ["released", "attach_failed", "submitting"])
def test_shadow_recorded_negative_not_rescued_by_managed_or_armed_row(state):
    frame, selected = shadow_stops_frame()
    data = frame["payload"]["checks"][0]["operands"]
    data["entry"]["protection_state"] = state
    data["armed_stop"] = {**data["entry"], "id": "armed-1"}
    result = one(frame, selected)
    assert result["status"] == "TRUE" and result["paging_eligible"] is False


@pytest.mark.parametrize("missing", ["position", "armed_stop", "managed", "entry", "native_negative"])
def test_shadow_missing_field_is_not_explicit_absence(missing):
    frame, selected = shadow_stops_frame()
    frame["payload"]["checks"][0]["operands"].pop(missing)
    assert one(frame, selected)["status"] == "UNKNOWN"


@pytest.mark.parametrize("field", ["account", "broker", "bot", "symbol"])
@pytest.mark.parametrize("row", ["position", "managed", "entry"])
def test_shadow_full_row_identity_is_required(row, field):
    frame, selected = shadow_stops_frame()
    frame["payload"]["checks"][0]["operands"][row][field] = "foreign"
    assert one(frame, selected)["status"] == "UNKNOWN"


def test_shadow_unexplained_legacy_payload_requires_explanation():
    frame, selected = shadow_stops_frame()
    frame["payload"]["checks"][0]["operands"]["entry"]["protection_state"] = None
    result = one(frame, selected)
    assert result["status"] == "UNKNOWN"
    assert result["reason_code"] == "stops_shadow_legacy_payload_unexplained"


@pytest.mark.parametrize("row", ["managed", "entry"])
def test_shadow_complete_positive_absence_remains_true(row):
    frame, selected = shadow_stops_frame()
    frame["payload"]["checks"][0]["operands"][row] = None
    assert one(frame, selected)["status"] == "TRUE"


def test_shadow_entry_binding_and_quantity_not_same_symbol_guess():
    frame, selected = shadow_stops_frame()
    data = frame["payload"]["checks"][0]["operands"]
    data["managed"]["entry_order_id"] = "other-entry"
    assert one(frame, selected)["status"] == "UNKNOWN"
    data["managed"]["entry_order_id"] = "entry-1"
    data["managed"]["current_quantity"] = "9"
    assert one(frame, selected)["status"] == "TRUE"


def test_shadow_native_negative_overrides_persisted_ack_only_if_proven_fresh():
    frame, selected = shadow_stops_frame()
    data = frame["payload"]["checks"][0]["operands"]
    data["native_negative"] = {**data["entry"], "id": "native-proof-1", "complete": True,
        "absence_proven": True, "observed_at": AS_OF, "evidence_ids": ["proof-1"],
        "position_id": "position-1", "entry_order_id": "entry-1", "managed_row_id": "managed-1"}
    assert one(frame, selected)["reason_code"] == "stops_shadow_positive_native_protection_absent"
    data["native_negative"]["complete"] = False
    assert one(frame, selected)["status"] == "UNKNOWN"
    data["native_negative"]["complete"] = True
    data["native_negative"]["observed_at"] = "2026-10-28T13:00:00Z"
    assert one(frame, selected)["status"] == "UNKNOWN"
    data["native_negative"]["observed_at"] = AS_OF
    data["native_negative"]["entry_order_id"] = "prior-entry"
    assert one(frame, selected)["status"] == "UNKNOWN"


def test_shadow_no_new_settle_default_and_exact_existing_boundary():
    frame, selected = shadow_stops_frame()
    data = frame["payload"]["checks"][0]["operands"]
    selected["windows"]["A1"].pop("settle_seconds")
    assert one(frame, selected)["reason_code"] == "window_unknown"
    selected["windows"]["A1"]["settle_seconds"] = 120
    data["position"]["opened_at"] = "2026-10-28T13:58:00Z"
    assert one(frame, selected)["reason_code"] == "stops_shadow_settle_window_open"
    data["position"]["opened_at"] = "2026-10-28T13:57:59.999999Z"
    assert one(frame, selected)["status"] == "FALSE"


def test_shadow_native_risk_and_persisted_evidence_are_distinct_assertions():
    frame, selected = shadow_stops_frame()
    actual_risk = deepcopy(envelope()["payload"]["checks"][0])
    actual_risk["condition_epoch"] = "native-condition-1"
    frame["payload"]["checks"].append(actual_risk)
    findings = evaluate_trade(frame, selected, as_of=AS_OF)
    assert sorted(f["status"] for f in findings) == ["FALSE", "TRUE"]
    assert all(f["paging_eligible"] is False for f in findings)


@pytest.mark.parametrize("stamp", ["2026-10-28T14:00:00.1234567Z",
                                  "2026-10-28 14:00:00Z", "20261028T140000Z"])
def test_strict_clock_never_silently_truncates_or_accepts_non_rfc3339(stamp):
    frame = envelope()
    frame["observed_at"] = stamp
    assert one(frame)["status"] == "UNKNOWN"


def test_shadow_replay_retains_counterfactual_true_but_no_delivery():
    frame, selected = shadow_stops_frame()
    frame["payload"]["checks"][0]["operands"]["entry"]["protection_state"] = "attach_failed"
    result = replay([observation(frame)], selected)
    assert result["counts"]["TRUE"] == 1 and result["would_page_incidents"] == 1
    assert result["paging_eligible"] is False and result["historical_coverage"] == "UNKNOWN"
