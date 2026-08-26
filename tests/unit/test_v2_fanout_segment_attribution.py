"""C3: every emitted Webull fan-out leg must carry a stable segment identity."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy


MARKER = "[V2-FANOUT-SEGMENT-BOUND]"
RTH_MS = int(datetime(2026, 8, 25, 15, 0, tzinfo=UTC).timestamp() * 1000)


def _strategy(*, fanout: bool = True) -> SchwabV2Strategy:
    return SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=fanout,
    ))


def _bar(ts: int, high: float) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=ts, open=high - 0.1, high=high, low=high - 0.2,
        close=high - 0.05, volume=25_000,
    )


def _signal(*, flip=None) -> dict:
    return {
        "touch": False, "touch_price": None, "flip": flip,
        "flip_level": 9.5 if flip == "BUY" else None,
        "trail": 9.5, "loss": 0.5, "state": "long", "state_age": 1,
    }


def _arm_reactive(strategy: SchwabV2Strategy):
    state = strategy.watchlist_state("DAIC")
    for offset, high in enumerate((12.0, 10.0, 11.0)):
        state.bars.append(_bar(RTH_MS + offset * 60_000, high))
        strategy._cw_v2_track(state, _signal(flip="BUY") if offset == 0 else _signal())
    strategy._cw_v2_track(state, _signal())
    return state


def test_emitted_draft_binds_identity_and_fires_success_marker(caplog) -> None:
    strategy = _strategy()
    strategy._now_ms = lambda: RTH_MS
    state = strategy.watchlist_state("DAIC")

    with caplog.at_level(logging.INFO):
        draft = strategy._build_webull_fanout_draft(
            state, entry_px=3.20, session_is_eh=True, source="eh_resting", entry_n=1,
        )

    assert draft.metadata["fanout_segment_id"] == str(RTH_MS)
    assert state.fanout_segment_id == RTH_MS
    lines = [r.getMessage() for r in caplog.records if MARKER in r.getMessage()]
    assert len(lines) == 1
    assert "trigger=fanout_draft" in lines[0] and "attributed=1" in lines[0]


def test_fanout_off_reactive_path_stays_quiet(caplog) -> None:
    """Negative control: the success marker must not fire merely because the primary ran."""
    strategy = _strategy(fanout=False)
    state = _arm_reactive(strategy)
    quote = Quote("DAIC", 12.49, 12.51, 12.50, RTH_MS + 180_000, 0)

    with caplog.at_level(logging.INFO):
        primary = strategy._cw_v2_quote(state, quote)

    assert primary is not None
    assert strategy.drain_webull_fanout_intents() == []
    assert not any(MARKER in r.getMessage() for r in caplog.records)


def test_prearm_resting_and_later_reactive_share_one_identity() -> None:
    strategy = _strategy()
    strategy._now_ms = lambda: RTH_MS
    state = strategy.watchlist_state("DAIC")

    resting = strategy._build_webull_fanout_draft(
        state, entry_px=3.20, session_is_eh=True, source="eh_resting", entry_n=1,
    )
    state.cw_arm_bar_ts = RTH_MS + 60_000  # the BUY flip closes after the resting cross
    reactive = strategy._build_webull_fanout_draft(
        state, entry_px=3.30, session_is_eh=False, source="reactive", entry_n=1,
    )

    assert resting.metadata["cw_arm_bar_ts"] == "0"
    assert reactive.metadata["cw_arm_bar_ts"] == str(RTH_MS + 60_000)
    assert resting.metadata["fanout_segment_id"] == reactive.metadata["fanout_segment_id"]


def test_resting_mirror_is_inside_the_attributed_population(caplog) -> None:
    """The six 08-25 mirror fills are real executions; they may not stay excluded or unstamped."""
    strategy = _strategy()
    strategy._webull_resting_mirror_enabled = True
    strategy._resting_entry_band_pct = 0.5
    strategy._eh_resting_enabled = False
    strategy._resting_session_is_eh = lambda *_args, **_kwargs: False
    strategy._now_ms = lambda: RTH_MS
    state = strategy.watchlist_state("DAIC")

    with caplog.at_level(logging.INFO):
        strategy._queue_resting_place(state, 3.20, slot="first")

    mirror = strategy.drain_webull_direct_intents()
    assert len(mirror) == 1
    assert mirror[0].metadata["fanout_source"] == "rth_resting_mirror"
    assert mirror[0].metadata["fanout_segment_id"] == str(RTH_MS)
    assert any(MARKER in r.getMessage() for r in caplog.records)


def test_segment_end_clears_identity_for_the_next_segment() -> None:
    strategy = _strategy()
    state = strategy.watchlist_state("DAIC")
    state.cw_armed = True
    state.cw_arm_bar_ts = RTH_MS
    state.fanout_segment_id = RTH_MS

    assert strategy._release_arm(state, "control") is True
    assert state.fanout_segment_id == 0


def test_session_anchor_reset_clears_identity_before_the_next_segment() -> None:
    """04:00 ET reset must not attribute the new session to yesterday's segment."""
    strategy = _strategy()
    state = strategy.watchlist_state("DAIC")
    state.fanout_segment_id = RTH_MS
    next_session_ms = RTH_MS + 24 * 60 * 60 * 1000

    strategy._apply_session_anchor_reset(state, next_session_ms)

    assert state.fanout_segment_id == 0
    strategy._now_ms = lambda: next_session_ms
    draft = strategy._build_webull_fanout_draft(
        state, entry_px=3.20, session_is_eh=True, source="eh_resting", entry_n=1,
    )
    assert draft.metadata["fanout_segment_id"] == str(next_session_ms)
    assert draft.metadata["fanout_segment_id"] != str(RTH_MS)


def test_sell_flip_clears_identity_before_the_next_segment() -> None:
    """The ordinary flip-close path must end the segment, not leak its id forward."""
    strategy = _strategy()
    state = strategy.watchlist_state("DAIC")
    state.cw_armed = True
    state.cw_arm_bar_ts = RTH_MS
    state.fanout_segment_id = RTH_MS
    state.bars.append(_bar(RTH_MS + 60_000, 10.0))

    strategy._cw_v2_track(state, _signal(flip="SELL"))

    assert state.fanout_segment_id == 0
    next_segment_ms = RTH_MS + 120_000
    state.bars.append(_bar(next_segment_ms, 10.25))
    strategy._now_ms = lambda: next_segment_ms
    draft = strategy._build_webull_fanout_draft(
        state, entry_px=3.20, session_is_eh=True, source="eh_resting", entry_n=1,
    )
    assert draft.metadata["fanout_segment_id"] == str(next_segment_ms)
    assert draft.metadata["fanout_segment_id"] != str(RTH_MS)
