from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from project_mai_tai.fanout_outcome_consumer import FanoutOutcome
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy, SymbolState


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


def _filled(metadata: dict[str, str], *, evidence: str | None = None) -> FanoutOutcome:
    return FanoutOutcome(
        record_id=uuid4(),
        created_at=datetime.now(UTC),
        symbol="MIMI",
        segment_id=int(metadata["fanout_segment_id"]),
        slot=metadata["fanout_slot"],
        slot_id=metadata["fanout_slot_id"],
        attempt_id=metadata.get("fanout_attempt_id", ""),
        outcome="filled",
        evidence_id=evidence or str(uuid4()),
    )


def _consume(
    strategy: SchwabV2Strategy,
    state: SymbolState,
    *,
    source: str,
) -> dict[str, str]:
    identity = strategy._fanout_identity_metadata(state, source=source)
    assert strategy._claim_fanout_webull(state, identity=identity, reason="first-attempt")
    assert strategy.apply_fanout_outcome(_filled(identity)) == "consumed"
    strategy._release_fanout_webull_claim(state, reason="position-closed", persist=False)
    return identity


def _bar(high: float, timestamp_ms: int) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=timestamp_ms,
        open=high - 0.1,
        high=high,
        low=high - 0.2,
        close=high - 0.05,
        volume=25_000,
    )


def _quote(px: float) -> Quote:
    return Quote("MIMI", px - 0.01, px + 0.01, px, SEGMENT, 0)


