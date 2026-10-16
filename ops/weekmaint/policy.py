"""Weekly-maintenance policy; broker facts never follow from DB terminalization."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FULL_TABLES = frozenset({"momentum_paper_events", "reconciliation_findings",
                         "market_capture_trades", "market_capture_quotes"})
TERMINAL = frozenset({"filled", "cancelled", "canceled", "rejected", "expired", "aborted",
                      "completed", "superseded"})
RETIRE_TABLES = frozenset({"broker_orders", "trade_intents", "oms_managed_positions", "virtual_positions"})
REASON = "weekmaint_stale"
BROKER_MAX_AGE = 30
BOOT_REQUIRED = ("recorded_services_restored", "oms_completed_sync_observed", "oms_first_commits_observed", "redis_read_ok",
                 "pg_read_ok", "guard_0340_scheduled", "preopen_scheduled", "universe_scheduled",
                 "SEC_scheduled", "prune_scheduled", "RETAIN1_unchanged")


class Refused(ValueError):
    pass


def aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise Refused("aware_clock_required")
    return value.astimezone(UTC)


def stamp(value):
    return aware(datetime.fromisoformat(value)) if isinstance(value, str) else aware(value)


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Refused("quantity_UNKNOWN") from None
    if not result.is_finite():
        raise Refused("quantity_UNKNOWN")
    return result


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def session_anchor(now):
    local = aware(now).astimezone(ET)
    anchor = local.replace(hour=4, minute=0, second=0, microsecond=0)
    if local < anchor:
        anchor -= timedelta(days=1)
    return anchor.astimezone(UTC)


def calendar_gate(now, *, force_test=False):
    local = aware(now).astimezone(ET)
    if local.weekday() != 5 and not force_test:
        raise Refused("Saturday_only_supervised_force_test_required")
    if local.weekday() == 5 and time(0) <= local.time() < time(1):
        raise Refused("Redis_00_01_window_excluded_even_force_test")


def sec_gate(receipt, now, expected_head, *, expected_lookup_day, force_test=False):
    """A current successful SEC execution, not After=, exit0 alone, or stale cache."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("report"), dict):
        raise Refused("SEC_receipt_UNKNOWN")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise Refused("SEC_approved_head_UNKNOWN")
    report = receipt["report"]
    if (not isinstance(report.get("http_errors"), dict)
            or not isinstance(report.get("counts"), dict)
            or not isinstance(report.get("sec_index_sha256"), str)):
        raise Refused("SEC_receipt_shape_UNKNOWN")
    started, ended = stamp(receipt.get("started_at")), stamp(receipt.get("ended_at"))
    local = aware(now).astimezone(ET)
    if ((local.weekday() != 5 and not force_test) or started.astimezone(ET).date() != local.date()
            or started.astimezone(ET).time() < time(1) or not started <= ended <= aware(now)
            or receipt.get("head") != expected_head or type(receipt.get("returncode")) is not int
            or receipt["returncode"] != 0
            or receipt.get("lookup_day") != expected_lookup_day
            or report.get("status") != "FULL" or report.get("cohort") != "SEC_INDEX"
            or report.get("security_type") != "TYPE_UNKNOWN"):
        raise Refused("current_Saturday_SEC_success_required")
    requested, completed = report.get("requested_symbols"), report.get("completed_symbols")
    if (type(requested) is not int or not 0 < requested <= 30_000 or completed != requested
            or type(completed) is not int or report.get("pending_symbols") != 0
            or type(report.get("pending_symbols")) is not int
            or type(report.get("sec_index_count")) is not int
            or not requested <= report["sec_index_count"] <= 40_000
            or len(str(report.get("sec_index_sha256", ""))) != 64
            or any(c not in "0123456789abcdef" for c in report["sec_index_sha256"])
            or report.get("http_errors", {}).get("403") != 0
            or report.get("http_errors", {}).get("429") != 0
            or type(report.get("http_errors", {}).get("403")) is not int
            or type(report.get("http_errors", {}).get("429")) is not int
            or type(report.get("counts", {}).get("FETCH_FAILED")) is not int
            or not 0 <= report["counts"]["FETCH_FAILED"] <= completed):
        raise Refused("SEC_incomplete_or_error")
    return sha(receipt)


