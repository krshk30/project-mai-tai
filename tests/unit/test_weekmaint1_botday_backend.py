"""Authored source-stage controls. No live SQL/broker/systemd/package operations."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from ops.weekmaint import backend, policy
from tests.unit.test_weekmaint1_policy import ACCOUNTS, NOW, retained_row, snapshot


@pytest.mark.parametrize("account", ACCOUNTS)
def test_manual_shares_do_not_gate_but_same_symbol_retirement_is_left_unchanged(account):
    facts = snapshot()
    facts["broker_evidence"][account]["positions"]["rows"] = [{"symbol": "ABC", "quantity": "1000",
        "bot_owned_quantity": "0", "ownership_complete": True}]
    policy.gates(facts, NOW)
    item = retained_row("oms_managed_positions", status="open", quantity="0")
    item["account"] = account
    plan, unknown = policy.retirement_plan([item], NOW, broker_evidence=facts["broker_evidence"])
    assert not plan and unknown == [{"table": "oms_managed_positions", "id": str(UUID(int=1)),
                                    "reason": "symbol_not_broker_flat_leave_unchanged"}]
    assert item["row"]["status"] == "open"


@pytest.mark.parametrize("quantity,complete", [("1", True), ("NaN", True), ("0", False), (None, True)])
def test_bot_owned_or_unknown_quantity_never_becomes_manual_allowance(quantity, complete):
    facts = snapshot()
    facts["broker_evidence"][next(iter(ACCOUNTS))]["positions"]["rows"] = [
        {"symbol": "ABC", "quantity": "1000", "bot_owned_quantity": quantity,
         "ownership_complete": complete}]
    with pytest.raises(policy.Refused):
        policy.gates(facts, NOW)


def test_old_DAY_nonterminal_is_retirement_set_not_zero_row_gate():
    facts = snapshot()
    item = retained_row("broker_orders", existence=NOW - timedelta(days=2))
    facts["nonterminal_bot_order_rows"] = [item]
    policy.gates(facts, NOW)
    plan, unknown = policy.retirement_plan([item], NOW, broker_evidence=facts["broker_evidence"])
    assert len(plan) == 1 and not unknown
    assert plan[0].before["status"] == "working" and plan[0].changes["status"] == "aborted"


def test_pre04_Saturday_does_not_widen_anchor_or_add_current_session_gate():
    early = datetime(2026, 10, 17, 5, 30, tzinfo=UTC)
    item = retained_row("broker_orders", existence=datetime(2026, 10, 16, 14, tzinfo=UTC))
    item["row"]["updated_at"] = "2026-10-16T14:00:00+00:00"
    policy.order_gate([item], early)
    assert policy.retirement_plan([item], early) == ([], [])


def test_approved_timer_clock_is_aware_and_unknown_zone_or_DST_disagreement_refuses():
    assert backend.timer_wall("Mon 2026-10-19 03:40:00 EDT") == datetime(2026, 10, 19, 7, 40, tzinfo=UTC)
    with pytest.raises(policy.Refused):
        backend.timer_wall("Mon 2026-10-19 03:40:00 EST")
    with pytest.raises(policy.Refused):
        backend.timer_wall("Mon 2026-10-19 03:40:00 Unknown")


@pytest.mark.parametrize("change", ["GTC", "missing_TIF", "recent_update", "recent_submit", "future", "unanchored"])
def test_nonDAY_recent_or_unknown_remains_hard_gate(change):
    facts = snapshot()
    item = retained_row("broker_orders", existence=NOW - timedelta(days=2))
    if change == "GTC":
        item["row"]["time_in_force"] = "GTC"
    elif change == "missing_TIF":
        item["row"].pop("time_in_force")
    elif change == "recent_update":
        item["row"]["updated_at"] = (NOW - timedelta(hours=2)).isoformat()
    elif change == "recent_submit":
        item["row"]["submitted_at"] = (NOW - timedelta(minutes=1)).isoformat()
    elif change == "future":
        item["row"]["updated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    else:
        item["existence_at"] = None
    facts["nonterminal_bot_order_rows"] = [item]
    original = deepcopy(item)
    with pytest.raises(policy.Refused):
        policy.gates(facts, NOW)
    assert item == original


def test_backend_commands_never_use_shell_and_unknown_command_outcome_refuses(tmp_path):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="controlled")
    item = backend.SystemBackend({"state_directory": str(tmp_path), "approved_head": "a" * 40,
        "lookup_day": "2026-10-19"}, None, None, run=run, clock=lambda: NOW)
    with pytest.raises(policy.Refused):
        item.command(["/bin/systemctl", "stop", "project-mai-tai-oms.service"])
    assert len(calls) == 1 and "shell" not in calls[0][1]


def test_claim_is_durable_before_stop_and_second_claim_cannot_repeat(tmp_path):
    item = backend.SystemBackend({"state_directory": str(tmp_path), "approved_head": "a" * 40,
        "lookup_day": "2026-10-19"}, None, None, clock=lambda: NOW)
    item._lock_held = True
    operation = item.claim_once_per_SEC_success("b" * 64)
    assert UUID(operation) and (tmp_path / f"sec-{'b' * 64}.claimed").exists()
    with pytest.raises(policy.Refused, match="unresolved_operation"):
        item.claim_once_per_SEC_success("b" * 64)


def test_unreviewed_package_window_does_not_invoke_global_attended_override(tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail("unreviewed post-SEC package operation invoked")
    item = backend.SystemBackend({"state_directory": str(tmp_path), "approved_head": "a" * 40,
        "lookup_day": "2026-10-19"}, None, None, run=forbidden, clock=lambda: NOW)
    with pytest.raises(policy.Refused, match="not_reviewed"):
        item.security_updates_only_preserving_existing_policy()


def test_preview_never_creates_state_or_SEC_claim(tmp_path):
    missing = tmp_path / "not-installed"
    config = {"state_directory": str(missing), "approved_head": "a" * 40, "lookup_day": "2026-10-19"}
    with pytest.raises(FileNotFoundError):
        backend.SystemBackend(config, None, None, preview=True)
    assert not missing.exists()
    config["state_directory"] = str(tmp_path)
    item = backend.SystemBackend(config, None, None, preview=True)
    item._lock_held = True
    with pytest.raises(policy.Refused):
        item.claim_once_per_SEC_success("b" * 64)
    assert list(tmp_path.iterdir()) == []
