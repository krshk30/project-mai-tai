"""Operator1000 modeled snapshots; actual132/66 remains a blocker."""
import copy
from datetime import timedelta

import pytest
import raw_gate_admission as raw
import test_ipdn_residual as controlled

NOW = controlled.NOW
V2 = '''=== V2 RESTART PRE-FLIGHT ===
  [ok]    past 18:00 ET
  [ok]    zero armed segments [published state, 2.0s old]
  [ok]    zero open managed rows
  [BLOCK] broker not flat (excluding operator manuals): IPDN=1000.000000
  ===> NO-GO. Do not restart v2. Re-run when the blocking lines clear.
'''
OMS = '''=== OMS RESTART PRE-FLIGHT ===
  [ok]    database reachable
  [info]  strict all-account-position flatness enabled; no symbols are excluded
  [ok]    zero open managed rows
  [BLOCK] live:schwab_1m_v2 NOT FLAT [2s old]: IPDN=1000
  [ok]    live:orb flat [2s old]
  ===> NO-GO. A position is open; the OMS ladder is managing it.
'''


def snapshot():
    args = controlled.controlled_rule_snapshot()
    controlled.admit(args)
    result = args[0]
    result.update(rc=0, blockers=[], remaining_general_failures=[], allowance_findings=args[3],
                  dated_operator_residual=controlled.gate.ipdn_residual(result, args[3], NOW))
    return result


@pytest.mark.parametrize("name,text", [("v2", V2), ("oms", OMS)])
def test_exact1000_raw_nogo_retained_with_fresh_direct_pre_and_post(name, text):
    before, after = snapshot(), snapshot()
    original = copy.deepcopy((before, after))
    result = raw.admit(name, 1, text, before, after, NOW)
    assert result["original_rc"] == 1 and result["original_verdict"] == "NO-GO"
    assert result["broker_flat"] is False and result["source_modified"] is False
    assert result["residual"]["current_bot_buy_quantity"] == result["residual"]["current_bot_sell_quantity"] == "127"
    assert (before, after) == original


@pytest.mark.parametrize("name,text", [("v2", V2), ("oms", OMS)])
@pytest.mark.parametrize("rc", [0, 2, 3, -15])
def test_cannot_transform_other_rc_or_unreadable_to_admission(name, text, rc):
    with pytest.raises((raw.Stop, ValueError)):
        raw.admit(name, rc, text, snapshot(), snapshot(), NOW)


@pytest.mark.parametrize("change", [lambda s: s.replace("IPDN=1000", "IPDN=132"),
                                    lambda s: s.replace("IPDN=1000", "OLOX=1000"),
                                    lambda s: s.replace("IPDN=1000", "IPDN=1000 OLOX=1"),
                                    lambda s: s + "  [BLOCK] unknown SELL\n", lambda s: s + "  [BLIND] unreadable\n",
                                    lambda s: s.replace("zero open managed rows", "1 open managed row(s)"),
                                    lambda s: s.replace("NO-GO", "GO"), lambda s: s + "  ===> GO.\n"])
@pytest.mark.parametrize("name,text", [("v2", V2), ("oms", OMS)])
def test_raw_other_quantity_symbol_extra_position_or_block_always_stops(name, text, change):
    with pytest.raises((raw.Stop, ValueError)):
        raw.admit(name, 1, change(text), snapshot(), snapshot(), NOW)


@pytest.mark.parametrize("change", [lambda s: s.replace("zero armed segments", "1 ARMED SEGMENT(S)"),
                                    lambda s: s.replace("2.0s old", "61.0s old"),
                                    lambda s: s.replace("past 18:00 ET", "before18:00"),
                                    lambda s: s + "  [OVERRIDE] ARMED accepted\n"])
def test_v2_armed_stale_clock_and_armed_override_block(change):
    with pytest.raises(raw.Stop):
        raw.admit("v2", 1, change(V2), snapshot(), snapshot(), NOW)


@pytest.mark.parametrize("change", [lambda s: s.replace("live:schwab_1m_v2 NOT", "live:orb NOT"),
                                    lambda s: s.replace("[2s old]", "[121s old]"),
                                    lambda s: s.replace("live:orb flat", "live:orb STALE"),
                                    lambda s: s.replace("database reachable", "database dead"),
                                    lambda s: s.replace("strict all-account-position flatness enabled", "operator symbols excluded")])
def test_oms_account_freshness_readability_and_strict_scope_block(change):
    with pytest.raises(raw.Stop):
        raw.admit("oms", 1, change(OMS), snapshot(), snapshot(), NOW)


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("change", [lambda s: s["broker_holdings"][0].__setitem__(2, "132"),
    lambda s: s["broker_holdings"].append(["live:orb", "IPDN", "66"]),
    lambda s: s["managed_rows"].append({"symbol": "IPDN", "quantity": "132"}),
    lambda s: s["virtual_rows"].append({"symbol": "IPDN", "quantity": "1"}),
    lambda s: s["working_orders"].append({"symbol": "OLOX", "side": "sell"}),
    lambda s: s["inflight_intents"].append({"symbol": "OLOX"}),
    lambda s: s["allowance_findings"].append({"symbol": "OLOX", "finding_type": "stuck_order", "severity": "warning"}),
    lambda s: s.__setitem__("proof_completed_at_utc", (NOW - timedelta(seconds=121)).isoformat()),
    lambda s: s["fill_balances"][-1].__setitem__("net", "1"),
    lambda s: s.__setitem__("schwab_identity_bound", False),
    lambda s: s.__setitem__("rc", 2)])
def test_pre_or_post_live_proof132_66_owned_sell_stale_unknown_blocks(which, change):
    snapshots = [snapshot(), snapshot()]
    change(snapshots[which])
    with pytest.raises((raw.Stop, ValueError)):
        raw.admit("v2", 1, V2, *snapshots, NOW)


def test_truthful_clock_only_residual_reason_and_no_whole_account_flatness():
    reason = "Operator dated residual; clock only; NOT whole-account flatness"
    text = V2.replace("  [ok]    past 18:00 ET", "  [OVERRIDE] clock gate (<18:00 ET) overridden by OPERATOR\n             reason: " + reason)
    assert raw.admit("v2", 1, text, snapshot(), snapshot(), NOW, clock_reason=reason)["broker_flat"] is False
    with pytest.raises(raw.Stop):
        raw.admit("v2", 1, text, snapshot(), snapshot(), NOW, clock_reason="different")


def original_v2_go():
    return V2.replace("  [BLOCK] broker not flat (excluding operator manuals): IPDN=1000.000000",
                      "  [ok]    broker flat on both real-money accounts (operator manuals excluded)").replace(
        "===> NO-GO. Do not restart v2. Re-run when the blocking lines clear.",
        "===> GO. Zero armed segments AND flat. Safe to restart v2.")


def test_original_zero_rc_requires_complete_fresh_go_not_just_success_exit():
    text = original_v2_go()
    raw.original_go("v2", 0, text)
    for changed in (text.replace("2.0s old", "-1.0s old"), text.replace("2.0s old", "61s old"),
                    text.replace("zero armed segments", "unknown state"), text + "[BLOCK] extra\n", ""):
        with pytest.raises(raw.Stop):
            raw.original_go("v2", 0, changed)
