from datetime import UTC, datetime

import pytest

from project_mai_tai.backtest.atr_combination_study import (
    Atom,
    FeatureSnapshot,
    LockedEntry,
    _rule_result,
    target_outcome,
)
from project_mai_tai.backtest.data import Quote


def _snapshot(*, split: str, reached_5: bool, price: float | None) -> FeatureSnapshot:
    ts = datetime(2026, 8, 24, 12, tzinfo=UTC)
    return FeatureSnapshot(
        session_day_et="2026-08-24",
        split=split,
        symbol="TEST",
        buy_signal_ts=ts,
        entry_ts=ts,
        entry_px=10.0,
        reached_5=reached_5,
        checkpoint_minutes=3,
        checkpoint_ts=ts,
        quote_ts=ts,
        bar_close_ts=ts,
        missing_state="",
        price_vs_entry_pct=price,
        max_up_so_far_pct=price,
        max_down_so_far_pct=price,
        touched_plus_2=False,
        touched_minus_3=False,
        volume_ratio_20=1.0,
        macd_histogram=0.1,
        macd_histogram_pct=0.01,
        macd_histogram_direction="rising",
        stochastic=50.0,
        rsi=50.0,
        dot_consensus=2,
        atr_trailing_stop=9.5,
        atr_direction="long",
        vwap=9.9,
        price_vs_vwap_pct=1.0,
        above_vwap=True,
        minutes_since_flip=3.0,
        last_bar_direction="up",
        latest_minute_new_low=False,
    )


def test_rule_result_keeps_split_separate_and_reports_unassessed() -> None:
    atom = Atom(
        checkpoint=3,
        feature="price_vs_entry_pct",
        text="price <= -1",
        evaluate=lambda row: (
            None if row.price_vs_entry_pct is None else row.price_vs_entry_pct <= -1
        ),
    )
    rows = [
        _snapshot(split="build", reached_5=True, price=1.0),
        _snapshot(split="build", reached_5=False, price=-2.0),
        _snapshot(split="build", reached_5=False, price=None),
        _snapshot(split="holdout", reached_5=True, price=-2.0),
    ]

    result = _rule_result((atom,), rows, "build")

    assert result["build_winner_kept"] == 1
    assert result["build_loser_removed"] == 1
    assert result["build_loser_kept"] == 1
    assert result["build_unassessed"] == 1


def test_target_outcome_fills_target_at_target_price() -> None:
    start = datetime(2026, 8, 24, 12, tzinfo=UTC)
    entry = LockedEntry(
        session_day_et="2026-08-24",
        symbol="TEST",
        buy_signal_ts=start,
        entry_ts=start,
        entry_px=10.0,
        reached_5=True,
        natural_exit_ts=start.replace(minute=5),
        natural_return_pct=1.0,
        natural_max_up_pct=6.0,
        natural_max_down_pct=-1.0,
    )
    quotes = [Quote(start.replace(minute=1), 10.1, 10.2), Quote(start.replace(minute=2), 10.3, 10.4)]

    outcome = target_outcome(entry, quotes, 2.0)

    assert outcome["reason"] == "target"
    assert outcome["return_pct"] == 2.0


def test_target_outcome_models_next_quote_stop_slippage() -> None:
    start = datetime(2026, 8, 24, 12, tzinfo=UTC)
    entry = LockedEntry(
        session_day_et="2026-08-24",
        symbol="TEST",
        buy_signal_ts=start,
        entry_ts=start,
        entry_px=10.0,
        reached_5=False,
        natural_exit_ts=start.replace(minute=5),
        natural_return_pct=-5.0,
        natural_max_up_pct=0.0,
        natural_max_down_pct=-10.0,
    )
    quotes = [Quote(start.replace(minute=1), 9.19, 9.2), Quote(start.replace(minute=2), 9.0, 9.1)]

    outcome = target_outcome(entry, quotes, 4.0)

    assert outcome["reason"] == "hard_stop"
    assert outcome["return_pct"] == pytest.approx(-10.0)