def broker_gate(expected_accounts, evidence, now, *, stopped_at=None):
    """10:0x: bot DAY expiry plus the original single start-read evidence."""
    if not expected_accounts or set(evidence) != set(expected_accounts):
        raise Refused("broker_account_coverage_UNKNOWN")
    if set(expected_accounts.values()) != {"schwab", "webull"}:
        raise Refused("both_broker_venues_required")
    for account, provider in expected_accounts.items():
        item = evidence[account]
        if (not isinstance(item, dict) or not account.startswith("live:") or item.get("account") != account
                or item.get("provider") != provider):
            raise Refused("broker_account_binding_UNKNOWN")
        for kind in ("positions", "bot_orders"):
            read = item.get(kind, {})
            if (not isinstance(read, dict) or read.get("success") is not True
                    or read.get("complete") is not True):
                raise Refused(f"{kind}_success_complete_UNKNOWN:{account}")
            if (read.get("cached") is True or read.get("pagination_exhausted") is False
                    or ("http_status" in read and (type(read["http_status"]) is not int or read["http_status"] != 200))):
                raise Refused(f"{kind}_failed_partial_or_cached:{account}")
            expected_source = "existing_positions_read" if kind == "positions" else "Saturday_bot_DAY_expiry"
            if read.get("source") != expected_source:
                raise Refused(f"{kind}_source_UNKNOWN:{account}")
            # Recheck original evidence age, never refresh or require a second read.
            observed = stamp(read.get("read_at") if kind == "positions" else read.get("checked_at"))
            if not 0 <= (aware(now) - observed).total_seconds() <= BROKER_MAX_AGE:
                raise Refused(f"{kind}_freshness_UNKNOWN:{account}")
            if not isinstance(read.get("rows"), list):
                raise Refused(f"{kind}_rows_UNKNOWN:{account}")
        for row in item["positions"]["rows"]:
            if not row.get("symbol") or row.get("ownership_complete") is not True:
                raise Refused(f"bot_ownership_UNKNOWN:{account}")
            total, owned = number(row.get("quantity")), number(row.get("bot_owned_quantity"))
            if owned < 0 or owned > abs(total):
                raise Refused(f"bot_ownership_conflict:{account}")
            if owned != 0:
                raise Refused(f"broker_bot_position_not_flat:{account}")
        orders = item["bot_orders"]
        if (orders.get("all_bot_orders_DAY") is not True
                or orders.get("working_bot_orders") != 0
                or type(orders.get("working_bot_orders")) is not int
                or orders["rows"]):
            raise Refused(f"broker_bot_DAY_expiry_UNKNOWN:{account}")


def order_gate(rows, now):
    """Old DAY rows are retirement candidates, not a zero-row gate (08:0x)."""
    for item in rows:
        if item["table"] != "broker_orders" or item["row"].get("status") in TERMINAL:
            continue
        row = item["row"]
        if str(row.get("time_in_force", "")).upper() != "DAY":
            raise Refused("nonterminal_bot_order_not_DAY")
        existence = stamp(item.get("existence_at"))
        updated = stamp(row.get("updated_at"))
        submitted = stamp(row["submitted_at"]) if row.get("submitted_at") else existence
        if any(value > aware(now) for value in (existence, updated, submitted)):
            raise Refused("bot_order_future_clock_UNKNOWN")
        if max(updated, submitted) >= aware(now) - timedelta(hours=2):
            raise Refused("bot_order_moving_within_2h")


def gates(snapshot, now, *, force_test=False, stopped_at=None):
    calendar_gate(now, force_test=force_test)
    # 07:1x: open zero-quantity rows may retire, but only after broker_gate.
    for key in ("open_managed_nonzero_or_unknown", "nonterminal_live_intents_today",
                "recent_or_unknown_bot_order_intent_count"):
        if type(snapshot.get(key)) is not int or snapshot[key] != 0:
            raise Refused(f"{key}_not_zero_or_UNKNOWN")
    for key in ("guard_paper_outside_session", "install_exclusion_held", "writer_inventory_complete"):
        if snapshot.get(key) is not True:
            raise Refused(f"{key}_UNKNOWN")
    broker_gate(snapshot["live_accounts"], snapshot.get("broker_evidence", {}), now, stopped_at=stopped_at)
    if not isinstance(snapshot.get("nonterminal_bot_order_rows"), list):
        raise Refused("bot_order_inventory_UNKNOWN")
    order_gate(snapshot["nonterminal_bot_order_rows"], now)


