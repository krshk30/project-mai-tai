"""Separate SIM contract; recorded entries with modeled exits are never LIVE.

Source staging only. The additive migration and exact producer binding require
separate review before storage or presentation can claim this contract.
"""

MODE = "AGG_ENTRY"
MODEL = "RECORDED_AGGREGATE_ENTRY"
VERSION = "ATRSIM_AGGREGATE_ENTRY_SAME_EXIT_V1"
LABEL = "RECORDED AGGREGATE-ENTRY REPLAY \u00b7 partial timing UNKNOWN"
ENTRY_ASSUMPTION = "RECORDED_ORDER_AGGREGATE_AT_ONE_RETAINED_FILL_TIME"
EXIT_POLICY = "C1_20261023_2129_A2_CEILING_FIRST_ELIGIBLE_STOP_PRINT"
REALIZED_SCOPE = "REPORTED_REAL_PNL_NOT_AGGREGATE_ENTRY_REPLAY"


def require_provenance(row, *, producer_head, source_pins):
    """Check independently supplied provenance, not a row's self-attestation."""
    if (not isinstance(producer_head, str) or len(producer_head) != 40
            or any(c not in "0123456789abcdef" for c in producer_head)
            or not isinstance(source_pins, dict) or not source_pins):
        raise ValueError("aggregate_independent_producer_binding_required")
    if (not isinstance(row, dict) or row.get("mode") != MODE
            or row.get("model") != MODEL or row.get("version") != VERSION
            or row.get("history_label") != LABEL
            or row.get("entry_assumption") != ENTRY_ASSUMPTION
            or row.get("exit_policy_version") != EXIT_POLICY
            or row.get("producer_head") != producer_head
            or row.get("source_pins") != source_pins
            or row.get("partial_fill_timing") != "UNKNOWN"
            or row.get("native_execution_qualified") is not False):
        raise ValueError("aggregate_stored_provenance_or_explicit_unknown_required")
    if any(not isinstance(name, str) or not name or not isinstance(value, str)
           or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
           for name, value in source_pins.items()):
        raise ValueError("aggregate_exact_source_pins_required")
    return row
