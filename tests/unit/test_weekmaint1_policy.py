"""Controlled maintenance policy/sequence inputs, not live broker/systemd evidence."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from uuid import UUID

import pytest

from ops.weekmaint import policy

NOW = datetime(2026, 10, 17, 14, tzinfo=UTC)
ACCOUNTS = {"live:controlled-schwab": "schwab", "live:controlled-webull": "webull"}
HEAD = "a" * 40


def broker_evidence(now=NOW):
    result = {account: {"account": account, "provider": provider, **{
        kind: {"source": "existing_positions_read" if kind == "positions" else "Saturday_bot_DAY_expiry",
               "success": True, "http_status": 200,
               "complete": True, "cached": False, "pagination_exhausted": True,
               "read_at": now.isoformat(), "checked_at": now.isoformat(), "rows": []}
        for kind in ("positions", "bot_orders")}}
        for account, provider in ACCOUNTS.items()}
    for item in result.values():
        item["bot_orders"].update(all_bot_orders_DAY=True, working_bot_orders=0)
    return result


def sec_receipt():
    return {"head": HEAD, "lookup_day": "2026-10-19", "returncode": 0,
        "started_at": "2026-10-17T05:00:00+00:00", "ended_at": "2026-10-17T06:00:00+00:00",
        "report": {"status": "FULL", "cohort": "SEC_INDEX", "security_type": "TYPE_UNKNOWN",
            "requested_symbols": 2, "completed_symbols": 2, "pending_symbols": 0,
            "sec_index_count": 2, "sec_index_sha256": "b" * 64,
            "http_errors": {"403": 0, "429": 0}, "counts": {"FETCH_FAILED": 0}}}


def snapshot(now=NOW):
    return {"live_accounts": ACCOUNTS.copy(), "broker_evidence": broker_evidence(now),
        "open_managed_nonzero_or_unknown": 0, "open_managed_zero": 0,
        "nonterminal_live_intents_today": 0,
        "nonterminal_bot_order_rows": [],
        "recent_or_unknown_bot_order_intent_count": 0,
        "guard_paper_outside_session": True, "install_exclusion_held": True,
        "writer_inventory_complete": True}


@pytest.mark.parametrize("key,value", [
    ("open_managed_nonzero_or_unknown", 1), ("open_managed_nonzero_or_unknown", None),
    ("nonterminal_live_intents_today", 1),
    ("nonterminal_live_intents_today", None), ("guard_paper_outside_session", False),
    ("install_exclusion_held", False), ("writer_inventory_complete", False),
])
def test_each_db_session_install_writer_gate_refuses(key, value):
    facts = snapshot()
    facts[key] = value
    with pytest.raises(policy.Refused):
        policy.gates(facts, NOW)


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("kind,change", [
    ("positions", {"rows": [{"symbol": "ABC", "quantity": "1", "bot_owned_quantity": "1", "ownership_complete": True}]}),
    ("positions", {"rows": [{"symbol": "ABC", "quantity": "-1", "bot_owned_quantity": "1", "ownership_complete": True}]}),
    ("bot_orders", {"rows": [{"symbol": "ABC", "id": "controlled-order"}]}),
    ("positions", {"cached": True}), ("positions", {"http_status": 429}),
    ("positions", {"success": False}), ("positions", {"complete": False}),
    ("bot_orders", {"pagination_exhausted": False}),
    ("bot_orders", {"checked_at": (NOW - timedelta(seconds=31)).isoformat()}),
    ("positions", {"read_at": (NOW + timedelta(seconds=1)).isoformat()}),
    ("positions", {"rows": [{"quantity": "NaN"}]}),
])
def test_each_broker_gate_and_unknown_refuses_both_venues(account, kind, change):
    facts = snapshot()
    facts["broker_evidence"][account][kind].update(change)
    with pytest.raises(policy.Refused):
        policy.gates(facts, NOW)


def test_empty_from_db_or_missing_account_is_not_broker_flat_proof():
    facts = snapshot()
    facts["broker_evidence"].pop(next(iter(ACCOUNTS)))
    with pytest.raises(policy.Refused, match="coverage"):
        policy.gates(facts, NOW)


def test_single_start_read_survives_stop_without_relabel_and_expires_instead_of_refresh():
    facts = snapshot()
    original = deepcopy(facts)
    policy.gates(facts, NOW + timedelta(seconds=2), stopped_at=NOW + timedelta(seconds=1))
    with pytest.raises(policy.Refused, match="freshness"):
        policy.gates(facts, NOW + timedelta(seconds=31), stopped_at=NOW + timedelta(seconds=1))
    assert facts == original


@pytest.mark.parametrize("local_hour", [0, 1])
def test_redis_window_boundary_and_force_test_do_not_override_exclusion(local_hour):
    now = datetime(2026, 10, 17, 4 + local_hour, tzinfo=UTC)
    if local_hour == 0:
        with pytest.raises(policy.Refused, match="Redis"):
            policy.calendar_gate(now, force_test=True)
    else:
        policy.calendar_gate(now)


def test_weekday_requires_force_test_but_not_automatic_weekday_timer():
    friday = NOW - timedelta(days=1)
    with pytest.raises(policy.Refused, match="Saturday"):
        policy.calendar_gate(friday)
    policy.calendar_gate(friday, force_test=True)


@pytest.mark.parametrize("mutation", ["previous_week", "wrong_head", "failed", "partial", "pending", "wrong_day", "http429"])
def test_after_ordering_or_old_sec_success_cannot_authorize(mutation):
    receipt = sec_receipt()
    if mutation == "previous_week":
        receipt["started_at"] = "2026-10-10T05:00:00+00:00"
    elif mutation == "wrong_head":
        receipt["head"] = "c" * 40
    elif mutation == "failed":
        receipt["returncode"] = 1
    elif mutation == "partial":
        receipt["report"]["completed_symbols"] = 1
    elif mutation == "pending":
        receipt["report"]["pending_symbols"] = 1
    elif mutation == "wrong_day":
        receipt["lookup_day"] = "2026-10-20"
    else:
        receipt["report"]["http_errors"]["429"] = 1
    with pytest.raises(policy.Refused):
        policy.sec_gate(receipt, NOW, HEAD, expected_lookup_day="2026-10-19")


def retained_row(table, *, status="working", existence=None, quantity="1", ordinal=1):
    row = {"id": str(UUID(int=ordinal)), "symbol": "ABC", "status": status,
           "quantity": quantity, "current_quantity": quantity, "time_in_force": "DAY",
           "updated_at": (NOW - timedelta(days=2)).isoformat(),
           "payload": {"price": "1.23456789"}}
    return {"table": table, "account": next(iter(ACCOUNTS)), "live_scope_verified": True,
            "existence_at": existence, "row": row, "source_json_text": json.dumps(row, sort_keys=True)}


@pytest.mark.parametrize("table", ["broker_orders", "trade_intents"])
def test_retire_only_previous_session_and_keep_anchored_same_session_or_unknown(table):
    anchor = policy.session_anchor(NOW)
    rows = [retained_row(table, existence=anchor - timedelta(seconds=1), ordinal=1),
            retained_row(table, existence=anchor, ordinal=2),
            retained_row(table, existence=None, ordinal=3)]
    original = deepcopy(rows)
    plan, unknown = policy.retirement_plan(rows, NOW)
    assert [item.row_id for item in plan] == [str(UUID(int=1))]
    assert [item["id"] for item in unknown] == [str(UUID(int=3))]
    assert rows == original and plan[0].before_json == rows[0]["source_json_text"]
    assert plan[0].changes["payload"] == {"weekmaint": {"reason": "weekmaint_stale"}}


def test_vp_original_quantity_retained_and_rerun_terminal_plan_is_empty():
    item = retained_row("virtual_positions", quantity="1000")
    plan, unknown = policy.retirement_plan([item], NOW)
    assert not unknown and plan[0].before["quantity"] == "1000" and plan[0].changes["quantity"] == "0"
    updated = deepcopy(item)
    updated["row"].update(plan[0].changes)
    assert policy.retirement_plan([updated], NOW) == ([], [])


def test_open_zero_managed_does_not_block_when_both_brokers_are_proven_flat():
    row = retained_row("oms_managed_positions", status="open", quantity="0")
    facts = snapshot()
    facts["open_managed_zero"] = 1
    policy.gates(facts, NOW)
    plan, unknown = policy.retirement_plan([row], NOW)
    assert not unknown and len(plan) == 1
    assert plan[0].before["current_quantity"] == "0"
    assert plan[0].changes["status"] == "closed"


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("kind,rows", [
    ("positions", [{"symbol": "ABC", "quantity": "1", "bot_owned_quantity": "1", "ownership_complete": True}]),
    ("bot_orders", [{"symbol": "ABC", "id": "controlled-order"}]),
])
def test_zero_managed_never_overrides_account_symbol_broker_position_or_order(account, kind, rows):
    backend = Backend()
    facts = snapshot()
    facts["open_managed_zero"] = 1
    facts["broker_evidence"][account][kind]["rows"] = rows
    backend.read_snapshot = lambda: facts
    backend.read_retirement_rows = lambda: [retained_row("oms_managed_positions", status="open", quantity="0")]
    with pytest.raises(policy.Refused, match="broker_"):
        policy.run_job(backend, NOW, dry_run=False)
    assert not backend.effects
    report = policy.run_job(backend, NOW)
    assert report["gates"] == "REFUSED_OR_UNKNOWN" and not backend.effects
    assert report["retirements"][0].before["current_quantity"] == "0"


@pytest.mark.parametrize("quantity", ["1", "-1", None])
def test_open_nonzero_or_unknown_managed_still_blocks(quantity):
    row = retained_row("oms_managed_positions", status="open", quantity=quantity)
    if quantity is None:
        with pytest.raises(policy.Refused, match="quantity_UNKNOWN"):
            policy.retirement_plan([row], NOW)
    else:
        assert policy.retirement_plan([row], NOW) == ([], [])
    facts = snapshot()
    facts["open_managed_nonzero_or_unknown"] = 1
    with pytest.raises(policy.Refused, match="open_managed_nonzero_or_unknown"):
        policy.gates(facts, NOW)


@pytest.mark.parametrize("free,expected", [(199, "SKIP_DISK_SHORT"), (200, "VACUUM_FULL")])
def test_disk_bound_is_twice_measured_relation_size(free, expected):
    assert policy.vacuum_decision("momentum_paper_events", 100, free,
        measured_bloated=True, writers_stopped=True)["action"] == expected


@pytest.mark.parametrize("table,measured,stopped", [("fills", True, True), ("dashboard_snapshots", True, True),
                                                    ("market_capture_quotes", False, True), ("market_capture_trades", True, False)])
def test_no_unapproved_table_or_unmeasured_rewrite(table, measured, stopped):
    with pytest.raises(policy.Refused):
        policy.vacuum_decision(table, 100, 200, measured_bloated=measured, writers_stopped=stopped)


class Backend:
    approved_head = HEAD
    expected_lookup_day = "2026-10-19"
    live_contract_approved = True
    security_window_approved = True

    def __init__(self):
        self.now, self.effects, self.claimed = NOW, [], False

    def clock(self):
        return self.now

    def read_snapshot(self):
        return snapshot(self.now)

    def read_sec_receipt(self):
        return sec_receipt()

    def read_retirement_rows(self):
        return [retained_row("broker_orders", existence=NOW - timedelta(days=2))]

    def claim_once_per_SEC_success(self, token):
        if self.claimed:
            raise policy.Refused("already_attempted_no_auto_retry")
        self.claimed = True
        self.effects.append("claim")
        return "controlled-operation"

    def persist_inventory_before_stop(self, *args):
        self.effects.append("inventory")

    def stop_exact_recorded_writers(self, *args):
        self.effects.append("stop")
        self.now += timedelta(seconds=1)
        return self.now - timedelta(seconds=1)

    def measure_table_and_own_filesystem(self, table):
        self.effects.append(("disk", table))
        return 100, 200, True

    def reboot_required(self):
        return False

    def verify_and_report_once(self, _operation):
        self.effects.append("verify_and_report_once")
        return "HEALTHY"

    def __getattr__(self, name):
        def effect(*args):
            self.effects.append(name)
        return effect


def test_dry_run_printable_full_plan_never_stops_archives_or_writes_markers():
    backend = Backend()
    report = policy.run_job(backend, NOW)
    assert report["dry_run"] and len(report["steps"]) == 11 and not backend.effects
    assert "NONE" in report["snapshot_pruning"]


def test_missing_live_contract_is_blocked_before_any_stop_or_write():
    backend = Backend()
    backend.live_contract_approved = False
    with pytest.raises(policy.Refused, match="approval_pending"):
        policy.run_job(backend, NOW, dry_run=False)
    assert not backend.effects


def test_apply_sequence_and_second_invocation_refuses_without_repeating_effects():
    backend = Backend()
    policy.run_job(backend, NOW, dry_run=False)
    assert backend.effects[:3] == ["claim", "inventory", "stop"]
    assert backend.effects.index("archive_transaction_with_csv") < backend.effects.index("vacuum_analyze_all_outside_transaction")
    assert backend.effects.index("security_updates_only_preserving_existing_policy") < backend.effects.index("restore_recorded_active_inventory")
    original = backend.effects[:]
    with pytest.raises(policy.Refused, match="already_attempted"):
        policy.run_job(backend, backend.clock(), dry_run=False)
    assert backend.effects == original


def test_failure_keeps_inventory_and_never_blindly_restarts_or_reboots():
    backend = Backend()
    def failed(*args):
        raise OSError("controlled unknown package outcome")
    backend.security_updates_only_preserving_existing_policy = failed
    with pytest.raises(OSError):
        policy.run_job(backend, NOW, dry_run=False)
    assert backend.effects[-1] == "report_failure_keep_inventory"
    assert "restore_recorded_active_inventory" not in backend.effects and "reboot_once" not in backend.effects


def test_boot_report_single_claim_and_failure_keeps_marker():
    backend = Backend()
    def claim(_operation):
        if backend.claimed:
            return False
        backend.claimed = True
        return True
    backend.claim_single_report = claim
    backend.read_boot_health_and_monday_schedule = lambda _operation: {}
    def record(*args):
        backend.effects.append("report")
    backend.write_boot_report_and_unique_health_line = record
    assert policy.boot_report(backend, "controlled") == "FAILED_LOUD_NO_RETRY"
    assert backend.effects == ["report"]
    assert policy.boot_report(backend, "controlled") == "ALREADY_CLAIMED_NO_RETRY"