@dataclass(frozen=True)
class Retirement:
    table: str
    row_id: str
    account: str
    symbol: str
    before: dict
    changes: dict
    before_json: str | None = None

    @property
    def source_hash(self):
        return hashlib.sha256(self.before_json.encode()).hexdigest() if self.before_json is not None else sha(self.before)


def retirement_plan(rows, now, *, broker_evidence=None):
    """Input rows are bound live-mode/live-alias rows from a read-only DB inventory."""
    selected, unknown = [], []
    anchor = session_anchor(now)
    for item in rows:
        table, row = item["table"], item["row"]
        if table not in RETIRE_TABLES:
            raise Refused("retirement_table_not_approved")
        if item.get("live_scope_verified") is not True:
            unknown.append({"table": table, "id": row["id"], "reason": "live_scope_UNKNOWN"})
            continue
        changes = {}
        if table in {"broker_orders", "trade_intents"}:
            if row.get("status") in TERMINAL:
                continue
            existence = item.get("existence_at")
            if existence is None:
                unknown.append({"table": table, "id": row["id"], "reason": "existence_anchor_UNKNOWN"})
                continue
            if stamp(existence) >= anchor:
                continue
            changes["status"] = "aborted" if table == "broker_orders" else "superseded"
            changes["payload"] = {"weekmaint": {"reason": REASON}}  # SQL merges, never re-encodes original prices.
        elif table == "oms_managed_positions":
            if row.get("status") != "open" or number(row.get("current_quantity")) != 0:
                continue
            changes = {"status": "closed"}
        elif number(row.get("quantity")) != 0:
            changes = {"quantity": "0"}
        if changes:
            if broker_evidence is not None:
                account = broker_evidence.get(item["account"], {})
                positions, orders = account.get("positions", {}), account.get("bot_orders", {})
                matching = [value for value in positions.get("rows", []) if value.get("symbol") == row["symbol"]]
                if (positions.get("success") is not True or positions.get("complete") is not True
                        or orders.get("complete") is not True or orders.get("working_bot_orders") != 0
                        or any(number(value.get("quantity")) != 0 for value in matching)):
                    unknown.append({"table": table, "id": row["id"], "reason": "symbol_not_broker_flat_leave_unchanged"})
                    continue
            changes["updated_at"] = aware(now).isoformat()
            selected.append(Retirement(table, str(row["id"]), item["account"], row["symbol"], row, changes,
                                       item.get("source_json_text")))
    return selected, unknown


def vacuum_decision(table, relation_bytes, free_bytes, *, measured_bloated, writers_stopped):
    if table not in FULL_TABLES or measured_bloated is not True or writers_stopped is not True:
        raise Refused("VACUUM_FULL_scope_or_quiescence_UNKNOWN")
    if type(relation_bytes) is not int or relation_bytes <= 0 or type(free_bytes) is not int or free_bytes < 0:
        raise Refused("VACUUM_FULL_disk_measurement_UNKNOWN")
    return {"table": table, "action": "VACUUM_FULL" if free_bytes >= 2 * relation_bytes else "SKIP_DISK_SHORT",
            "relation_bytes": relation_bytes, "free_bytes": free_bytes, "required_bytes": 2 * relation_bytes}


