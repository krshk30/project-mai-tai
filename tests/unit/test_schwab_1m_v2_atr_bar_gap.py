"""True range must never be computed across a bar gap.

⭐ WHY (2026-07-30, live). v2 was stopped 10:12-11:33 ET, leaving a single 85-minute hole on every
watchlist symbol. `href`/`lref` reference `prev.close`, so ONE bar carried 85 minutes of movement
into a 5-period Wilder. NUWE's ATR read 0.149 against a true 1-minute ATR of ~0.06, and
`loss = 3.5 * ATR` placed the resting buy-stop at 4.74 while the operator's TOS chart -- identical
params (5 / 3.5 / WILDERS / modified) -- showed the trail at ~4.40.

The parameters were never wrong. The DATA was discontinuous.
"""
from __future__ import annotations

import logging

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    _ATR_MAX_BAR_GAP_MS,
    OHLCVBar,
    SchwabV2Strategy,
)

MIN = 60_000


def _strat() -> SchwabV2Strategy:
    return SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_atr_flip_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
    ))


def _bar(ts_min: int, hi: float, lo: float, close: float) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=ts_min * MIN, open=(hi + lo) / 2, high=hi, low=lo,
        close=close, volume=50_000,
    )


def _feed(strat, sym: str, bars: list[OHLCVBar]) -> float | None:
    st = strat.watchlist_state(sym)
    for b in bars:
        st.bars.append(b)
        strat._update_atr_state(st, b)
    return st.atr_wilders


def _calm(n: int, start_min: int = 0) -> list[OHLCVBar]:
    """n contiguous quiet 1-minute bars, range 0.10, around 4.20."""
    return [_bar(start_min + i, 4.25, 4.15, 4.20) for i in range(n)]


def test_a_gap_bar_contributes_only_its_OWN_range() -> None:
    """THE REGRESSION: with the guard removed, the post-gap bar's TR carries the whole 85-minute
    move and the ATR balloons."""
    calm = _calm(10)

    # Baseline: the next contiguous bar, same 0.10 range, price unchanged.
    atr_contig = _feed(_strat(), "AAA", calm + [_bar(10, 4.25, 4.15, 4.20)])

    # The NUWE shape: same 0.10 range, but 85 minutes later AND 0.90 lower. Spanning the hole
    # would make true range ~0.95 instead of 0.10.
    atr_gap = _feed(_strat(), "AAA", calm + [_bar(95, 3.35, 3.25, 3.30)])

    assert atr_contig is not None and atr_gap is not None
    assert abs(atr_gap - atr_contig) < 1e-9, (
        "after a gap, only the bar's OWN range may reach the ATR — not the jump across the hole"
    )


def test_spanning_the_gap_would_have_inflated_the_ATR() -> None:
    """Proves the guard is doing real work: the same bars, with the gap bar's price DISPLACED,
    must still not inflate the ATR -- because prev.close is no longer consulted."""
    calm = _calm(10)
    near = _feed(_strat(), "AAA", calm + [_bar(95, 4.25, 4.15, 4.20)])
    far = _feed(_strat(), "AAA", calm + [_bar(95, 3.35, 3.25, 3.30)])  # 0.90 lower after the hole
    assert near is not None and far is not None
    assert abs(far - near) < 1e-9, (
        "after a gap the ATR must depend only on the bar's own range, not on the jump"
    )


def test_a_CONTIGUOUS_jump_is_still_honoured() -> None:
    """⛔ The guard must not swallow real volatility. A genuine adjacent-bar gap-down is exactly
    what true range exists to capture."""
    calm = _calm(10)
    flat = _feed(_strat(), "AAA", calm + [_bar(10, 4.25, 4.15, 4.20)])
    jump = _feed(_strat(), "AAA", calm + [_bar(10, 3.35, 3.25, 3.30)])
    assert flat is not None and jump is not None
    assert jump > flat, "an adjacent-bar move must still raise the ATR"


def test_the_threshold_value_is_pinned() -> None:
    """90s: tolerates ordinary late arrival, catches the smallest real hole seen (2 minutes)."""
    assert _ATR_MAX_BAR_GAP_MS == 90_000


def test_one_bar_late_is_NOT_treated_as_a_gap() -> None:
    """Jitter tolerance -- a bar arriving at +90s must still use the full true range."""
    calm = _calm(10)
    strat = _strat()
    st = strat.watchlist_state("AAA")
    for b in calm:
        st.bars.append(b)
        strat._update_atr_state(st, b)
    baseline = st.atr_wilders
    late = OHLCVBar(timestamp_ms=10 * MIN + 30_000, open=3.30, high=3.35,
                    low=3.25, close=3.30, volume=50_000)   # +90s exactly, big move
    st.bars.append(late)
    strat._update_atr_state(st, late)
    assert baseline is not None and st.atr_wilders is not None
    assert st.atr_wilders > baseline, "a 90s-late bar is still the next bar, not a gap"


def test_replay_gap_does_not_exhaust_live_markers_and_every_marker_has_a_denominator(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warmup gap and two later live gaps on one symbol must emit three distinct markers.

    The old one-per-symbol set emitted only the replay marker, leaving both live gaps invisible.
    """

    strat = _strat()
    state = strat.watchlist_state("AAA")
    caplog.set_level(logging.WARNING)

    for bar in _calm(5):
        state.bars.append(bar)
        strat._update_atr_state(state, bar, observation_phase="replay")

    replay_gap = _bar(10, 4.25, 4.15, 4.20)
    state.bars.append(replay_gap)
    strat._update_atr_state(state, replay_gap, observation_phase="replay")

    live_contiguous = _bar(11, 4.25, 4.15, 4.20)
    state.bars.append(live_contiguous)
    strat._update_atr_state(state, live_contiguous, observation_phase="live")
    for minute in (20, 30):
        live_gap = _bar(minute, 4.25, 4.15, 4.20)
        state.bars.append(live_gap)
        strat._update_atr_state(state, live_gap, observation_phase="live")

    markers = [
        record.getMessage()
        for record in caplog.records
        if "[V2-ATR-BAR-GAP]" in record.getMessage()
    ]
    assert len(markers) == 3
    assert "phase=replay observed_gaps_phase=1 evaluated_pairs_phase=2" in markers[0]
    assert "symbol_observed_gaps_phase=1 marker_cap=none" in markers[0]
    assert "phase=live observed_gaps_phase=1 evaluated_pairs_phase=2" in markers[1]
    assert "symbol_observed_gaps_phase=1 marker_cap=none" in markers[1]
    assert "phase=live observed_gaps_phase=2 evaluated_pairs_phase=3" in markers[2]
    assert "symbol_observed_gaps_phase=2 marker_cap=none" in markers[2]
