import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_strategy_backtest import (
    BreakSignal,
    EntryQuote,
    QuotePoint,
    TradePoint,
    assumed_entry_ask,
    break_attempts,
    detect_halts,
    evaluate_attempt,
    fixed_opening_high,
    load_attempts_csv,
)


DAY = date(2026, 9, 3)


def at(hour: int, minute: int, second: int = 0, micros: int = 0) -> datetime:
    return datetime(2026, 9, 3, hour, minute, second, micros, tzinfo=UTC)


def trade(hour: int, minute: int, second: int, price: str) -> TradePoint:
    return TradePoint(at(hour, minute, second), Decimal(price), 100)


def quote(hour: int, minute: int, second: int, bid: str, ask: str) -> QuotePoint:
    return QuotePoint(at(hour, minute, second), Decimal(bid), Decimal(ask))


def signal(crossed_at: datetime | None = None) -> BreakSignal:
    crossed = crossed_at or at(13, 31, 10)
    return BreakSignal("TEST", Decimal("10"), 1, crossed.replace(second=0), crossed)


def test_opening_high_is_fixed_to_0925_through_0929() -> None:
    trades = [
        trade(13, 24, 59, "20"),
        trade(13, 25, 1, "9.90"),
        trade(13, 27, 1, "10.00"),
        trade(13, 29, 59, "9.95"),
        trade(13, 30, 0, "30"),
    ]
    assert fixed_opening_high(DAY, trades) == Decimal("10.0")


def test_break_attempts_emit_every_later_bar_rebreak() -> None:
    trades = [
        trade(13, 30, 5, "10.01"),
        trade(13, 30, 6, "9.99"),
        trade(13, 30, 7, "10.02"),
        trade(13, 31, 0, "10.03"),
        trade(13, 31, 5, "9.98"),
        trade(13, 32, 0, "10.04"),
    ]
    found = break_attempts(
        day=DAY,
        symbol="TEST",
        opening_high=Decimal("10"),
        trades=trades,
    )
    assert [item.crossed_at for item in found] == [at(13, 30, 5), at(13, 31), at(13, 32)]
    assert [item.attempt for item in found] == [1, 2, 3]


def test_frozen_population_loader_preserves_every_attempt(tmp_path: Path) -> None:
    path = tmp_path / "attempts.csv"
    path.write_text(
        "day,symbol,attempt,opening_high,entry_time_et\n"
        "2026-09-03,CHPT,1,7.73,09:30:45\n"
        "2026-09-03,CHPT,2,7.73,09:35:12\n"
    )
    attempts = load_attempts_csv(path)

    assert sum(len(rows) for rows in attempts.values()) == 2
    assert [row.attempt for row in attempts[(DAY, "CHPT")]] == [1, 2]


def test_fill_uses_first_post_break_ask_not_a_stale_pre_break_quote() -> None:
    quotes = [
        quote(13, 31, 9, "9.80", "9.90"),
        quote(13, 31, 10, "10.00", "10.05"),
    ]
    result = assumed_entry_ask(signal(), quotes, [])
    assert result.price == Decimal("10.05")
    assert result.at == at(13, 31, 10)


def test_fill_can_wait_within_two_seconds_for_ask_to_enter_valid_band() -> None:
    quotes = [
        quote(13, 31, 10, "9.80", "9.90"),
        quote(13, 31, 11, "10.00", "10.05"),
    ]
    result = assumed_entry_ask(signal(), quotes, [])
    assert result.price == Decimal("10.05")
    assert result.at == at(13, 31, 11)


def test_fill_below_trigger_is_unanswerable() -> None:
    result = assumed_entry_ask(signal(), [quote(13, 31, 10, "9.80", "9.90")], [])
    assert result.price is None
    assert "below" in result.reason


def test_fill_past_deployed_one_point_five_percent_cap_is_unanswerable() -> None:
    result = assumed_entry_ask(signal(), [quote(13, 31, 10, "10.19", "10.20")], [])
    assert result.price is None
    assert "+1.5% fill band" in result.reason


def test_fill_at_deployed_cap_is_accepted() -> None:
    result = assumed_entry_ask(signal(), [quote(13, 31, 10, "10.14", "10.15")], [])
    assert result.price == Decimal("10.15")


def test_no_stop_allows_drawdown_then_later_target() -> None:
    row = evaluate_attempt(
        day=DAY,
        target_pct=Decimal("3"),
        signal=signal(),
        entry=EntryQuote(Decimal("10"), at(13, 31, 10)),
        quotes=[
            quote(13, 31, 10, "9.80", "9.90"),
            quote(13, 32, 0, "9.50", "9.60"),
            quote(13, 34, 0, "10.30", "10.40"),
        ],
        halts=[],
    )
    assert row.reached is True
    assert row.target_at == at(13, 34)
    assert row.max_down == Decimal("-5.00")
    assert row.max_down_at == at(13, 32)


def test_drawdown_stops_at_first_target_not_later_low() -> None:
    row = evaluate_attempt(
        day=DAY,
        target_pct=Decimal("3"),
        signal=signal(),
        entry=EntryQuote(Decimal("10"), at(13, 31, 10)),
        quotes=[
            quote(13, 31, 10, "9.90", "10.00"),
            quote(13, 32, 0, "10.30", "10.40"),
            quote(13, 40, 0, "8.00", "8.10"),
        ],
        halts=[],
    )
    assert row.reached is True
    assert row.max_down == Decimal("-1.00")


def test_never_reached_uses_drawdown_through_1000() -> None:
    row = evaluate_attempt(
        day=DAY,
        target_pct=Decimal("3"),
        signal=signal(),
        entry=EntryQuote(Decimal("10"), at(13, 31, 10)),
        quotes=[
            quote(13, 31, 10, "9.90", "10.00"),
            quote(13, 59, 59, "8.00", "8.10"),
            quote(14, 0, 0, "7.00", "7.10"),
        ],
        halts=[],
    )
    assert row.reached is False
    assert row.max_down == Decimal("-20.0")
    assert row.max_down_at == at(13, 59, 59)


def test_target_uses_executable_bid_not_ask() -> None:
    row = evaluate_attempt(
        day=DAY,
        target_pct=Decimal("3"),
        signal=signal(),
        entry=EntryQuote(Decimal("10"), at(13, 31, 10)),
        quotes=[
            quote(13, 31, 10, "10.10", "10.40"),
            quote(13, 59, 0, "10.20", "10.50"),
        ],
        halts=[],
    )
    assert row.reached is False
    assert row.target_at is None


def test_halted_quotes_are_excluded_from_target_and_drawdown() -> None:
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
        signal=signal(),
        entry=EntryQuote(Decimal("10"), at(13, 31, 10)),
        quotes=quotes,
        halts=halts,
    )
    assert len(halts) == 1
    assert row.reached is True
    assert row.target_at == reopen
    assert row.max_down == Decimal("0")


def test_runner_is_read_only_and_has_no_exit_or_stop_simulation() -> None:
    source = (
        Path(__file__).resolve().parents[2].joinpath("scripts/orb_strategy_backtest.py").read_text()
    )
    upper = source.upper()
    assert "BROKER_ADAPTER" not in upper
    assert "TRAILING" not in upper
    assert "STOP 0%" not in upper
    assert "EXIT_RULE" not in upper
    assert " INSERT " not in upper
    assert " UPDATE " not in upper
    assert " DELETE " not in upper
