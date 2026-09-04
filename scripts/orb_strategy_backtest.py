#!/usr/bin/env python3
"""Measure raw post-break movement for the fixed 09:25-09:30 ORB setup."""

from __future__ import annotations

import argparse
import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence
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
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.orb_tick_aggregator import OrbTickAggregator

EASTERN = ZoneInfo("America/New_York")
DISCLOSURE = "SIMULATED | NO REALISED CONTROL | NOT SIZE-QUALIFIED"
ASK_MAX_AGE = timedelta(seconds=2)
FILL_RULE = (
    "first 1-minute trade bar from 09:30 through 09:59 whose high is strictly above the "
    "fixed 09:25-09:29 high; entry time is its first crossing print, with an assumed fill "
    "at the latest NBBO ask already visible then if positive and no more than 2 seconds old"
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
class BreakSignal:
    symbol: str
    opening_high: Decimal
    bar_at: datetime
    crossed_at: datetime


@dataclass(frozen=True)
class MovementRow:
    day: date
    symbol: str
    entry_at: datetime
    entry_price: Decimal | None
    high_bid: Decimal | None
    high_at: datetime | None
    low_bid: Decimal | None
    low_at: datetime | None
    reached_five: bool | None
    assumption: str

    @property
    def high_pct(self) -> Decimal | None:
        return percent_from_entry(self.high_bid, self.entry_price)

    @property
    def low_pct(self) -> Decimal | None:
        return percent_from_entry(self.low_bid, self.entry_price)


@dataclass
class DayResult:
    day: date
    watched: int
    broke: int
    rows: list[MovementRow] = field(default_factory=list)
    unavailable_reason: str = ""


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def at_et(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), EASTERN).astimezone(UTC)


def percent_from_entry(value: Decimal | None, entry: Decimal | None) -> Decimal | None:
    if value is None or entry is None or entry <= 0:
        return None
    return (value / entry - Decimal("1")) * Decimal("100")


def dedupe_trades(rows: Iterable[TradePoint]) -> list[TradePoint]:
    seen: set[tuple[datetime, Decimal, int, str, str]] = set()
    result: list[TradePoint] = []
    for row in sorted(rows, key=lambda item: item.at):
        key = (row.at, row.price, row.size, row.exchange, row.conditions)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def detect_halts(trades: Sequence[TradePoint], quotes: Sequence[QuotePoint]) -> list[HaltWindow]:
    """Use the shared halt classifier; never hand-list a symbol or session."""
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


def build_bars(day: date, trades: Sequence[TradePoint]) -> list[OrbBar]:
    """Build the same sparse one-minute trade bars as the deployed ORB aggregator."""
    aggregator = OrbTickAggregator(session_open=at_et(day, 9, 25))
    bars: list[OrbBar] = []
    for trade in trades:
        bar = aggregator.add_tick(trade.at, float(trade.price), float(trade.size))
        if bar is not None:
            bars.append(bar)
    final = aggregator.flush()
    if final is not None:
        bars.append(final)
    return bars


def first_break(day: date, symbol: str, trades: Sequence[TradePoint]) -> BreakSignal | None:
    observe = at_et(day, 9, 25)
    market_open = at_et(day, 9, 30)
    window_end = at_et(day, 10, 0)
    bars = build_bars(day, trades)
    opening_bars = [bar for bar in bars if observe <= bar.timestamp < market_open]
    if not opening_bars:
        return None
    opening_high = Decimal(str(max(bar.high for bar in opening_bars)))
    break_bar = next(
        (
            bar
            for bar in bars
            if market_open <= bar.timestamp < window_end and Decimal(str(bar.high)) > opening_high
        ),
        None,
    )
    if break_bar is None:
        return None
    crossed_at = next(
        trade.at
        for trade in trades
        if trade.at.replace(second=0, microsecond=0) == break_bar.timestamp
        and trade.price > opening_high
    )
    return BreakSignal(symbol, opening_high, break_bar.timestamp, crossed_at)


