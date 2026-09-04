import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_strategy_backtest import (
    BreakSignal,
    DayResult,
    QuotePoint,
    TradePoint,
    assumed_entry_ask,
    detect_halts,
    first_break,
    movement_after_entry,
    render,
)


DAY = date(2026, 9, 3)


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 3, hour, minute, second, tzinfo=UTC)


def trade(hour: int, minute: int, second: int, price: str) -> TradePoint:
    return TradePoint(at(hour, minute, second), Decimal(price), 100)


def quote(hour: int, minute: int, second: int, bid: str, ask: str) -> QuotePoint:
    return QuotePoint(at(hour, minute, second), Decimal(bid), Decimal(ask))


def signal() -> BreakSignal:
    return BreakSignal("TEST", Decimal("10"), at(13, 31), at(13, 31, 10))


def test_fixed_opening_high_uses_0925_through_0929_and_first_break_bar() -> None:
    trades = [
        trade(13, 25, 5, "9.90"),
        trade(13, 26, 5, "10.00"),
        trade(13, 29, 50, "9.95"),
        trade(13, 30, 10, "10.00"),
        trade(13, 31, 2, "9.99"),
        trade(13, 31, 10, "10.01"),
        trade(13, 32, 0, "10.20"),
    ]

    found = first_break(DAY, "TEST", trades)

    assert found is not None
    assert found.opening_high == Decimal("10.0")
    assert found.bar_at == at(13, 31)
    assert found.crossed_at == at(13, 31, 10)


def test_equal_high_is_not_a_break() -> None:
    trades = [
        trade(13, 25, 5, "10.00"),
        trade(13, 30, 5, "10.00"),
        trade(13, 31, 0, "10.00"),
    ]
    assert first_break(DAY, "TEST", trades) is None


def test_fill_uses_latest_visible_ask_and_never_looks_forward() -> None:
    quotes = [
        quote(13, 31, 9, "10.00", "10.05"),
        quote(13, 31, 11, "10.10", "10.15"),
    ]
    assert assumed_entry_ask(signal(), quotes, []) == Decimal("10.05")


def test_stale_or_nonpositive_latest_ask_is_unanswerable() -> None:
    stale = [quote(13, 31, 7, "10.00", "10.05")]
    zero_latest = [
        quote(13, 31, 9, "10.00", "10.05"),
        quote(13, 31, 10, "10.00", "0"),
    ]
    assert assumed_entry_ask(signal(), stale, []) is None
    assert assumed_entry_ask(signal(), zero_latest, []) is None


def test_extrema_use_executable_bid_not_ask() -> None:
    quotes = [
        quote(13, 31, 10, "10.20", "10.30"),
        quote(13, 32, 0, "9.80", "19.00"),
        quote(14, 0, 0, "30.00", "31.00"),
    ]
    row = movement_after_entry(
        day=DAY,
        symbol="TEST",
        signal=signal(),
        entry_price=Decimal("10"),
        quotes=quotes,
        halts=[],
    )
    assert row.high_bid == Decimal("10.20")
    assert row.high_pct == Decimal("2.00")
    assert row.low_bid == Decimal("9.80")
    assert row.low_pct == Decimal("-2.00")
    assert row.reached_five is False


def test_halted_quotes_are_excluded_from_extrema_and_plus_five() -> None:
    last_print = at(13, 31, 5)
    reopen = at(13, 36, 5)
    trades = [
        TradePoint(last_print, Decimal("10"), 1),
        TradePoint(reopen, Decimal("10"), 1),
    ]
    quotes = [
        quote(13, 32, 0, "12", "12.1"),
        quote(13, 33, 0, "8", "8.1"),
        quote(13, 36, 5, "10.1", "10.2"),
    ]
    halts = detect_halts(trades, quotes)
    row = movement_after_entry(
        day=DAY,
        symbol="TEST",
        signal=signal(),
        entry_price=Decimal("10"),
        quotes=quotes,
        halts=halts,
    )
    assert len(halts) == 1
    assert row.high_bid == Decimal("10.1")
    assert row.low_bid == Decimal("10.1")
    assert row.reached_five is False


def test_plus_five_is_inclusive() -> None:
    row = movement_after_entry(
        day=DAY,
        symbol="TEST",
        signal=signal(),
        entry_price=Decimal("10"),
        quotes=[quote(13, 31, 10, "10.50", "10.60")],
        halts=[],
    )
    assert row.reached_five is True


def test_missing_fill_quote_is_reported_unanswerable() -> None:
    row = movement_after_entry(
        day=DAY,
        symbol="TEST",
        signal=signal(),
        entry_price=None,
        quotes=[],
        halts=[],
    )
    assert row.reached_five is None
    assert row.entry_price is None
    assert "UNANSWERABLE" in row.assumption


def test_report_has_no_exit_or_verdict_columns() -> None:
    row = movement_after_entry(
        day=DAY,
        symbol="TEST",
        signal=signal(),
        entry_price=Decimal("10"),
        quotes=[quote(13, 31, 10, "10.50", "10.60")],
        halts=[],
    )
    report = render([DayResult(DAY, 3, 1, [row])])
    header = next(line for line in report.splitlines() if line.startswith("| day"))
    assert "exit" not in header.lower()
    assert "verdict" not in report.lower()
    assert "1/3 watched stocks broke" in report


def test_incomplete_session_is_named_not_silently_dropped() -> None:
    result = DayResult(DAY, 0, 0, unavailable_reason="09:30-10:00 window not complete")
    report = render([result])
    assert "2026-09-03: NOT REACHABLE" in report


def test_runner_is_read_only() -> None:
    source = (
        Path(__file__).resolve().parents[2].joinpath("scripts/orb_strategy_backtest.py").read_text()
    )
    upper = source.upper()
    assert "BROKER_ADAPTER" not in upper
    assert " INSERT " not in upper
    assert " UPDATE " not in upper
    assert " DELETE " not in upper
