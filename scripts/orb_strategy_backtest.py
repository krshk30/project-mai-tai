#!/usr/bin/env python3
"""Simulate fixed-high ORB targets with breakeven-stop re-entry."""

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
TARGETS = (Decimal("3"), Decimal("5"))
ASK_MAX_AGE = timedelta(seconds=2)
FILL_RULE = (
    "fixed level is the highest 1-minute trade bar from 09:25 through 09:29; the first "
    "09:30-09:59 print strictly above it is the break, filled at the latest NBBO ask already "
    "visible then if positive and no more than 2 seconds old; after a stop, re-entry cannot "
    "occur in the same 1-minute bar and requires another print at or below the fixed level "
    "before a later-bar print breaks it again"
)
STOP_RULE = (
    "breakeven triggers on the first executable bid at or below entry and fills at that bid; "
    "the spread and any gap through entry are charged"
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
class AttemptRow:
    day: date
    target_pct: Decimal
    symbol: str
    attempt: int
    opening_high: Decimal
    entry_at: datetime
    entry_price: Decimal | None
    slip_pct: Decimal | None
    max_down: Decimal | None
    max_up: Decimal | None
    max_up_at: datetime | None
    exit_rule: str
    exit_at: datetime | None
    exit_price: Decimal | None
    return_pct: Decimal | None
    note: str


@dataclass
class DayRun:
    day: date
    target_pct: Decimal
    rows: list[AttemptRow] = field(default_factory=list)
    unavailable_reason: str = ""


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def at_et(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), EASTERN).astimezone(UTC)


def percent(value: Decimal | None, basis: Decimal | None) -> Decimal | None:
    if value is None or basis is None or basis <= 0:
        return None
    return (value / basis - Decimal("1")) * Decimal("100")


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
    aggregator = OrbTickAggregator(session_open=at_et(day, 9, 25))
    bars: list[OrbBar] = []
    for trade in trades:
        completed = aggregator.add_tick(trade.at, float(trade.price), float(trade.size))
        if completed is not None:
            bars.append(completed)
    final = aggregator.flush()
    if final is not None:
        bars.append(final)
    return bars


def fixed_opening_high(day: date, trades: Sequence[TradePoint]) -> Decimal | None:
    start = at_et(day, 9, 25)
    end = at_et(day, 9, 30)
    bars = [bar for bar in build_bars(day, trades) if start <= bar.timestamp < end]
    return Decimal(str(max(bar.high for bar in bars))) if bars else None


def next_break(
    *,
    day: date,
    symbol: str,
    opening_high: Decimal,
    trades: Sequence[TradePoint],
    after: datetime | None,
) -> BreakSignal | None:
    start = at_et(day, 9, 30)
    end = at_et(day, 10, 0)
    candidates = [trade for trade in trades if start <= trade.at < end]
    if after is None:
        crossing = next((trade for trade in candidates if trade.price > opening_high), None)
    else:
        armed = False
        crossing = None
        first_reentry_bar = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for trade in candidates:
            if trade.at <= after:
                continue
            if trade.price <= opening_high:
                armed = True
            elif armed and trade.at >= first_reentry_bar:
                crossing = trade
                break
    if crossing is None:
        return None
    return BreakSignal(
        symbol=symbol,
        opening_high=opening_high,
        bar_at=crossing.at.replace(second=0, microsecond=0),
        crossed_at=crossing.at,
    )


