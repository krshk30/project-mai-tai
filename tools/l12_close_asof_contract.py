"""Pure input binding, NOT a collector, native decoder, replay or permission.

The role catalog is code-owned, not supplied by the packet or observed values.
Even fully bound bytes require reader/source review and actual callback/boot.
"""

import hashlib
import re

CANDIDATE_HEAD = "aa87a13c0ea36adc8c982f77783026121f38cc65"
NATIVE = {
    "schwab_working_orders": "schwab", "schwab_gross_positions": "schwab",
    "webull_working_orders": "webull", "webull_gross_positions": "webull",
}
DURABLE = (
    "broker_orders", "broker_order_events", "fills", "trade_intents", "dashboard_snapshots",
    "retirement_receipts", "retirement_evidence", "managed_positions", "accounts_and_strategies",
)
STATE = ("admission_snapshot", "strategy_line_owner_notes")
ROLES = (*NATIVE, *DURABLE, *STATE)


def is_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def is_head(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def inspect_packet(packet, blobs, *, expected_capture_head, reader_pins):
    """blobs is already-read local bytes supplied by MAIN; never opens a file.

    reader_pins is a SEPARATE reviewed source-policy operand, never inferred
    from a record's observed reader hash. All fifteen roles are always reported.
    """
    packet = packet if isinstance(packet, dict) else {}
    blobs = blobs if isinstance(blobs, dict) else {}
    reader_pins = reader_pins if isinstance(reader_pins, dict) else {}
    issues = []
    if (type(packet.get("version")) is not int or packet["version"] != 1
            or packet.get("purpose") != "L12_ACTUAL_CLOSE_ASOF_INPUTS"
            or packet.get("candidate_head") != CANDIDATE_HEAD
            or not is_head(expected_capture_head) or packet.get("capture_head") != expected_capture_head):
        issues.append("PACKET_SOURCE_BINDING_UNKNOWN")
    asof = packet.get("asof_ms")
    if type(asof) is not int or asof <= 0 or packet.get("clock_domain") != "UTC_UNIX_MS":
        issues.append("ASOF_CLOCK_UNKNOWN")
        asof = None
    accounts = packet.get("accounts")
    if (not isinstance(accounts, dict) or set(accounts) != {"schwab", "webull"}
            or any(not isinstance(name, str) or not name for name in accounts.values())
            or len(set(accounts.values())) != 2):
        issues.append("BOTH_ACCOUNT_IDENTITIES_UNKNOWN")
        accounts = {}
    if not isinstance(packet.get("db_snapshot_id"), str) or not packet["db_snapshot_id"]:
        issues.append("COMMON_DURABLE_SNAPSHOT_UNKNOWN")
    if not isinstance(packet.get("publisher_epoch"), str) or not packet["publisher_epoch"]:
        issues.append("PUBLISHER_EPOCH_UNKNOWN")
    entries = packet.get("artifacts") if isinstance(packet.get("artifacts"), dict) else {}
    if set(entries) - set(ROLES):
        issues.append("UNREVIEWED_ARTIFACT_ROLE")
    rows, locations = {}, {}
    for role in ROLES:
        faults = []
        record = entries.get(role)
        if not isinstance(record, dict):
            rows[role] = {"state": "UNKNOWN", "issues": ["ARTIFACT_NOT_DELIVERED"]}
            continue
        if not is_sha(reader_pins.get(role)) or record.get("reader_source_sha256") != reader_pins[role]:
            faults.append("READER_SOURCE_PIN_UNBOUND")
        reference, pointer = record.get("reference"), record.get("record_pointer")
        if (not isinstance(reference, str) or not reference or not isinstance(pointer, str)
                or (pointer and not pointer.startswith("/"))):
            faults.append("RAW_LOCATION_UNKNOWN")
            reference = None
        else:
            locations.setdefault((reference, pointer), []).append(role)
        content = blobs.get(reference)
        if (not isinstance(content, bytes) or not content or not is_sha(record.get("sha256"))
                or hashlib.sha256(content).hexdigest() != record["sha256"]):
            faults.append("RAW_BYTES_MISSING_OR_HASH_MISMATCH")
        if record.get("complete") is not True:
            faults.append("READER_COMPLETENESS_UNKNOWN")
        if record.get("clock_domain") != "UTC_UNIX_MS":
            faults.append("ARTIFACT_CLOCK_UNKNOWN")
        if role in NATIVE:
            provider = NATIVE[role]
            if (record.get("provider") != provider or not accounts
                    or record.get("account") != accounts.get(provider)
                    or record.get("scope") != "ACCOUNT_WIDE"):
                faults.append("NATIVE_ACCOUNT_SCOPE_UNKNOWN")
            start, end = record.get("request_started_ms"), record.get("response_completed_ms")
            if (asof is None or type(start) is not int or type(end) is not int
                    or not 0 < start <= end <= asof or asof - start >= 20_000):
                faults.append("NATIVE_WIRE_INTERVAL_STALE_OR_UNKNOWN")
        elif role in DURABLE:
            if (record.get("db_snapshot_id") != packet.get("db_snapshot_id")
                    or asof is None or record.get("snapshot_asof_ms") != asof
                    or type(record.get("snapshot_asof_ms")) is not int):
                faults.append("DURABLE_COMMON_ASOF_UNKNOWN")
        else:
            stamp = record.get("captured_at_ms")
            if (record.get("publisher_epoch") != packet.get("publisher_epoch")
                    or asof is None or type(stamp) is not int or not 0 < stamp <= asof):
                faults.append("STATE_EPOCH_OR_ASOF_UNKNOWN")
        rows[role] = {"state": "UNKNOWN" if faults else "BYTES_AND_HEADERS_BOUND_NOT_SEMANTIC_PROOF",
            "issues": faults, "reference": reference, "record_pointer": pointer,
            "raw_sha256": record.get("sha256")}
    for roles in locations.values():
        if len(roles) > 1:
            for role in roles:
                rows[role]["issues"].append("SAME_RAW_LOCATION_REUSED_FOR_DISTINCT_ROLES")
                rows[role]["state"] = "UNKNOWN"
    bound = sum(not row["issues"] for row in rows.values())
    return {"status": "FORMAT_BOUND_REQUIRES_REVIEW" if not issues and bound == len(ROLES) else "INPUTS_UNKNOWN",
        "candidate_head": CANDIDATE_HEAD, "capture_head": packet.get("capture_head"),
        "expected_roles": len(ROLES), "bound_roles": bound, "issues": issues, "roles": rows,
        "native_flatness": "UNKNOWN_NOT_DECODED_OR_QUALIFIED",
        "original_buy_acceptance": "UNRUN", "actual_close_boot": "UNRUN", "permission_granted": False}
