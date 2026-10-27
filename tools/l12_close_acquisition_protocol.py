"""Pure post-close input/epoch protocol. No IO, subprocess, provider or approval.

Plan can be authored before16. It never collects, stops writers, rewrites schema,
infers empty books, stamps completeness or makes a missing OFF cache historical.
"""

from tools.l12_close_asof_contract import NATIVE, ROLES, is_head, is_sha
from tools.l12_today_close_replay import CLOSE_MS

EPOCH_FIELDS = ("boot_id", "process_invocation_id", "capture_head", "db_snapshot_id",
                "writer_exclusion_id", "source_schema_sha256", "settings_profile_sha256")


def acquisition_plan(*, capture_head, execution_head, accounts, reader_pins, profile_sha256):
    if (not is_head(capture_head) or not is_head(execution_head) or not is_sha(profile_sha256)
            or not isinstance(accounts, dict) or set(accounts) != {"schwab", "webull"}
            or len(set(accounts.values())) != 2
            or any(not isinstance(value, str) or not value.startswith("live:") for value in accounts.values())
            or set(reader_pins) != set(ROLES) or any(not is_sha(value) for value in reader_pins.values())):
        raise ValueError("independently_reviewed_capture_contract_required")
    steps = [dict(number=1, action="MAIN_REVIEW_SOURCE_SCHEMA_PROFILE_AND_EXCLUSION",
                  required=list(EPOCH_FIELDS), mutation="NONE_AUTHORIZED_BY_THIS_PLAN")]
    for role, provider in NATIVE.items():
        steps.append(dict(number=len(steps) + 1, action="AUTHORIZED_COLLECTOR_CAPTURE_OR_REUSE_RAW",
            role=role, provider=provider, account=accounts[provider], reader_sha256=reader_pins[role],
            required=["unmodified_raw_bytes_and_hash", "actual_request_and_completion_UTC_ms",
                      "native_account_identity", "scope_and_all_page_end_evidence",
                      "gross_positions_not_net_only" if "positions" in role else "nonterminal_orders_not_today_only"],
            missing_or_nonempty_unproved="UNKNOWN_NO_FLAT_SUBSTITUTE"))
    steps.extend([
        dict(number=6, action="MAIN_COHERENT_READONLY_DURABLE_AND_OWNER_LINE_EXPORT",
             required=["single_reviewed_snapshot_and_asof", "all_decoder_tables_and_columns_even_zero_counts",
                       "all_local_live_alias_inventory", "exact_row_counts_no_truncation",
                       "source_primary_keys_payloads_events_fills_retained", "source_schema_hash",
                       "original_bar_availability_times_and_explicit_coverage_only"],
             missing_projection_tables="REFUSE_PENDING_EXPLICIT_EMPTY_ADDITIVE_SCHEMA_POLICY"),
        dict(number=7, action="BIND_ADMISSION_STATE_OR_REVIEW_EXPLICIT_RECONSTRUCTION",
             installed_L12_OFF="NO_HISTORICAL_CACHE_INVENTION",
             reconstruction="SEPARATELY_REVIEWED_COUNTERFACTUAL_FROM_REAL_NATIVE_AND_DURABLE_RAW",
             missing="UNKNOWN_NO_PERMISSION"),
        dict(number=8, action="COMPARE_ACTUAL_BEFORE_AFTER_EPOCH_AND_CAPTURE_CLOCKS",
             exact_equal=list(EPOCH_FIELDS), failure="PRESERVE_RAW_REFUSE_NO_AUTORETRY"),
        dict(number=9, action="MAIN_REVIEW_HASHES_DECODER_AND_EXACT_PRIVATE_REPLAY_CLAIM",
             chain=["decode_close_copy", "decode_line_restore", "bound_close_replay"],
             required=["approved_and_independently_observed_execution_head",
                       "exact_future_boot_clock_session", "held_private_target_and_writer_fence",
                       "durable_postcommit_fsync_journal", "original_750ms_250ms_guards"],
             failure="PRESERVE_PARTIAL_COPY_JOURNAL_NO_RESEED_OR_AUTORETRY"),
    ])
    return dict(status="ACQUISITION_PROTOCOL_READY_FOR_REVIEW_NOT_EXECUTION", capture_head=capture_head,
        execution_head=execution_head, settings_profile_sha256=profile_sha256, accounts=dict(accounts),
        reader_pins=dict(reader_pins), not_before_ms=CLOSE_MS, roles=list(ROLES), steps=steps,
        permission_granted=False, acquisition_executed=False,
        real_close_proof="UNKNOWN_UNTIL_ACTUAL_AFTER16_RECEIPTS")


def epoch_receipt(before, after, *, asof_ms, native_intervals, reviewed_capture_head, reviewed_profile_sha256):
    """Validate supplied evidence, not evidence acquisition or native semantics.

    Monotonic clocks establish ordering only within the exact boot/invocation.
    Caller cannot promote this result to a complete native book or install GO.
    """
    issues = []
    if not isinstance(before, dict) or not isinstance(after, dict):
        return dict(status="UNKNOWN", issues=["EPOCH_RECEIPTS_MISSING"], permission_granted=False)
    for key in EPOCH_FIELDS:
        if not isinstance(before.get(key), str) or not before[key] or before[key] != after.get(key):
            issues.append("EPOCH_CHANGED_OR_MISSING:" + key)
    if before.get("capture_head") != reviewed_capture_head or not is_head(reviewed_capture_head):
        issues.append("CAPTURE_HEAD_NOT_REVIEWED")
    if (before.get("settings_profile_sha256") != reviewed_profile_sha256
            or not is_sha(reviewed_profile_sha256) or not is_sha(before.get("source_schema_sha256"))):
        issues.append("PROFILE_OR_SCHEMA_UNBOUND")
    for record in (before, after):
        if record.get("writers_excluded") is not True:
            issues.append("WRITER_EXCLUSION_UNKNOWN")
    start, end = before.get("observed_ms"), after.get("observed_ms")
    mono_start, mono_end = before.get("monotonic_ns"), after.get("monotonic_ns")
    if (any(type(value) is not int for value in (start, asof_ms, end, mono_start, mono_end))
            or not CLOSE_MS <= start <= asof_ms <= end or not 0 < mono_start <= mono_end):
        issues.append("AFTER_CLOSE_CLOCK_OR_MONOTONIC_ORDER_UNKNOWN")
    if not isinstance(native_intervals, dict) or set(native_intervals) != set(NATIVE):
        issues.append("FOUR_NATIVE_ROLE_INTERVALS_REQUIRED")
    else:
        for role, pair in native_intervals.items():
            if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                    or any(type(value) is not int for value in pair)
                    or type(asof_ms) is not int
                    or not CLOSE_MS <= pair[0] <= pair[1] <= asof_ms
                    or asof_ms - pair[0] >= 20_000
                    or type(start) is not int or pair[0] < start):
                issues.append("NATIVE_INTERVAL_STALE_OR_UNBOUND:" + role)
    return dict(status="EPOCH_AND_CLOCK_HEADERS_BOUND_NOT_NATIVE_SEMANTICS" if not issues else "UNKNOWN",
        issues=issues, permission_granted=False, native_completeness="UNQUALIFIED")
