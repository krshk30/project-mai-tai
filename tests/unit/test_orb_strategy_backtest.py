import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_strategy_backtest import (
    BreakSignal,
    QuotePoint,
    TradePoint,
    assumed_entry_ask,
    detect_halts,
    evaluate_attempt,
    fixed_opening_high,
    next_break,
    simulate_symbol,
)


DAY = date(2026, 9, 3)


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 3, hour, minute, second, tzinfo=UTC)


def trade(hour: int, minute: int, second: int, price: str) -> TradePoint:
    return TradePoint(at(hour, minute, second), Decimal(price), 100)


def quote(hour: int, minute: int, second: int, bid: str, ask: str) -> QuotePoint:
    return QuotePoint(at(hour, minute, second), Decimal(bid), Decimal(ask))


def signal(crossed_at: datetime | None = None) -> BreakSignal:
    crossed = crossed_at or at(13, 31, 10)
    return BreakSignal("TEST", Decimal("10"), crossed.replace(second=0), crossed)


def test_opening_high_is_fixed_to_0925_through_0929() -> None:
    trades = [
        trade(13, 24, 59, "20"),
        trade(13, 25, 1, "9.90"),
        trade(13, 27, 1, "10.00"),
        trade(13, 29, 59, "9.95"),
        trade(13, 30, 0, "30"),
    ]
    assert fixed_opening_high(DAY, trades) == Decimal("10.0")


def test_first_break_is_first_post_open_print_strictly_above_level() -> None:
    trades = [
        trade(13, 30, 1, "10.00"),
        trade(13, 31, 5, "9.99"),
        trade(13, 31, 10, "10.01"),
    ]
    found = next_break(
        day=DAY,
        symbol="TEST",
        opening_high=Decimal("10"),
        trades=trades,
        after=None,
    )
    assert found is not None
    assert found.crossed_at == at(13, 31, 10)


def test_reentry_requires_a_new_below_to_above_break_after_stop() -> None:
    trades = [
        trade(13, 31, 11, "10.20"),
        trade(13, 32, 0, "10.10"),
        trade(13, 33, 0, "10.00"),
        trade(13, 34, 0, "10.01"),
    ]
    found = next_break(
        day=DAY,
        symbol="TEST",
        opening_high=Decimal("10"),
        trades=trades,
        after=at(13, 31, 10),
    )
    assert found is not None
    assert found.crossed_at == at(13, 34)


def test_reentry_cannot_repeat_inside_the_stop_bar() -> None:
    trades = [
        trade(13, 31, 11, "9.99"),
        trade(13, 31, 12, "10.01"),
        trade(13, 31, 13, "9.98"),
        trade(13, 31, 14, "10.02"),
        trade(13, 32, 0, "10.03"),
    ]
    found = next_break(
        day=DAY,
        symbol="TEST",
        opening_high=Decimal("10"),
        trades=trades,
        after=at(13, 31, 10),
    )
    assert found is not None
    assert found.crossed_at == at(13, 32)


def test_fill_uses_latest_visible_ask_not_a_future_quote() -> None:
    quotes = [
        quote(13, 31, 9, "10.00", "10.05"),
        quote(13, 31, 11, "10.10", "10.15"),
    ]
    assert assumed_entry_ask(signal(), quotes, []) == Decimal("10.05")


def test_breakeven_stop_fills_at_bid_and_charges_spread() -> None:
    row = evaluate_attempt(
        day=DAY,
        target_pct=Decimal("3"),
        attempt=1,
        signal=signal(),
        entry_price=Decimal("10.05"),
        quotes=[quote(13, 31, 10, "10.00", "10.05")],
        halts=[],
    )
    assert row.exit_rule == "STOP 0%"
    assert row.exit_price == Decimal("10.00")
    assert row.return_pct == Decimal("10.00") / Decimal("10.05") * 100 - 100


