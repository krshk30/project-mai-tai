import sys
from datetime import date, time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_exit_ladder_comparison import (  # noqa: E402
    PopulationEntry,
    QuotePoint,
    at_et,
    close_at,
    compare_entry,
    executable_quotes,
    floor_ladder,
    minute_close_arm,
    post_touch_pullback,
)

DAY = date(2026, 8, 26)
ENTRY = Decimal("10.00")


def q(minute: int, second: int, bid: str, ask: str = "10.01") -> QuotePoint:
    return QuotePoint(at_et(DAY, time(9, minute, second)), Decimal(bid), Decimal(ask))


def ten(bid: str) -> QuotePoint:
    return QuotePoint(close_at(DAY), Decimal(bid), Decimal(bid) + Decimal("0.01"))


def ladder(path, *, floor="3", trail="5", arm="TOUCH"):
    return floor_ladder(
        entry_price=ENTRY,
        path=path,
        close_time=close_at(DAY),
        ten_quote=path[-1] if path and path[-1].at >= close_at(DAY) else None,
        initial_floor=Decimal(floor),
        trail_points=Decimal(trail),
        arm_mode=arm,
    )


def test_pullback_stops_when_first_touch_high_is_exceeded() -> None:
    path = [q(30, 1, "10.50"), q(30, 2, "10.30"), q(30, 3, "10.51"), q(30, 4, "9.00")]

    result = post_touch_pullback(entry_price=ENTRY, path=path, close_time=close_at(DAY))

    assert result.touch_at == path[0].at
    assert result.reference_price == Decimal("10.50")
    assert result.started_at == path[1].at
    assert result.trough_price == Decimal("10.30")
    assert result.giveback_points == Decimal("2.00")
    assert result.recovered_at == path[2].at
    assert result.duration_seconds == Decimal("1.0")


def test_pullback_reports_never_recovered_through_ten() -> None:
    path = [q(30, 1, "10.50"), q(31, 0, "10.20"), q(32, 0, "10.40"), ten("10.10")]

    result = post_touch_pullback(entry_price=ENTRY, path=path, close_time=close_at(DAY))

    assert result.trough_price == Decimal("10.20")
    assert result.recovered_at is None
    assert result.answer == "never exceeded pre-pullback high by 10:00"


def test_floor_below_arm_survives_first_tick_back_through_five() -> None:
    path = [
        q(30, 1, "10.50"),
        q(30, 2, "10.49"),
        q(30, 3, "10.31"),
        q(30, 4, "10.29"),
        ten("10.20"),
    ]

    result = ladder(path, floor="3", trail="5")

    assert result.exit_at == path[3].at
    assert result.exit_price == Decimal("10.29")


def test_four_percent_floor_is_distinct_from_three_percent_floor() -> None:
    path = [q(30, 1, "10.50"), q(30, 2, "10.39"), q(30, 3, "10.29"), ten("10.20")]

    floor_four = ladder(path, floor="4", trail="5")
    floor_three = ladder(path, floor="3", trail="5")

    assert floor_four.exit_at == path[1].at
    assert floor_three.exit_at == path[2].at


def test_three_point_trail_is_distinct_from_five_point_trail() -> None:
    path = [
        q(30, 1, "10.50"),
        q(30, 2, "11.00"),
        q(30, 3, "10.69"),
        q(30, 4, "10.49"),
        ten("10.20"),
    ]

    trail_three = ladder(path, floor="3", trail="3")
    trail_five = ladder(path, floor="3", trail="5")

    assert trail_three.exit_at == path[2].at
    assert trail_five.exit_at == path[3].at


def test_minute_close_does_not_arm_on_intraminute_touch() -> None:
    path = [q(30, 10, "10.60"), q(30, 59, "10.40"), q(31, 0, "10.30"), ten("10.20")]

    assert (
        minute_close_arm(
            entry_price=ENTRY,
            path=path,
            close_time=close_at(DAY),
            halts=[],
        )
        is None
    )
    result = ladder(path, arm="MINUTE_CLOSE")
    assert result.outcome == "10:00-NOT-ARMED"


def test_minute_close_arms_at_next_boundary_not_last_quote_timestamp() -> None:
    path = [q(30, 59, "10.60"), q(31, 0, "10.20"), ten("10.10")]

    arm = minute_close_arm(
        entry_price=ENTRY,
        path=path,
        close_time=close_at(DAY),
        halts=[],
    )
    result = ladder(path, floor="3", arm="MINUTE_CLOSE")

    assert arm == (at_et(DAY, time(9, 31)), Decimal("10.60"))
    assert result.armed_at == at_et(DAY, time(9, 31))
    assert result.exit_at == path[1].at


def test_halted_quotes_cannot_arm_update_high_or_trigger() -> None:
    from project_mai_tai.market_halts import HaltWindow

    quotes = [q(30, 0, "10.40"), q(31, 0, "12.00"), q(36, 0, "10.60"), ten("10.30")]
    halt = HaltWindow(quotes[0].at, quotes[2].at, 2)
    path = executable_quotes(quotes, [halt], at_et(DAY, time(9, 30)))
    result = ladder(path, floor="3", trail="3")

    assert quotes[1] not in path
    assert result.armed_at == quotes[2].at
    assert result.high_pct == Decimal("6.00")


def test_minute_overlapping_halt_cannot_supply_an_arm_close() -> None:
    from project_mai_tai.market_halts import HaltWindow

    quotes = [q(30, 0, "10.60"), q(31, 0, "10.40"), q(36, 0, "10.70"), ten("10.30")]
    halt = HaltWindow(quotes[0].at, quotes[2].at, 2)
    path = executable_quotes(quotes, [halt], at_et(DAY, time(9, 30)))

    arm = minute_close_arm(
        entry_price=ENTRY,
        path=path,
        close_time=close_at(DAY),
        halts=[halt],
    )

    assert arm == (at_et(DAY, time(9, 37)), Decimal("10.70"))


def test_missing_ten_quote_makes_population_row_ungradable() -> None:
    entry = PopulationEntry(DAY, "VIVK", time(9, 30), ENTRY)
    quotes = [q(30, 1, "10.50"), q(31, 0, "10.40")]

    row = compare_entry(entry, [], quotes)

    assert row.gradable is False
    assert "incomplete" in row.gradability_reason


def test_flat_target_and_all_eight_ladders_share_one_population_row() -> None:
    entry = PopulationEntry(DAY, "DAIC", time(9, 30), ENTRY)
    quotes = [q(30, 1, "10.50"), q(30, 59, "10.60"), q(31, 0, "10.30"), ten("10.20")]

    row = compare_entry(entry, [], quotes)

    assert len(row.ladders) == 8
    assert row.flat_five.exit_at == quotes[0].at
    assert row.gradable is True
