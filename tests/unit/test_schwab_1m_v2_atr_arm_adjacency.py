"""A BUY flip may arm CW-v2 only when its consumed bar pair is adjacent."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
    SymbolState,
    session_start_ts_ms,
)

MINUTE_MS = 60_000
BASE_TS = int(datetime(2026, 9, 1, 11, 0, tzinfo=UTC).timestamp() * 1000)


def _strategy() -> SchwabV2Strategy:
    return SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_atr_flip_enabled=True,
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
        )
    )


def _bar(ts_ms: int, close: float) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=ts_ms,
        open=close,
        high=close + 0.05,
        low=close - 0.05,
        close=close,
        volume=25_000,
    )


def _buy_flip_signal(
    strategy: SchwabV2Strategy,
    symbol: str,
    *,
    gap_minutes: int,
) -> tuple[SymbolState, dict]:
    state = strategy.watchlist_state(symbol)
    current = _bar(BASE_TS, 10.25)
    previous = _bar(BASE_TS - gap_minutes * MINUTE_MS, 9.75)
    state.atr_session_anchor_ms = session_start_ts_ms(current.timestamp_ms)
    state.atr_hl.extend([0.10] * 4)
    state.atr_prev_bar = previous
    state.atr_wilders = 0.10
    state.atr_state = "short"
    state.atr_trail = 10.00
    state.atr_prev_state = "short"
    state.atr_prev_trail = 10.00
    state.bars.append(current)

    signal = strategy._update_atr_state(state, current)
    assert signal is not None and signal["flip"] == "BUY"
    return state, signal


def test_nonadjacent_buy_flip_is_refused_before_arm_and_names_gap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = _strategy()
    state, signal = _buy_flip_signal(strategy, "GAPPY", gap_minutes=21)
    caplog.set_level(logging.WARNING)

    strategy._cw_v2_track(state, signal)

    assert state.cw_armed is False
    assert state.cw_trigger == 0.0
    assert state.cw_segment_high == 0.0
    markers = [
        record.getMessage()
        for record in caplog.records
        if "[V2-ATR-ARM-GAP]" in record.getMessage()
    ]
    assert len(markers) == 1
    assert "phase=live" in markers[0]
    assert "nonadjacent_arms_evaluated_session=1 refused=1" in markers[0]
    assert "consumed_gap_count=1" in markers[0]
    assert f"consumed_gaps={BASE_TS - 21 * MINUTE_MS}->{BASE_TS}(21.0min)" in markers[0]


def test_adjacent_buy_flip_still_arms_without_guard_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = _strategy()
    state, signal = _buy_flip_signal(strategy, "ADJACENT", gap_minutes=1)
    caplog.set_level(logging.WARNING)

    strategy._cw_v2_track(state, signal)

    assert state.cw_armed is True
    assert state.cw_trigger == pytest.approx(10.30)
    assert not any("[V2-ATR-ARM-GAP]" in record.getMessage() for record in caplog.records)


def test_guard_denominator_counts_each_refused_arm_once_per_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = _strategy()
    caplog.set_level(logging.WARNING)

    for symbol, gap_minutes in (("FIRST", 2), ("SECOND", 30)):
        state, signal = _buy_flip_signal(strategy, symbol, gap_minutes=gap_minutes)
        strategy._cw_v2_track(state, signal)

    markers = [
        record.getMessage()
        for record in caplog.records
        if "[V2-ATR-ARM-GAP]" in record.getMessage()
    ]
    assert len(markers) == 2
    assert "nonadjacent_arms_evaluated_session=1" in markers[0]
    assert "nonadjacent_arms_evaluated_session=2" in markers[1]


def test_nonadjacent_sell_flip_still_disarms() -> None:
    strategy = _strategy()
    state = strategy.watchlist_state("SELL")
    current = _bar(BASE_TS, 9.75)
    previous = _bar(BASE_TS - 20 * MINUTE_MS, 10.25)
    state.atr_session_anchor_ms = session_start_ts_ms(current.timestamp_ms)
    state.atr_hl.extend([0.10] * 4)
    state.atr_prev_bar = previous
    state.atr_wilders = 0.10
    state.atr_state = "long"
    state.atr_trail = 10.00
    state.atr_prev_state = "long"
    state.atr_prev_trail = 10.00
    state.cw_armed = True
    state.bars.append(current)

    signal = strategy._update_atr_state(state, current)
    assert signal is not None and signal["flip"] == "SELL"
    strategy._cw_v2_track(state, signal)

    assert state.cw_armed is False