def run_job(backend, now, *, dry_run=True, force_test=False):
    """Recheck DB/writers after quiescence; reuse the single start positions read."""
    snapshot = backend.read_snapshot()
    plan, unknown = retirement_plan(backend.read_retirement_rows(), now)
    report = {"dry_run": dry_run, "retirements": plan, "unresolved_rows": unknown,
        "steps": ["gates_and_SEC_success", "durable_writer_inventory", "stop_exact_writers",
            "quiescent_DB_recheck_original_start_positions_age_bound", "archive_transaction_and_audit_CSV",
            "VACUUM_ANALYZE_all", "measured_four_table_VACUUM_FULL_disk_checks",
            "journal_30d", "security_only_approved_package_window",
            "durable_reboot_marker_if_required_else_restart_original_inventory", "single_boot_report"],
        "snapshot_pruning": "NONE; reader audit and per-type policy approval required"}
    try:
        gates(snapshot, now, force_test=force_test)
        sec_token = sec_gate(backend.read_sec_receipt(), now, backend.approved_head,
                             expected_lookup_day=backend.expected_lookup_day, force_test=force_test)
        report["SEC_success_sha256"] = sec_token
        report["gates"] = "PASS_REQUIRES_RECHECK"
    except (Refused, KeyError, TypeError, ValueError) as error:
        report.update(gates="REFUSED_OR_UNKNOWN", reason=str(error))
        if not dry_run:
            raise Refused(str(error)) from error
        return report
    if dry_run:
        return report
    if unknown:
        raise Refused("unresolved_retirement_inventory")
    if backend.live_contract_approved is not True or backend.security_window_approved is not True:
        raise Refused("broker_writer_install_and_package_contract_approval_pending")
    operation = backend.claim_once_per_SEC_success(sec_token)
    try:
        backend.persist_inventory_before_stop(operation, snapshot)
        stopped_at = backend.stop_exact_recorded_writers(operation)
        calendar_gate(backend.clock(), force_test=force_test)
        fresh = backend.read_snapshot()
        gates(fresh, backend.clock(), force_test=force_test, stopped_at=stopped_at)
        # Re-plan AFTER quiescence. Never apply the earlier preview through a race.
        plan, unknown = retirement_plan(backend.read_retirement_rows(), backend.clock(),
                                        broker_evidence=fresh["broker_evidence"])
        if (any(value["reason"] != "symbol_not_broker_flat_leave_unchanged" for value in unknown)
                or any(item.account not in fresh["live_accounts"] for item in plan)):
            raise Refused("quiescent_inventory_UNKNOWN")
        backend.record_skipped_rows(operation, unknown)
        backend.archive_transaction_with_csv(operation, plan)
        backend.vacuum_analyze_all_outside_transaction()
        for table in sorted(FULL_TABLES):
            calendar_gate(backend.clock(), force_test=force_test)
            size, free, measured = backend.measure_table_and_own_filesystem(table)
            decision = vacuum_decision(table, size, free, measured_bloated=measured, writers_stopped=True)
            if decision["action"] == "VACUUM_FULL":
                backend.vacuum_full_outside_transaction(table)
            backend.record_vacuum_decision(operation, decision)
        calendar_gate(backend.clock(), force_test=force_test)
        backend.vacuum_journal_30_days()
        calendar_gate(backend.clock(), force_test=force_test)
        backend.security_updates_only_preserving_existing_policy()
        calendar_gate(backend.clock(), force_test=force_test)
        if backend.reboot_required():
            backend.persist_reboot_marker(operation)
            backend.reboot_once(operation)
        else:
            backend.restore_recorded_active_inventory(operation)
            if backend.verify_and_report_once(operation) != "HEALTHY":
                raise Refused("verification_failed_or_UNKNOWN_no_success")
        return report | {"operation": operation, "status": "EXECUTION_FINISHED_VERIFY_RECEIPT"}
    except BaseException:
        # Unknown OS/DB operation outcome is not license for a blind restart/retry.
        backend.report_failure_keep_inventory(operation)
        raise


def boot_report(backend, operation):
    if not backend.claim_single_report(operation):
        return "ALREADY_CLAIMED_NO_RETRY"
    try:
        evidence = backend.read_boot_health_and_monday_schedule(operation)
    except BaseException as error:
        # A missing/failed probe also gets one failed report attempt. Never
        # retry an ambiguous report write or turn missing evidence into health.
        try:
            backend.write_boot_report_and_unique_health_line(operation,
                {**{key: False for key in BOOT_REQUIRED}, "probe_failure_type": type(error).__name__}, False)
        finally:
            backend.report_failure_keep_inventory(operation)
        raise
    try:
        good = all(evidence.get(key) is True for key in BOOT_REQUIRED)
        backend.write_boot_report_and_unique_health_line(operation, evidence, good)
        if good:
            backend.clear_marker(operation)
        return "HEALTHY" if good else "FAILED_LOUD_NO_RETRY"
    except BaseException:
        backend.report_failure_keep_inventory(operation)
        raise
