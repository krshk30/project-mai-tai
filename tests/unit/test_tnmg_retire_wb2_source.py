"""UNRUN worker-authored controls. Main executor only; no provider or live DB.

The real retained export is immutable evidence. The evaluation clock and every
adversarial mutation below are explicitly CONTROLLED, not native observations.
These pure controls do not qualify the DB loader, transaction or production run.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path('/private/tmp/OCT30-WBEXIT2-STALE1-L2-SOURCE')
SPEC = importlib.util.spec_from_file_location("tnmg_retire_source", ROOT / "retire_tnmg_wb2.py")
retire = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retire)
CONTROLLED_NOW = "2026-10-30T14:05:00+00:00"


@pytest.fixture
def episode():
    raw = (ROOT / "tnmg_episode_1030.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == retire.EXPORT_SHA256
    return json.loads(raw)


def owner(episode):
    return next(r for r in episode["system_incidents"] if r["id"] == retire.WB2)


def test_real_retained_closed_episode_plan_changes_only_derived_owner(episode):
    before = deepcopy(episode)
    result = retire.plan(episode, now=CONTROLLED_NOW)
    assert episode == before
    assert result["outcome"] == "WOULD_RETIRE_ONLY_WB2"
    assert result["owner_id"] == retire.WB2
    new = result["update"]["payload"]
    old = owner(before)["payload"]
    assert new["remaining"] == "0"
    assert new["child_fill_highwater"] == {retire.STOP: "0", retire.TARGET: "69"}
    assert new["generation"] == old["generation"] + 1
    assert new["phase"] == "RESOLVED"
    changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
    assert changed == {"remaining", "child_fill_highwater", "generation", "phase", "resolved_at",
                       "updated_at", "durable_accounting_retirement"}
    assert new["original_attempt_outcome"] == "FAIL"
    assert new["originals"] == old["originals"]
    assert new["cancel_bases"] == old["cancel_bases"] == []
    assert "terminal" not in new and "all_originals_terminal" not in new
    assert new["durable_accounting_retirement"]["wire_authority"] is False
    assert new["durable_accounting_retirement"]["new_native_observation"] is False


def test_second_identical_plan_is_byte_preserving_noop(episode):
    first = retire.plan(episode, now=CONTROLLED_NOW)
    owner(episode).update(deepcopy(first["update"]))
    before = deepcopy(episode)
    second = retire.plan(episode, now="2026-10-30T14:06:00+00:00")
    assert second["outcome"] == "ALREADY_RESOLVED_NOOP"
    assert second["update"] is None
    assert episode == before


@pytest.mark.parametrize("case", [
    "no_fill", "partial_fill", "wrong_fill_price", "foreign_fill", "wrong_child",
    "wrong_base", "no_bookkeeping", "partial_census", "unknown_child_status",
    "managed_open", "managed_positive", "VP_positive", "VP_missing", "native_positive",
    "native_missing", "native_clock_before_close", "native_clock_missing", "native_clock_future",
    "live_invocation", "sell_reservation", "extra_order", "pending_exit_intent", "WB3_active",
])
def test_adverse_evidence_refuses_without_mutating_any_input(episode, case):
    state = owner(episode)["payload"]
    child_fill = next(r for r in episode["tnmg_fills_today"] if r["side"] == "sell")
    protection = episode["protection_intent"]["payload"]["metadata"]
    # Keep repeated projections coherent where the targeted adverse proof is
    # downstream; refusal must not depend only on an inconsistent test projection.
    bookkeeping = json.loads(protection["wbattach1_terminal_bookkeeping"])
    if case == "no_fill": episode["tnmg_fills_today"].remove(child_fill)
    elif case == "partial_fill": child_fill["quantity"] = 68
    elif case == "wrong_fill_price": child_fill["price"] = "4.53"
    elif case == "foreign_fill": child_fill["strategy_id"] = "00000000-0000-0000-0000-000000000001"
    elif case == "wrong_child": state["originals"][0]["children"][1]["native_id"] = "FOREIGN"
    elif case == "wrong_base": state["originals"][0]["base"] = "FOREIGN"
    elif case == "no_bookkeeping": protection.pop("wbattach1_terminal_bookkeeping")
    elif case == "partial_census": bookkeeping["proof"]["children"][0]["evidence"]["complete"] = False
    elif case == "unknown_child_status": bookkeeping["proof"]["children"][0]["status"] = "UNKNOWN"
    elif case == "managed_open": episode["managed_row"]["status"] = "open"
    elif case == "managed_positive": episode["managed_row"]["current_quantity"] = 1
    elif case == "VP_positive": episode["virtual_position"][0]["quantity"] = 1
    elif case == "VP_missing": episode["virtual_position"] = []
    elif case == "native_positive": episode["account_positions"][0]["quantity"] = 1
    elif case == "native_missing": episode["account_positions"] = []
    elif case == "native_clock_before_close": episode["account_positions"][0]["source_updated_at"] = "2026-10-30T13:34:00+00:00"
    elif case == "native_clock_missing": episode["account_positions"][0]["source_updated_at"] = None
    elif case == "native_clock_future": episode["account_positions"][0]["source_updated_at"] = "2026-10-30T16:00:00+00:00"
    elif case == "live_invocation": state["invocation"] = {"nonce": "CONTROLLED-live"}
    elif case == "sell_reservation": state["sell"] = {"ack": "UNKNOWN", "coid": "CONTROLLED"}
    elif case == "extra_order": episode["tnmg_orders_today"].append({"id": "CONTROLLED-extra-working-sell"})
    elif case == "pending_exit_intent": episode["tnmg_intents_today"].append({"id": "CONTROLLED-pending-exit"})
    elif case == "WB3_active": next(r for r in episode["system_incidents"] if r["id"] == retire.WB3)["payload"]["active_token"] = "CONTROLLED-live"
    if case in {"partial_census", "unknown_child_status"}:
        protection["wbattach1_terminal_bookkeeping"] = json.dumps(bookkeeping)
    # Exact export duplicates are source provenance, not separate evidence. The
    # actual DB loader serializes these same objects twice in its projection.
    for index, row in enumerate(episode["tnmg_intents_today"]):
        if row.get("id") == retire.PROTECTION:
            episode["tnmg_intents_today"][index] = deepcopy(episode["protection_intent"])
    before = deepcopy(episode)
    with pytest.raises(retire.Refusal):
        retire.plan(episode, now=CONTROLLED_NOW)
    assert episode == before
