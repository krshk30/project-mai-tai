import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_momentum_turn_report import BarPoint  # noqa: E402
from orb_operator_filter_census import (  # noqa: E402
    DAY_GATE,
    LIVE,
    LOOKBACK_ABSOLUTE_LEVELS,
    LOOKBACK_VOLUME_BASELINE,
    POST_FILL_SELL,
    BreakRow,
    IndicatorSnapshot,
    QuoteCoverage,
    QuotePoint,
    SettledDayReport,
    at_et,
    break_indices,
    choppiness,
    evaluate_break,
    evaluate_day_gate,
    evaluate_live_momentum,
    evaluate_post_fill,
    measured_body_percent,
    parse_body_delay,
    render_settled,
    run_settled_name_day,
    settled_decisions,
)
from orb_operator_filter_census import TradePoint  # noqa: E402
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


def snapshot(
    *,
    atr: str = "LONG",
    atr_level: str = "9",
    histogram: str = "0.2",
    prior_histogram: str = "0.1",
    volume_average: str | None = "500",
    rsi: str = "60",
    prior_rsi: str = "55",
    stoch: str = "70",
    prior_stoch: str = "60",
) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        atr_state=atr,
        atr_level=Decimal(atr_level),
        macd=Decimal("0.3"),
        signal=Decimal("0.2"),
        histogram=Decimal(histogram),
        prior_histogram=Decimal(prior_histogram),
        volume_average=Decimal(volume_average) if volume_average is not None else None,
        rsi=Decimal(rsi),
        prior_rsi=Decimal(prior_rsi),
        stoch_k=Decimal(stoch),
        prior_stoch_k=Decimal(prior_stoch),
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


def settled_fixture(
    *,
    bar_0931: BarPoint | None = None,
    bar_0932: BarPoint | None = None,
    snapshot_0930: IndicatorSnapshot | None = None,
    snapshot_0931: IndicatorSnapshot | None = None,
    snapshot_0932: IndicatorSnapshot | None = None,
) -> tuple[list[BarPoint], list[IndicatorSnapshot]]:
    bars = [
        bar(25, "9.40", "9.50", "9.30", "9.50"),
        bar(26, "9.50", "9.60", "9.40", "9.60"),
        bar(27, "9.60", "9.70", "9.50", "9.70"),
        bar(28, "9.70", "9.80", "9.60", "9.80"),
        bar(29, "9.80", "10.00", "9.70", "9.90"),
        bar(30, "9.90", "10.00", "9.85", "9.95"),
        bar_0931 or bar(31, "9.95", "10.20", "9.90", "10.01"),
        bar_0932 or bar(32, "10.01", "10.30", "9.95", "10.20"),
    ]
    indicators = [snapshot() for _ in range(5)] + [
        snapshot_0930 or snapshot(),
        snapshot_0931 or snapshot(),
        snapshot_0932 or snapshot(),
    ]
    return bars, indicators


def trade(minute: int, second: int, price: str) -> TradePoint:
    return TradePoint(at_et(DAY, 9, minute) + timedelta(seconds=second), Decimal(price))


def quote(minute: int, second: int, bid: str, ask: str) -> QuotePoint:
    return QuotePoint(
        at_et(DAY, 9, minute) + timedelta(seconds=second),
        Decimal(bid),
        Decimal(ask),
    )


def run_settled(
    *,
    bars: list[BarPoint] | None = None,
    indicators: list[IndicatorSnapshot] | None = None,
    trades: list[TradePoint] | None = None,
    quotes: list[QuotePoint] | None = None,
    assignment: str = LOOKBACK_VOLUME_BASELINE,
    body_threshold_pct: Decimal = Decimal("45"),
) -> object:
    if bars is None or indicators is None:
        bars, indicators = settled_fixture()
    return run_settled_name_day(
        day=DAY,
        symbol="TEST",
        assignment=assignment,
        bars=bars,
        indicators=indicators,
        trades=trades or [],
        quotes=quotes or [],
        halts=[],
        opening_high=Decimal("10"),
        body_threshold_pct=body_threshold_pct,
    )


def test_settled_output_has_exactly_three_declared_decision_kinds() -> None:
    bars, indicators = settled_fixture()
    run = run_settled(
        bars=bars,
        indicators=indicators,
        trades=[trade(31, 15, "10.01")],
        quotes=[quote(31, 10, "9.99", "10.01"), quote(32, 1, "9.80", "9.82")],
    )

    decisions = settled_decisions(run)

    assert {decision.kind for decision in decisions} == {DAY_GATE, LIVE, POST_FILL_SELL}
    assert all(
        all(check.startswith(f"{decision.kind}:") for check in decision.checks)
        for decision in decisions
    )
    emitted_checks = " ".join(check for decision in decisions for check in decision.checks)
    assert "R2" not in emitted_checks
    assert "R6" not in emitted_checks
    assert "R7" not in emitted_checks


