"""D20 denominator (#862 pinned schema): a crossed LIVE mirror must stay COUNTABLE while suppressed.

#858's guards fixed the 08-31 duplicate legs by returning before the cross detector's emit —
which was also the only record a crossing happened, so post-fix the duplicate grade's
denominator would read zero by construction and a PASS would be circular. Per the #862 event
contract, `[V2-FANOUT-MIRROR-LIVE-CROSS]` fires BEFORE the claim/mirror guards, once per REAL
below-to-at-or-above transition (edge-triggered, `cross_seq` per slot), live bars only, and is
not gated on Schwab quantity or the claim — those are other protections and must not cover for
the mirror-live veto under test.

Every test drives `_fanout_rth_resting_cross` and reads the emitted log — behavioural, not
source-inspection (the #739 lesson).
"""

from __future__ import annotations

import logging

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
    SymbolState,
)

SEGMENT = 1787846400000
OBSERVED = "[V2-FANOUT-MIRROR-LIVE-CROSS]"


def _strategy() -> tuple[SchwabV2Strategy, SymbolState]:
    strategy = SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        )
    )
    strategy._resting_session_is_eh = lambda now=None: False
    strategy._now_ms = lambda: SEGMENT
    strategy._entries_held = False
    strategy._resting_entry_enabled = True
    state = strategy.watchlist_state("MIMI")
    state.fanout_segment_id = SEGMENT
    state.resting_active = True
    state.resting_level = 5.0
    state.last_resting_placed_slot = "first"
    state.bars.append(
        OHLCVBar(
            timestamp_ms=SEGMENT - 30_000,
            open=4.9, high=5.0, low=4.8, close=4.95, volume=25_000,
        )
    )
    return strategy, state


def _live_mirror(state: SymbolState) -> None:
    """The state the mirror placement path leaves behind: rest working at Webull, edge armed
    ('a new mirror level is placed' resets the observation edge, per the #862 contract)."""
    state.webull_resting_active = True
    state.fanout_mirror_cross_below_seen = True


def _quote(px: float) -> Quote:
    return Quote("MIMI", px - 0.01, px + 0.01, px, SEGMENT, 0)


def _cross(strategy: SchwabV2Strategy, state: SymbolState, caplog, px: float = 5.02) -> str:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="project_mai_tai.strategy_core.schwab_1m_v2"):
        strategy._fanout_rth_resting_cross(state, _quote(px))
    return caplog.text


def test_first_cross_after_placement_is_observed_and_still_suppressed(caplog) -> None:
    """⭐ THE CONTROL. Placement arms the edge, so the first live up-cross emits cross_seq=1
    with the slot_id join key — and the #858 suppression is untouched (no draft, no claim)."""
    strategy, state = _strategy()
    _live_mirror(state)

    text = _cross(strategy, state, caplog)

    assert OBSERVED in text, (
        "the crossing left no record — the duplicate grade's denominator is zero by "
        "construction and any PASS would be circular"
    )
    line = next(ln for ln in text.splitlines() if OBSERVED in ln)
    assert "slot_id=" in line and "cross_seq=1" in line
    assert strategy.drain_webull_fanout_intents() == [], "observation must not weaken the guard"
    assert state.fanout_webull_claimed is False, "observation must not claim"


def test_repeated_quotes_above_the_level_do_not_inflate_the_denominator(caplog) -> None:
    """#862: 'Repeated quotes above one level must not inflate the denominator.' Only a print
    BELOW the level re-arms the edge; the re-cross then emits cross_seq=2 on the same slot."""
    strategy, state = _strategy()
    _live_mirror(state)
    assert OBSERVED in _cross(strategy, state, caplog)

    assert OBSERVED not in _cross(strategy, state, caplog, px=5.10), (
        "a held level is being counted once per QUOTE — the denominator is up-cross EDGES"
    )

    assert OBSERVED not in _cross(strategy, state, caplog, px=4.90), "a dip is not a cross"
    text = _cross(strategy, state, caplog, px=5.05)
    assert OBSERVED in text, "a REAL second up-cross of the same slot was swallowed"
    assert "cross_seq=2" in text, "the second real up-cross must be numbered, same slot identity"


def test_an_unarmed_edge_emits_nothing(caplog) -> None:
    """⛔ Mutant-killer for dropping the edge condition: mirror live but no placement arm and
    no below print — an above-print alone is not a transition."""
    strategy, state = _strategy()
    state.webull_resting_active = True
    assert state.fanout_mirror_cross_below_seen is False

    assert OBSERVED not in _cross(strategy, state, caplog)


def test_no_observation_below_the_level(caplog) -> None:
    """THE PAIR ARM — vary ONLY px. Below the level there is no crossing to count."""
    strategy, state = _strategy()
    _live_mirror(state)

    assert OBSERVED not in _cross(strategy, state, caplog, px=4.90)


def test_no_observation_without_a_live_mirror(caplog) -> None:
    """⛔ POPULATION GUARD. The denominator is crossed-LIVE-MIRROR opportunities. With no
    mirror working, the same crossing must not enter it — the detector legitimately EMITS
    instead (the single-leg path), which is the existing control behaviour."""
    strategy, state = _strategy()
    state.fanout_mirror_cross_below_seen = True
    assert state.webull_resting_active is False

    text = _cross(strategy, state, caplog)

    assert OBSERVED not in text, "a non-mirror crossing entered the denominator"
    assert len(strategy.drain_webull_fanout_intents()) == 1, "the single-leg emit must survive"


def test_no_observation_off_a_stale_bar(caplog) -> None:
    """A warmup-replayed or stale tape must not manufacture denominator (#528 mirror rule)."""
    strategy, state = _strategy()
    _live_mirror(state)
    state.bars[-1] = OHLCVBar(
        timestamp_ms=SEGMENT - 3_600_000,
        open=4.9, high=5.0, low=4.8, close=4.95, volume=25_000,
    )

    assert OBSERVED not in _cross(strategy, state, caplog)


def test_a_new_segment_slot_restarts_cross_seq(caplog) -> None:
    """`cross_seq` distinguishes up-crosses WITHIN one economic slot; a new segment's slot is a
    new denominator lineage and numbers from 1 again."""
    strategy, state = _strategy()
    _live_mirror(state)
    assert "cross_seq=1" in _cross(strategy, state, caplog)

    state.fanout_segment_id = SEGMENT + 600_000   # next segment, same symbol
    state.fanout_mirror_cross_below_seen = True   # new placement arms the edge
    text = _cross(strategy, state, caplog, px=5.20)
    assert OBSERVED in text and "cross_seq=1" in text, (
        "a second segment's first crossing must be seq 1 of a NEW slot, not seq 2 of the old"
    )


def test_observation_is_not_gated_on_the_claim(caplog) -> None:
    """#862: 'Do not gate the observation on Schwab position quantity or fanout_webull_claimed.
    Those are other protections and would cover for the mirror-live veto under test.'"""
    strategy, state = _strategy()
    _live_mirror(state)
    identity = strategy._fanout_identity_metadata(state, source="rth_resting_mirror")
    assert strategy._claim_fanout_webull(state, identity=identity, reason="mirror-test")

    text = _cross(strategy, state, caplog)

    assert OBSERVED in text, "a held claim hid the crossing — another protection covered for it"
    assert strategy.drain_webull_fanout_intents() == []
