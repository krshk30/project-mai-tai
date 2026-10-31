"""C6 approved reason-map extension, CONTROLLED classification only.

Requires the exact adapter already qualified by Main's seven C6 controls in the
SAME composed root. No exporter/collector/native/book/producer/phone is invoked.
PAGE below is a shadow CATEGORY, not permission to page or qualified evidence.
"""

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from ops.smartalert1 import delivery as delivery
from ops.smartalert1 import operational_policy as policy
from ops.smartalert1 import system as system


ADAPTER_SHA = "d490eb4b81058cb06df4156823535333454e8a4dcc609d90b71d17a8d2b4ec6d"
C6 = (
    ("B_keepwatch_stuck", "keepwatch_rehold_or_frozen"),
    ("B_bot_reading_lag", "bot_reading_lag_180s"),
    ("B_bar_lag", "bar_lag_4min"),
)
OLD_L3 = {
    "B_DB_write_error": ("actual_error_count_measured",),
    "B_OMS_stall": ("oms_functional_progress_measured",),
    "B_broker_API_error": ("actual_error_count_measured",),
    "B_checker_blindness": ("checker_coverage_measured",),
    "B_guard_STOP": ("live_guard_stop",),
    "B_quote_stale": ("quote_age_measured",),
    "B_restart_loop": ("restart_delta_measured",),
    "B_scanner_silent": ("scanner_progress_measured",),
    "B_service_down": ("service_down",),
    "B_token_dead_or_near_expiry": ("refresh_expiry_measured", "token_dead"),
}
OLD_L2 = {
    "A1": ("owned_quantity_unprotected", "stops_shadow_bound_protection_row_absent",
           "stops_shadow_exit_handoff_recorded", "stops_shadow_insufficient_managed_quantity",
           "stops_shadow_no_protection_row", "stops_shadow_positive_native_protection_absent",
           "stops_shadow_recorded_pair_not_resting"),
    "A2": ("eligible_opportunity_without_order",),
    "A4": ("lifecycle_ack_missing_or_mismatched", "lifecycle_attach_child_identity_mismatch",
           "lifecycle_attach_exit_or_cancel_unresolved", "lifecycle_attach_quantity_mismatch",
           "lifecycle_attempt_base_ambiguous", "lifecycle_attempt_identity_ambiguous",
           "lifecycle_competing_attempt_unresolved", "lifecycle_entry_association_unproven",
           "lifecycle_intent_identity_mismatch", "lifecycle_no_matching_attach_contract",
           "lifecycle_owner_identity_mismatch", "lifecycle_owner_row_proven_missing",
           "positive_orphan_mismatch"),
    "A5": ("exit_quantity_still_at_risk",),
    "A6": ("eligible_opportunity_without_order",),
}


def categories():
    # Actual generator source checks stay intact. A missing adapter refuses;
    # no CONTROLLED source string is inserted to manufacture a passing map.
    return policy.category_map(policy._sha(policy.TRADE_SOURCE))


@pytest.mark.parametrize("cls,reason", C6)
def test_exact_c6_mapping_requires_actual_previously_qualified_adapter(cls, reason):
    assert hashlib.sha256(Path(system.__file__).read_bytes()).hexdigest() == ADAPTER_SHA
    assert cls in system.CLASSES
    assert policy._L3_PAGE[cls] == (reason,)
    assert categories()["exact"]["L3"][cls] == {reason: "PAGE"}


def test_c6_extension_preserves_every_old_mapping_default_and_dynamic_rule():
    selected = categories()
    assert {key: value for key, value in policy._L3_PAGE.items() if key not in dict(C6)} == OLD_L3
    assert set(policy._L3_PAGE) == set(OLD_L3) | set(dict(C6))
    assert policy._L2_PAGE == OLD_L2
    assert selected["default"] == "DIGEST"
    for lane, table in (("L2", OLD_L2), ("L3", OLD_L3)):
        assert {key: selected["exact"][lane][key] for key in table} == {
            key: {reason: "PAGE" for reason in reasons} for key, reasons in table.items()}
    trade_sha = policy._sha(policy.TRADE_SOURCE)
    assert selected["source_bound_true"] == {"L2": {
        "A3": {"category": "PAGE", "reason_field": "refusal_code", "source_file_sha256": trade_sha},
        "A7": {"category": "PAGE", "reason_field": "block_reason", "source_file_sha256": trade_sha}}}
    assert policy.POLICYVERSION == "smartalert1-L4-operational-shadow-v1"
    assert policy.CALIBRATION_REF == "UNCALIBRATED-shadow-only"


@pytest.mark.parametrize("cls,reason", C6)
@pytest.mark.parametrize("status,expected", [("TRUE", "PAGE"), ("FALSE", "DIGEST"), ("UNKNOWN", "DIGEST")])
def test_c6_category_never_coerces_unknown_or_false_into_positive_evidence(cls, reason, status, expected):
    finding = {"class": cls, "reason_code": reason, "status": status, "manual": False, "paper": False}
    before = deepcopy(finding)
    # Only the actual pure category function is under test. This minimal argument
    # is NOT a complete/approved producer policy or a real upstream finding.
    assert delivery.shadow_category("L3", finding, {"category_map": categories()}) == expected
    assert finding == before


@pytest.mark.parametrize("cls,reason", C6)
def test_c6_mapping_is_exact_no_wildcard_or_class_only_paging(cls, reason):
    finding = {"class": cls, "reason_code": reason + "_CONTROLLED_UNBOUND", "status": "TRUE",
               "manual": False, "paper": False}
    assert delivery.shadow_category("L3", finding, {"category_map": categories()}) == "DIGEST"


@pytest.mark.parametrize("cls,reason", C6)
def test_generator_retains_source_reason_guard_for_missing_c6_literal(tmp_path, monkeypatch, cls, reason):
    originals = {}
    for relative in (policy.TRADE_SOURCE, policy.SYSTEM_SOURCE, policy.SYSTEM_RULES):
        original = policy.REPO / relative
        raw = original.read_bytes()
        originals[original] = raw
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Adverse FILE fixture only, never imported/compiled/executed. Delete the
        # selected reason from the real source text instead of faking support.
        fixture = raw.replace(reason.encode("ascii"), b"CONTROLLED_MISSING_REASON") if relative == policy.SYSTEM_RULES else raw
        target.write_bytes(fixture)
    monkeypatch.setattr(policy, "REPO", tmp_path)
    with pytest.raises(ValueError, match="reason code not in L3 source") as caught:
        categories()
    assert cls + "/" + reason in str(caught.value)
    assert all(path.read_bytes() == raw for path, raw in originals.items())
