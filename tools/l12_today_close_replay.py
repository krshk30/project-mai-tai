"""Pure staging contract for the real October 23 close; never runs a bot.

This is the input stage of the replay harness, not a permission or a replay
result. An actual isolated boot caller must be reviewed separately.
"""

from tools.l12_close_asof_contract import CANDIDATE_HEAD, ROLES, inspect_packet, is_sha

# October 23, 2026 16:00 America/New_York (UTC-04:00).
CLOSE_MS = 1_792_785_600_000
SESSION_DATE = "2026-10-23"


def stage_replay(packet, blobs, *, now_ms, expected_capture_head, reader_pins,
                 reviewed_raw_hashes, reviewed_boot_at_ms):
    """Inputs are already-read objects supplied by main; no I/O or live clock.

    Reviewed hashes and boot target are separate operands, never copied from
    packet fields by this helper. No historical or pre-close fallback exists.
    """
    binding = inspect_packet(packet, blobs, expected_capture_head=expected_capture_head,
                             reader_pins=reader_pins)
    packet = packet if isinstance(packet, dict) else {}
    review = reviewed_raw_hashes if isinstance(reviewed_raw_hashes, dict) else {}
    issues = []
    asof = packet.get("asof_ms")
    if type(now_ms) is not int or now_ms < CLOSE_MS:
        issues.append("REAL_CLOSE_NOT_REACHED_OR_CLOCK_UNKNOWN")
    if (packet.get("session_date") != SESSION_DATE or type(asof) is not int
            or asof < CLOSE_MS or type(now_ms) is not int or asof > now_ms):
        issues.append("TODAY_POST_CLOSE_ASOF_UNKNOWN")
    if (type(reviewed_boot_at_ms) is not int or type(asof) is not int
            or reviewed_boot_at_ms <= asof):
        issues.append("REVIEWED_FUTURE_BOOT_TARGET_UNKNOWN")
    if binding["status"] != "FORMAT_BOUND_REQUIRES_REVIEW":
        issues.append("SOURCE_INPUT_BINDING_UNKNOWN")
    if set(review) != set(ROLES):
        issues.append("REVIEWED_RAW_ROLE_DENOMINATOR_UNKNOWN")
    entries = packet.get("artifacts")
    entries = entries if isinstance(entries, dict) else {}
    for role in ROLES:
        record = entries.get(role)
        if (not is_sha(review.get(role)) or not isinstance(record, dict)
                or record.get("sha256") != review.get(role)):
            issues.append("REVIEWED_RAW_HASH_UNKNOWN:" + role)
        if isinstance(record, dict):
            stamp = record.get("request_started_ms", record.get("captured_at_ms", asof))
            if type(stamp) is not int or stamp < CLOSE_MS:
                issues.append("ARTIFACT_NOT_POST_CLOSE:" + role)
    # Carry byte identities, not mutable payload references or a fake result.
    identities = {role: binding["roles"][role].get("raw_sha256") for role in ROLES}
    return {
        "status": "INPUT_STAGE_BOUND_NOT_BOOT_EXECUTED" if not issues else "REPLAY_INPUTS_UNKNOWN",
        "candidate_head": CANDIDATE_HEAD, "capture_head": expected_capture_head,
        "session_date": SESSION_DATE, "close_ms": CLOSE_MS, "asof_ms": asof,
        "reviewed_boot_at_ms": reviewed_boot_at_ms, "issues": issues,
        "raw_hashes": identities, "input_binding": binding,
        "boot_result": "UNRUN", "native_flatness": "UNKNOWN",
        "permission_granted": False,
    }
