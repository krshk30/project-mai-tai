"""New CONTROLLED FIRSTBAR planning controls. No historical outcome claims."""

from dataclasses import replace
from decimal import Decimal

import pytest

from project_mai_tai.atr_simulation.firstbar import (
    CompletedFlipBar, FirstBarMode, plan_firstbar_entry,
)
from project_mai_tai.atr_simulation.execution_eligibility import execution_status
from project_mai_tai.atr_simulation.types import (
    Boundary, Coverage, EpisodeInput, EventKey, EvidenceKind, FlipIdentity,
    LiveFacts, SourceRef,
)
from tests.support.atr_simulation_eligibility import POLICY, controlled_trade


SOURCE = SourceRef("CONTROLLED:firstbar", "a" * 64, "$", EvidenceKind.CONTROLLED)
NS = 1_000_000_000
END = Boundary(300 * NS, SOURCE)
COVERAGE = Coverage(0, END.available_ns, True, (), (SOURCE,))
BAR = CompletedFlipBar("X", 0, 60 * NS, Decimal("2"), Decimal("1.99"),
                       Decimal("1.98"), "long", True, SOURCE)


def trade(seconds, price, *, size=100, sequence=1, conditions="14"):
    key = EventKey(seconds * NS, sequence, f"controlled:{seconds}:{sequence}", SOURCE)
    return controlled_trade(key, "X", Decimal(price), size, conditions=conditions)


def episode(rows):
    return EpisodeInput(
        FlipIdentity("2026-10-23", "X", "webull", "controlled-account",
                     "schwab_1m_v2", "flip", "segment"),
        Boundary(60 * NS, SOURCE, bar_start_ns=0), True, SOURCE,
        Decimal("1.98"), 196, SOURCE, Boundary(240 * NS, SOURCE), END,
        tuple(rows), (), COVERAGE, COVERAGE, (), COVERAGE,
        LiveFacts(None, (), (), replace(COVERAGE, complete=None)),
        execution_policy=POLICY,
    )


def plan(item, bar=BAR, mode=FirstBarMode.FIRSTBAR):
    return plan_firstbar_entry(item, bar, mode=mode,
                               entry_cutoff=Boundary(180 * NS, SOURCE))


@pytest.mark.parametrize("mode", tuple(FirstBarMode))
def test_flip_bar_print_not_entry_and_next_print_cannot_confirm_permission(mode):
    item = episode((trade(59, "2.5"), trade(60, "2.01"), trade(61, "1.5")))
    result = plan(item, mode=mode)
    assert result.decision == "ENTRY_CANDIDATE"
    assert result.candidate is item.trades[1]
    assert result.trigger == Decimal("2.01")
    assert result.requested_quantity == 196
    # Candidate's 100 shares are NOT an allocation of the requested 196.
    assert result.candidate.size == 100
    assert not hasattr(result, "fills")


@pytest.mark.parametrize("mode", tuple(FirstBarMode))
def test_false_flip_skipped_even_if_later_price_breaks(mode):
    result = plan(episode((trade(61, "3"),)),
                  replace(BAR, close=Decimal("1.97"), atr_state_at_close="short"), mode)
    assert result.decision == "SKIPPED_FALSE_FLIP"
    assert result.candidate is None


@pytest.mark.parametrize("seconds", (180, 240, 300))
def test_no_entry_at_cutoff_sell_or_session_boundary(seconds):
    result = plan(episode((trade(seconds, "3"),)))
    assert result.decision == "SKIPPED_NO_BREAK_BEFORE_BOUNDARY"


def test_missing_coverage_unknown_not_skipped_or_filled():
    item = episode((trade(61, "3"),))
    result = plan(replace(item, tape_coverage=replace(COVERAGE, complete=None)))
    assert result.decision == "UNKNOWN"
    assert "UNKNOWN_TAPE_WINDOW" in result.issues
    assert result.candidate is None


def test_earlier_unknown_eligible_break_cannot_be_hidden_by_later_known_break():
    # An unrecognized code is definite exclusion in the pinned classifier.
    # Missing typed evidence, rather than a fabricated code, is truly UNKNOWN.
    unknown = replace(trade(61, "3"), execution=None)
    assert execution_status(unknown)[0] is None
    item = episode((unknown, trade(62, "3")))
    result = plan(item)
    assert result.decision == "UNKNOWN"
    assert "UNKNOWN_EXECUTION_FIRSTBAR_ENTRY_ELIGIBILITY" in result.issues
    assert result.candidate is item.trades[1]


def test_unrecognized_condition_code_is_definite_exclusion_not_unknown():
    excluded = trade(61, "3", conditions="999999")
    assert execution_status(excluded) == (False, "unknown_conditions=999999")
    item = episode((excluded, trade(62, "3")))
    result = plan(item)
    assert result.decision == "ENTRY_CANDIDATE"
    assert result.candidate is item.trades[1]


def test_known_excluded_and_oddlot_breaks_do_not_select_entry():
    item = episode((trade(61, "3", conditions="37"), trade(62, "3", size=99),
                    trade(63, "2.01")))
    result = plan(item)
    assert result.decision == "ENTRY_CANDIDATE"
    assert result.candidate is item.trades[2]


def test_close_not_available_at_bar_start():
    with pytest.raises(ValueError, match="completed_flip_bar_availability_required"):
        plan(episode((trade(61, "3"),)), replace(BAR, available_ns=59 * NS))


def test_duplicate_raw_print_is_refused_not_double_counted():
    row = trade(61, "3")
    with pytest.raises(ValueError, match="unique_causal_symbol_tape_required"):
        plan(episode((row, row)))


def test_no_rewind_to_earlier_event_with_larger_sequence():
    with pytest.raises(ValueError, match="unique_causal_symbol_tape_required"):
        plan(episode((trade(62, "3", sequence=1), trade(61, "3", sequence=999))))
