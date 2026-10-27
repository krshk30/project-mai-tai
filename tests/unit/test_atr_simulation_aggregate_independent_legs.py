"""C1 23:29 independent actual-leg mechanics; CONTROLLED, not real replay."""

from dataclasses import replace
from decimal import Decimal

import pytest

from project_mai_tai.atr_simulation import aggregate_entry_publication as pub
from project_mai_tai.atr_simulation.firstbar_live_baseline import (
    independent_leg_totals, simulate_aggregate_cohort, simulate_aggregate_entry,
)
from tests.unit.test_atr_firstbar_live_baseline import item, trade
from tests.unit.test_atr_simulation_aggregate_entry_publication import operands, prepare, unknown_first


def test_overlap_changes_only_information_not_each_independent_position_economics():
    tape = (trade(101, "1.56"), trade(110, "1.64"))
    items = (item(tape), item(tape, order="order2", filled=105))
    rows = simulate_aggregate_cohort(items)
    for row, first_seconds in zip(rows, (100, 105)):
        assert row.position.filled_qty == 196 and row.position.vwap == Decimal("1.56")
        assert row.position.first_fill_ts == items[0].leg.filled_ns + (first_seconds - 100) * 1000000000
        assert len(row.position.fills) == 1 and row.position.fills[0].quantity == 196
        assert row.position.fills[0].price == Decimal("1.56")
        assert row.position.fills[0].at.event_ns == items[0].leg.filled_ns + (first_seconds - 100) * 1000000000
        assert len(row.exits) == 1 and row.exits[0].reason.value == "TARGET_5"
        assert row.exits[0].price == Decimal("1.64") and row.exits[0].quantity == 196
        assert row.exits[0].at.event_ns == items[0].leg.filled_ns + 10000000000
        assert row.flags == ("SAME_ACCOUNT_OVERLAP",)
        assert row.issues == ()
        assert row.modeled_gross_pnl == Decimal("15.68")
        assert row.decision == "MODELED_BASELINE_CLOSED"
    assert rows[0].same_account_overlap_order_ids == ("order2",)
    assert rows[1].same_account_overlap_order_ids == ("order1",)


def test_cross_account_positions_are_independent_and_not_same_account_overlap():
    tape = (trade(101, "1.64"),)
    rows = simulate_aggregate_cohort((item(tape, account="A"), item(tape, account="B")))
    assert all(not row.flags and not row.same_account_overlap_order_ids for row in rows)
    assert [row.position.filled_qty for row in rows] == [196, 196]
    assert independent_leg_totals(rows)["all_legs"]["gross_pnl"] == Decimal("31.36")


def test_three_totals_partition_all_legs_without_counting_peers_twice():
    tape = (trade(101, "1.56"), trade(110, "1.64"))
    rows = simulate_aggregate_cohort((item(tape, account="A"),
        item(tape, account="A", order="order2", filled=105), item(tape, account="B")))
    totals = independent_leg_totals(rows)
    assert totals["all_legs"] == dict(legs=3, unknown_legs=0, gross_pnl=Decimal("47.04"), known_component_gross_pnl=Decimal("47.04"))
    assert totals["excluding_same_account_overlap"]["gross_pnl"] == Decimal("15.68")
    assert totals["excluding_same_account_overlap"]["legs"] == 1
    assert totals["same_account_overlap"]["gross_pnl"] == Decimal("31.36")
    assert totals["same_account_overlap"]["legs"] == 2


def test_genuine_eligibility_unknown_survives_information_flag_and_three_totals():
    unknown = replace(trade(101, "1.56"), execution=None)
    tape = (unknown, trade(102, "1.64"))
    rows = simulate_aggregate_cohort((item(tape), item(tape, order="order2")))
    assert all(row.flags == ("SAME_ACCOUNT_OVERLAP",) for row in rows)
    assert all("UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY" in row.issues for row in rows)
    assert all(row.modeled_gross_pnl is None and row.decision == "UNKNOWN" for row in rows)
    totals = independent_leg_totals(rows)
    assert totals["all_legs"]["gross_pnl"] is None
    assert totals["all_legs"]["unknown_legs"] == 2
    assert totals["same_account_overlap"]["gross_pnl"] is None
    assert totals["excluding_same_account_overlap"]["legs"] == 0


