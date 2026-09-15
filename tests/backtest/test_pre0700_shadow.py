from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.backtest.data import SchwabBar, Trade, build_bars
from project_mai_tai.backtest.pre0700_shadow import (
    COVERAGE_EXPECTED_MINUTES,
    EvidenceUnknown,
    ProbeReading,
    SymbolDayInput,
    _assert_after_close,
    _canonical_0708,
    analyze_shadow_trades,
    compare_overlap,
    evaluate_symbol_day,
    parse_probe_lines,
    summarize,
)

DAY = date(2026, 9, 15)
UTC = timezone.utc
LOCAL_ET = ZoneInfo("America/New_York")


@pytest.fixture
def crossing_tape() -> tuple[Trade, ...]:
    """Committed synthetic tape: initialized long, SELL at 06:59, BUY at 07:03."""

    start = datetime.combine(DAY, time(6, 50), tzinfo=LOCAL_ET)
    prices = [10.0] * 9 + [9.0, 9.0, 9.0, 9.0, 9.20] + [9.20] * 5
    return tuple(
        Trade(ts=start + timedelta(minutes=index, seconds=1), price=price, size=100)
        for index, price in enumerate(prices)
    )


def _schwab_overlap(trades: tuple[Trade, ...]) -> tuple[SchwabBar, ...]:
    bars = build_bars(trades, datetime.combine(DAY, time(4), tzinfo=LOCAL_ET))
    return tuple(
        SchwabBar(
            ts=int(bar.timestamp.timestamp() * 1000),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=int(bar.volume),
        )
        for bar in bars
        if time(7, 0) <= bar.timestamp.astimezone(LOCAL_ET).time() <= time(7, 8)
    )


def _matching_probe(bars: tuple[SchwabBar, ...], *, symbol: str = "TEST") -> ProbeReading:
    state, trail, problem = _canonical_0708(bars, DAY)
    assert problem == ""
    assert state is not None and trail is not None
    return ProbeReading(
        symbol=symbol,
        bar_at=datetime.combine(DAY, time(7, 8), tzinfo=LOCAL_ET),
        state=state,
        trail=trail,
    )


def _input(trades: tuple[Trade, ...]) -> SymbolDayInput:
    bars = _schwab_overlap(trades)
    return SymbolDayInput(
        day=DAY,
        symbol="TEST",
        massive_trades=trades,
        schwab_bars=bars,
        levelone_ticks=(),
        probe=_matching_probe(bars),
    )


def test_0703_cross_is_exactly_one_blind_window_buy(crossing_tape: tuple[Trade, ...]) -> None:
    events = analyze_shadow_trades(crossing_tape, DAY)

    assert [(event.at.strftime("%H:%M"), event.side) for event in events.pile_b_buy_flips] == [
        ("07:03", "BUY")
    ]
    assert len(events.pile_b_touches) == 1
    assert events.pile_b_buy_flips[0].trail == events.pile_b_touches[0].prior_trail
    assert events.pile_b_touches[0].prior_trail <= events.pile_b_touches[0].close
    assert events.pile_b_touches[0].close <= events.pile_b_touches[0].band_limit


def test_the_same_cross_shifted_ten_minutes_is_pile_a_not_pile_b(
    crossing_tape: tuple[Trade, ...],
) -> None:
    shifted = tuple(replace(trade, ts=trade.ts - timedelta(minutes=10)) for trade in crossing_tape)
    events = analyze_shadow_trades(shifted, DAY)

    assert [(event.at.strftime("%H:%M"), event.side) for event in events.pile_a_flips if event.side == "BUY"] == [
        ("06:53", "BUY")
    ]
    assert events.pile_b_buy_flips == ()
    assert events.pile_b_touches == ()


def test_no_massive_trades_is_unknown_not_zero(crossing_tape: tuple[Trade, ...]) -> None:
    reference = _input(crossing_tape)
    report = evaluate_symbol_day(replace(reference, massive_trades=()))

    assert report.unknown is True
    assert report.status == "UNKNOWN"
    assert report.coverage_minutes == 0
    assert report.pile_a_flips == ()
    assert report.pile_b_buy_flips == ()


def test_one_percent_overlap_perturbation_is_fidelity_low(
    crossing_tape: tuple[Trade, ...],
) -> None:
    massive = build_bars(
        crossing_tape, datetime.combine(DAY, time(4), tzinfo=LOCAL_ET)
    )
    schwab = _schwab_overlap(crossing_tape)
    perturbed = tuple(
        replace(
            bar,
            high=bar.high * 1.01,
            low=bar.low * 1.01,
            close=bar.close * 1.01,
        )
        for bar in schwab
    )

    fidelity = compare_overlap(massive, perturbed, DAY)

    assert fidelity.common == 9
    assert fidelity.agree == 0
    assert fidelity.low is True


def test_probe_disagreement_is_instrument_mismatch(crossing_tape: tuple[Trade, ...]) -> None:
    item = _input(crossing_tape)
    assert item.probe is not None
    report = evaluate_symbol_day(
        replace(item, probe=replace(item.probe, state="short" if item.probe.state == "long" else "long"))
    )

    assert report.instrument_mismatch is True
    assert report.status == "INSTRUMENT_MISMATCH"
    assert "probe=" in report.instrument_detail
    range_summary = summarize([report], DAY, DAY)
    assert range_summary.instrument_valid_symbol_days == 0
    assert range_summary.pile_b_buy_flips == 0


def test_coverage_denominator_is_188_minutes() -> None:
    start = datetime.combine(DAY, time(4), tzinfo=LOCAL_ET)
    trades = tuple(
        Trade(ts=start + timedelta(minutes=index, seconds=1), price=10.0, size=1)
        for index in range(COVERAGE_EXPECTED_MINUTES)
    )
    events = analyze_shadow_trades(trades, DAY)
    reference = _input(
        tuple(
            Trade(
                ts=datetime.combine(DAY, time(6, 50), tzinfo=LOCAL_ET)
                + timedelta(minutes=index, seconds=1),
                price=10.0,
                size=1,
            )
            for index in range(19)
        )
    )
    report = evaluate_symbol_day(replace(reference, massive_trades=trades))

    assert len(events.rows) == COVERAGE_EXPECTED_MINUTES
    assert report.coverage_minutes == 188
    assert report.coverage_expected == 188


def test_probe_parser_reads_the_bar_timestamp_not_the_log_clock() -> None:
    bar_at = datetime.combine(DAY, time(7, 8), tzinfo=LOCAL_ET)
    line = (
        "2026-09-15 12:09:03,100 INFO [V2-ATR-PROBE] sym=MYSZ "
        f"ts_ms={int(bar_at.timestamp() * 1000)} close=2.400000 high=2.500000 low=2.300000 "
        "tr=0.100000 loss=0.200000 trail=2.294100 state=long age=0 touch=false "
        "flip=none vol=100 fired_seg=false"
    )

    readings = parse_probe_lines([line])

    assert readings[(DAY, "MYSZ")].bar_at.strftime("%H:%M") == "07:08"
    assert readings[(DAY, "MYSZ")].trail == pytest.approx(2.2941)


def test_production_query_is_refused_during_the_live_window() -> None:
    with pytest.raises(EvidenceUnknown, match="after-close only"):
        _assert_after_close(datetime(2026, 9, 15, 12, 0, tzinfo=UTC))

    _assert_after_close(datetime(2026, 9, 15, 20, 0, tzinfo=UTC))
