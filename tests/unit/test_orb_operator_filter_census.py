import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_momentum_turn_report import BarPoint  # noqa: E402
from orb_operator_filter_census import (  # noqa: E402
    BreakRow,
    IndicatorSnapshot,
    at_et,
    break_indices,
    choppiness,
    evaluate_break,
)
from project_mai_tai.market_halts import HaltWindow  # noqa: E402

DAY = date(2026, 9, 3)


def bar(minute: int, open_: str, high: str, low: str, close: str, volume: int = 1000) -> BarPoint:
    return BarPoint(
        at_et(DAY, 9, minute),
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        volume,
        "live",
    )


def snapshot(*, atr: str = "LONG", histogram: str = "0.2", prior_histogram: str = "0.1") -> IndicatorSnapshot:
    return IndicatorSnapshot(
        atr_state=atr,
        atr_level=Decimal("9"),
        macd=Decimal("0.3"),
        signal=Decimal("0.2"),
        histogram=Decimal(histogram),
        prior_histogram=Decimal(prior_histogram),
        volume_average=Decimal("500"),
        rsi=Decimal("60"),
        prior_rsi=Decimal("55"),
        stoch_k=Decimal("70"),
        prior_stoch_k=Decimal("60"),
    )


def passing_fixture() -> tuple[list[BarPoint], list[IndicatorSnapshot], int]:
    bars = [
        bar(25, "9.4", "9.5", "9.3", "9.5"),
        bar(26, "9.5", "9.6", "9.4", "9.6"),
        bar(27, "9.6", "9.7", "9.5", "9.7"),
        bar(28, "9.7", "9.8", "9.6", "9.8"),
        bar(29, "9.8", "10.0", "9.7", "9.9"),
        bar(30, "9.9", "10.1", "9.85", "10.08", 1000),
    ]
    return bars, [snapshot() for _ in bars], 5


def evaluate(bars, indicators, index) -> BreakRow:
    from orb_operator_filter_census import QuotePoint

    return evaluate_break(
        day=DAY,
        symbol="TEST",
        break_number=1,
        bars=bars,
        index=index,
        indicators=indicators,
        opening_high=Decimal("10"),
        halts=[],
        quotes=[QuotePoint(bars[index].at + timedelta(seconds=30), Decimal("10"), Decimal("10.1"))],
    )


def test_break_requires_a_cross_from_at_or_below() -> None:
    bars, _, _ = passing_fixture()
    bars.append(bar(31, "10.08", "10.2", "10.05", "10.1"))

    assert break_indices(bars, Decimal("10")) == [5]


def test_red_break_bar_is_rejected() -> None:
    bars, indicators, index = passing_fixture()
    bars[index] = bar(30, "10.1", "10.2", "9.9", "10.0")

    row = evaluate(bars, indicators, index)

    assert "R2 RED_BREAK_BAR" in row.reasons


def test_real_body_guard_rejects_a_wick_break() -> None:
    bars, indicators, index = passing_fixture()
    bars[index] = bar(30, "9.99", "10.20", "9.80", "10.01")

    row = evaluate(bars, indicators, index)

    assert "R4 BODY_LT_45PCT" in row.reasons


def test_stack_guard_reads_all_five_components() -> None:
    bars, indicators, index = passing_fixture()
    indicators[index] = snapshot(histogram="0.05", prior_histogram="0.10")

    row = evaluate(bars, indicators, index)

    assert "R3 STACK_DISAGREES" in row.reasons
    assert row.macd_bullish is False


def test_atr_guard_rejects_a_short_bar_before_the_break() -> None:
    bars, indicators, index = passing_fixture()
    bars[index] = bar(30, "9.9", "9.99", "9.85", "9.98")
    bars.append(bar(31, "9.98", "10.1", "9.95", "10.08"))
    indicators[index] = snapshot(atr="SHORT")
    indicators.append(snapshot(atr="LONG"))
    index += 1

    row = evaluate(bars, indicators, index)

    assert "R1 ATR_NOT_CONTINUOUSLY_LONG" in row.reasons


def test_four_red_opening_bars_are_rejected() -> None:
    bars, indicators, index = passing_fixture()
    for offset in range(4):
        item = bars[offset]
        bars[offset] = bar(25 + offset, str(item.open), str(item.high), str(item.low), str(item.open - Decimal("0.05")))

    row = evaluate(bars, indicators, index)

    assert "R5 FOUR_OF_FIVE_RED" in row.reasons


def test_chop_guard_is_behavioural() -> None:
    bars = [
        bar(25, "10", "10.2", "9.8", "10"),
        bar(26, "10", "10.2", "9.8", "10.1"),
        bar(27, "10.1", "10.2", "9.8", "9.9"),
        bar(28, "9.9", "10.2", "9.8", "10.1"),
        bar(29, "10.1", "10.2", "9.8", "9.95"),
    ]

    efficiency, reversals, is_choppy = choppiness(bars, 4)

    assert efficiency is not None and efficiency < Decimal("0.35")
    assert reversals == 3
    assert is_choppy is True


def test_third_accepted_trade_is_rejected_by_the_two_trade_cap() -> None:
    bars, indicators, index = passing_fixture()
    from orb_operator_filter_census import QuotePoint

    row = evaluate_break(
        day=DAY,
        symbol="TEST",
        break_number=3,
        bars=bars,
        index=index,
        indicators=indicators,
        opening_high=Decimal("10"),
        halts=[],
        quotes=[QuotePoint(bars[index].at, Decimal("10"), Decimal("10.1"))],
        accepted_before=2,
    )

    assert "R7 TWO_ACCEPTED_TRADES_ALREADY" in row.reasons


def test_raw_third_break_does_not_consume_trade_cap_after_rejections() -> None:
    bars, indicators, index = passing_fixture()
    from orb_operator_filter_census import QuotePoint

    row = evaluate_break(
        day=DAY,
        symbol="TEST",
        break_number=3,
        bars=bars,
        index=index,
        indicators=indicators,
        opening_high=Decimal("10"),
        halts=[],
        quotes=[QuotePoint(bars[index].at, Decimal("10"), Decimal("10.1"))],
        accepted_before=0,
    )

    assert row.status == "PASS"


def test_missing_executable_quote_is_unanswerable() -> None:
    bars, indicators, index = passing_fixture()

    row = evaluate_break(
        day=DAY,
        symbol="TEST",
        break_number=1,
        bars=bars,
        index=index,
        indicators=indicators,
        opening_high=Decimal("10"),
        halts=[],
        quotes=[],
    )

    assert "UNANSWERABLE NO_NBBO" in row.reasons


def test_break_inside_a_confirmed_halt_is_unanswerable() -> None:
    bars, indicators, index = passing_fixture()
    from orb_operator_filter_census import QuotePoint

    row = evaluate_break(
        day=DAY,
        symbol="TEST",
        break_number=1,
        bars=bars,
        index=index,
        indicators=indicators,
        opening_high=Decimal("10"),
        halts=[
            HaltWindow(
                bars[index].at - timedelta(minutes=1),
                bars[index].at + timedelta(minutes=2),
                3,
            )
        ],
        quotes=[QuotePoint(bars[index].at, Decimal("10"), Decimal("10.1"))],
    )

    assert row.status == "UNANSWERABLE"
    assert "UNANSWERABLE HALT" in row.reasons


def test_future_atr_state_cannot_reject_an_earlier_break() -> None:
    bars, indicators, index = passing_fixture()
    bars.append(bar(31, "10.08", "10.2", "10.0", "10.15"))
    indicators.append(snapshot(atr="SHORT"))

    row = evaluate(bars, indicators, index)

    assert row.status == "PASS"
