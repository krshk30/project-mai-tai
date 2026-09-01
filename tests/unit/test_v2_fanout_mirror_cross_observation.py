"""D20 denominator (2026-09-01): a crossed LIVE mirror must stay COUNTABLE while suppressed.

#858's guards fixed the 08-31 duplicate legs by returning before the cross detector's emit —
which was also the only line recording that a crossing happened. Next session's fan-out
acceptance grades `duplicate_legs` against crossed-live-mirror slots (08-31 had 5 by the old
line, 18-19 by proxy), so without a pre-guard observation the grade's denominator reads zero
and a PASS would be dishonest. The marker fires BEFORE the claim/mirror guards, once per
slot_id, on a live bar only.

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
OBSERVED = "[V2-FANOUT-MIRROR-CROSS-OBSERVED]"


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


def _quote(px: float) -> Quote:
    return Quote("MIMI", px - 0.01, px + 0.01, px, SEGMENT, 0)


def _cross(strategy: SchwabV2Strategy, state: SymbolState, caplog, px: float = 5.02) -> str:
    with caplog.at_level(logging.INFO, logger="project_mai_tai.strategy_core.schwab_1m_v2"):
        strategy._fanout_rth_resting_cross(state, _quote(px))
    return caplog.text


def test_crossed_live_mirror_is_observed_once_and_still_suppressed(caplog) -> None:
    """⭐ THE CONTROL. Mirror live + px over the level: exactly one OBSERVED line, carrying the
    slot_id join key — and the #858 suppression is untouched (no draft, no claim). A second
    quote over the level must NOT log a second line: the denominator is SLOTS, not quotes."""
    strategy, state = _strategy()
    state.webull_resting_active = True

    text = _cross(strategy, state, caplog)

    assert OBSERVED in text, (
        "the crossing left no record — next session's duplicate grade has denominator zero "
        "and a PASS would be dishonest"
    )
    line = next(ln for ln in text.splitlines() if OBSERVED in ln)
    assert "slot_id=" in line and "slot_id= " not in line, "the slot_id join key is missing"
    assert strategy.drain_webull_fanout_intents() == [], "observation must not weaken the guard"
    assert state.fanout_webull_claimed is False, "observation must not claim"

    caplog.clear()
    text2 = _cross(strategy, state, caplog, px=5.10)
    assert OBSERVED not in text2, "dedup broken: one slot is being counted once per QUOTE"


def test_no_observation_below_the_level(caplog) -> None:
    """THE PAIR ARM — vary ONLY px. Below the level there is no crossing to count."""
    strategy, state = _strategy()
    state.webull_resting_active = True

    text = _cross(strategy, state, caplog, px=4.90)

    assert OBSERVED not in text, "an uncrossed level is being counted into the denominator"


def test_no_observation_without_a_live_mirror(caplog) -> None:
    """⛔ POPULATION GUARD. The denominator is crossed-LIVE-MIRROR slots. With no mirror
    working, the same crossing must not enter it — here the detector legitimately EMITS
    instead (the pre-#858 single-leg path), which is the existing control behaviour."""
    strategy, state = _strategy()
    assert state.webull_resting_active is False

    text = _cross(strategy, state, caplog)

    assert OBSERVED not in text, (
        "a non-mirror crossing entered the denominator — the grade would divide by the wrong "
        "population"
    )
    assert len(strategy.drain_webull_fanout_intents()) == 1, "the single-leg emit must survive"


def test_no_observation_off_a_stale_bar(caplog) -> None:
    """A warmup-replayed or stale tape must not manufacture denominator (#528 mirror rule)."""
    strategy, state = _strategy()
    state.webull_resting_active = True
    state.bars[-1] = OHLCVBar(
        timestamp_ms=SEGMENT - 3_600_000,
        open=4.9, high=5.0, low=4.8, close=4.95, volume=25_000,
    )

    text = _cross(strategy, state, caplog)

    assert OBSERVED not in text, "a stale bar produced a live-crossing observation"


def test_a_new_segment_slot_is_observed_again(caplog) -> None:
    """The dedup key is the slot_id, not the symbol: a NEW segment's resting slot is a new
    denominator unit and must log its own line."""
    strategy, state = _strategy()
    state.webull_resting_active = True
    _cross(strategy, state, caplog)
    caplog.clear()

    state.fanout_segment_id = SEGMENT + 600_000   # next segment, same symbol
    text = _cross(strategy, state, caplog, px=5.20)

    assert OBSERVED in text, "a second segment's crossing was swallowed by the first's dedup"
