"""New pure replay integration mechanics; CONTROLLED, not the 173 replay."""

from dataclasses import replace
from decimal import Decimal

import pytest

from project_mai_tai.atr_simulation.execution_eligibility import execution_status
from project_mai_tai.atr_simulation.firstbar import CompletedFlipBar, FirstBarMode
from project_mai_tai.atr_simulation.firstbar_replay import (
    FirstBarFillPolicy, FirstBarReplayEngine, FirstBarReplayInput,
)
from project_mai_tai.atr_simulation.types import (
    Boundary, Coverage, EpisodeInput, EventKey, EvidenceKind, FlipIdentity,
    LiveFacts, SimExitReason, SourceRef,
)
from tests.support.atr_simulation_eligibility import POLICY, controlled_trade


SOURCE = SourceRef("CONTROLLED:replay", "c" * 64, "$", EvidenceKind.CONTROLLED)
NS = 1_000_000_000
COVERAGE = Coverage(0, 300 * NS, True, (), (SOURCE,))
POLICY_MODE = FirstBarFillPolicy.OBSERVED_PRINT_VOLUME


def trade(seconds, price="2.01", size=100):
    return controlled_trade(EventKey(seconds * NS, 0, f"raw:{seconds}", SOURCE),
                            "X", Decimal(price), size)


def item(rows, account="one", quantity=196):
    episode = EpisodeInput(
        FlipIdentity("2026-10-23", "X", "webull", account, "ATR", "flip", "segment"),
        Boundary(60 * NS, SOURCE, bar_start_ns=0), True, SOURCE,
        Decimal("1.98"), quantity, SOURCE, Boundary(240 * NS, SOURCE),
        Boundary(300 * NS, SOURCE), tuple(rows), (), COVERAGE, COVERAGE,
        (), COVERAGE, LiveFacts(None, (), (), replace(COVERAGE, complete=None)), POLICY,
    )
    bar = CompletedFlipBar("X", 0, 60 * NS, Decimal("2"), Decimal("1.99"),
                           Decimal("1.98"), "long", True, SOURCE)
    return FirstBarReplayInput(episode, bar, Boundary(180 * NS, SOURCE))


def run(value):
    return FirstBarReplayEngine().simulate(value, fill_policy=POLICY_MODE)


def test_candidate_connects_to_partial_allocations_and_target_not_full_print_shortcut():
    value = item((trade(61), trade(62), trade(63, "2.2")))
    for result in run(value):
        assert [fill.quantity for fill in result.fills] == [100, 96]
        assert result.position.filled_qty == 196
        assert result.exits[0].reason == SimExitReason.TARGET_5
        assert result.gross_pnl == Decimal("21.56")  # C1 21:29: ceiling(2.01*1.05)=2.12.
        assert result.live is value.episode.live
        assert result.clock_label == "reconstructed SIP clock"


def test_modes_are_separate_worlds_and_only_x3_times_out():
    results = run(item((trade(61, size=196), trade(182, "2.00"))))
    firstbar, x3 = results
    assert firstbar.mode is FirstBarMode.FIRSTBAR
    assert x3.mode is FirstBarMode.FIRSTBAR_X3
    assert firstbar.exits[0].reason is SimExitReason.OPEN
    assert x3.exits[0].reason is SimExitReason.TIME_NO_1PCT_120S
    assert x3.exits[0].at.event_ns == 182 * NS
    assert len(firstbar.fills) == len(x3.fills) == 1


def test_same_physical_print_never_reused_across_accounts_in_either_world():
    rows = (trade(61), trade(62))
    engine = FirstBarReplayEngine()
    for cohort in ((item(rows, "one"), item(rows, "two")),
                   (item(rows, "two"), item(rows, "one"))):
        results = engine.simulate_many(cohort, fill_policy=POLICY_MODE)
        for mode in FirstBarMode:
            selected = tuple(result for result in results if result.mode is mode)
            assert sum(fill.quantity for result in selected for fill in result.fills) == 200
            by_account = {result.identity.account_id: result.position.filled_qty for result in selected}
            assert by_account == {"one": 196, "two": 4}


def test_unknown_plan_cannot_spend_volume_or_certify_other_account():
    rows = (trade(61), trade(62))
    known = item(rows, "two")
    uncertain = item(rows, "one")
    uncertain = replace(uncertain, episode=replace(uncertain.episode, watched=None))
    results = FirstBarReplayEngine().simulate_many((uncertain, known), fill_policy=POLICY_MODE)
    for result in results:
        assert result.decision == "UNKNOWN"
        assert result.fills == ()
        assert result.gross_pnl is None