def v2_receipt(receipt, *, flagged=False):
    receipt["version"] = pub.INDEPENDENT_ADAPTER_VERSION
    receipt["cohort_policy"] = pub.INDEPENDENT_COHORT_POLICY
    for case in receipt["cases"]:
        for row in case["account_rows"]:
            row.update(version=pub.INDEPENDENT_ADAPTER_VERSION, flags=[], same_account_overlap_order_ids=[],
                       cohort_policy=pub.INDEPENDENT_COHORT_POLICY)
    receipt["day_evidence"][0]["session_end"] = dict(available_ns=1791849600000000000)
    if flagged:
        left = receipt["cases"][0]["account_rows"][0]
        right = receipt["cases"][1]["account_rows"][0]
        stamp = left["recorded_aggregate"]["filled_ns"] + 500000000
        right["recorded_aggregate"]["filled_ns"] = stamp
        right["aggregate_entry"]["at"]["event_ns"] = stamp
        right["position"]["first_fill_ts"] = stamp
        for row, peer in ((left, right), (right, left)):
            row["flags"] = ["SAME_ACCOUNT_OVERLAP"]
            row["same_account_overlap_order_ids"] = [peer["order_id"]]
    count = 2 if flagged else 0
    def total(legs):
        return dict(legs=legs, unknown_legs=0, gross_pnl=str(legs * 50), known_component_gross_pnl=str(legs * 50))
    receipt["independent_leg_totals"] = dict(all_legs=total(253),
        excluding_same_account_overlap=total(253-count), same_account_overlap=total(count))
    return receipt


def test_v2_publication_preserves_known_flagged_rows_and_three_totals(operands):
    receipt, binding = operands
    v2_receipt(receipt, flagged=True)
    payload = pub.document(prepare(receipt, binding).publication_raw)
    rows = payload["result_rows"]
    flagged = [row for row in rows if row["flags"]]
    assert len(flagged) == 2 and all(row["gross_pnl"] == "50.00" and not row["issues"] for row in flagged)
    assert all(row["source_adapter_version"] == pub.INDEPENDENT_ADAPTER_VERSION for row in rows)
    study = payload["coverage"]["aggregate_entry_study"]
    assert study["same_account_overlap_informational"] is True
    assert study["cohort_policy"] == pub.INDEPENDENT_COHORT_POLICY
    assert Decimal(study["independent_leg_totals"]["all_legs"]["gross_pnl"]) == Decimal("12650")
    assert Decimal(study["independent_leg_totals"]["excluding_same_account_overlap"]["gross_pnl"]) == Decimal("12550")
    assert Decimal(study["independent_leg_totals"]["same_account_overlap"]["gross_pnl"]) == Decimal("100")
    assert all(row["native_execution_qualified"] is False for row in flagged)


def test_v2_publication_keeps_other_unknown_null_with_partition_counts(operands):
    receipt, binding = operands
    v2_receipt(receipt, flagged=True)
    unknown_first(receipt)
    receipt["cases"][0]["account_rows"][0]["issues"] = ["UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY"]
    receipt["independent_leg_totals"]["all_legs"].update(unknown_legs=1, gross_pnl=None, known_component_gross_pnl="12600")
    receipt["independent_leg_totals"]["same_account_overlap"].update(unknown_legs=1, gross_pnl=None, known_component_gross_pnl="50")
    payload = pub.document(prepare(receipt, binding).publication_raw)
    assert payload["result_rows"][0]["gross_pnl"] is None
    assert payload["result_rows"][0]["flags"] == ["SAME_ACCOUNT_OVERLAP"]
    assert payload["coverage"]["aggregate_entry_study"]["independent_leg_totals"]["all_legs"]["gross_pnl"] is None


@pytest.mark.parametrize("fault", ("missing-policy", "old-mask", "cross-account-peer", "wrong-total", "missing-total", "dropped-flag", "mixed-version"))
def test_v2_policy_flags_and_totals_cannot_reclassify_or_erase_truth(operands, fault):
    receipt, binding = operands
    v2_receipt(receipt, flagged=True)
    row = receipt["cases"][0]["account_rows"][0]
    if fault == "missing-policy":
        del receipt["cohort_policy"]
    elif fault == "old-mask":
        row["issues"] = ["UNKNOWN_OVERLAPPING_ACTUAL_ENTRY_POSITIONS"]
    elif fault == "cross-account-peer":
        row["same_account_overlap_order_ids"] = [receipt["cases"][0]["account_rows"][1]["order_id"]]
    elif fault == "wrong-total":
        receipt["independent_leg_totals"]["same_account_overlap"]["gross_pnl"] = "0"
    elif fault == "missing-total":
        del receipt["independent_leg_totals"]["excluding_same_account_overlap"]
    elif fault == "dropped-flag":
        row["flags"], row["same_account_overlap_order_ids"] = [], []
    else:
        row["version"] = pub.ADAPTER_VERSION
    with pytest.raises(ValueError):
        prepare(receipt, binding)


def test_legacy_v1_publication_stays_null_without_new_independent_policy(operands):
    receipt, binding = operands
    unknown_first(receipt)
    payload = pub.document(prepare(receipt, binding).publication_raw)
    row = payload["result_rows"][0]
    assert row["gross_pnl"] is None and row["source_adapter_version"] == pub.ADAPTER_VERSION
    assert "flags" not in row
    assert "independent_leg_totals" not in payload["coverage"]["aggregate_entry_study"]
    assert "cohort_policy" not in payload["coverage"]["aggregate_entry_study"]
