from dataclasses import replace
from datetime import UTC, datetime, timedelta

from project_mai_tai.backtest.atr_straight_down_study import (
    BarState,
    _trades_in_bar,
    hypothesis_results,
)
from project_mai_tai.backtest.data import Trade


def _state(
    *, symbol: str, target: bool, bar_number: int, traded_above: bool | None
) -> BarState:
    signal = datetime(2026, 8, 24, 12, tzinfo=UTC)
    return BarState(
        session_day_et="2026-08-24",
        split="build",
        symbol=symbol,
        buy_signal_ts=signal,
        entry_ts=signal + timedelta(seconds=2),
        entry_px=10.0,
        never_touched_plus_1=target,
        bar_number=bar_number,
        bar_start_ts=signal + timedelta(minutes=bar_number - 1),
        bar_close_ts=signal + timedelta(minutes=bar_number),
        strategy_bar_available=True,
        post_fill_trade_prints=1 if traded_above is not None else 0,
        missing_state="" if traded_above is not None else "trade_prints",
        close_vs_entry_pct=-1.0,
        close_above_entry=False,
        traded_above_entry=traded_above,
        bar_direction="down",
        running_low_pct=-2.0,
        volume_ratio_20=1.0,
        macd_histogram=-0.1,
        macd_histogram_pct=-0.01,
        macd_histogram_direction="falling",
        stochastic=20.0,
        rsi=30.0,
        dot_consensus=1,
        vwap=10.1,
        price_vs_vwap_pct=-1.0,
        above_vwap=False,
        atr_trailing_stop=10.2,
        atr_stop_vs_price_pct=2.0,
        atr_stop_position="above",
    )


def test_trade_bar_excludes_pre_fill_and_next_boundary() -> None:
    start = datetime(2026, 8, 24, 12, tzinfo=UTC)
    entry = start + timedelta(seconds=2)
    end = start + timedelta(minutes=1)
    trades = [
        Trade(start + timedelta(seconds=1), 11.0, 1),
        Trade(start + timedelta(seconds=2), 10.0, 1),
        Trade(end - timedelta(microseconds=1), 9.9, 1),
        Trade(end, 12.0, 1),
    ]

    observed = _trades_in_bar(trades, entry, start, end)

    assert [trade.price for trade in observed] == [10.0, 9.9]


def test_hypothesis_counts_targets_and_comparators_separately() -> None:
    states = []
    for symbol, target, bar2, bar3 in (
        ("TARGET_MATCH", True, False, False),
        ("TARGET_MISS", True, False, True),
        ("OTHER_MATCH", False, False, False),
        ("OTHER_MISS", False, True, False),
    ):
        base = _state(symbol=symbol, target=target, bar_number=1, traded_above=False)
        states.extend(
            [
                base,
                replace(base, bar_number=2, traded_above_entry=bar2),
                replace(base, bar_number=3, traded_above_entry=bar3),
                replace(base, bar_number=4, traded_above_entry=False),
                replace(base, bar_number=5, traded_above_entry=False),
            ]
        )

    result = next(
        row
        for row in hypothesis_results(states)
        if row["horizon_bar"] == 3 and row["split"] == "build"
    )

    assert result["target_caught"] == 1
    assert result["comparator_touched"] == 1
    assert result["target_total"] == 2
    assert result["comparator_total"] == 2


def test_hypothesis_does_not_treat_missing_prints_as_no_print_above() -> None:
    states = [
        _state(symbol="TARGET", target=True, bar_number=bar, traded_above=False)
        for bar in range(1, 6)
    ]
    states[2] = replace(
        states[2], traded_above_entry=None, post_fill_trade_prints=0, missing_state="trade_prints"
    )

    result = next(
        row
        for row in hypothesis_results(states)
        if row["horizon_bar"] == 3 and row["split"] == "build"
    )

    assert result["target_assessed"] == 0
    assert result["target_caught"] == 0
    assert result["unavailable"] == 1