def test_later_unknown_pending_volume_marks_both_accounts_unknown():
    unknown = replace(trade(62), execution=None)
    assert execution_status(unknown)[0] is None
    rows = (trade(61), unknown, trade(63))
    results = FirstBarReplayEngine().simulate_many(
        (item(rows, "one"), item(rows, "two")), fill_policy=POLICY_MODE)
    for mode in FirstBarMode:
        selected = tuple(result for result in results if result.mode is mode)
        assert sum(fill.quantity for result in selected for fill in result.fills) == 100
        assert all(result.decision == "UNKNOWN" and result.gross_pnl is None for result in selected)


def test_known_false_flip_is_footer_skip_not_trade_row():
    value = item((trade(61),))
    value = replace(value, flip_bar=replace(value.flip_bar, close=Decimal("1.97"),
                                           atr_state_at_close="short"))
    for result in run(value):
        assert result.is_skipped
        assert not result.is_trade
        assert result.fills == ()
        assert result.decision == "SKIPPED_FALSE_FLIP"


def test_conflicting_raw_id_content_refused_before_allocation():
    one = item((trade(61),), "one")
    two = item((trade(61, size=200),), "two")
    with pytest.raises(ValueError, match="conflicting_shared_firstbar_print"):
        FirstBarReplayEngine().simulate_many((one, two), fill_policy=POLICY_MODE)


def test_sell_boundary_cancels_remainder_before_print_allocation():
    value = item((trade(61), trade(62)))
    value = replace(value, episode=replace(value.episode, sell_flip=Boundary(62 * NS, SOURCE)))
    for result in run(value):
        assert [fill.quantity for fill in result.fills] == [100]
        assert result.exits[0].reason is SimExitReason.SELL_FLIP
        assert result.exits[0].quantity == 100
        assert result.position.remainder_cancel_reason == "SELL_FLIP"


@pytest.mark.parametrize("transition", ("overlapping-flips", "next-sell-transition"))
def test_same_account_distinct_flips_never_authorize_overlapping_positions(transition):
    rows = (trade(61, size=196), trade(120, size=196),
            trade(181, size=196), trade(240, size=196))
    first = item(rows)
    first = replace(first, episode=replace(first.episode, sell_flip=Boundary(120 * NS, SOURCE)))
    second = replace(first, episode=replace(first.episode,
        identity=replace(first.episode.identity, flip_id="flip-two", segment_id="segment-two"),
        sell_flip=Boundary(240 * NS, SOURCE)))
    if transition == "next-sell-transition":
        second = replace(second, episode=replace(second.episode,
            buy_flip=Boundary(180 * NS, SOURCE, bar_start_ns=120 * NS)),
            flip_bar=replace(second.flip_bar, start_ns=120 * NS, available_ns=180 * NS),
            entry_cutoff=Boundary(270 * NS, SOURCE))
    results = FirstBarReplayEngine().simulate_many((first, second), fill_policy=POLICY_MODE)
    for mode in FirstBarMode:
        selected = tuple(result for result in results if result.mode is mode)
        assert selected[0].identity.account_id == selected[1].identity.account_id
        assert selected[0].identity.flip_id != selected[1].identity.flip_id
        if transition == "overlapping-flips":
            assert all(result.decision == "UNKNOWN" for result in selected)
            assert all("UNKNOWN_OVERLAPPING_FIRSTBAR_SLOT" in result.issues for result in selected)
            assert all(not result.fills and result.gross_pnl is None for result in selected)
        else:
            assert all(result.decision == "TRADE_CLOSED" for result in selected)
            assert [result.fills[0].at.event_ns for result in selected] == [61 * NS, 181 * NS]
            assert selected[0].exits[0].at.event_ns == 120 * NS
            assert selected[0].exits[0].at.event_ns < selected[1].fills[0].at.event_ns


def test_sell_boundary_without_exit_print_does_not_clear_old_position_for_new_flip():
    first = item((trade(61, size=196),))
    first = replace(first, episode=replace(first.episode, sell_flip=Boundary(120 * NS, SOURCE)))
    second = item((trade(181, size=196),))
    second = replace(second, episode=replace(second.episode,
        identity=replace(second.episode.identity, flip_id="flip-two", segment_id="segment-two"),
        buy_flip=Boundary(180 * NS, SOURCE, bar_start_ns=120 * NS)),
        flip_bar=replace(second.flip_bar, start_ns=120 * NS, available_ns=180 * NS),
        entry_cutoff=Boundary(270 * NS, SOURCE))
    results = FirstBarReplayEngine().simulate_many((first, second), fill_policy=POLICY_MODE)
    for mode in FirstBarMode:
        old, new = tuple(result for result in results if result.mode is mode)
        assert old.position.filled_qty == 196
        assert old.exits[0].reason is SimExitReason.OPEN
        assert new.decision == "UNKNOWN"
        assert "UNKNOWN_PRIOR_FIRSTBAR_SLOT_NOT_CLOSED" in new.issues
        assert not new.fills
        assert new.gross_pnl is None
