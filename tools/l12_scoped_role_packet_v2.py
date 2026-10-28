"""V2 role accounting. Source ABSENT is never promoted to complete source.

No collector/factory/service calls. Existing historical V1 contract stays
unchanged; this explicit V2 boundary consumes reviewed full-PK chunk evidence.
"""

from copy import deepcopy
from hashlib import sha256

from project_mai_tai.brokerdecides1 import decode
from tools.l12_close_asof_contract import CANDIDATE_HEAD, DURABLE, NATIVE, ROLES, is_sha
from tools.l12_close_acquisition_protocol import epoch_receipt
from tools.l12_close_line_decode import decode_line_restore
from tools.l12_composed_execution_binding import bind_composed_execution
from tools.l12_scoped_chunks_v2 import validate_v2
from tools.l12_today_close_replay import CLOSE_MS

POINTERS = {"broker_orders": "/tables/broker_orders", "broker_order_events": "/tables/broker_order_events",
    "fills": "/tables/fills", "trade_intents": "/tables/trade_intents", "dashboard_snapshots": "/tables/dashboard_snapshots",
    "retirement_receipts": "/tables/brokerdecides_dispatch_retirements",
    "retirement_evidence": "/tables/brokerdecides_dispatch_evidence",
    "managed_positions": "/tables/oms_managed_positions", "accounts_and_strategies": "/tables"}
TABLE_ROLE = {"retirement_receipts": "brokerdecides_dispatch_retirements",
              "retirement_evidence": "brokerdecides_dispatch_evidence"}


