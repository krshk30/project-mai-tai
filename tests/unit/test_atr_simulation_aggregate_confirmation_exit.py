"""C1 00:00 confirmation mechanics, CONTROLLED, not historical case acceptance."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib

import pytest

from project_mai_tai.atr_simulation import aggregate_entry_publication as pub
from project_mai_tai.atr_simulation.firstbar_live_baseline import (
    BASELINE_POLICY_VERSION, BASELINE_VERSION, entry_bar_confirmation,
    independent_leg_totals, prepare_baseline_day, simulate_aggregate_cohort, simulate_aggregate_entry,
)
from project_mai_tai.atr_simulation.firstbar_study import StudyTapePacket
from project_mai_tai.atr_simulation.types import Coverage
from tests.support.atr_simulation_eligibility import POLICY
from tests.unit.test_atr_firstbar_live_baseline import BASE, DAY, END, NS, SOURCE, item, trade
from tests.unit.test_atr_simulation_aggregate_entry_publication import operands as operands
from tests.unit.test_atr_simulation_aggregate_entry_read_model import independent, bound, port, project


def short(value):
    close = replace(value.day.bar_closes[0], close=Decimal("1.52"), line=Decimal("1.60"), state="short")
    return replace(value, day=replace(value.day, bar_closes=(close,)))


def test_entry_bar_short_closes_exactly_at_available_close_not_prior_SELL_state():
    result = simulate_aggregate_entry(short(item((trade(101, "1.55"), trade(119, "1.55"), trade(120, "1.55")))))
    assert result.previous_sell.available_ns == BASE + 50 * NS
    assert result.entry_bar_close.bar_start_ns == BASE + 60 * NS
    assert result.confirmation_exit.available_ns == BASE + 120 * NS
    assert result.exits[0].reason.value == "CONFIRMATION_EXIT"
    assert result.exits[0].at.event_ns == BASE + 120 * NS
    assert result.exits[0].price == Decimal("1.55") and result.exits[0].quantity == 196
    assert result.position.remainder_cancel_reason == "CONFIRMATION_EXIT"
    assert result.modeled_gross_pnl == Decimal("-1.96") and not result.issues
    assert result.baseline_policy_version == "C1_20261024_0000_ENTRY_BAR_SHORT_CONFIRMATION_EXIT"


def test_same_close_SELL_transition_is_confirmation_not_stale_transition_waiver():
    result = simulate_aggregate_entry(short(item((trade(119, "1.55"), trade(120, "1.54")), sells=(50, 120))))
    assert result.next_sell.available_ns == BASE + 120 * NS
    assert result.exits[0].reason.value == "CONFIRMATION_EXIT"
    assert result.exits[0].at.event_ns == BASE + 120 * NS
    assert result.modeled_gross_pnl == Decimal("-3.92")


def test_confirmation_waits_for_first_eligible_roundlot_not_oddlot_or_excluded_condition():
    result = simulate_aggregate_entry(short(item((trade(120, "1.50", size=99),
        trade(121, "1.50", conditions="999999"), trade(122, "1.54"), trade(123, "1.53")))))
    assert result.exits[0].reason.value == "CONFIRMATION_EXIT"
    assert result.exits[0].at.event_ns == BASE + 122 * NS
    assert result.exits[0].price == Decimal("1.54") and result.modeled_gross_pnl == Decimal("-3.92")


def test_unknown_execution_at_confirmation_never_certifies_later_exit_as_known():
    uncertain = replace(trade(120, "1.54"), execution=None)
    result = simulate_aggregate_entry(short(item((uncertain, trade(121, "1.54")))))
    assert result.decision == "UNKNOWN" and result.modeled_gross_pnl is None
    assert "UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY" in result.issues


def test_confirmation_boundary_wins_over_target_on_the_same_first_eligible_print():
    result = simulate_aggregate_entry(short(item((trade(119, "1.62"), trade(120, "1.65")))))
    assert result.exits[0].reason.value == "CONFIRMATION_EXIT"
    assert result.exits[0].price == Decimal("1.65")
    assert result.modeled_gross_pnl == Decimal("17.64")


@pytest.mark.parametrize("price,reason,pnl", (("1.64", "TARGET_5", "15.68"), ("1.43", "STOP_8", "-25.48")))
def test_target_and_stop_before_entry_bar_close_are_unchanged(price, reason, pnl):
    result = simulate_aggregate_entry(short(item((trade(110, price), trade(120, "1.55")))))
    assert result.exits[0].reason.value == reason and result.exits[0].at.event_ns == BASE + 110 * NS
    assert result.exits[0].price == Decimal(price) and result.modeled_gross_pnl == Decimal(pnl)


def test_long_entry_bar_keeps_later_new_SELL_and_no_confirmation():
    result = simulate_aggregate_entry(item((trade(120, "1.56"), trade(500, "1.55"))))
    assert result.confirmation_exit is None and result.entry_bar_close.state == "long"
    assert result.exits[0].reason.value == "SELL_FLIP" and result.exits[0].at.event_ns == BASE + 500 * NS
    assert result.modeled_gross_pnl == Decimal("-1.96")


@pytest.mark.parametrize("boundary,reason", (("SELL", "SELL_FLIP"), ("SESSION", "SESSION_END")))
def test_earlier_real_boundary_is_not_replaced_by_later_confirmation(boundary, reason):
    value = short(item((trade(121, "1.55"),), sells=(50, 110) if boundary == "SELL" else (50, 500)))
    if boundary == "SESSION":
        value = replace(value, day=replace(value.day,
            session_end=replace(value.day.session_end, available_ns=BASE+119*NS)))
    result = simulate_aggregate_entry(value)
    assert result.exits[0].reason.value == reason and result.exits[0].at.event_ns == BASE+121*NS
    assert result.exits[0].price == Decimal("1.55") and result.modeled_gross_pnl == Decimal("-1.96")


def test_close_equal_to_line_does_not_satisfy_strict_below_line_rule():
    value = item((trade(121, "1.64"),))
    close = replace(value.day.bar_closes[0], state="short", close=Decimal("1.56"), line=Decimal("1.56"))
    result = simulate_aggregate_entry(replace(value, day=replace(value.day, bar_closes=(close,))))
    assert result.confirmation_exit is None and result.exits[0].reason.value == "TARGET_5"
    assert result.modeled_gross_pnl == Decimal("15.68")


def test_actual_oracle_preparation_keeps_nontransition_SHORT_closes_and_exact_availability():
    prices = [Decimal("10") if n < 20 else Decimal("10")-Decimal(n-20)/100 for n in range(80)]
    raw = "".join(f"{BASE // NS + n*60}|{price}|100|{{14}}\n" for n, price in enumerate(prices)).encode()
    src = replace(SOURCE, sha256=hashlib.sha256(raw).hexdigest())
    day = prepare_baseline_day(StudyTapePacket("X", DAY, raw, src,
        Coverage(BASE, END, True, (), (src,))), POLICY)
    assert len(day.bar_closes) == 80 and [row.close for row in day.bar_closes] == prices
    assert all(row.available_ns == BASE+(n+1)*60*NS for n, row in enumerate(day.bar_closes))
    assert all(row.bar_start_ns == BASE+n*60*NS for n, row in enumerate(day.bar_closes))
    flip_clocks = {event.available_ns for _, event in day.all_flips}
    assert any(row.state == "short" and row.close < row.line
               and row.available_ns not in flip_clocks for row in day.bar_closes if row.line is not None)


@pytest.mark.parametrize("price,quantity,exit_price,reason,pnl", (
    ("1.53", 196, "1.61", "TARGET_5", "15.68"),
    ("4.7661", 63, "4.36", "STOP_8", "-25.5843"),
    ("3.5885", 167, "3.77", "TARGET_5", "30.3105"),
    ("3.5891", 84, "3.77", "TARGET_5", "15.1956")))
def test_controlled_must_not_change_price_geometries_with_long_entry_bar(price, quantity, exit_price, reason, pnl):
    # Actual recorded geometries, but CONTROLLED clocks/state/tape, not real-case replay.
    result = simulate_aggregate_entry(item((trade(121, exit_price),), price=price, quantity=quantity))
    assert result.confirmation_exit is None
    assert result.exits[0].reason.value == reason and result.exits[0].price == Decimal(exit_price)
    assert result.modeled_gross_pnl == Decimal(pnl)


@pytest.mark.parametrize("fault", ("missing", "seed", "prior-minute", "future-minute", "state-conflict"))
def test_missing_or_wrong_entry_close_is_unknown_not_a_prior_or_future_state_substitute(fault):
    value = item((trade(121, "1.64"),))
    close = value.day.bar_closes[0]
    if fault == "missing":
        closes = ()
    elif fault == "seed":
        closes = (replace(close, line=None, state=None),)
    elif fault == "prior-minute":
        closes = (replace(close, bar_start_ns=BASE, available_ns=BASE+60*NS, state="short"),)
    elif fault == "future-minute":
        closes = (replace(close, bar_start_ns=BASE+120*NS, available_ns=BASE+180*NS, state="short"),)
    else:
        closes = (replace(close, state="long", close=Decimal("1.40"), line=Decimal("1.60")),)
    result = simulate_aggregate_entry(replace(value, day=replace(value.day, bar_closes=closes)))
    assert result.decision == "UNKNOWN" and result.modeled_gross_pnl is None
    assert any(issue.startswith("UNKNOWN_ENTRY_BAR_ORACLE_") for issue in result.issues)


@pytest.mark.parametrize("fault", ("duplicate", "early-close"))
def test_malformed_close_grid_is_refused_not_retimed(fault):
    value = item()
    close = value.day.bar_closes[0]
    closes = (close, close) if fault == "duplicate" else (replace(close, available_ns=BASE+119*NS),)
    with pytest.raises(ValueError, match="completed_oracle_closes"):
        entry_bar_confirmation(replace(value.day, bar_closes=closes), value.leg.filled_ns)


def test_no_eligible_postclose_print_does_not_invent_barclose_execution():
    result = simulate_aggregate_entry(short(item((trade(119, "1.55"), trade(120, "1.55", size=99)))))
    assert result.exits[0].reason.value == "OPEN" and result.exits[0].price is None
    assert result.modeled_gross_pnl is None and result.position.remainder_cancel_reason == "CONFIRMATION_EXIT"


def test_confirmation_shortens_intervals_and_repartitions_overlap_totals_without_masking():
    early = short(item((trade(120, "1.55"), trade(140, "1.64"))))
    late = item((trade(140, "1.64"),), order="order2", filled=130)
    rows = simulate_aggregate_cohort((early, late))
    assert [row.flags for row in rows] == [(), ()]
    assert [row.modeled_gross_pnl for row in rows] == [Decimal("-1.96"), Decimal("15.68")]
    totals = independent_leg_totals(rows)
    assert totals["all_legs"]["gross_pnl"] == Decimal("13.72")
    assert totals["excluding_same_account_overlap"]["legs"] == 2
    assert totals["same_account_overlap"]["legs"] == 0 and totals["same_account_overlap"]["gross_pnl"] == 0


def confirmation_receipt(receipt):
    independent(receipt)
    row = receipt["cases"][0]["account_rows"][0]
    close = row["entry_bar_close"]
    close.update(close="9.9", line="10", state="short")
    row["confirmation_exit"] = dict(available_ns=close["available_ns"], source=close["source"],
                                    bar_start_ns=close["bar_start_ns"])
    # Other legs sharing that completed minute must carry the same close evidence.
    for case in receipt["cases"]:
        for leg in case["account_rows"]:
            if leg["entry_bar_close"]["bar_start_ns"] == close["bar_start_ns"]:
                leg["entry_bar_close"] = deepcopy(close)
                leg["confirmation_exit"] = deepcopy(row["confirmation_exit"])
    receipt["day_evidence"][0]["all_bar_closes"] = [deepcopy(close)
        if value["bar_start_ns"] == close["bar_start_ns"] else value
        for value in receipt["day_evidence"][0]["all_bar_closes"]]
    row["exits"][0].update(reason="CONFIRMATION_EXIT")
    row["exits"][0]["at"]["event_ns"] = close["available_ns"]
    row["position"]["remainder_cancel_reason"] = "CONFIRMATION_EXIT"
    receipt["exit_reason_counts"] = dict(Counter(exit["reason"] for case in receipt["cases"]
        for leg in case["account_rows"] for exit in leg["exits"]))
    return receipt


def test_new_policy_publishes_and_projects_exact_confirmation_without_relabeling_old_exit_policy(operands):
    receipt, producer = operands
    confirmation_receipt(receipt)
    binding = bound(receipt, producer)
    payload = pub.document(binding.publication_raw)
    row = payload["result_rows"][0]
    view = project(port(binding), binding)
    assert BASELINE_VERSION == pub.CONFIRMATION_ADAPTER_VERSION
    assert BASELINE_POLICY_VERSION == pub.CONFIRMATION_POLICY_VERSION
    assert row["exits"][0]["reason"] == "CONFIRMATION_EXIT"
    assert row["exit_policy_version"] == "C1_20261023_2129_A2_CEILING_FIRST_ELIGIBLE_STOP_PRINT"
    assert row["baseline_policy_version"] == "C1_20261024_0000_ENTRY_BAR_SHORT_CONFIRMATION_EXIT"
    assert view["exit_reason_counts"]["CONFIRMATION_EXIT"] == 1
    assert view["baseline_policy_version"] == pub.CONFIRMATION_POLICY_VERSION
    assert any("CONFIRMATION_EXIT" in row["summary"] for row in view["rows"])
    assert view["native_execution_qualified"] is False


@pytest.mark.parametrize("fault", ("root-policy", "row-policy", "exit-count", "preclose", "wrong-minute", "foreign-source", "boundary", "missing-close"))
def test_confirmation_metadata_cannot_certify_wrong_or_missing_causal_evidence(operands, fault):
    receipt, producer = operands
    confirmation_receipt(receipt)
    row = receipt["cases"][0]["account_rows"][0]
    if fault == "root-policy":
        del receipt["baseline_policy_version"]
    elif fault == "row-policy":
        row["baseline_policy_version"] = "OLD"
    elif fault == "exit-count":
        receipt["exit_reason_counts"]["CONFIRMATION_EXIT"] = 2
    elif fault == "preclose":
        row["exits"][0]["at"]["event_ns"] -= 1
    elif fault == "wrong-minute":
        row["entry_bar_close"]["bar_start_ns"] -= 60000000000
    elif fault == "foreign-source":
        row["entry_bar_close"]["source"]["sha256"] = "f" * 64
    elif fault == "boundary":
        row["confirmation_exit"]["available_ns"] -= 1
    else:
        row["entry_bar_close"] = None
    with pytest.raises(ValueError):
        bound(receipt, producer)


def test_superseded_independent_v2_can_remain_historical_but_is_not_current_page_binding(operands):
    receipt, producer = operands
    independent(receipt)
    receipt["version"] = pub.INDEPENDENT_ADAPTER_VERSION
    for case in receipt["cases"]:
        for row in case["account_rows"]:
            row["version"] = pub.INDEPENDENT_ADAPTER_VERSION
    # Historical publication remains valid; current projector policy refuses it.
    from tests.unit.test_atr_simulation_aggregate_entry_publication import prepare
    historical = prepare(receipt, producer)
    assert pub.document(historical.publication_raw)["coverage"]["aggregate_entry_study"]["source_adapter_version"] == pub.INDEPENDENT_ADAPTER_VERSION
    with pytest.raises(ValueError, match="confirmation_v3"):
        bound(receipt, producer)
