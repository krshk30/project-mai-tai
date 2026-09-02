from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.backtest.atr_flip_hold_study import (
    FlipCandidate,
    HoldPolicy,
    simulate_policy,
)
from project_mai_tai.backtest.data import Quote

BASE = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def _candidate(quotes: list[Quote], *, sell_after: int | None = None) -> FlipCandidate:
    return FlipCandidate(
        session_day_et="2026-09-01",
        symbol="TEST",
        scanner_window_start=BASE - timedelta(minutes=1),
        scanner_window_end=None,
        buy_bar_ts=BASE - timedelta(minutes=1),
        buy_signal_ts=BASE,
        buy_close=10.0,
        buy_trail=9.5,
        decision_gap_minutes=1.0,
        entry_ts=quotes[0].ts,
        entry_px=quotes[0].ask,
        entry_bid=quotes[0].bid,
        entry_quote_index=0,
        sell_signal_ts=(BASE + timedelta(seconds=sell_after) if sell_after else None),
    )


def _quote(seconds: int, bid: float, ask: float | None = None) -> Quote:
    return Quote(BASE + timedelta(seconds=seconds), bid, ask if ask is not None else bid + 0.01)


def test_hard_stop_fills_on_quote_after_touch() -> None:
    quotes = [_quote(0, 9.99, 10.0), _quote(1, 9.19), _quote(2, 9.10)]
    outcome = simulate_policy(
        _candidate(quotes),
        quotes,
        BASE + timedelta(minutes=1),
        HoldPolicy("stop", -8.0),
    )

    assert outcome.exit_reason == "hard_stop"
    assert outcome.exit_ts == BASE + timedelta(seconds=2)
    assert outcome.exit_px == pytest.approx(9.10)


def test_scaled_exit_weights_both_targets() -> None:
    quotes = [_quote(0, 9.99, 10.0), _quote(1, 10.51), _quote(2, 10.81)]
    outcome = simulate_policy(
        _candidate(quotes),
        quotes,
        BASE + timedelta(minutes=1),
        HoldPolicy("scale", -10.0, 5.0, 0.4, 8.0, 0.0),
    )

    assert outcome.exit_reason == "target"
    assert outcome.first_sale_fraction == pytest.approx(0.4)
    assert outcome.return_pct == pytest.approx(0.4 * 5.0 + 0.6 * 8.0)


def test_earned_floor_replaces_wide_initial_stop() -> None:
    quotes = [
        _quote(0, 9.99, 10.0),
        _quote(1, 10.51),
        _quote(2, 10.19),
        _quote(3, 10.18),
    ]
    outcome = simulate_policy(
        _candidate(quotes),
        quotes,
        BASE + timedelta(minutes=1),
        HoldPolicy("floor", -10.0, 5.0, 0.5, 10.0, 2.0),
    )

    assert outcome.exit_reason == "earned_floor"
    assert outcome.return_pct == pytest.approx(0.5 * 5.0 + 0.5 * 1.8)


def test_atr_sell_exits_before_session_close() -> None:
    quotes = [_quote(0, 9.99, 10.0), _quote(1, 10.20), _quote(2, 10.10)]
    outcome = simulate_policy(
        _candidate(quotes, sell_after=2),
        quotes,
        BASE + timedelta(minutes=1),
        HoldPolicy("flip", None),
    )

    assert outcome.exit_reason == "atr_sell"
    assert outcome.exit_ts == BASE + timedelta(seconds=2)
    assert outcome.return_pct == pytest.approx(1.0)


def test_scaled_runner_can_exceed_first_target_before_trailing_exit() -> None:
    quotes = [
        _quote(0, 9.99, 10.0),
        _quote(1, 10.51),
        _quote(2, 11.20),
        _quote(3, 10.95),
        _quote(4, 10.90),
    ]
    outcome = simulate_policy(
        _candidate(quotes),
        quotes,
        BASE + timedelta(minutes=1),
        HoldPolicy("trail", -10.0, 5.0, 0.5, None, 0.0, 2.0),
    )

    assert outcome.exit_reason == "trail"
    assert outcome.exit_ts == BASE + timedelta(seconds=4)
    assert outcome.return_pct == pytest.approx(0.5 * 5.0 + 0.5 * 9.0)