def bind_roles_v2(packet, reader, *, manifest_raw, metadata_raw, chunk_review,
        reader_pins, reviewed_role_hashes, expected_capture_head, now_ms, reviewed_boot_ms,
        execution_approval, observed_execution_root, observed_component_pins,
        epoch_before, epoch_after, reviewed_capture_profile_sha256, reconstruction_approval,
        execution_migration_sources=None, execution_schema=None, execution_exclusions=None):
    """Reader supplies local bytes. Every role/hash/pin is independently reviewed.

    Matching headers/bytes are not native completeness or BUY permission. Real
    normalizers, local evidence and live-time admission remain separate gates.
    """
    packet = deepcopy(packet)
    execution = bind_composed_execution(execution_approval, observed_root=observed_execution_root,
        observed_pins=observed_component_pins, migration_sources=execution_migration_sources,
        observed_schema=execution_schema, observed_exclusions=execution_exclusions)
    plan = validate_v2(manifest_raw, metadata_raw, reader, **chunk_review)
    if (not isinstance(packet, dict) or type(packet.get("version")) is not int or packet["version"] != 2
            or packet.get("purpose") != "L12_SCOPED_CLOSE_ROLE_PACKET_V2"
            or packet.get("schema_component_head") != CANDIDATE_HEAD
            or packet.get("clock_domain") != "UTC_UNIX_MS"
            or packet.get("capture_head") != expected_capture_head
            or expected_capture_head != chunk_review["expected_capture_head"]
            or packet.get("execution_root") != execution["execution_root"]
            or packet.get("snapshot_id") != plan["snapshot_id"]
            or type(packet.get("asof_ms")) is not int or packet["asof_ms"] != plan["asof_ms"]
            or type(now_ms) is not int or not CLOSE_MS <= packet["asof_ms"] <= now_ms
            or type(reviewed_boot_ms) is not int or reviewed_boot_ms <= packet["asof_ms"]
            or not isinstance(packet.get("publisher_epoch"), str) or not packet["publisher_epoch"]):
        raise ValueError("explicit_v2_source_snapshot_execution_clock_binding_required")
    accounts, artifacts = packet.get("accounts"), packet.get("artifacts")
    if (not isinstance(accounts, dict) or set(accounts) != {"schwab", "webull"}
            or any(not isinstance(value, str) or not value.startswith("live:") for value in accounts.values())
            or len(set(accounts.values())) != 2
            or not isinstance(artifacts, dict) or set(artifacts) != set(ROLES)
            or set(reader_pins) != set(ROLES) or set(reviewed_role_hashes) != set(ROLES)):
        raise ValueError("complete_code_owned_role_and_account_membership_required")
    bound, intervals, raw_roles, locations = {}, {}, {}, set()
    for role in ROLES:
        item = artifacts[role]
        if (not isinstance(item, dict) or not is_sha(reader_pins.get(role))
                or item.get("reader_source_sha256") != reader_pins[role]
                or not is_sha(reviewed_role_hashes.get(role)) or item.get("sha256") != reviewed_role_hashes[role]
                or not isinstance(item.get("reference"), str) or not item["reference"]
                or not isinstance(item.get("pointer"), str)):
            raise ValueError("independently_reviewed_role_preimage_required")
        location = (item["reference"], item["pointer"])
        if location in locations:
            raise ValueError("distinct_role_location_required")
        locations.add(location)
        raw = reader(item["reference"])
        if (not isinstance(raw, bytes) or not raw or len(raw) > (chunk_review["limits"].manifest_bytes if role in DURABLE else 4 * 1024 * 1024)
                or sha256(raw).hexdigest() != item["sha256"]
                or (role in DURABLE and raw != manifest_raw)
                or item.get("clock_domain") != "UTC_UNIX_MS"):
            raise ValueError("role_raw_hash_or_resource_bound_mismatch")
        raw_roles[role] = raw
        if role in DURABLE:
            if (item["pointer"] != POINTERS[role] or item.get("snapshot_id") != plan["snapshot_id"]
                    or item.get("asof_ms") != plan["asof_ms"] or type(item.get("asof_ms")) is not int):
                raise ValueError("durable_manifest_pointer_snapshot_mismatch")
            absent = role in TABLE_ROLE and plan["tables"][TABLE_ROLE[role]]["source_state"] == "ABSENT_CANDIDATE_PROJECTION"
            if absent:
                if (item.get("complete") is not False or item.get("accounting") != "SOURCE_ABSENT_PRIVATE_DERIVATION_REVIEWED"
                        or "row_count" in item or "source_rows" in item):
                    raise ValueError("projection_absence_must_not_be_marked_complete_or_empty_source")
                bound[role] = "SOURCE_ABSENT_ACCOUNTED_NOT_SOURCE_COMPLETE"
            else:
                if item.get("complete") is not True or item.get("accounting") != "FULL_PK_MEMBERSHIP_BOUND":
                    raise ValueError("durable_full_membership_accounting_required")
                bound[role] = "FULL_PK_MEMBERSHIP_BOUND_NOT_NATIVE_PROOF"
        elif role in NATIVE:
            start, end = item.get("request_started_ms"), item.get("response_completed_ms")
            if (item.get("complete") is not True or item["pointer"] != ""
                    or item.get("provider") != NATIVE[role] or item.get("account") != accounts[NATIVE[role]]
                    or item.get("scope") != "ACCOUNT_WIDE"
                    or type(start) is not int or type(end) is not int
                    or not CLOSE_MS <= start <= end <= plan["asof_ms"] or plan["asof_ms"] - start >= 20_000):
                raise ValueError("native_role_scope_or_original_wire_interval_unknown")
            intervals[role] = (start, end)
            bound[role] = "RAW_NATIVE_ROLE_BOUND_SEMANTIC_NORMALIZATION_REQUIRED"
        else:
            if (item["pointer"] != "" or item.get("complete") is not True
                    or item.get("publisher_epoch") != packet["publisher_epoch"]
                    or type(item.get("captured_at_ms")) is not int
                    or not CLOSE_MS <= item["captured_at_ms"] <= plan["asof_ms"]):
                raise ValueError("state_origin_epoch_or_capture_clock_unknown")
            bound[role] = "CAPTURED_RAW_STATE_BYTES_BOUND_NOT_BOOT_PROOF"
    epochs = epoch_receipt(epoch_before, epoch_after, asof_ms=plan["asof_ms"], native_intervals=intervals,
        reviewed_capture_head=expected_capture_head, reviewed_profile_sha256=reviewed_capture_profile_sha256)
    if (epochs["status"] != "EPOCH_AND_CLOCK_HEADERS_BOUND_NOT_NATIVE_SEMANTICS"
            or epoch_before["db_snapshot_id"] != plan["snapshot_id"]
            or epoch_before["source_schema_sha256"] != plan["schema_sha256"]):
        raise ValueError("actual_source_epoch_or_snapshot_changed")
    admission = artifacts["admission_snapshot"]
    origin = admission.get("origin")
    if origin == "EXPLICIT_REVIEWED_COUNTERFACTUAL_RECONSTRUCTION":
        expected = dict(classification="COUNTERFACTUAL_CANDIDATE_FROM_REAL_RAW_INPUTS",
            source_manifest_sha256=sha256(manifest_raw).hexdigest(),
            native_raw_hashes={role: artifacts[role]["sha256"] for role in NATIVE},
            admission_sha256=admission["sha256"], snapshot_id=plan["snapshot_id"],
            asof_ms=plan["asof_ms"], capture_head=expected_capture_head)
        if (not isinstance(reconstruction_approval, dict)
                or any(reconstruction_approval.get(key) != value for key, value in expected.items())
                or not isinstance(reconstruction_approval.get("review_id"), str) or not reconstruction_approval["review_id"]):
            raise ValueError("off_publisher_reconstruction_requires_separate_exact_review")
        bound["admission_snapshot"] = "EXPLICIT_COUNTERFACTUAL_NOT_HISTORICAL_CACHE"
    elif origin != "CAPTURED_BYTES":
        raise ValueError("admission_origin_unknown")
    snapshot = decode(raw_roles["admission_snapshot"])
    if (snapshot.epoch != packet["publisher_epoch"] or {book.account for book in snapshot.books} != set(accounts.values())
            or snapshot.completed_ms > plan["asof_ms"]):
        raise ValueError("admission_packet_epoch_accounts_or_wire_clock_mismatch")
    line = decode_line_restore(raw_roles["strategy_line_owner_notes"],
        reviewed_sha256=artifacts["strategy_line_owner_notes"]["sha256"],
        reader_source_sha256=reader_pins["strategy_line_owner_notes"],
        snapshot_id=plan["snapshot_id"], asof_ms=plan["asof_ms"])
    return dict(status="V2_ROLES_ACCOUNTED_NOT_SERVICE_NATIVE_OR_PERMISSION_PROOF", roles=bound,
        scope_manifest_sha256=plan["manifest_sha256"], source_schema_sha256=plan["schema_sha256"],
        execution_binding=execution, epoch=epochs, admission_origin=origin,
        actual_boot="UNRUN", native_completeness="UNQUALIFIED", permission_granted=False,
        line_restore=line, admission_snapshot=snapshot, chunk_validation=plan)