def assumed_entry_ask(
    signal: BreakSignal,
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> Decimal | None:
    """Use the quote visible at the crossing; never look forward or skip a bad latest quote."""
    eligible = [quote for quote in quotes if quote.at <= signal.crossed_at]
    if not eligible:
        return None
    quote = eligible[-1]
    if (
        quote.ask <= 0
        or signal.crossed_at - quote.at > ASK_MAX_AGE
        or timestamp_is_halted(quote.at, list(halts))
    ):
        return None
    return quote.ask


def movement_after_entry(
    *,
    day: date,
    symbol: str,
    signal: BreakSignal,
    entry_price: Decimal | None,
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> MovementRow:
    if entry_price is None:
        return MovementRow(
            day,
            symbol,
            signal.crossed_at,
            None,
            None,
            None,
            None,
            None,
            None,
            "UNANSWERABLE: no fresh positive ask at the crossing",
        )
    end = at_et(day, 10, 0)
    executable = [
        quote
        for quote in quotes
        if signal.crossed_at <= quote.at < end
        and quote.bid > 0
        and not timestamp_is_halted(quote.at, list(halts))
    ]
    if not executable:
        return MovementRow(
            day,
            symbol,
            signal.crossed_at,
            entry_price,
            None,
            None,
            None,
            None,
            None,
            "UNANSWERABLE: no executable bid from entry through 10:00",
        )
    high = max(executable, key=lambda quote: quote.bid)
    low = min(executable, key=lambda quote: quote.bid)
    high_pct = percent_from_entry(high.bid, entry_price)
    return MovementRow(
        day,
        symbol,
        signal.crossed_at,
        entry_price,
        high.bid,
        high.at,
        low.bid,
        low.at,
        high_pct is not None and high_pct >= Decimal("5"),
        "ASSUMED ASK FILL",
    )


def load_universe(session_factory, day: date) -> set[str]:
    cutoff = at_et(day, 9, 25)
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (symbol) symbol, event_type
            FROM scanner_confirmed_events
            WHERE trade_date=:day AND event_at<=:cutoff
            ORDER BY symbol, event_at DESC, id DESC
        )
        SELECT symbol FROM latest WHERE event_type='CONFIRM' ORDER BY symbol
    """
    with session_factory() as session:
        return {
            str(row[0]).upper()
            for row in session.execute(text(sql), {"day": day, "cutoff": cutoff})
        }


def load_market(
    session_factory, day: date, symbol: str
) -> tuple[list[TradePoint], list[QuotePoint]]:
    start = at_et(day, 9, 25)
    end = at_et(day, 10, 0)
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
                    "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<=:end "
                    "ORDER BY event_ts,id"
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
                    "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<=:end "
                    "AND bid_price IS NOT NULL AND ask_price IS NOT NULL ORDER BY event_ts,id"
                ),
                {"symbol": symbol, "start": start, "end": end},
            )
        ]
    return dedupe_trades(trades), quotes


def run_day(session_factory, day: date, now: datetime | None = None) -> DayResult:
    now_utc = utc(now or datetime.now(UTC))
    if now_utc < at_et(day, 10, 0):
        return DayResult(day, 0, 0, unavailable_reason="09:30-10:00 window not complete")
    universe = load_universe(session_factory, day)
    rows: list[MovementRow] = []
    broke = 0
    for symbol in sorted(universe):
        trades, quotes = load_market(session_factory, day, symbol)
        signal = first_break(day, symbol, trades)
        if signal is None:
            continue
        broke += 1
        halts = detect_halts(trades, quotes)
        entry_price = assumed_entry_ask(signal, quotes, halts)
        rows.append(
            movement_after_entry(
                day=day,
                symbol=symbol,
                signal=signal,
                entry_price=entry_price,
                quotes=quotes,
                halts=halts,
            )
        )
    return DayResult(day, len(universe), broke, rows)


def clock(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone(EASTERN).strftime("%H:%M:%S")


def minute(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone(EASTERN).strftime("%H:%M")


def money(value: Decimal | None) -> str:
    return "-" if value is None else f"${value:.4f}"


def pct(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def render(results: Sequence[DayResult]) -> str:
    rows = sorted(
        (row for result in results for row in result.rows),
        key=lambda row: (row.day, row.entry_at),
    )
    lines = [DISCLOSURE, f"Fill rule: {FILL_RULE}", ""]
    lines.append("| day | sym | entry | px | hi % | hi at | lo % | lo at | +5% | note |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    for row in rows:
        reached = "YES" if row.reached_five is True else "NO" if row.reached_five is False else "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    row.day.isoformat(),
                    row.symbol,
                    clock(row.entry_at),
                    money(row.entry_price),
                    pct(row.high_pct),
                    minute(row.high_at),
                    pct(row.low_pct),
                    minute(row.low_at),
                    reached,
                    row.assumption,
                ]
            )
            + " |"
        )
    lines.append("")
    for result in results:
        if result.unavailable_reason:
            lines.append(f"{result.day.isoformat()}: NOT REACHABLE - {result.unavailable_reason}.")
        else:
            lines.append(
                f"{result.day.isoformat()}: {result.broke}/{result.watched} watched stocks broke the opening high."
            )
    return "\n".join(lines)


def write_csv(path: Path, results: Sequence[DayResult]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "day",
                "symbol",
                "entry_time_et",
                "entry_price",
                "high_pct",
                "high_at_et",
                "low_pct",
                "low_at_et",
                "reached_5pct",
                "note",
                "qualification",
            ],
        )
        writer.writeheader()
        for row in sorted(
            (row for result in results for row in result.rows),
            key=lambda item: (item.day, item.entry_at),
        ):
            writer.writerow(
                {
                    "day": row.day,
                    "symbol": row.symbol,
                    "entry_time_et": clock(row.entry_at),
                    "entry_price": row.entry_price or "",
                    "high_pct": f"{row.high_pct:.4f}" if row.high_pct is not None else "",
                    "high_at_et": minute(row.high_at),
                    "low_pct": f"{row.low_pct:.4f}" if row.low_pct is not None else "",
                    "low_at_et": minute(row.low_at),
                    "reached_5pct": row.reached_five,
                    "note": row.assumption,
                    "qualification": DISCLOSURE,
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--csv", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    end_day = args.end_date or args.start_date
    if end_day < args.start_date:
        raise SystemExit("--end-date must not precede --start-date")
    session_factory = build_session_factory(get_settings())
    results: list[DayResult] = []
    day = args.start_date
    while day <= end_day:
        if day.weekday() < 5:
            results.append(run_day(session_factory, day))
        day += timedelta(days=1)
    print(render(results))
    if args.csv:
        write_csv(args.csv, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
