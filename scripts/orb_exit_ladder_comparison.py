#!/usr/bin/env python3
"""Compare fixed ORB floor ladders against a flat five-percent target."""

from __future__ import annotations

import argparse
import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.market_halts import (
    HALT_MIN_PRINT_GAP,
    HaltWindow,
    confirmed_halt_window,
    timestamp_is_halted,
)
from project_mai_tai.settings import get_settings

EASTERN = ZoneInfo("America/New_York")
DISCLOSURE = "SIMULATED | NO REALISED CONTROL | NOT SIZE-QUALIFIED"
TARGET_PCT = Decimal("5")


@dataclass(frozen=True)
class PopulationEntry:
    day: date
    symbol: str
    entry_time: time
    entry_price: Decimal | None
    group: str = "PRIMARY"
    source: str = "FROZEN FROM OPERATOR-APPROVED ROW"


PRIMARY_POPULATION = (
    PopulationEntry(date(2026, 8, 25), "DAIC", time(9, 30, 21), Decimal("3.66")),
    PopulationEntry(date(2026, 8, 26), "DAIC", time(9, 30, 4), Decimal("6.11")),
    PopulationEntry(date(2026, 8, 26), "CRE", time(9, 30, 42), Decimal("6.37")),
    PopulationEntry(date(2026, 8, 28), "QNRX", time(9, 30, 10), Decimal("7.25")),
    PopulationEntry(date(2026, 8, 31), "AEHL", time(9, 30, 13), Decimal("6.76")),
    PopulationEntry(date(2026, 8, 31), "YDDL", time(9, 34, 26), Decimal("2.49")),
    PopulationEntry(date(2026, 8, 31), "RDHL", time(9, 30, 53), Decimal("1.39")),
    PopulationEntry(date(2026, 9, 1), "SSM", time(9, 37, 41), Decimal("3.87")),
    PopulationEntry(date(2026, 9, 2), "VIVK", time(9, 30, 29), Decimal("1.11")),
)

SEPARATE_POPULATION = (
    PopulationEntry(
        date(2026, 8, 27),
        "PPCB",
        time(9, 32),
        None,
        group="PPCB_SEPARATE",
        source="ASSUMED FIRST POSITIVE ASK AT/AFTER OPERATOR-SUPPLIED 09:32 ATR FLIP",
    ),
)


@dataclass(frozen=True)
class TradePoint:
    at: datetime
    price: Decimal
    size: int
    exchange: str = ""
    conditions: str = ""


@dataclass(frozen=True)
class QuotePoint:
    at: datetime
    bid: Decimal
    ask: Decimal


@dataclass(frozen=True)
class MinutePoint:
    minute: datetime
    bid: Decimal | None
    return_pct: Decimal | None
    halted: bool


@dataclass(frozen=True)
class ExitResult:
    rule: str
    exit_at: datetime | None
    exit_price: Decimal | None
    return_pct: Decimal | None
    high_bid: Decimal | None
    high_pct: Decimal | None
    giveback_points: Decimal | None
    floor_hit_before_five: bool
    answer: str = ""


@dataclass(frozen=True)
class ComparisonRow:
    entry: PopulationEntry
    entry_at: datetime
    entry_price: Decimal | None
    entry_source: str
    minutes: tuple[MinutePoint, ...]
    ladder_five: ExitResult
    ladder_three: ExitResult
    flat_five: ExitResult
    ten_at: datetime | None
    ten_price: Decimal | None


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def at_et(day: date, value: time) -> datetime:
    return datetime.combine(day, value, EASTERN).astimezone(UTC)


def close_at(day: date) -> datetime:
    return at_et(day, time(10, 0))


def percent(value: Decimal | None, basis: Decimal | None) -> Decimal | None:
    if value is None or basis is None or basis <= 0:
        return None
    return (value / basis - Decimal("1")) * Decimal("100")


