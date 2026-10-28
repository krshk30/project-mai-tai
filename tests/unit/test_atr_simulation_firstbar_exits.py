"""New no-X4/X3 causal mechanics; CONTROLLED, historical samples UNRUN."""

from dataclasses import replace
from decimal import Decimal

import pytest

from project_mai_tai.atr_simulation.firstbar import FirstBarMode
from project_mai_tai.atr_simulation.firstbar_exits import FirstBarPosition
from project_mai_tai.atr_simulation.execution_eligibility import execution_status
from project_mai_tai.atr_simulation.types import (
    Boundary, Coverage, EntryFact, EventKey, EvidenceKind, SimExitReason, SourceRef,
)
from tests.support.atr_simulation_eligibility import controlled_trade


SOURCE = SourceRef("CONTROLLED:firstbar-exits", "b" * 64, "$", EvidenceKind.CONTROLLED)
NS = 1_000_000_000


def key(seconds, sequence=0):
    return EventKey(seconds * NS, sequence, f"CONTROLLED:{seconds}:{sequence}", SOURCE)


def trade(seconds, price, *, size=100, conditions="14"):
    return controlled_trade(key(seconds), "X", Decimal(price), size, conditions=conditions)


def position(mode=FirstBarMode.FIRSTBAR):
    result = FirstBarPosition(mode=mode, sell_flip=Boundary(500 * NS, SOURCE),
                              session_end=Boundary(600 * NS, SOURCE),
                              coverage=Coverage(0, 600 * NS, True, (), (SOURCE,)))
    assert result.add_fill(EntryFact("CONTROLLED:first", key(0), Decimal("10"), 100, SOURCE))
    return result


@pytest.mark.parametrize("mode", tuple(FirstBarMode))
def test_two_percent_then_minus_half_percent_has_no_x4_floor(mode):
    result = position(mode)
    result.on_trade(trade(1, "10.2"))
    result.on_trade(trade(2, "9.94"))
    assert result.exit is None
    assert result.can_fill


def test_firstbar_without_x3_does_not_time_exit():
    result = position()
    result.on_trade(trade(121, "9.99"))
    assert result.exit is None


def test_x3_next_eligible_print_not_invented_deadline_price():
    result = position(FirstBarMode.FIRSTBAR_X3)
    result.on_trade(trade(120, "10.11", size=99))
    assert result.exit is None
    result.on_trade(trade(122, "9.99"))
    assert result.exit.reason == SimExitReason.TIME_NO_1PCT_120S
    assert result.exit.at == key(122)
    assert result.exit.price == Decimal("9.99")


def test_exact_deadline_one_percent_qualifies():
    result = position(FirstBarMode.FIRSTBAR_X3)
    result.on_trade(trade(120, "10.10"))
    result.on_trade(trade(121, "9.99"))
    assert result.hit_one_in_time is True
    assert result.exit is None


def test_partial_reanchors_vwap_but_not_first_fill_clock():
    result = position(FirstBarMode.FIRSTBAR_X3)
    assert result.add_fill(EntryFact("CONTROLLED:partial", key(100), Decimal("12"), 100, SOURCE))
    assert result.vwap == Decimal("11")
    assert result.first == key(0)
    result.on_trade(trade(121, "11"))
    assert result.exit.reason == SimExitReason.TIME_NO_1PCT_120S
    assert result.exit.quantity == 200
    assert result.exit.price == Decimal("11")
    assert not result.add_fill(EntryFact("CONTROLLED:too-late", key(122), Decimal("10"), 100, SOURCE))


def test_unknown_exit_eligibility_blocks_further_exposure_and_marks_result():
    result = position()
    unknown = replace(trade(1, "11"), execution=None)
    assert execution_status(unknown)[0] is None
    result.on_trade(unknown)
    assert not result.can_fill
    assert not result.add_fill(EntryFact("CONTROLLED:uncertain", key(2), Decimal("10"), 100, SOURCE))
    result.on_trade(trade(3, "11"))
    assert result.exit.reason == SimExitReason.TARGET_5
    assert "UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY" in result.exit.issues


def test_unrecognized_condition_is_excluded_without_falsely_unknown_exit():
    result = position()
    excluded = trade(1, "11", conditions="999999")
    assert execution_status(excluded) == (False, "unknown_conditions=999999")
    result.on_trade(excluded)
    assert result.exit is None
    assert result.can_fill
    assert not result.issues


@pytest.mark.parametrize("mode", tuple(FirstBarMode))
def test_sell_boundary_cancels_remainder_before_equal_time_partial(mode):
    result = position(mode)
    assert not result.add_fill(EntryFact("CONTROLLED:sell-boundary", key(500), Decimal("10"), 100, SOURCE))
    result.on_trade(trade(500, "10"))
    assert result.exit.reason == SimExitReason.SELL_FLIP
    assert result.exit.quantity == 100
    assert result.remainder_cancel_reason == "SELL_FLIP"