def test_day_gate_is_once_only_and_later_short_atr_cannot_kill_the_day() -> None:
    bars, indicators = settled_fixture(snapshot_0931=snapshot(atr="SHORT"))
    run = run_settled(
        bars=bars,
        indicators=indicators,
        trades=[trade(32, 15, "10.05")],
        quotes=[quote(32, 10, "10.00", "10.02")],
    )

    assert run.gate.status == "ELIGIBLE"
    assert sum(decision.kind == DAY_GATE for decision in settled_decisions(run)) == 1
    assert run.gate.evaluated_at == at_et(DAY, 9, 31)
    assert run.live[0].bar_at == at_et(DAY, 9, 30)
    assert run.live[0].evaluated_at == at_et(DAY, 9, 31)
    assert any(decision.action == "FILL" for decision in run.live)


def test_day_gate_uses_price_against_trail_not_a_later_or_conflicting_state_label() -> None:
    bars, indicators = settled_fixture(snapshot_0930=snapshot(atr="SHORT", atr_level="9"))

    gate = evaluate_day_gate(
        day=DAY,
        symbol="TEST",
        assignment=LOOKBACK_VOLUME_BASELINE,
        bars=bars,
        indicators=indicators,
        opening_high=Decimal("10"),
    )

    assert gate.close == Decimal("9.95")
    assert gate.atr_level == Decimal("9")
    assert gate.status == "ELIGIBLE"


def test_live_pull_is_temporary_and_a_later_bar_can_arm_and_fill() -> None:
    bars, indicators = settled_fixture(
        snapshot_0930=snapshot(histogram="-0.01"),
        snapshot_0931=snapshot(),
    )
    run = run_settled(
        bars=bars,
        indicators=indicators,
        trades=[trade(32, 15, "10.05")],
        quotes=[quote(32, 10, "10.00", "10.02")],
    )

    assert [decision.action for decision in run.live] == ["PULL", "ARM", "FILL"]
    assert run.post_fill is not None


def test_pencil_break_fills_first_then_sells_at_executable_bid() -> None:
    bars, indicators = settled_fixture(
        bar_0931=bar(31, "9.99", "10.20", "9.90", "10.01"),
    )
    run = run_settled(
        bars=bars,
        indicators=indicators,
        trades=[trade(31, 15, "10.01")],
        quotes=[quote(31, 10, "9.99", "10.01"), quote(32, 1, "9.80", "9.82")],
    )

    fill = next(decision for decision in run.live if decision.action == "FILL")

    assert fill.fill_price == Decimal("10")
    assert run.post_fill is not None
    assert run.post_fill.action == "SELL"
    assert run.post_fill.exit_bid == Decimal("9.80")
    assert run.post_fill.return_pct == Decimal("-2.00")


def test_real_body_holds_after_the_fill() -> None:
    bars, indicators = settled_fixture(
        bar_0931=bar(31, "9.90", "10.20", "9.90", "10.10"),
    )
    run = run_settled(
        bars=bars,
        indicators=indicators,
        trades=[trade(31, 15, "10.01")],
        quotes=[quote(31, 10, "9.99", "10.01")],
    )

    assert any(decision.action == "FILL" for decision in run.live)
    assert run.post_fill is not None
    assert run.post_fill.action == "HOLD"


def test_body_measurement_never_reads_a_trade_after_its_decision_time() -> None:
    fill = trade(31, 5, "10.01")
    fill_bar = bar(31, "10.00", "20.00", "5.00", "19.00")
    measurement_at = fill.at + timedelta(seconds=10)

    measured = measured_body_percent(
        fill=fill,
        fill_bar=fill_bar,
        trades=[fill, trade(31, 10, "10.10"), trade(31, 30, "20.00")],
        measurement_at=measurement_at,
        delay=timedelta(seconds=10),
    )

    assert measured == Decimal("1")