def detect_halts(
    trades: Sequence[TradePoint], quotes: Sequence[QuotePoint]
) -> list[HaltWindow]:
    quote_times = [quote.at for quote in quotes]
    windows: list[HaltWindow] = []
    for previous, current in zip(trades, trades[1:], strict=False):
        if current.at - previous.at < HALT_MIN_PRINT_GAP:
            continue
        quote_updates = bisect_left(quote_times, current.at) - bisect_right(
            quote_times, previous.at
        )
        window = confirmed_halt_window(
            last_print_at=previous.at,
            reopen_print_at=current.at,
            quote_updates=quote_updates,
        )
        if window is not None:
            windows.append(window)
    return windows


def minute_overlaps_halt(minute: datetime, halts: Sequence[HaltWindow]) -> bool:
    end = minute + timedelta(minutes=1)
    return any(
        halt.last_print_at < end and halt.reopen_print_at > minute for halt in halts
    )


def executable_quotes(
    quotes: Sequence[QuotePoint], halts: Sequence[HaltWindow], start: datetime
) -> list[QuotePoint]:
    return [
        quote
        for quote in quotes
        if quote.at >= start
        and quote.bid > 0
        and not timestamp_is_halted(quote.at, list(halts))
    ]


def resolve_entry_price(
    entry: PopulationEntry,
    entry_at: datetime,
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> tuple[Decimal | None, str]:
    if entry.entry_price is not None:
        return entry.entry_price, entry.source
    quote = next(
        (
            item
            for item in quotes
            if item.at >= entry_at
            and item.ask > 0
            and not timestamp_is_halted(item.at, list(halts))
        ),
        None,
    )
    if quote is None:
        return None, f"UNANSWERABLE: {entry.source}; no executable ask"
    return quote.ask, f"{entry.source} ({quote.at.astimezone(EASTERN):%H:%M:%S} ET)"


def price_at_ten(
    quotes: Sequence[QuotePoint], halts: Sequence[HaltWindow], day: date
) -> QuotePoint | None:
    cutoff = close_at(day)
    return next(
        (
            quote
            for quote in quotes
            if quote.at >= cutoff
            and quote.bid > 0
            and not timestamp_is_halted(quote.at, list(halts))
        ),
        None,
    )


def flat_target(
    *,
    entry_price: Decimal | None,
    path: Sequence[QuotePoint],
    close_time: datetime,
    ten_quote: QuotePoint | None,
) -> ExitResult:
    if entry_price is None:
        return unanswered("UNANSWERABLE: entry price unavailable")
    target = entry_price * Decimal("1.05")
    quote = next(
        (item for item in path if item.at < close_time and item.bid >= target), None
    )
    if quote is not None:
        ret = percent(quote.bid, entry_price)
        return ExitResult("+5%", quote.at, quote.bid, ret, quote.bid, ret, Decimal("0"), False)
    if ten_quote is None:
        return unanswered("UNANSWERABLE: no executable 10:00 quote")
    eligible = [item for item in path if item.at <= ten_quote.at]
    high = max((item.bid for item in eligible), default=None)
    return ExitResult(
        "10:00",
        ten_quote.at,
        ten_quote.bid,
        percent(ten_quote.bid, entry_price),
        high,
        percent(high, entry_price),
        None,
        False,
    )


def floor_ladder(
    *,
    entry_price: Decimal | None,
    path: Sequence[QuotePoint],
    close_time: datetime,
    ten_quote: QuotePoint | None,
    trail_points: Decimal,
) -> ExitResult:
    if entry_price is None:
        return unanswered("UNANSWERABLE: entry price unavailable")
    high_bid: Decimal | None = None
    armed = False
    for quote in path:
        if quote.at >= close_time:
            break
        high_bid = quote.bid if high_bid is None else max(high_bid, quote.bid)
        high_pct = percent(high_bid, entry_price)
        current_pct = percent(quote.bid, entry_price)
        if not armed:
            if current_pct is not None and current_pct >= TARGET_PCT:
                armed = True
            continue
        floor_pct = max(TARGET_PCT, (high_pct or TARGET_PCT) - trail_points)
        floor_price = entry_price * (Decimal("1") + floor_pct / Decimal("100"))
        if quote.bid <= floor_price:
            exit_pct = percent(quote.bid, entry_price)
            return ExitResult(
                f"FLOOR-{trail_points.normalize()}PP",
                quote.at,
                quote.bid,
                exit_pct,
                high_bid,
                high_pct,
                (high_pct - exit_pct) if high_pct is not None and exit_pct is not None else None,
                False,
            )
    if ten_quote is None:
        return unanswered("UNANSWERABLE: no executable 10:00 quote")
    high_bid = max((quote.bid for quote in path if quote.at < close_time), default=high_bid)
    high_pct = percent(high_bid, entry_price)
    exit_pct = percent(ten_quote.bid, entry_price)
    return ExitResult(
        "10:00",
        ten_quote.at,
        ten_quote.bid,
        exit_pct,
        high_bid,
        high_pct,
        (high_pct - exit_pct) if armed and high_pct is not None and exit_pct is not None else None,
        False,
    )


def unanswered(reason: str) -> ExitResult:
    return ExitResult("UNANSWERABLE", None, None, None, None, None, None, False, reason)


def minute_path(
    *,
    entry_at: datetime,
    entry_price: Decimal | None,
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> tuple[MinutePoint, ...]:
    minute = entry_at.replace(second=0, microsecond=0)
    end = close_at(entry_at.astimezone(EASTERN).date())
    rows: list[MinutePoint] = []
    while minute <= end:
        next_minute = minute + timedelta(minutes=1)
        minute_quotes = [item for item in quotes if minute <= item.at < next_minute and item.bid > 0]
        quote = minute_quotes[-1] if minute_quotes else None
        halted = minute_overlaps_halt(minute, halts)
        rows.append(
            MinutePoint(
                minute,
                quote.bid if quote is not None else None,
                percent(quote.bid if quote is not None else None, entry_price),
                halted,
            )
        )
        minute = next_minute
    return tuple(rows)


def compare_entry(
    entry: PopulationEntry,
    trades: Sequence[TradePoint],
    quotes: Sequence[QuotePoint],
) -> ComparisonRow:
    entry_at = at_et(entry.day, entry.entry_time)
    halts = detect_halts(trades, quotes)
    entry_price, entry_source = resolve_entry_price(entry, entry_at, quotes, halts)
    path = executable_quotes(quotes, halts, entry_at)
    ten_quote = price_at_ten(quotes, halts, entry.day)
    close_time = close_at(entry.day)
    return ComparisonRow(
        entry,
        entry_at,
        entry_price,
        entry_source,
        minute_path(
            entry_at=entry_at,
            entry_price=entry_price,
            quotes=quotes,
            halts=halts,
        ),
        floor_ladder(
            entry_price=entry_price,
            path=path,
            close_time=close_time,
            ten_quote=ten_quote,
            trail_points=Decimal("5"),
        ),
        floor_ladder(
            entry_price=entry_price,
            path=path,
            close_time=close_time,
            ten_quote=ten_quote,
            trail_points=Decimal("3"),
        ),
        flat_target(
            entry_price=entry_price,
            path=path,
            close_time=close_time,
            ten_quote=ten_quote,
        ),
        ten_quote.at if ten_quote else None,
        ten_quote.bid if ten_quote else None,
    )


def load_market(
    session_factory, day: date, symbol: str
) -> tuple[list[TradePoint], list[QuotePoint]]:
    start = at_et(day, time(9, 20))
    end = at_et(day, time(16, 0))
    with session_factory() as session:
        trades = [
            TradePoint(
                utc(row[0]),
                Decimal(str(row[1])),
                int(row[2] or 0),
                str(row[3] or ""),
                str(row[4] or ""),
            )
            for row in session.execute(
                text(
                    "SELECT event_ts,price,size,exchange,conditions FROM market_capture_trades "
                    "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<:end ORDER BY event_ts,id"
                ),
                {"symbol": symbol, "start": start, "end": end},
            )
            if Decimal(str(row[1])) > 0
        ]
        quotes = [
            QuotePoint(utc(row[0]), Decimal(str(row[1])), Decimal(str(row[2])))
            for row in session.execute(
                text(
                    "SELECT event_ts,bid_price,ask_price FROM market_capture_quotes "
                    "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<:end "
                    "AND bid_price IS NOT NULL AND ask_price IS NOT NULL ORDER BY event_ts,id"
                ),
                {"symbol": symbol, "start": start, "end": end},
            )
        ]
    return trades, quotes


def clock(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone(EASTERN).strftime("%H:%M:%S")


def money(value: Decimal | None) -> str:
    return "-" if value is None else f"${value:.4f}"


def pct(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def points(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:.2f}pp"


def render_exit(result: ExitResult) -> str:
    if result.return_pct is None:
        return f"UNANSWERABLE ({result.answer})"
    return f"{result.rule} {clock(result.exit_at)} {money(result.exit_price)} {pct(result.return_pct)}"


def render(rows: Sequence[ComparisonRow], *, include_header: bool = True) -> str:
    lines = (
        [
            DISCLOSURE,
            "Minute price = final positive NBBO bid in that minute. Halted quotes are shown but never "
            "trigger or price an exit. A 10:00 close uses the first executable bid at or after 10:00.",
            "Floor trigger execution = first observed executable bid at or below the active floor; "
            "a gap through the floor is priced at that lower bid.",
        ]
        if include_header
        else []
    )
    for row in rows:
        lines.extend(
            [
                "",
                f"{row.entry.day} {row.entry.symbol}",
                f"Entry: {clock(row.entry_at)} ET @ {money(row.entry_price)} | {row.entry_source}",
                "",
                "| minute | bid price | % from entry | state |",
                "|---|---:|---:|---|",
            ]
        )
        for item in row.minutes:
            state = "HALT: NOT EXECUTABLE" if item.halted else "OPEN"
            lines.append(
                f"| {item.minute.astimezone(EASTERN):%H:%M} | {money(item.bid)} | "
                f"{pct(item.return_pct)} | {state} |"
            )
        lines.extend(
            [
                "",
                "| rule | high reached | exit | giveback from high | floor hit before +5% | 10:00 price |",
                "|---|---:|---|---:|---|---:|",
                f"| Ladder 5pp | {pct(row.ladder_five.high_pct)} | {render_exit(row.ladder_five)} | "
                f"{points(row.ladder_five.giveback_points)} | "
                f"{'YES' if row.ladder_five.floor_hit_before_five else 'NO'} | "
                f"{money(row.ten_price)} @ {clock(row.ten_at)} |",
                f"| Ladder 3pp | {pct(row.ladder_three.high_pct)} | {render_exit(row.ladder_three)} | "
                f"{points(row.ladder_three.giveback_points)} | "
                f"{'YES' if row.ladder_three.floor_hit_before_five else 'NO'} | "
                f"{money(row.ten_price)} @ {clock(row.ten_at)} |",
                f"| Flat +5% | {pct(row.flat_five.high_pct)} | {render_exit(row.flat_five)} | - | - | "
                f"{money(row.ten_price)} @ {clock(row.ten_at)} |",
                f"Difference 5pp - flat: {pct(difference(row.ladder_five, row.flat_five))} | "
                f"Difference 3pp - flat: {pct(difference(row.ladder_three, row.flat_five))}",
            ]
        )
    return "\n".join(lines)


def difference(left: ExitResult, right: ExitResult) -> Decimal | None:
    if left.return_pct is None or right.return_pct is None:
        return None
    return left.return_pct - right.return_pct


def render_summary(rows: Sequence[ComparisonRow], title: str) -> str:
    lines = [
        title,
        "",
        "| name | n | ladder 5pp | ladder 3pp | flat +5% | 5pp - flat | 3pp - flat |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    gradable = [
        row
        for row in rows
        if row.ladder_five.return_pct is not None
        and row.ladder_three.return_pct is not None
        and row.flat_five.return_pct is not None
    ]
    for row in rows:
        lines.append(
            f"| {row.entry.symbol} {row.entry.day:%m-%d} | "
            f"{'1/1' if row in gradable else '0/1'} | {pct(row.ladder_five.return_pct)} | "
            f"{pct(row.ladder_three.return_pct)} | {pct(row.flat_five.return_pct)} | "
            f"{pct(difference(row.ladder_five, row.flat_five))} | "
            f"{pct(difference(row.ladder_three, row.flat_five))} |"
        )
    lines.append(
        f"| Pooled | {len(gradable)}/{len(rows)} | "
        f"{pct(sum((row.ladder_five.return_pct for row in gradable), Decimal('0')))} | "
        f"{pct(sum((row.ladder_three.return_pct for row in gradable), Decimal('0')))} | "
        f"{pct(sum((row.flat_five.return_pct for row in gradable), Decimal('0')))} | "
        f"{pct(sum((difference(row.ladder_five, row.flat_five) for row in gradable), Decimal('0')))} | "
        f"{pct(sum((difference(row.ladder_three, row.flat_five) for row in gradable), Decimal('0')))} |"
    )
    return "\n".join(lines)


def write_csv(path: Path, rows: Sequence[ComparisonRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "day",
                "symbol",
                "group",
                "entry_time_et",
                "entry_price",
                "entry_source",
                "ladder_5pp_rule",
                "ladder_5pp_exit_et",
                "ladder_5pp_exit_price",
                "ladder_5pp_return_pct",
                "ladder_5pp_giveback_points",
                "ladder_3pp_rule",
                "ladder_3pp_exit_et",
                "ladder_3pp_exit_price",
                "ladder_3pp_return_pct",
                "ladder_3pp_giveback_points",
                "flat_rule",
                "flat_exit_et",
                "flat_exit_price",
                "flat_return_pct",
                "price_at_1000",
                "price_at_1000_time_et",
                "floor_hit_before_five",
                "qualification",
            ),
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "day": row.entry.day,
                    "symbol": row.entry.symbol,
                    "group": row.entry.group,
                    "entry_time_et": clock(row.entry_at),
                    "entry_price": row.entry_price or "",
                    "entry_source": row.entry_source,
                    "ladder_5pp_rule": row.ladder_five.rule,
                    "ladder_5pp_exit_et": clock(row.ladder_five.exit_at),
                    "ladder_5pp_exit_price": row.ladder_five.exit_price or "",
                    "ladder_5pp_return_pct": row.ladder_five.return_pct or "",
                    "ladder_5pp_giveback_points": row.ladder_five.giveback_points or "",
                    "ladder_3pp_rule": row.ladder_three.rule,
                    "ladder_3pp_exit_et": clock(row.ladder_three.exit_at),
                    "ladder_3pp_exit_price": row.ladder_three.exit_price or "",
                    "ladder_3pp_return_pct": row.ladder_three.return_pct or "",
                    "ladder_3pp_giveback_points": row.ladder_three.giveback_points or "",
                    "flat_rule": row.flat_five.rule,
                    "flat_exit_et": clock(row.flat_five.exit_at),
                    "flat_exit_price": row.flat_five.exit_price or "",
                    "flat_return_pct": row.flat_five.return_pct or "",
                    "price_at_1000": row.ten_price or "",
                    "price_at_1000_time_et": clock(row.ten_at),
                    "floor_hit_before_five": row.ladder_five.floor_hit_before_five
                    or row.ladder_three.floor_hit_before_five,
                    "qualification": DISCLOSURE,
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session_factory = build_session_factory(get_settings())
    rows: list[ComparisonRow] = []
    for entry in (*PRIMARY_POPULATION, *SEPARATE_POPULATION):
        trades, quotes = load_market(session_factory, entry.day, entry.symbol)
        rows.append(compare_entry(entry, trades, quotes))
    primary = [row for row in rows if row.entry.group == "PRIMARY"]
    separate = [row for row in rows if row.entry.group == "PPCB_SEPARATE"]
    print(render(primary))
    print("\n\n" + render_summary(primary, "Nine-name summary"))
    print("\n\nPPCB separate\n")
    print(render(separate, include_header=False))
    print("\n\n" + render_summary(separate, "PPCB separate summary"))
    if args.csv:
        write_csv(args.csv, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
