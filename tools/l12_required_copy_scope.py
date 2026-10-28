"""Pure L12 required-copy membership and explicit candidate projection policy.

This is a reviewed scope proposal, NOT a collector/decoder compatibility claim.
No market tape, broad schema scans, implicit empty projections or timeout waiver.
"""

import hashlib
import json

REQUIRED = {
    "broker_accounts": "Identity and ALL live aliases",
    "strategies": "All strategy identities",
    "trade_intents": "All durable intents incl unresolved crossday all strategies/accounts",
    "broker_orders": "All orders incl manual ORB momentum aliases and historical terminal evidence",
    "broker_order_events": "All persisted events linked to orders",
    "fills": "All true fills preserving order strategy account and timestamps",
    "virtual_positions": "All virtual position lineage",
    "account_positions": "All cached positions not native completeness",
    "dashboard_snapshots": "All owner retry dispatch replacement country and receipt rows without subtype pruning",
    "oms_managed_positions": "All managed ownership rows",
    "oms_armed_stops": "All armed stop state",
    "bot_control_states": "All control generation state",
    "bot_control_commands": "All referenced control authority commands",
    "bot_control_attempts": "All inflight and terminal control attempts",
    "brokerdecides_dispatch_retirements": "Candidate derived projection needs explicit absence policy",
    "brokerdecides_dispatch_evidence": "Candidate derived projection needs explicit absence policy",
    "strategy_bar_history": "Full persisted strategy line provenance; scoped later only by explicit review",
    "momentum_live_sec_country": "Persisted route policy inputs only no SEC requests",
    "paper_exit_rule_configs": "Confirmation exit policy references",
    "v2_confirmation_exit_evaluations": "Confirmation source lineage",
    "market_data_subscriptions": "Operational subscription membership not market tape",
    "schwab_ineligible_today": "Existing eligibility guards",
    "webull_ineligible_today": "Existing eligibility guards",
}
PROJECTIONS = frozenset({"brokerdecides_dispatch_retirements", "brokerdecides_dispatch_evidence"})
EXCLUDED_TAPE = frozenset({"market_trade_ticks", "market_quote_ticks",
    "market_capture_trades", "market_capture_quotes", "market_capture_bars"})
SCOPE_VERSION = "L12_REQUIRED_OPERATIONAL_FULL_MEMBERSHIP_V2_PROPOSAL"


def full_membership_scope():
    return dict(version=SCOPE_VERSION,
        tables={name: dict(purpose=purpose, row_membership="ALL_ROWS_NO_TIME_ACCOUNT_STATUS_STRATEGY_PRUNING",
                          columns="ALL_SOURCE_COLUMNS_PK_FK_PAYLOAD_AND_CLOCKS",
                          exact_rows="NOT_YET_EXPORTED", exact_bytes="NOT_YET_EXPORTED")
                for name, purpose in REQUIRED.items()},
        excluded_tape=sorted(EXCLUDED_TAPE), permission_granted=False,
        source_decision="REQUIRES_MAIN_CICERO_SCOPE_REVIEW",
        existing_decoder_compatibility="OLD_ALL_MODEL_V1_REFUSES_THIS_SCOPE_UNTIL_REVIEWED_V2_ADAPTER",
        no_data_limit_is_completeness=True)


def scope_hash():
    return hashlib.sha256(json.dumps(full_membership_scope(), sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def validate_inventory(rows):
    if not isinstance(rows, list) or len(rows) != len(REQUIRED):
        raise ValueError("exact_required_inventory_membership")
    by_name = {row.get("table"): row for row in rows if isinstance(row, dict)}
    if len(by_name) != len(REQUIRED) or set(by_name) != set(REQUIRED):
        raise ValueError("duplicate_missing_or_unreviewed_inventory_table")
    faults = []
    missing_projection = []
    for name, row in by_name.items():
        if row.get("exists") is False:
            if name in PROJECTIONS:
                missing_projection.append(name)
            else:
                faults.append(name + ":MISSING_REQUIRED_SOURCE")
        elif (row.get("exists") is not True or row.get("relation_kind") != "r"
                or row.get("has_children") is not False):
            faults.append(name + ":NONORDINARY_OR_PARTITION_SCOPE_REVIEW_REQUIRED")
        elif not isinstance(row.get("columns"), list) or not row["columns"]:
            faults.append(name + ":SOURCE_COLUMN_INVENTORY_UNKNOWN")
        for target in row.get("foreign_key_targets", []):
            if (not isinstance(target, dict) or target.get("schema") != "public"
                    or target.get("table") not in REQUIRED):
                faults.append(name + ":FK_CLOSURE_OUTSIDE_REVIEWED_SCOPE")
    return dict(status="METADATA_INVENTORY_REQUIRES_EXPORT_REVIEW" if not faults else "UNKNOWN",
        issues=faults, missing_candidate_projections=sorted(missing_projection),
        estimates_are_not_exact_counts=True, exact_export_counts="UNKNOWN",
        exact_export_bytes="UNKNOWN", permission_granted=False, scope_sha256=scope_hash())


def projection_construction_plan(inventory, approval, *, observed_inventory_sha256, observed_capture_root):
    summary = validate_inventory(inventory)
    missing = summary["missing_candidate_projections"]
    if summary["status"] == "UNKNOWN":
        raise ValueError("source_inventory_unknown")
    if not missing:
        return dict(status="NO_ABSENCE_CONSTRUCTION_NEEDED", permission_granted=False)
    if (not isinstance(approval, dict) or approval.get("scope_sha256") != scope_hash()
            or sorted(approval.get("missing_tables", [])) != missing
            or approval.get("action") != "PRIVATE_CANDIDATE_ONLY_CANONICAL_INSTALL_DERIVATION"
            or approval.get("never_source_rows_or_boot_count_grant") is not True
            or not isinstance(approval.get("review_id"), str) or not approval["review_id"]
            or not isinstance(observed_inventory_sha256, str)
            or len(observed_inventory_sha256) != 64
            or any(value not in "0123456789abcdef" for value in observed_inventory_sha256)
            or approval.get("source_inventory_sha256") != observed_inventory_sha256
            or not isinstance(observed_capture_root, str) or len(observed_capture_root) != 40
            or any(value not in "0123456789abcdef" for value in observed_capture_root)
            or approval.get("capture_root") != observed_capture_root):
        raise ValueError("explicit_reviewed_projection_construction_required")
    return dict(status="REVIEWED_PRIVATE_CONSTRUCTION_PLAN_NOT_EXECUTED",
        tables=missing, action=approval["action"], source_rows="ABSENT_NOT_EMPTY_SOURCE",
        derivation="CANONICAL_INSTALL_FROM_FULL_CAPTURED_DURABLE_MEMBERSHIP",
        late_event_invalidation="ALL_DYNAMIC_SOURCE_CHECKS_REMAIN",
        permission_granted=False, application_schema_mutation_authorized=False)
