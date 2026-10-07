"""Supplemental both-switches-ON controls, using the retained recorded fixtures."""
from tests.unit.test_slotclear1_fresh_flip import lpcn, buy, crossing
from tests.unit.test_slotclear1_fresh_sell import seeded, observe, recorded_probes
from tests.unit.test_v2_flip_owned_first_entry import _book, _leg, PRIMARY, WEBULL
import pytest
from copy import deepcopy
import json
from pathlib import Path


def test_both_switches_on_preserves_recorded_narrow_fresh_buy_and_no_duplicate():
    strategy, state, clock, _, _ = lpcn()
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_sell_enabled = True
    buy(strategy, state)
    draft = strategy.on_quote("LPCN", crossing(clock))
    assert draft is not None and draft.metadata["cw_entry_slot"] == "first"
    assert len(strategy.drain_webull_fanout_intents()) == 1
    assert strategy.on_quote("LPCN", crossing(clock)) is None
    assert strategy.drain_webull_fanout_intents() == []


def test_both_switches_on_keeps_recorded_fresh_sell_on_normal_three_bar_rest():
    strategy, state, clock, _, _, _ = seeded(partial=True)
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = True
    for index, probe in enumerate(list(recorded_probes("SXTC"))[:4]):
        observe(strategy, state, clock, probe)
        primary = strategy.drain_pending_intents()
        mirror = strategy.drain_webull_direct_intents()
        assert len(primary) == len(mirror) == int(index == 3)
        assert not state.slotclear_fresh_buy_bar_ms


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("phase", ["bound", "consumed", "awaiting_close", "unknown"])
def test_both_switches_on_preserves_exact_owned_row_on_recorded_sell(account, phase):
    strategy, state, clock, _, _, _ = seeded(partial=True)
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = True
    probe = list(recorded_probes("SXTC"))[0]
    clock[0] = probe[0]
    opportunity = 1791380522895
    state.flip_owner_phase = phase
    state.flip_owner_opportunity_id = state.fanout_segment_id = opportunity
    state.flip_owner_first_rest_placed = True
    state.flip_owner_fill_accounts = {account}
    state.flip_owner_position_ids = {account: "controlled-owned-row"}
    state.flip_owner_position_entry_ms = {account: opportunity}
    # Owner-state safety counterfactual, not a claim of an SXTC historical fill.
    _book(strategy, clock, "SXTC", _leg(account, "controlled-owned-row", entered_ms=opportunity))
    observe(strategy, state, clock, probe, book=False)
    assert state.flip_owner_opportunity_id == opportunity
    assert state.flip_owner_position_ids == {account: "controlled-owned-row"}
    assert state.flip_owner_open_positions[account].managed_row_id == "controlled-owned-row"
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert not any(d.intent_type == "open" for d in strategy.drain_pending_intents())


def test_present_durable_sxtc_tickets_veto_entry_after_recorded_sell_even_handoff_off():
    strategy, state, clock, _, _, _ = seeded(partial=True)
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = True
    strategy.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled = False
    receipt = json.loads(Path(__file__).with_name("RPG_TICKETS_PRESENT_2026-10-07.json").read_text())
    tickets = {row["id"]: deepcopy(row["payload"]) for row in receipt["rows"]
               if row["payload"]["old"]["symbol"] == "SXTC"}
    assert len(tickets) == 2
    strategy._rpg_handoffs = deepcopy(tickets)
    assert strategy._rpg_entry_owned(state, account=PRIMARY, slot="first")
    assert strategy._rpg_entry_owned(state, account=WEBULL, slot="first")
    for index, probe in enumerate(recorded_probes("SXTC")):
        observe(strategy, state, clock, probe)
        if index == 0:
            assert not state.cw_resting_taken and not state.cw_reclaim_taken
        assert not any(d.intent_type == "open" for d in strategy.drain_pending_intents())
        assert not any(d.intent_type == "open" for d in strategy.drain_webull_direct_intents())
        assert strategy._rpg_handoffs == tickets
    assert not state.resting_active and not state.webull_resting_active
    print("PRESENT_TICKET_CONTROL: 19 recorded probes; SLOT released; PRIMARY/MIRROR veto; "
          "0 open drafts; durable payloads unchanged; handoff OFF; not historical clearance")
