"""Normalize V2 native role bytes without manufacturing V1-complete projections.

No provider, target, service, clock projection or admission construction. Every
call revalidates V2 bytes/epoch first, then existing production native readers.
Boundary agreement is not future boot freshness or permission to BUY.
"""

from hashlib import sha256

from project_mai_tai.brokerdecides1_native import schwab_book, webull_book
from tools.l12_close_asof_contract import NATIVE
from tools.l12_close_export_restore import strict_json
from tools.l12_scoped_role_packet_v2 import bind_roles_v2


# Only source-owned diagnostic codes may be exposed; raw inputs/exception text
# can include private response data and are never copied into a result.
FAILURE_CODES = frozenset({
    "v2_source_scope_snapshot_binding_unknown",
    "explicit_v2_source_snapshot_execution_clock_binding_required",
    "explicit_composed_root_and_component_review_required",
    "explicit_class_source_closure_review_required",
    "explicit_class_version_required",
    "independently_reviewed_role_preimage_required",
    "reviewed_native_identity_or_http_unknown",
    "native_raw_changed_after_role_binding",
    "native_unknown_or_admission_economics_scope_clock_disagree",
})


def _economic(book):
    return dict(account=book.account, provider=book.provider, working=sorted(book.working),
        positions=sorted(book.positions), complete=book.complete, scope_policy=book.scope_policy,
        window_days=book.window_days, wire_started_ms=book.wire_started_ms,
        wire_completed_ms=book.wire_completed_ms)


def normalize_v2_native(packet, reader, *, role_review, reviewed_native_accounts,
        reviewed_schwab_scope, reviewed_schwab_window_days,
        http_statuses, reviewed_webull_order_request_account):
    """Fail before any private copy; caller retains raw bytes, not error payloads."""
    result = dict(status="V2_NATIVE_UNKNOWN_NO_PRIVATE_COPY", permission_granted=False,
        acquisition_executed=False, admission_packet_constructed=False, actual_boot="UNRUN")
    phase = "ROLE_BINDING"
    try:
        bound = bind_roles_v2(packet, reader, **role_review)
        phase = "NATIVE_IDENTITY_HTTP"
        if (not isinstance(reviewed_native_accounts, dict) or set(reviewed_native_accounts) != {"schwab", "webull"}
                or any(not isinstance(value, str) or not value for value in reviewed_native_accounts.values())
                or not isinstance(http_statuses, dict) or set(http_statuses) != set(NATIVE)
                or any(type(value) is not int or value != 200 for value in http_statuses.values())):
            raise ValueError("reviewed_native_identity_or_http_unknown")
        phase = "NATIVE_RAW_HASHES"
        records = packet["artifacts"]
        bodies = {}
        for role in NATIVE:
            record = records[role]
            data = reader(record["reference"])
            if not isinstance(data, bytes) or sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("native_raw_changed_after_role_binding")
            bodies[role] = strict_json(data)

        def interval(provider):
            items = [records[role] for role, value in NATIVE.items() if value == provider]
            return min(item["request_started_ms"] for item in items), max(item["response_completed_ms"] for item in items)

        phase = "NATIVE_NORMALIZATION"
        start, end = interval("schwab")
        schwab = schwab_book(account=packet["accounts"]["schwab"], native_account=reviewed_native_accounts["schwab"],
            account_body=bodies["schwab_gross_positions"], orders=bodies["schwab_working_orders"],
            wire_started_ms=start, wire_completed_ms=end, scope=reviewed_schwab_scope,
            complete=True, window_days=reviewed_schwab_window_days)
        start, end = interval("webull")
        webull = webull_book(account=packet["accounts"]["webull"], native_account=reviewed_native_accounts["webull"],
            position_pages=bodies["webull_gross_positions"], order_pages=bodies["webull_working_orders"],
            wire_started_ms=start, wire_completed_ms=end, scope="all_native_working_including_previous_sessions",
            complete=True, order_request_account=reviewed_webull_order_request_account)
        books = (schwab, webull)
        snapshot = bound["admission_snapshot"]
        phase = "ADMISSION_ECONOMICS"
        expected = sorted((_economic(book) for book in snapshot.books), key=lambda item: item["provider"])
        actual = sorted((_economic(book) for book in books), key=lambda item: item["provider"])
        if not all(book.complete for book in books) or expected != actual:
            raise ValueError("native_unknown_or_admission_economics_scope_clock_disagree")
        result.update(status="V2_NATIVE_SEMANTICS_BOUND_NOT_BOOT_OR_PERMISSION", books=actual,
            role_boundary=bound, admission_origin=bound["admission_origin"],
            future_boot_freshness="NOT_ESTABLISHED_OR_RETIMED", local_evidence="NOT_ESTABLISHED_BY_NATIVE_BOOK")
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        code = str(exc)
        result.update(failure_type=type(exc).__name__, failure_stage=phase,
            failure_code=code if code in FAILURE_CODES else "UNCLASSIFIED_LOCAL_INPUT_ERROR")
    return result
