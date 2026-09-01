"""DB2/DB3 (2026-08-31): the Webull fan-out claim vs the Schwab-scoped union.

Three one-variable controlled pairs, each with both outcomes reachable:

1. The software cross detector must not emit while a mirrored rest is WORKING at Webull
   (vary only `webull_resting_active`).
2. The zero-hold clock must not ARM while the mirror is working — post-#843 the union is
   Schwab-scoped, so union==0 is the mirror's normal steady state (vary only
   `webull_resting_active`).
3. An expired hold must not RELEASE a FILLED claim — a filled Webull leg is a live position
   at its venue, invisible to the Schwab union by design (vary only the claim outcome).

Live evidence 2026-08-31: all 29 filled Webull legs had their claim erased ~29s after fill by
`schwab_union_zero_positive_evidence_hold_expired`; 5 became mirror-fill + cross-fill duplicate
bare positions (YDDL, WETO, NCRA x2, RDHL), each a 0.8-13.4s race the async #848 slot
consumption could not win.
"""

from __future__ import annotations

import logging

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    FANOUT_POSITIVE_ZERO_HOLD_MS,
    OHLCVBar,
    SchwabV2Strategy,
    SymbolState,
)

SEGMENT = 1787846400000


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
    state = strategy.watchlist_state("MIMI")
    state.fanout_segment_id = SEGMENT
    return strategy, state


def _armed_rest(strategy: SchwabV2Strategy, state: SymbolState) -> None:
    """A live Schwab RTH rest with a fresh bar over the liquidity floor: every guard of the
    cross detector EXCEPT the one under test passes."""
    strategy._entries_held = False       # boot hold released (same convention as the #848 tests)
    strategy._resting_entry_enabled = True  # prod runs with the resting entry ON
    state.resting_active = True
    state.resting_level = 5.0
    state.last_resting_placed_slot = "first"
    state.bars.append(
        OHLCVBar(
            timestamp_ms=SEGMENT - 30_000,
            open=4.9,
            high=5.0,
            low=4.8,
            close=4.95,
            volume=25_000,
        )
    )


def _quote(px: float) -> Quote:
    return Quote("MIMI", px - 0.01, px + 0.01, px, SEGMENT, 0)


def _mirror_claim(strategy: SchwabV2Strategy, state: SymbolState, *, outcome: str) -> None:
    identity = strategy._fanout_identity_metadata(state, source="rth_resting_mirror")
    assert strategy._claim_fanout_webull(state, identity=identity, reason="mirror-test")
    state.fanout_claim_outcome = outcome


# --- Pair 1: the cross detector vs a working mirror -------------------------------------------


def test_cross_emits_when_no_mirror_is_working() -> None:
    """CONTROL: with the claim free and no working rest, the detector queues the leg — the
    duplicate outcome is reachable, so pair-arm B below measures the guard, not a dead path."""
    strategy, state = _strategy()
    _armed_rest(strategy, state)
    assert state.webull_resting_active is False

    strategy._fanout_rth_resting_cross(state, _quote(5.02))

    assert len(strategy.drain_webull_fanout_intents()) == 1


def test_cross_suppressed_while_mirror_rest_is_working() -> None:
    """Vary ONLY `webull_resting_active`. The 08-31 ordering: zero-hold freed the claim while
    the mirrored rest still sat at Webull; the cross then filled BOTH legs into one slot."""
    strategy, state = _strategy()
    _armed_rest(strategy, state)
    state.webull_resting_active = True

    strategy._fanout_rth_resting_cross(state, _quote(5.02))

    assert strategy.drain_webull_fanout_intents() == []
    assert state.fanout_webull_claimed is False  # nothing claimed either


# --- Pair 2: arming the zero-hold vs a working mirror -----------------------------------------


def test_zero_hold_arms_when_positive_evidence_and_no_working_rest() -> None:
    """CONTROL: positive held evidence with NO working rest still arms the hold and, after the
    calibrated bound, still releases — the #824 mechanism survives the fix."""
    strategy, state = _strategy()
    _mirror_claim(strategy, state, outcome="held")
    state.webull_resting_active = False

    strategy.update_position("MIMI", 0, held_qty=0)
    assert state.fanout_zero_hold_started_ms == SEGMENT

    strategy._now_ms = lambda: SEGMENT + FANOUT_POSITIVE_ZERO_HOLD_MS + 1
    strategy.update_position("MIMI", 0, held_qty=0)
    assert state.fanout_webull_claimed is False  # released


def test_zero_hold_never_arms_while_mirror_rest_is_working(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Vary ONLY `webull_resting_active`. While the mirror works at Webull, union==0 is its
    normal steady state — not release evidence. On 08-31 this armed ~25s after EVERY mirror
    placement, all day."""
    strategy, state = _strategy()
    _mirror_claim(strategy, state, outcome="held")
    state.webull_resting_active = True

    with caplog.at_level(logging.INFO):
        strategy.update_position("MIMI", 0, held_qty=0)

    assert state.fanout_zero_hold_started_ms == 0
    assert "[V2-FANOUT-CLAIM-ZERO-HOLD]" not in caplog.text
    assert state.fanout_webull_claimed is True


# --- Pair 3: an expired hold vs a FILLED claim ------------------------------------------------


def test_expired_hold_releases_a_held_claim_with_no_working_rest() -> None:
    """CONTROL: outcome `held`, no working rest — the expiry release still fires."""
    strategy, state = _strategy()
    _mirror_claim(strategy, state, outcome="held")
    state.webull_resting_active = False
    state.fanout_zero_hold_started_ms = SEGMENT

    strategy._now_ms = lambda: SEGMENT + FANOUT_POSITIVE_ZERO_HOLD_MS + 1
    strategy.update_position("MIMI", 0, held_qty=0)

    assert state.fanout_webull_claimed is False


def test_expired_hold_vetoes_release_of_a_filled_claim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Vary ONLY the claim outcome (`filled` vs `held`). A filled Webull leg is a live position
    at ITS venue; the Schwab-scoped union cannot testify about it. WETO 08-31 11:30:41 ET: the
    filled position's claim was erased 35s after its fill by this branch."""
    strategy, state = _strategy()
    _mirror_claim(strategy, state, outcome="filled")
    state.webull_resting_active = False
    state.fanout_zero_hold_started_ms = SEGMENT

    strategy._now_ms = lambda: SEGMENT + FANOUT_POSITIVE_ZERO_HOLD_MS + 1
    with caplog.at_level(logging.INFO):
        strategy.update_position("MIMI", 0, held_qty=0)

    assert state.fanout_webull_claimed is True
    assert "[V2-FANOUT-CLAIM-ZERO-HOLD-VETOED]" in caplog.text
    # Timer cleared: the veto logs once per zero episode, not on every 5s poll.
    assert state.fanout_zero_hold_started_ms == 0
    strategy.update_position("MIMI", 0, held_qty=0)
    assert caplog.text.count("[V2-FANOUT-CLAIM-ZERO-HOLD-VETOED]") == 1