def assumed_entry_ask(
    signal: BreakSignal,
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> Decimal | None:
    eligible = [quote for quote in quotes if quote.at <= signal.crossed_at]
    if not eligible:
        return None
    latest = eligible[-1]
    if (
        latest.ask <= 0
        or signal.crossed_at - latest.at > ASK_MAX_AGE
        or timestamp_is_halted(latest.at, list(halts))
    ):
        return None
    return latest.ask


def _executable_quotes(
    quotes: Sequence[QuotePoint],
    *,
    start: datetime,
    end: datetime,
    halts: Sequence[HaltWindow],
) -> list[QuotePoint]:
    return [
        quote
        for quote in quotes
        if start <= quote.at < end
        and quote.bid > 0
        and not timestamp_is_halted(quote.at, list(halts))
    ]


def max_drawdown(quotes: Sequence[QuotePoint], entry_price: Decimal) -> Decimal | None:
    if not quotes:
        return None
    return min(
        Decimal("0"),
        *(percent(quote.bid, entry_price) for quote in quotes),
    )


def evaluate_attempt(
    *,
    day: date,
    target_pct: Decimal,
    attempt: int,
    signal: BreakSignal,
    entry_price: Decimal | None,
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> AttemptRow:
    slip = percent(entry_price, signal.opening_high)
    if entry_price is None:
        return AttemptRow(
            day,
            target_pct,
            signal.symbol,
            attempt,
            signal.opening_high,
            signal.crossed_at,
            None,
            None,
            None,
            None,
            None,
            "UNANSWERABLE",
            None,
            None,
            None,
            "no fresh positive ask at the break",
        )
    ten = at_et(day, 10, 0)
    path = _executable_quotes(quotes, start=signal.crossed_at, end=ten, halts=halts)
    observed: list[QuotePoint] = []
    target_price = entry_price * (Decimal("1") + target_pct / Decimal("100"))
    exit_quote: QuotePoint | None = None
    exit_rule = ""
    for quote in path:
        observed.append(quote)
        if quote.bid >= target_price:
            exit_quote = quote
            exit_rule = f"+{target_pct.normalize()}%"
            break
        if quote.bid <= entry_price:
            exit_quote = quote
            exit_rule = "STOP 0%"
            break
    if exit_quote is None:
        close_quotes = [
            quote
            for quote in quotes
            if quote.at >= ten and quote.bid > 0 and not timestamp_is_halted(quote.at, list(halts))
        ]
        if not close_quotes:
            return AttemptRow(
                day,
                target_pct,
                signal.symbol,
                attempt,
                signal.opening_high,
                signal.crossed_at,
                entry_price,
                slip,
                max_drawdown(observed, entry_price),
                max((percent(item.bid, entry_price) for item in observed), default=None),
                max(observed, key=lambda item: item.bid).at if observed else None,
                "UNANSWERABLE",
                None,
                None,
                None,
                "no executable bid for the 10:00 close",
            )
        exit_quote = close_quotes[0]
        observed.append(exit_quote)
        exit_rule = "10:00"
    max_quote = max(observed, key=lambda item: item.bid)
    min_quote = min(observed, key=lambda item: item.bid)
    return AttemptRow(
        day,
        target_pct,
        signal.symbol,
        attempt,
        signal.opening_high,
        signal.crossed_at,
        entry_price,
        slip,
        min(Decimal("0"), percent(min_quote.bid, entry_price) or Decimal("0")),
        percent(max_quote.bid, entry_price),
        max_quote.at,
        exit_rule,
        exit_quote.at,
        exit_quote.bid,
        percent(exit_quote.bid, entry_price),
        "ASSUMED ASK FILL",
    )


def simulate_symbol(
    *,
    day: date,
    target_pct: Decimal,
    symbol: str,
    trades: Sequence[TradePoint],
    quotes: Sequence[QuotePoint],
) -> list[AttemptRow]:
    opening_high = fixed_opening_high(day, trades)
    if opening_high is None:
        return []
    halts = detect_halts(trades, quotes)
    rows: list[AttemptRow] = []
    after: datetime | None = None
    while True:
        signal = next_break(
            day=day,
            symbol=symbol,
            opening_high=opening_high,
            trades=trades,
            after=after,
        )
        if signal is None:
            break
        entry_price = assumed_entry_ask(signal, quotes, halts)
        row = evaluate_attempt(
            day=day,
            target_pct=target_pct,
            attempt=len(rows) + 1,
            signal=signal,
            entry_price=entry_price,
            quotes=quotes,
            halts=halts,
        )
        rows.append(row)
        if row.exit_rule != "STOP 0%" or row.exit_at is None:
            break
        after = row.exit_at
    return rows


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
    end = at_et(day, 10, 1)
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


def run_day(session_factory, day: date, target_pct: Decimal, now: datetime | None = None) -> DayRun:
    if utc(now or datetime.now(UTC)) < at_et(day, 10, 0):
        return DayRun(day, target_pct, unavailable_reason="09:30-10:00 window not complete")
    rows: list[AttemptRow] = []
    for symbol in sorted(load_universe(session_factory, day)):
        trades, quotes = load_market(session_factory, day, symbol)
        rows.extend(
            simulate_symbol(
                day=day,
                target_pct=target_pct,
                symbol=symbol,
                trades=trades,
                quotes=quotes,
            )
        )
    return DayRun(day, target_pct, rows)


def clock(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone(EASTERN).strftime("%H:%M:%S")


def minute(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone(EASTERN).strftime("%H:%M")


def money(value: Decimal | None) -> str:
    return "-" if value is None else f"${value:.4f}"


def pct(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def render(runs: Sequence[DayRun]) -> str:
    lines = [DISCLOSURE, f"Fill rule: {FILL_RULE}", f"Stop fill: {STOP_RULE}"]
    for target in TARGETS:
        lines.extend(
            [
                "",
                f"Target +{target.normalize()}%",
                "",
                "| day | sym | attempt | opening high | entry | px | slip % | pre-run down | max up | up at | exit | exit at | exit px | return |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|",
            ]
        )
        target_runs = sorted(
            (run for run in runs if run.target_pct == target),
            key=lambda run: run.day,
        )
        for run in target_runs:
            totals: dict[str, int] = {}
            for row in run.rows:
                totals[row.symbol] = totals.get(row.symbol, 0) + 1
            for row in sorted(
                run.rows,
                key=lambda item: (item.entry_at, item.symbol, item.attempt),
            ):
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            row.day.isoformat(),
                            row.symbol,
                            f"{row.attempt}/{totals[row.symbol]}",
                            money(row.opening_high),
                            clock(row.entry_at),
                            money(row.entry_price),
                            pct(row.slip_pct),
                            pct(row.max_down),
                            pct(row.max_up),
                            minute(row.max_up_at),
                            row.exit_rule,
                            clock(row.exit_at),
                            money(row.exit_price),
                            pct(row.return_pct),
                        ]
                    )
                    + " |"
                )
        lines.append("")
        for run in target_runs:
            if run.unavailable_reason:
                lines.append(f"{run.day}: NOT REACHABLE - {run.unavailable_reason}.")
                continue
            attempts = len(run.rows)
            wins = sum(row.exit_rule == f"+{target.normalize()}%" for row in run.rows)
            stops = sum(row.exit_rule == "STOP 0%" for row in run.rows)
            closes = sum(row.exit_rule == "10:00" for row in run.rows)
            unanswerable = sum(row.exit_rule == "UNANSWERABLE" for row in run.rows)
            total_return = sum(
                (row.return_pct for row in run.rows if row.return_pct is not None), Decimal("0")
            )
            lines.append(
                f"{run.day}: attempts {attempts}; wins {wins}/{attempts}; stops {stops}/{attempts}; "
                f"10:00 {closes}/{attempts}; UNANSWERABLE {unanswerable}/{attempts}; "
                f"total {pct(total_return)}."
            )
    return "\n".join(lines)


def write_csv(path: Path, runs: Sequence[DayRun]) -> None:
    with path.open("w", newline="") as handle:
        fields = [
            "day",
            "target_pct",
            "symbol",
            "attempt",
            "opening_high",
            "entry_time_et",
            "entry_price",
            "slip_pct",
            "max_down_before_exit",
            "max_up_before_exit",
            "max_up_at_et",
            "exit_rule",
            "exit_time_et",
            "exit_price",
            "return_pct",
            "note",
            "qualification",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for run in sorted(runs, key=lambda item: (item.target_pct, item.day)):
            for row in sorted(
                run.rows,
                key=lambda item: (item.entry_at, item.symbol, item.attempt),
            ):
                writer.writerow(
                    {
                        "day": row.day,
                        "target_pct": row.target_pct,
                        "symbol": row.symbol,
                        "attempt": row.attempt,
                        "opening_high": row.opening_high,
                        "entry_time_et": clock(row.entry_at),
                        "entry_price": row.entry_price or "",
                        "slip_pct": row.slip_pct if row.slip_pct is not None else "",
                        "max_down_before_exit": row.max_down if row.max_down is not None else "",
                        "max_up_before_exit": row.max_up if row.max_up is not None else "",
                        "max_up_at_et": minute(row.max_up_at),
                        "exit_rule": row.exit_rule,
                        "exit_time_et": clock(row.exit_at),
                        "exit_price": row.exit_price or "",
                        "return_pct": row.return_pct if row.return_pct is not None else "",
                        "note": row.note,
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
    runs: list[DayRun] = []
    for target in TARGETS:
        day = args.start_date
        while day <= end_day:
            if day.weekday() < 5:
                runs.append(run_day(session_factory, day, target))
            day += timedelta(days=1)
    print(render(runs))
    if args.csv:
        write_csv(args.csv, runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
