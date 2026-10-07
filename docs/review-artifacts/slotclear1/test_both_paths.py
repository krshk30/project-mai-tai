"""Supplemental both-switches-ON controls, using the retained recorded fixtures."""
from tests.unit.test_slotclear1_fresh_flip import lpcn, buy, crossing
from tests.unit.test_slotclear1_fresh_sell import seeded, observe, recorded_probes


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
