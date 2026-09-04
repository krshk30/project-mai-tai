import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from project_mai_tai.market_halts import HaltWindow

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_raw_price_walk import (  # noqa: E402
    AtrSnapshot,
    MinuteRow,
    QuotePoint,
    SymbolWalk,
    TradePoint,
    at_et,
    first_break,
    minute_walk,
    opening_high,
    render,
    spread_percent,
)

DAY = date(2026, 8, 26)


def trade(hour: int, minute: int, second: int, price: str) -> TradePoint:
    return TradePoint(at_et(DAY, hour, minute) + timedelta(seconds=second), Decimal(price), 1)


def quote(hour: int, minute: int, second: int, bid: str, ask: str) -> QuotePoint:
    return QuotePoint(
        at_et(DAY, hour, minute) + timedelta(seconds=second),
        Decimal(bid),
        Decimal(ask),
    )


def test_opening_high_is_fixed_to_0925_through_0929() -> None:
    rows = [trade(9, 25, 1, "2.00"), trade(9, 29, 59, "2.10"), trade(9, 30, 0, "9.00")]

    assert opening_high(DAY, rows) == Decimal("2.10")


def test_first_break_uses_first_print_strictly_above_level() -> None:
    rows = [
        trade(9, 29, 59, "2.20"),
        trade(9, 30, 1, "2.10"),
        trade(9, 30, 2, "2.11"),
        trade(9, 30, 3, "2.50"),
    ]

    assert first_break(DAY, Decimal("2.10"), rows) == rows[2].at


def test_minute_walk_keeps_every_minute_through_1000() -> None:
    break_at = trade(9, 58, 37, "2.11").at

    rows = minute_walk(
        day=DAY,
        level=Decimal("2.10"),
        break_at=break_at,
        trades=[],
        quotes=[],
        halts=[],
    )

    assert [row.minute for row in rows] == [at_et(DAY, 9, 58), at_et(DAY, 9, 59), at_et(DAY, 10, 0)]


def test_minute_walk_uses_final_trade_and_quote_without_forward_fill() -> None:
    rows = minute_walk(
        day=DAY,
        level=Decimal("2.00"),
        break_at=trade(9, 59, 1, "2.01").at,
        trades=[trade(9, 59, 2, "2.02"), trade(9, 59, 58, "2.04")],
        quotes=[quote(9, 59, 3, "2.01", "2.03"), quote(9, 59, 59, "2.03", "2.05")],
        halts=[],
    )

    assert rows[0].price == Decimal("2.04")
    assert rows[0].bid == Decimal("2.03")
    assert rows[0].ask == Decimal("2.05")
    assert rows[1].price is None
    assert rows[1].bid is None
    assert rows[1].ask is None


def test_minute_walk_flags_halt_without_removing_the_minute() -> None:
    start = at_et(DAY, 9, 31)
    halt = HaltWindow(
        last_print_at=start + timedelta(seconds=10),
        reopen_print_at=start + timedelta(minutes=5, seconds=10),
        quote_updates=3,
    )

    rows = minute_walk(
        day=DAY,
        level=Decimal("2.00"),
        break_at=start,
        trades=[],
        quotes=[],
        halts=[halt],
    )

    assert len(rows) == 30
    assert [row.minute for row in rows if row.halted] == [
        at_et(DAY, 9, 31),
        at_et(DAY, 9, 32),
        at_et(DAY, 9, 33),
        at_et(DAY, 9, 34),
        at_et(DAY, 9, 35),
        at_et(DAY, 9, 36),
    ]


def test_spread_uses_quote_midpoint() -> None:
    assert spread_percent(Decimal("1.00"), Decimal("1.10")) == Decimal("0.10") / Decimal("1.05") * 100


def test_render_names_missing_trade_and_quote_and_halt() -> None:
    walk = SymbolWalk(
        symbol="DAIC",
        opening_high=Decimal("6.35"),
        break_at=datetime(2026, 8, 26, 13, 30, 4, tzinfo=UTC),
        atr=AtrSnapshot("SHORT", Decimal("6.40"), "derived"),
        rows=(
            MinuteRow(
                minute=at_et(DAY, 9, 30),
                price=None,
                level_pct=None,
                bid=None,
                ask=None,
                spread_pct=None,
                halted=True,
            ),
        ),
    )

    output = render([walk])

    assert "SIMULATED | NOT SIZE-QUALIFIED" in output
    assert "09:30 HALT" in output
    assert "NO TRADE" in output
    assert "NO QUOTE" in output
    assert "ATR at break-bar close: SHORT @ $6.4000" in output


def test_render_contains_no_trade_rule_results() -> None:
    output = render([]).lower()

    for forbidden in ("target", "stop", "return", "attempt", "verdict"):
        assert forbidden not in output