def test_body_threshold_and_measurement_delay_are_parameters() -> None:
    fill = trade(31, 15, "10.01")
    fill_bar = bar(31, "10.00", "10.20", "9.80", "10.20")
    common = {
        "day": DAY,
        "symbol": "TEST",
        "assignment": LOOKBACK_VOLUME_BASELINE,
        "opening_high": Decimal("10"),
        "fill": fill,
        "fill_bar": fill_bar,
        "trades": [fill],
        "quotes": [quote(32, 1, "9.80", "9.82")],
        "halts": [],
        "body_delay": None,
        "delay_label": "bar-close",
    }

    assert evaluate_post_fill(**common, body_threshold_pct=Decimal("45")).action == "HOLD"
    assert evaluate_post_fill(**common, body_threshold_pct=Decimal("60")).action == "SELL"
    assert parse_body_delay("bar-close") == (None, "bar-close")
    assert parse_body_delay("15") == (timedelta(seconds=15), "15s-after-fill")


def test_lookback_assignment_is_reported_both_ways_without_changing_live_momentum() -> None:
    bars, indicators = settled_fixture(
        snapshot_0930=snapshot(rsi="45", prior_rsi="40", stoch="45", prior_stoch="40"),
    )

    baseline = evaluate_day_gate(
        day=DAY,
        symbol="TEST",
        assignment=LOOKBACK_VOLUME_BASELINE,
        bars=bars,
        indicators=indicators,
        opening_high=Decimal("10"),
    )
    absolute = evaluate_day_gate(
        day=DAY,
        symbol="TEST",
        assignment=LOOKBACK_ABSOLUTE_LEVELS,
        bars=bars,
        indicators=indicators,
        opening_high=Decimal("10"),
    )

    assert baseline.status == "ELIGIBLE"
    assert absolute.status == "KILLED"
    live = evaluate_live_momentum(
        day=DAY,
        symbol="TEST",
        assignment=LOOKBACK_VOLUME_BASELINE,
        opening_high=Decimal("10"),
        bar=bars[5],
        current=indicators[5],
    )
    assert live.action == "ARM"


def test_four_red_runup_is_a_day_gate_not_a_live_pull() -> None:
    bars, indicators = settled_fixture()
    for offset in range(4):
        item = bars[offset]
        bars[offset] = bar(
            25 + offset,
            str(item.open),
            str(item.high),
            str(item.low),
            str(item.open - Decimal("0.05")),
        )

    run = run_settled(bars=bars, indicators=indicators)

    assert run.gate.status == "KILLED"
    assert run.live == ()


def test_state_machine_emits_only_one_fill_even_when_later_prints_break_again() -> None:
    bars, indicators = settled_fixture()
    run = run_settled(
        bars=bars,
        indicators=indicators,
        trades=[trade(31, 15, "10.01"), trade(32, 15, "10.10")],
        quotes=[quote(31, 10, "9.99", "10.01"), quote(32, 1, "9.80", "9.82")],
    )

    assert sum(decision.action == "FILL" for decision in run.live) == 1


def test_render_names_both_assignments_old_new_totals_and_daic_control() -> None:
    bars, indicators = settled_fixture()
    runs = tuple(
        run_settled(
            bars=bars,
            indicators=indicators,
            assignment=assignment,
            trades=[trade(31, 15, "10.01")],
            quotes=[quote(31, 10, "9.99", "10.01"), quote(32, 1, "9.80", "9.82")],
        )
        for assignment in (LOOKBACK_VOLUME_BASELINE, LOOKBACK_ABSOLUTE_LEVELS)
    )
    runs = tuple(
        type(run)(
            day=date(2026, 8, 25),
            symbol="DAIC",
            assignment=run.assignment,
            gate=run.gate,
            live=run.live,
            post_fill=run.post_fill,
        )
        for run in runs
    )
    report = SettledDayReport(
        day=date(2026, 8, 25),
        watched_symbols=("DAIC",),
        no_level_symbols=(),
        legacy_rows=(),
        runs=runs,
    )

    output = render_settled(
        [report],
        coverage=QuoteCoverage(date(2026, 8, 24), date(2026, 9, 4)),
        requested_days=[date(2026, 8, 25)],
        unreachable_days=[],
        assignments=(LOOKBACK_VOLUME_BASELINE, LOOKBACK_ABSOLUTE_LEVELS),
        body_threshold_pct=Decimal("45"),
        delay_label="bar-close",
    )

    assert "Legacy comparison denominator" in output
    assert "unanswerable / candidates" in output
    assert "volume-baseline" in output
    assert "absolute-levels" in output
    assert "DAIC 2026-08-25 control" in output
    assert "DAY_GATE" in output
    assert "POST_FILL_SELL" in output
