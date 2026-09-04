import sys
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_exit_ladder_comparison import (  # noqa: E402
    PopulationEntry,
    QuotePoint,
    at_et,
    close_at,
    compare_entry,
    flat_target,
    floor_ladder,
    minute_path,
)

DAY = date(2026, 8, 26)
ENTRY = Decimal("10.00")


def q(minute: int, second: int, bid: str, ask: str = "10.01") -> QuotePoint:
    return QuotePoint(at_et(DAY, time(9, minute, second)), Decimal(bid), Decimal(ask))


def ten(bid: str) -> QuotePoint:
    return QuotePoint(close_at(DAY), Decimal(bid), Decimal(bid) + Decimal("0.01"))


def test_floor_does_not_exist_before_five_percent() -> None:
    path = [q(31, 0, "9.00"), q(32, 0, "10.49"), ten("10.20")]

    result = floor_ladder(
        entry_price=ENTRY,
        path=path,
        close_time=close_at(DAY),
        ten_quote=path[-1],
        trail_points=Decimal("5"),
    )

    assert result.rule == "10:00"
    assert result.floor_hit_before_five is False


def test_flat_target_takes_first_executable_bid_at_five_percent() -> None:
    path = [q(31, 0, "10.49"), q(31, 5, "10.55"), q(31, 10, "11.00"), ten("9.00")]

    result = flat_target(
        entry_price=ENTRY,
        path=path,
        close_time=close_at(DAY),
        ten_quote=path[-1],
    )

    assert result.rule == "+5%"
    assert result.exit_at == path[1].at
    assert result.exit_price == Decimal("10.55")


def test_five_point_ladder_arms_then_trails_high() -> None:
    path = [
        q(31, 0, "10.50"),
        q(32, 0, "11.60"),
        q(33, 0, "11.20"),
        q(34, 0, "11.09"),
        ten("12.00"),
    ]

    result = floor_ladder(
        entry_price=ENTRY,
        path=path,
        close_time=close_at(DAY),
        ten_quote=path[-1],
        trail_points=Decimal("5"),
    )

    assert result.rule == "FLOOR-5PP"
    assert result.exit_price == Decimal("11.09")
    assert result.return_pct == Decimal("10.900")
    assert result.high_pct == Decimal("16.00")
    assert result.giveback_points == Decimal("5.100")


def test_three_point_ladder_is_tighter_on_the_same_quotes() -> None:
    path = [q(31, 0, "10.50"), q(32, 0, "11.00"), q(33, 0, "10.69"), ten("12.00")]

    result = floor_ladder(
        entry_price=ENTRY,
        path=path,
        close_time=close_at(DAY),
        ten_quote=path[-1],
        trail_points=Decimal("3"),
    )

    assert result.rule == "FLOOR-3PP"
    assert result.exit_at == path[2].at


def test_activation_quote_does_not_immediately_hit_its_new_floor() -> None:
    path = [q(31, 0, "10.50"), q(32, 0, "10.60"), q(33, 0, "10.49"), ten("10.00")]

    result = floor_ladder(
        entry_price=ENTRY,
        path=path,
        close_time=close_at(DAY),
        ten_quote=path[-1],
        trail_points=Decimal("5"),
    )

    assert result.exit_at == path[2].at


def test_halted_quotes_cannot_trigger_or_price_an_exit() -> None:
    entry = PopulationEntry(DAY, "DAIC", time(9, 30), ENTRY)
    quotes = [q(30, 0, "10.60"), q(31, 0, "9.00"), q(36, 0, "10.40"), ten("10.30")]
    from project_mai_tai.market_halts import HaltWindow

    halt = HaltWindow(quotes[0].at, quotes[2].at, 2)
    from orb_exit_ladder_comparison import executable_quotes

    path = executable_quotes(quotes, [halt], at_et(DAY, time(9, 30)))
    result = floor_ladder(
        entry_price=entry.entry_price,
        path=path,
        close_time=close_at(DAY),
        ten_quote=quotes[-1],
        trail_points=Decimal("5"),
    )

    assert quotes[1] not in path
    assert result.rule == "FLOOR-5PP"
    assert result.exit_price == Decimal("10.40")


def test_hard_close_uses_first_executable_quote_at_or_after_ten() -> None:
    first = QuotePoint(close_at(DAY) + timedelta(seconds=2), Decimal("9.80"), Decimal("9.81"))
    path = [q(31, 0, "10.20"), first]

    result = floor_ladder(
        entry_price=ENTRY,
        path=path,
        close_time=close_at(DAY),
        ten_quote=first,
        trail_points=Decimal("5"),
    )

    assert result.rule == "10:00"
    assert result.exit_at == first.at
    assert result.exit_price == Decimal("9.80")


def test_minute_table_uses_bid_and_keeps_missing_minutes() -> None:
    rows = minute_path(
        entry_at=at_et(DAY, time(9, 58, 37)),
        entry_price=ENTRY,
        quotes=[q(58, 59, "10.20", "99.00")],
        halts=[],
    )

    assert len(rows) == 3
    assert rows[0].bid == Decimal("10.20")
    assert rows[1].bid is None
    assert rows[2].bid is None


def test_compare_runs_both_ladders_and_flat_on_one_entry() -> None:
    entry = PopulationEntry(DAY, "DAIC", time(9, 30), ENTRY)
    quotes = [q(30, 1, "10.50"), q(31, 0, "11.00"), q(32, 0, "10.49"), ten("10.00")]

    row = compare_entry(entry, [], quotes)

    assert row.ladder_five.rule == "FLOOR-5PP"
    assert row.ladder_three.rule == "FLOOR-3PP"
    assert row.flat_five.rule == "+5%"