def test_target_wins_when_its_bid_arrives_before_stop() -> None:
    row = evaluate_attempt(
        day=DAY,
        target_pct=Decimal("3"),
        attempt=1,
        signal=signal(),
        entry_price=Decimal("10"),
        quotes=[
            quote(13, 31, 10, "10.10", "10.20"),
            quote(13, 31, 20, "10.31", "10.40"),
            quote(13, 31, 30, "9.90", "10.00"),
        ],
        halts=[],
    )
    assert row.exit_rule == "+3%"
    assert row.exit_at == at(13, 31, 20)
    assert row.max_down == Decimal("0")


def test_target_triggers_on_bid_not_ask() -> None:
    row = evaluate_attempt(
        day=DAY,
        target_pct=Decimal("3"),
        attempt=1,
        signal=signal(),
        entry_price=Decimal("10"),
        quotes=[
            quote(13, 31, 10, "10.10", "10.40"),
            quote(13, 31, 20, "9.90", "10.00"),
        ],
        halts=[],
    )
    assert row.exit_rule == "STOP 0%"
    assert row.exit_at == at(13, 31, 20)


def test_max_down_stops_at_target_not_later_window_low() -> None:
    row = evaluate_attempt(
        day=DAY,
        target_pct=Decimal("3"),
        attempt=1,
        signal=signal(),
        entry_price=Decimal("10"),
        quotes=[
            quote(13, 31, 10, "10.10", "10.20"),
            quote(13, 31, 20, "10.30", "10.40"),
            quote(13, 40, 0, "8.00", "8.10"),
        ],
        halts=[],
    )
    assert row.max_down == Decimal("0")
    assert row.max_up == Decimal("3.00")


def test_halted_target_and_stop_quotes_cannot_fire() -> None:
    last_print = at(13, 31, 5)
    reopen = at(13, 36, 5)
    trades = [
        TradePoint(last_print, Decimal("10"), 1),
        TradePoint(reopen, Decimal("10"), 1),
    ]
    quotes = [
        quote(13, 32, 0, "10.50", "10.60"),
        quote(13, 33, 0, "9.00", "9.10"),
        quote(13, 36, 5, "10.30", "10.40"),
    ]
    halts = detect_halts(trades, quotes)
    row = evaluate_attempt(
        day=DAY,
        target_pct=Decimal("3"),
        attempt=1,
        signal=signal(),
        entry_price=Decimal("10"),
        quotes=quotes,
        halts=halts,
    )
    assert len(halts) == 1
    assert row.exit_rule == "+3%"
    assert row.exit_at == reopen
    assert row.max_down == Decimal("0")


def test_simulation_emits_every_stop_and_reentry_attempt() -> None:
    trades = [
        trade(13, 25, 0, "10.00"),
        trade(13, 30, 5, "10.01"),
        trade(13, 31, 0, "9.99"),
        trade(13, 31, 5, "10.01"),
        trade(13, 32, 0, "9.98"),
        trade(13, 32, 5, "10.02"),
    ]
    quotes = [
        quote(13, 30, 4, "10.00", "10.01"),
        quote(13, 30, 6, "10.00", "10.01"),
        quote(13, 31, 4, "10.00", "10.01"),
        quote(13, 31, 6, "10.00", "10.01"),
        quote(13, 32, 4, "10.00", "10.01"),
        quote(13, 32, 6, "10.00", "10.01"),
    ]
    rows = simulate_symbol(
        day=DAY,
        target_pct=Decimal("3"),
        symbol="TEST",
        trades=trades,
        quotes=quotes,
    )
    assert [row.attempt for row in rows] == [1, 2, 3]
    assert [row.exit_rule for row in rows] == ["STOP 0%", "STOP 0%", "STOP 0%"]


def test_runner_is_read_only_and_has_no_trailing_stop() -> None:
    source = (
        Path(__file__).resolve().parents[2].joinpath("scripts/orb_strategy_backtest.py").read_text()
    )
    upper = source.upper()
    assert "BROKER_ADAPTER" not in upper
    assert "TRAILING" not in upper
    assert " INSERT " not in upper
    assert " UPDATE " not in upper
    assert " DELETE " not in upper
