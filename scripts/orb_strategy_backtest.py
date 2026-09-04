#!/usr/bin/env python3
"""Measure uninterrupted ORB drawdown through first target or 10:00 ET."""

from __future__ import annotations

import argparse
import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.backtest.orb_entry import DEFAULT_GAP_CAP_PCT
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
ENTRY_QUOTE_MAX_DELAY = timedelta(seconds=2)
ENTRY_GAP_CAP_PCT = Decimal(str(DEFAULT_GAP_CAP_PCT))
FILL_RULE = (
    "entry time is the break print; entry price is the first positive NBBO ask in the band "
    "from the fixed trigger through "
    f"trigger +{ENTRY_GAP_CAP_PCT.normalize()}%, at or within 2 seconds after the break; "
    "otherwise UNANSWERABLE"
)
PATH_RULE = (
    "no stop and no early exit; executable bids are observed until the first target touch or "
    "10:00 ET, with shared confirmed-halt windows excluded"
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
    attempt: int
    bar_at: datetime
    crossed_at: datetime


@dataclass(frozen=True)
class EntryQuote:
    price: Decimal | None
    at: datetime | None
    reason: str = ""


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
    max_down_at: datetime | None
    target_at: datetime | None
    reached: bool | None
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


def minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


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


def break_attempts(
    *,
    day: date,
    symbol: str,
    opening_high: Decimal,
    trades: Sequence[TradePoint],
) -> list[BreakSignal]:
    """Return one crossing per bar; later attempts require a fresh reset below the level."""
    start = at_et(day, 9, 30)
    end = at_et(day, 10, 0)
    armed = True
    last_break_bar: datetime | None = None
    signals: list[BreakSignal] = []
    for trade in trades:
        if not start <= trade.at < end:
            continue
        bar_at = minute_floor(trade.at)
        if trade.price <= opening_high:
            armed = True
            continue
        if not armed or (last_break_bar is not None and bar_at <= last_break_bar):
            continue
        signals.append(
            BreakSignal(
                symbol=symbol,
                opening_high=opening_high,
                attempt=len(signals) + 1,
                bar_at=bar_at,
                crossed_at=trade.at,
            )
        )
        armed = False
        last_break_bar = bar_at
    return signals


def assumed_entry_ask(
    signal: BreakSignal,
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> EntryQuote:
    latest = signal.crossed_at + ENTRY_QUOTE_MAX_DELAY
    candidates = [
        item
        for item in quotes
        if signal.crossed_at <= item.at <= latest
        and item.ask > 0
        and not timestamp_is_halted(item.at, list(halts))
    ]
    if not candidates:
        return EntryQuote(None, None, "no positive post-break ask within 2 seconds")
    bound = signal.opening_high * (Decimal("1") + ENTRY_GAP_CAP_PCT / Decimal("100"))
    quote = next(
        (item for item in candidates if signal.opening_high <= item.ask <= bound),
        None,
    )
    if quote is None:
        if all(item.ask < signal.opening_high for item in candidates):
            return EntryQuote(None, candidates[-1].at, "post-break asks remain below the fixed trigger")
        return EntryQuote(
            None,
            candidates[0].at,
            f"post-break asks do not enter the trigger-to-+{ENTRY_GAP_CAP_PCT}% fill band",
        )
    return EntryQuote(quote.ask, quote.at)


def executable_quotes(
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


def evaluate_attempt(
    *,
    day: date,
    target_pct: Decimal,
    signal: BreakSignal,
    entry: EntryQuote,
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> AttemptRow:
    if entry.price is None or entry.at is None:
        return AttemptRow(
            day,
            target_pct,
            signal.symbol,
            signal.attempt,
            signal.opening_high,
            signal.crossed_at,
            None,
            None,
            None,
            None,
            None,
            None,
            entry.reason,
        )
    path = executable_quotes(
        quotes,
        start=entry.at,
        end=at_et(day, 10, 0),
        halts=halts,
    )
    if not path:
        return AttemptRow(
            day,
            target_pct,
            signal.symbol,
            signal.attempt,
            signal.opening_high,
            signal.crossed_at,
            entry.price,
            percent(entry.price, signal.opening_high),
            None,
            None,
            None,
            None,
            "no executable bid from entry through 10:00",
        )
    target_price = entry.price * (Decimal("1") + target_pct / Decimal("100"))
    target_quote = next((quote for quote in path if quote.bid >= target_price), None)
    measured = path if target_quote is None else path[: path.index(target_quote) + 1]
    low_quote = min(measured, key=lambda item: item.bid)
    low_pct = percent(low_quote.bid, entry.price)
    if low_pct is None or low_pct >= 0:
        drawdown = Decimal("0")
        drawdown_at = signal.crossed_at
    else:
        drawdown = low_pct
        drawdown_at = low_quote.at
    return AttemptRow(
        day,
        target_pct,
        signal.symbol,
        signal.attempt,
        signal.opening_high,
        signal.crossed_at,
        entry.price,
        percent(entry.price, signal.opening_high),
        drawdown,
        drawdown_at,
        target_quote.at if target_quote is not None else None,
        target_quote is not None,
        "ASSUMED POST-BREAK ASK FILL",
    )


def simulate_symbol(
    *,
    day: date,
    target_pct: Decimal,
    symbol: str,
    trades: Sequence[TradePoint],
    quotes: Sequence[QuotePoint],
    signals: Sequence[BreakSignal] | None = None,
) -> list[AttemptRow]:
    opening_high = fixed_opening_high(day, trades)
    if opening_high is None:
        return []
    halts = detect_halts(trades, quotes)
    return [
        evaluate_attempt(
            day=day,
            target_pct=target_pct,
            signal=signal,
            entry=assumed_entry_ask(signal, quotes, halts),
            quotes=quotes,
            halts=halts,
        )
        for signal in (
            signals
            if signals is not None
            else break_attempts(
                day=day,
                symbol=symbol,
                opening_high=opening_high,
                trades=trades,
            )
        )
    ]


def load_attempts_csv(path: Path) -> dict[tuple[date, str], list[BreakSignal]]:
    attempts: dict[tuple[date, str], list[BreakSignal]] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            day = date.fromisoformat(row["day"])
            symbol = row["symbol"].upper()
            crossed_et = datetime.combine(
                day,
                time.fromisoformat(row["entry_time_et"]),
                EASTERN,
            )
            crossed_at = crossed_et.astimezone(UTC)
            attempts.setdefault((day, symbol), []).append(
                BreakSignal(
                    symbol=symbol,
                    opening_high=Decimal(row["opening_high"]),
                    attempt=int(row["attempt"]),
                    bar_at=minute_floor(crossed_at),
                    crossed_at=crossed_at,
                )
            )
    return attempts


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
                    "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<:end "
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
                    "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<:end "
                    "AND bid_price IS NOT NULL AND ask_price IS NOT NULL ORDER BY event_ts,id"
                ),
                {"symbol": symbol, "start": start, "end": end},
            )
        ]
    return dedupe_trades(trades), quotes


def run_day(
    session_factory,
    day: date,
    target_pct: Decimal,
    now: datetime | None = None,
    attempts: dict[tuple[date, str], list[BreakSignal]] | None = None,
) -> DayRun:
    if utc(now or datetime.now(UTC)) < at_et(day, 10, 0):
        return DayRun(day, target_pct, unavailable_reason="09:30-10:00 window not complete")
    rows: list[AttemptRow] = []
    symbols = (
        {symbol for attempt_day, symbol in attempts if attempt_day == day}
        if attempts is not None
        else load_universe(session_factory, day)
    )
    for symbol in sorted(symbols):
        trades, quotes = load_market(session_factory, day, symbol)
        day_signals = attempts.get((day, symbol), []) if attempts is not None else None
        rows.extend(
            simulate_symbol(
                day=day,
                target_pct=target_pct,
                symbol=symbol,
                trades=trades,
                quotes=quotes,
                signals=day_signals,
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


def target_result(row: AttemptRow) -> str:
    if row.reached is None:
        return "UNANSWERABLE"
    return minute(row.target_at) if row.reached else "NEVER REACHED"


def drawdown_band(value: Decimal) -> str:
    if value >= Decimal("-1"):
        return "0 to -1%"
    if value >= Decimal("-2"):
        return "-1 to -2%"
    if value >= Decimal("-3"):
        return "-2 to -3%"
    if value >= Decimal("-5"):
        return "-3 to -5%"
    return "worse than -5%"


def render(runs: Sequence[DayRun]) -> str:
    lines = [DISCLOSURE, f"Fill rule: {FILL_RULE}", f"Path: {PATH_RULE}"]
    for target in TARGETS:
        lines.extend(
            [
                "",
                f"Target +{target.normalize()}%",
                "",
                "| day | sym | attempt | opening high | entry | entry px | slip % | max down | down at | target at |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        target_runs = sorted(
            (run for run in runs if run.target_pct == target),
            key=lambda run: run.day,
        )
        all_rows: list[AttemptRow] = []
        for run in target_runs:
            all_rows.extend(run.rows)
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
                            minute(row.max_down_at),
                            target_result(row),
                        ]
                    )
                    + " |"
                )
        gradable = [row for row in all_rows if row.reached is not None]
        reached = [row for row in gradable if row.reached and row.max_down is not None]
        lines.extend(
            [
                "",
                f"Target +{target.normalize()}% summary",
                "",
                "| reached | median down | worst down | 0 to -1% | -1 to -2% | -2 to -3% | -3 to -5% | worse than -5% |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        if reached:
            downs = [row.max_down for row in reached if row.max_down is not None]
            labels = (
                "0 to -1%",
                "-1 to -2%",
                "-2 to -3%",
                "-3 to -5%",
                "worse than -5%",
            )
            bands = {label: sum(drawdown_band(value) == label for value in downs) for label in labels}
            denominator = len(reached)
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"{denominator}/{len(gradable)}",
                        pct(median(downs)),
                        pct(min(downs)),
                        f"{bands['0 to -1%']}/{denominator}",
                        f"{bands['-1 to -2%']}/{denominator}",
                        f"{bands['-2 to -3%']}/{denominator}",
                        f"{bands['-3 to -5%']}/{denominator}",
                        f"{bands['worse than -5%']}/{denominator}",
                    ]
                )
                + " |"
            )
        else:
            lines.append(f"| 0/{len(gradable)} | - | - | 0/0 | 0/0 | 0/0 | 0/0 | 0/0 |")
        lines.append(f"UNANSWERABLE: {len(all_rows) - len(gradable)}/{len(all_rows)} attempts.")
        for run in target_runs:
            if run.unavailable_reason:
                lines.append(f"{run.day}: NOT REACHABLE - {run.unavailable_reason}.")
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
            "max_down_to_target_or_1000",
            "max_down_at_et",
            "target_at_et",
            "target_result",
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
                        "max_down_to_target_or_1000": (
                            row.max_down if row.max_down is not None else ""
                        ),
                        "max_down_at_et": minute(row.max_down_at),
                        "target_at_et": minute(row.target_at),
                        "target_result": target_result(row),
                        "note": row.note,
                        "qualification": DISCLOSURE,
                    }
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--csv", type=Path)
    parser.add_argument(
        "--attempts-csv",
        type=Path,
        help="optional frozen entry-attempt population to replay without reselection",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    end_day = args.end_date or args.start_date
    if end_day < args.start_date:
        raise SystemExit("--end-date must not precede --start-date")
    session_factory = build_session_factory(get_settings())
    attempts = load_attempts_csv(args.attempts_csv) if args.attempts_csv else None
    runs: list[DayRun] = []
    for target in TARGETS:
        day = args.start_date
        while day <= end_day:
            if day.weekday() < 5:
                runs.append(run_day(session_factory, day, target, attempts=attempts))
            day += timedelta(days=1)
    print(render(runs))
    if args.csv:
        write_csv(args.csv, runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
