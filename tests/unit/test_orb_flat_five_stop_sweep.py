import sys
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_exit_ladder_comparison import PopulationEntry, QuotePoint, at_et  # noqa: E402
from orb_flat_five_stop_sweep import AtrTrigger, evaluate_stop  # noqa: E402
from project_mai_tai.market_halts import HaltWindow  # noqa: E402

DAY = date(2026, 8, 26)
ENTRY = Decimal("10")
NAME = PopulationEntry(DAY, "TEST", time(9, 30), ENTRY)


def quote(second: int, bid: str, minute: int = 30) -> QuotePoint:
    return QuotePoint(at_et(DAY, time(9, minute, second)), Decimal(bid), Decimal(bid))


def test_timestamped_stop_before_target_wins() -> None:
    result = evaluate_stop(
        entry_price=ENTRY,
        stop_pct=Decimal("2"),
        path=[quote(1, "9.79"), quote(2, "10.51")],
        atr_triggers=[],
        halts=[],
    )

    assert result.exit_rule == "STOP"
    assert result.exit_at == quote(1, "9.79").at
    assert result.recovered_after_stop is True


def test_timestamped_target_before_stop_wins() -> None:
    result = evaluate_stop(
        entry_price=ENTRY,
        stop_pct=Decimal("2"),
        path=[quote(1, "10.51"), quote(2, "9.79")],
        atr_triggers=[],
        halts=[],
    )

    assert result.exit_rule == "+5%"
    assert result.stop_at == quote(2, "9.79").at


def test_atr_decision_can_beat_a_later_stop() -> None:
    atr = AtrTrigger(
        at_et(DAY, time(9, 31)),
        at_et(DAY, time(9, 32)),
        quote(0, "9.85", 32).at,
        Decimal("9.85"),
    )
    result = evaluate_stop(
        entry_price=ENTRY,
        stop_pct=Decimal("4"),
        path=[quote(0, "9.85", 32), quote(1, "9.50", 32)],
        atr_triggers=[atr],
        halts=[],
    )

    assert result.exit_rule == "ATR"
    assert result.atr_bar_minute == at_et(DAY, time(9, 31))


def test_halted_stop_quote_is_excluded_and_reopen_gap_is_charged() -> None:
    halted = quote(0, "9.90", 31)
    reopen = quote(0, "9.20", 36)
    halt = HaltWindow(quote(59, "10", 30).at, reopen.at, 5)
    result = evaluate_stop(
        entry_price=ENTRY,
        stop_pct=Decimal("5"),
        path=[reopen],
        atr_triggers=[],
        halts=[halt],
    )

    assert halted not in [reopen]
    assert result.exit_rule == "STOP"
    assert result.exit_price == Decimal("9.20")
    assert result.stop_on_reopen is True


def test_ten_oclock_is_not_an_exit() -> None:
    target_at_ten = QuotePoint(at_et(DAY, time(10, 0)), Decimal("10.51"), Decimal("10.51"))
    result = evaluate_stop(
        entry_price=ENTRY,
        stop_pct=Decimal("8"),
        path=[quote(0, "10.10", 31), target_at_ten],
        atr_triggers=[],
        halts=[],
    )

    assert result.exit_rule == "+5%"
    assert result.exit_at == target_at_ten.at


def test_cross_feed_timestamp_tie_is_unanswerable() -> None:
    event_at = quote(0, "9.79", 32).at
    atr = AtrTrigger(
        event_at - timedelta(minutes=1),
        event_at,
        event_at,
        Decimal("9.79"),
    )
    result = evaluate_stop(
        entry_price=ENTRY,
        stop_pct=Decimal("2"),
        path=[quote(0, "9.79", 32)],
        atr_triggers=[atr],
        halts=[],
    )

    assert result.exit_rule == "UNANSWERABLE"
    assert "cannot be ordered" in result.answer