def test_confirmed_fill_consumes_webull_resting_slot_after_claim_release(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The MIMI/PPCB ordering: fill, temporary claim clears, replacement tries same slot."""

    strategy, state = _strategy()
    strategy._queue_resting_place(state, 5.0, slot="first")
    first_primary = strategy.drain_pending_intents()
    first_mirror = strategy.drain_webull_direct_intents()
    assert len(first_primary) == len(first_mirror) == 1

    assert strategy.apply_fanout_outcome(_filled(first_mirror[0].metadata)) == "consumed"
    assert state.fanout_webull_resting_taken is True

    strategy._queue_resting_cancel(state, reason="filled")
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    assert strategy._release_fanout_webull_claim(
        state, reason="positive_zero_hold_elapsed", persist=False
    )
    assert state.fanout_webull_claimed is False
    assert state.fanout_webull_resting_taken is True

    with caplog.at_level(logging.INFO):
        strategy._queue_resting_place(state, 5.1, slot="first")

    # Reading A: Webull's fill consumes Webull's resting slot only. Schwab's primary remains
    # independently eligible; emitting another Webull mirror would be the six-excess-fill defect.
    assert len(strategy.drain_pending_intents()) == 1
    assert strategy.drain_webull_direct_intents() == []
    assert state.webull_resting_active is False
    assert "[V2-FANOUT-SLOT-CONSUMED]" in caplog.text
    assert "attempted=1 suppressed=1" in caplog.text


def test_consumed_resting_slot_does_not_consume_webull_reclaim_slot() -> None:
    """Reading A plus the #644 composition cap: one resting and one reclaim per venue."""

    strategy, state = _strategy()
    resting = strategy._fanout_identity_metadata(state, source="rth_resting_mirror")
    assert strategy._claim_fanout_webull(state, identity=resting, reason="first-resting")
    assert strategy.apply_fanout_outcome(_filled(resting)) == "consumed"
    strategy._release_fanout_webull_claim(state, reason="position-closed", persist=False)

    assert strategy._claim_fanout_webull(
        state,
        identity=strategy._fanout_identity_metadata(state, source="reactive"),
        reason="first-reclaim",
    )
    assert state.fanout_webull_resting_taken is True
    assert state.fanout_webull_reclaim_taken is False


def test_consumed_slot_blocks_the_schwab_fill_fanout_but_not_the_schwab_fill() -> None:
    strategy, state = _strategy()
    _consume(strategy, state, source="rth_resting")
    strategy._fanout_on_fill_enabled = True
    state.resting_level = 5.0
    state.last_resting_placed_slot = "first"

    strategy.update_position("MIMI", 1, held_qty=1)

    assert state.position_qty_held == 1
    assert state.cw_resting_taken is True
    assert strategy.drain_webull_fanout_intents() == []


def test_consumed_resting_slot_blocks_the_rth_software_cross_emitter() -> None:
    strategy, state = _strategy()
    _consume(strategy, state, source="rth_resting")
    strategy._entries_held = False
    strategy._resting_entry_enabled = True
    strategy._resting_max_bar_age_ms = 300_000
    strategy._liquidity_floor_ok = lambda _state: True
    state.resting_active = True
    state.resting_level = 5.0
    state.bars.append(_bar(5.0, SEGMENT))

    strategy._fanout_rth_resting_cross(state, _quote(5.1))

    assert strategy.drain_webull_fanout_intents() == []


def test_consumed_resting_slot_blocks_only_the_webull_half_of_the_eh_cross() -> None:
    strategy, state = _strategy()
    _consume(strategy, state, source="eh_resting")
    strategy._entries_held = False
    strategy._eh_resting_enabled = True
    strategy._resting_max_bar_age_ms = 300_000
    strategy._resting_session_is_eh = lambda now=None: True
    state.resting_active = True
    state.resting_level = 5.0
    state.bars.append(_bar(5.0, SEGMENT))

    primary = strategy._eh_resting_cross_check(state, _quote(5.1))

    assert primary is not None
    assert primary.metadata["fanout_slot"] == "resting"
    assert strategy.drain_webull_fanout_intents() == []


def test_consumed_reclaim_slot_blocks_only_the_webull_half_of_the_reactive_cross() -> None:
    strategy, state = _strategy()
    for high, timestamp_ms in ((12.0, 1), (10.0, 2), (11.0, 3)):
        state.bars.append(_bar(high, timestamp_ms))
        strategy._cw_v2_track(
            state,
            {
                "flip": "BUY" if timestamp_ms == 1 else None,
                "flip_level": 9.5 if timestamp_ms == 1 else None,
                "trail": 9.5,
            },
        )
    strategy._cw_v2_track(state, {"flip": None, "trail": 9.5})
    _consume(strategy, state, source="reactive")

    primary = strategy._cw_v2_quote(state, _quote(12.5))

    assert primary is not None
    assert primary.metadata["cw_entry_slot"] == "reclaim"
    assert strategy.drain_webull_fanout_intents() == []


def test_segment_end_releases_both_webull_composition_slots() -> None:
    strategy, state = _strategy()
    resting = strategy._fanout_identity_metadata(state, source="rth_resting_mirror")
    assert strategy._claim_fanout_webull(state, identity=resting, reason="resting")
    assert strategy.apply_fanout_outcome(_filled(resting)) == "consumed"
    strategy._release_fanout_webull_claim(state, reason="between-slots", persist=False)

    reclaim = strategy._fanout_identity_metadata(state, source="reactive")
    assert strategy._claim_fanout_webull(state, identity=reclaim, reason="reclaim")
    assert strategy.apply_fanout_outcome(_filled(reclaim)) == "consumed"
    assert state.fanout_webull_resting_taken is True
    assert state.fanout_webull_reclaim_taken is True

    state.cw_armed = True
    assert strategy._release_arm(state, "segment-ended") is True
    assert state.fanout_webull_resting_taken is False
    assert state.fanout_webull_reclaim_taken is False


def test_restart_replay_keeps_consumed_slot_through_historical_anchor_replay() -> None:
    strategy = SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        )
    )
    strategy._now_ms = lambda: SEGMENT
    strategy.configure_fanout_identity_persistence(None, {"MIMI": SEGMENT})
    identity = strategy._fanout_identity_metadata(
        strategy.watchlist_state("MIMI"), source="rth_resting_mirror", segment_id=SEGMENT
    )

    assert strategy.apply_fanout_outcome(_filled(identity)) == "consumed"
    state = strategy.watchlist_state("MIMI")
    current_anchor = strategy._restored_fanout_session_anchor_ms
    strategy._apply_session_anchor_reset(state, current_anchor)

    assert state.fanout_webull_resting_taken is True
    assert not strategy._claim_fanout_webull(
        state, identity=identity, reason="same-session-after-restart"
    )

    strategy._apply_session_anchor_reset(state, current_anchor + 24 * 60 * 60 * 1000)
    assert state.fanout_webull_resting_taken is False
