#!/usr/bin/env python3
"""Measure post-target pullbacks and compare revised ORB floor ladders."""

from __future__ import annotations

import argparse
import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
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
INITIAL_FLOORS = (Decimal("3"), Decimal("4"))
TRAIL_DISTANCES = (Decimal("5"), Decimal("3"))
ARM_MODES = ("TOUCH", "MINUTE_CLOSE")


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
class PullbackResult:
    touch_at: datetime | None
    touch_price: Decimal | None
    reference_price: Decimal | None
    started_at: datetime | None
    trough_at: datetime | None
    trough_price: Decimal | None
    giveback_points: Decimal | None
    giveback_pct: Decimal | None
    recovered_at: datetime | None
    duration_seconds: Decimal | None
    answer: str = ""


@dataclass(frozen=True)
class ExitResult:
    rule: str
    outcome: str
    armed_at: datetime | None
    exit_at: datetime | None
    exit_price: Decimal | None
    return_pct: Decimal | None
    high_pct: Decimal | None
    giveback_points: Decimal | None
    answer: str = ""


@dataclass(frozen=True)
class ComparisonRow:
    entry: PopulationEntry
    entry_at: datetime
    entry_price: Decimal | None
    entry_source: str
    pullback: PullbackResult
    ladders: tuple[ExitResult, ...]
    flat_five: ExitResult
    ten_at: datetime | None
    ten_price: Decimal | None
    halt_count: int
    gradable: bool
    gradability_reason: str


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


def executable_quotes(
    quotes: Sequence[QuotePoint], halts: Sequence[HaltWindow], start: datetime
) -> list[QuotePoint]:
    return [
        quote
        for quote in quotes
        if quote.at >= start and quote.bid > 0 and not timestamp_is_halted(quote.at, list(halts))
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


def post_touch_pullback(
    *, entry_price: Decimal | None, path: Sequence[QuotePoint], close_time: datetime
) -> PullbackResult:
    if entry_price is None:
        return PullbackResult(
            None, None, None, None, None, None, None, None, None, None, "entry unavailable"
        )
    target = entry_price * Decimal("1.05")
    touch_index = next(
        (
            index
            for index, quote in enumerate(path)
            if quote.at < close_time and quote.bid >= target
        ),
        None,
    )
    if touch_index is None:
        return PullbackResult(
            None, None, None, None, None, None, None, None, None, None, "+5% never touched"
        )
    touch = path[touch_index]
    after_touch = [quote for quote in path[touch_index + 1 :] if quote.at < close_time]
    reference = touch
    pullback_index: int | None = None
    for index, quote in enumerate(after_touch):
        if quote.bid >= reference.bid:
            reference = quote
            continue
        pullback_index = index
        break
    if pullback_index is None:
        return PullbackResult(
            touch.at,
            touch.bid,
            reference.bid,
            None,
            reference.at,
            reference.bid,
            Decimal("0"),
            Decimal("0"),
            reference.at,
            Decimal("0"),
            "no retracement before 10:00",
        )
    pullback_path = after_touch[pullback_index:]
    recovered_index = next(
        (index for index, quote in enumerate(pullback_path) if quote.bid > reference.bid),
        None,
    )
    measured = pullback_path if recovered_index is None else pullback_path[: recovered_index + 1]
    trough = min(measured, key=lambda quote: quote.bid)
    recovered = pullback_path[recovered_index] if recovered_index is not None else None
    reference_pct = percent(reference.bid, entry_price)
    trough_pct = percent(trough.bid, entry_price)
    return PullbackResult(
        touch.at,
        touch.bid,
        reference.bid,
        pullback_path[0].at,
        trough.at,
        trough.bid,
        (reference_pct - trough_pct)
        if reference_pct is not None and trough_pct is not None
        else None,
        percent(trough.bid, reference.bid),
        recovered.at if recovered else None,
        Decimal(str((recovered.at - pullback_path[0].at).total_seconds())) if recovered else None,
        "" if recovered else "never exceeded pre-pullback high by 10:00",
    )


def minute_close_arm(
    *,
    entry_price: Decimal,
    path: Sequence[QuotePoint],
    close_time: datetime,
    halts: Sequence[HaltWindow],
) -> tuple[datetime, Decimal] | None:
    minute = path[0].at.replace(second=0, microsecond=0) if path else close_time
    while minute < close_time:
        boundary = minute + timedelta(minutes=1)
        candidates = [quote for quote in path if minute <= quote.at < boundary]
        if any(halt.last_print_at < boundary and halt.reopen_print_at > minute for halt in halts):
            minute = boundary
            continue
        if candidates:
            close_quote = candidates[-1]
            close_pct = percent(close_quote.bid, entry_price)
            if close_pct is not None and close_pct >= TARGET_PCT:
                return boundary, close_quote.bid
        minute = boundary
    return None


def floor_ladder(
    *,
    entry_price: Decimal | None,
    path: Sequence[QuotePoint],
    close_time: datetime,
    ten_quote: QuotePoint | None,
    initial_floor: Decimal,
    trail_points: Decimal,
    arm_mode: str,
    halts: Sequence[HaltWindow] = (),
) -> ExitResult:
    name = f"{arm_mode}-F{initial_floor.normalize()}-T{trail_points.normalize()}"
    if entry_price is None:
        return unanswered(name, "entry price unavailable")
    if arm_mode == "TOUCH":
        arm_quote = next(
            (
                quote
                for quote in path
                if quote.at < close_time and percent(quote.bid, entry_price) >= TARGET_PCT
            ),
            None,
        )
        arm = (arm_quote.at, arm_quote.bid) if arm_quote else None
    elif arm_mode == "MINUTE_CLOSE":
        arm = minute_close_arm(
            entry_price=entry_price,
            path=path,
            close_time=close_time,
            halts=halts,
        )
    else:
        raise ValueError(f"unsupported arm mode: {arm_mode}")

    if arm is None:
        if ten_quote is None:
            return unanswered(name, "no qualifying arm and no executable 10:00 quote")
        highs = [percent(quote.bid, entry_price) for quote in path if quote.at < close_time]
        return ExitResult(
            name,
            "10:00-NOT-ARMED",
            None,
            ten_quote.at,
            ten_quote.bid,
            percent(ten_quote.bid, entry_price),
            max((value for value in highs if value is not None), default=None),
            None,
        )

    armed_at, arm_price = arm
    high_pct = percent(arm_price, entry_price) or TARGET_PCT
    for quote in path:
        if quote.at < armed_at or quote.at >= close_time:
            continue
        current_pct = percent(quote.bid, entry_price)
        if current_pct is None:
            continue
        high_pct = max(high_pct, current_pct)
        floor_pct = max(initial_floor, high_pct - trail_points)
        floor_price = entry_price * (Decimal("1") + floor_pct / Decimal("100"))
        if quote.bid <= floor_price:
            return ExitResult(
                name,
                "FLOOR",
                armed_at,
                quote.at,
                quote.bid,
                current_pct,
                high_pct,
                high_pct - current_pct,
            )
    if ten_quote is None:
        return unanswered(name, "no executable 10:00 quote", armed_at=armed_at)
    ten_pct = percent(ten_quote.bid, entry_price)
    return ExitResult(
        name,
        "10:00",
        armed_at,
        ten_quote.at,
        ten_quote.bid,
        ten_pct,
        high_pct,
        high_pct - (ten_pct or Decimal("0")),
    )


def flat_target(
    *,
    entry_price: Decimal | None,
    path: Sequence[QuotePoint],
    close_time: datetime,
    ten_quote: QuotePoint | None,
) -> ExitResult:
    if entry_price is None:
        return unanswered("FLAT+5", "entry price unavailable")
    quote = next(
        (
            item
            for item in path
            if item.at < close_time and percent(item.bid, entry_price) >= TARGET_PCT
        ),
        None,
    )
    if quote is not None:
        quote_pct = percent(quote.bid, entry_price)
        return ExitResult(
            "FLAT+5",
            "+5%",
            quote.at,
            quote.at,
            quote.bid,
            quote_pct,
            quote_pct,
            Decimal("0"),
        )
    if ten_quote is None:
        return unanswered("FLAT+5", "no executable 10:00 quote")
    highs = [percent(quote.bid, entry_price) for quote in path if quote.at < close_time]
    return ExitResult(
        "FLAT+5",
        "10:00",
        None,
        ten_quote.at,
        ten_quote.bid,
        percent(ten_quote.bid, entry_price),
        max((value for value in highs if value is not None), default=None),
        None,
    )


def unanswered(rule: str, reason: str, *, armed_at: datetime | None = None) -> ExitResult:
    return ExitResult(
        rule,
        "UNANSWERABLE",
        armed_at,
        None,
        None,
        None,
        None,
        None,
        f"UNANSWERABLE: {reason}",
    )


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
    ladders = tuple(
        floor_ladder(
            entry_price=entry_price,
            path=path,
            close_time=close_time,
            ten_quote=ten_quote,
            initial_floor=floor,
            trail_points=trail,
            arm_mode=arm_mode,
            halts=halts,
        )
        for arm_mode in ARM_MODES
        for floor in INITIAL_FLOORS
        for trail in TRAIL_DISTANCES
    )
    gradable = entry_price is not None and ten_quote is not None
    reason = "" if gradable else "UNANSWERABLE: incomplete entry-to-10:00 executable quote window"
    return ComparisonRow(
        entry,
        entry_at,
        entry_price,
        entry_source,
        post_touch_pullback(entry_price=entry_price, path=path, close_time=close_time),
        ladders,
        flat_target(
            entry_price=entry_price,
            path=path,
            close_time=close_time,
            ten_quote=ten_quote,
        ),
        ten_quote.at if ten_quote else None,
        ten_quote.bid if ten_quote else None,
        sum(
            1
            for halt in halts
            if halt.last_print_at < (ten_quote.at if ten_quote else close_time)
            and halt.reopen_print_at > entry_at
        ),
        gradable,
        reason,
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


def seconds(value: Decimal | None) -> str:
    if value is None:
        return "-"
    if Decimal("0") < value < Decimal("1"):
        return "<1s"
    return f"{value:.0f}s"


def render_exit(result: ExitResult, *, gradable: bool) -> str:
    if not gradable:
        return "UNANSWERABLE"
    if result.return_pct is None:
        return result.answer or "UNANSWERABLE"
    return (
        f"{result.outcome} {clock(result.exit_at)} "
        f"{money(result.exit_price)} {pct(result.return_pct)}"
    )


def difference(left: ExitResult, right: ExitResult, *, gradable: bool) -> Decimal | None:
    if not gradable or left.return_pct is None or right.return_pct is None:
        return None
    return left.return_pct - right.return_pct


def render_pullbacks(rows: Sequence[ComparisonRow], title: str) -> str:
    lines = [
        title,
        "",
        "| name | n | first +5% | touch bid | pullback ref | deepest pullback | at | recovery | duration | came back | halts | 10:00 |",
        "|---|---:|---|---:|---:|---:|---|---|---:|---|---:|---:|",
    ]
    for row in rows:
        pullback = row.pullback
        deepest = (
            f"{points(-pullback.giveback_points)} / {pct(pullback.giveback_pct)}"
            if pullback.giveback_points is not None
            else pullback.answer
        )
        lines.append(
            f"| {row.entry.symbol} {row.entry.day:%m-%d} | {'1/1' if row.gradable else '0/1'} | "
            f"{clock(pullback.touch_at)} | {money(pullback.touch_price)} | "
            f"{money(pullback.reference_price)} | {deepest} | "
            f"{clock(pullback.trough_at)} | {clock(pullback.recovered_at)} | "
            f"{seconds(pullback.duration_seconds)} | "
            f"{'YES' if pullback.recovered_at else 'NO'} | {row.halt_count} | "
            f"{money(row.ten_price)} @ {clock(row.ten_at)} |"
        )
        if not row.gradable:
            lines.append(
                f"| {row.entry.symbol} note | 0/1 | {row.gradability_reason} | | | | | | | | | |"
            )
    return "\n".join(lines)


def render_pullback_summary(rows: Sequence[ComparisonRow], title: str) -> str:
    measured = [
        row.pullback for row in rows if row.gradable and row.pullback.giveback_points is not None
    ]
    recovered = [item for item in measured if item.recovered_at is not None]
    givebacks = [item.giveback_points for item in measured if item.giveback_points is not None]
    durations = [item.duration_seconds for item in recovered if item.duration_seconds is not None]
    return "\n".join(
        [
            title,
            "",
            "| n measured | came back | never came back | median pullback | worst pullback | median recovery | longest recovery |",
            "|---:|---:|---:|---:|---:|---:|---:|",
            f"| {len(measured)}/{len(rows)} | {len(recovered)}/{len(measured)} | "
            f"{len(measured) - len(recovered)}/{len(measured)} | "
            f"{points(-median(givebacks)) if givebacks else '-'} | "
            f"{points(-max(givebacks)) if givebacks else '-'} | "
            f"{seconds(median(durations)) if durations else '-'} | "
            f"{seconds(max(durations)) if durations else '-'} |",
        ]
    )


def render_comparison(rows: Sequence[ComparisonRow], title: str) -> str:
    lines = [
        title,
        "",
        "| name | n | arm | floor | trail | armed | ladder exit | flat +5% | ladder-flat | 10:00 |",
        "|---|---:|---|---:|---:|---|---|---|---:|---:|",
    ]
    for row in rows:
        for result in row.ladders:
            parts = result.rule.split("-")
            arm = parts[0] if result.rule.startswith(ARM_MODES) else "-"
            floor_name = parts[1] if len(parts) == 3 else ""
            trail_name = parts[2] if len(parts) == 3 else ""
            lines.append(
                f"| {row.entry.symbol} {row.entry.day:%m-%d} | {'1/1' if row.gradable else '0/1'} | "
                f"{arm} | {floor_name.removeprefix('F')}% | {trail_name.removeprefix('T')}pp | "
                f"{clock(result.armed_at)} | {render_exit(result, gradable=row.gradable)} | "
                f"{render_exit(row.flat_five, gradable=row.gradable)} | "
                f"{pct(difference(result, row.flat_five, gradable=row.gradable))} | "
                f"{money(row.ten_price)} @ {clock(row.ten_at)} |"
            )
    return "\n".join(lines)


def render_summary(rows: Sequence[ComparisonRow], title: str) -> str:
    lines = [
        title,
        "",
        "| arm | floor | trail | n | ladder total | flat total | difference | touch-without-close cost |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    gradable = [row for row in rows if row.gradable]
    for arm_mode in ARM_MODES:
        for floor in INITIAL_FLOORS:
            for trail in TRAIL_DISTANCES:
                selected = [
                    next(
                        result
                        for result in row.ladders
                        if result.rule == f"{arm_mode}-F{floor.normalize()}-T{trail.normalize()}"
                    )
                    for row in gradable
                ]
                ladder_total = sum(
                    (result.return_pct or Decimal("0") for result in selected), Decimal("0")
                )
                flat_total = sum(
                    (row.flat_five.return_pct or Decimal("0") for row in gradable), Decimal("0")
                )
                touch_without_close = sum(
                    1
                    for row, result in zip(gradable, selected, strict=True)
                    if row.pullback.touch_at is not None and result.armed_at is None
                )
                lines.append(
                    f"| {arm_mode} | +{floor}% | {trail}pp | {len(gradable)}/{len(rows)} | "
                    f"{pct(ladder_total)} | {pct(flat_total)} | {pct(ladder_total - flat_total)} | "
                    f"{touch_without_close}/{len(gradable)} |"
                )
    return "\n".join(lines)


def render_close_arm_cost(rows: Sequence[ComparisonRow], title: str) -> str:
    missed = [
        row
        for row in rows
        if row.gradable
        and row.pullback.touch_at is not None
        and not any(
            result.armed_at is not None and result.rule.startswith("MINUTE_CLOSE")
            for result in row.ladders
        )
    ]
    lines = [
        title,
        "",
        "| name | n | +5% touch | minute close arm | 10:00 return | cost vs flat +5% |",
        "|---|---:|---:|---|---:|---:|",
    ]
    for row in missed:
        lines.append(
            f"| {row.entry.symbol} {row.entry.day:%m-%d} | 1/1 | "
            f"{pct(row.flat_five.return_pct)} | NEVER | "
            f"{pct(percent(row.ten_price, row.entry_price))} | "
            f"{pct(percent(row.ten_price, row.entry_price) - row.flat_five.return_pct)} |"
        )
    total_cost = sum(
        (
            percent(row.ten_price, row.entry_price) - row.flat_five.return_pct
            for row in missed
            if percent(row.ten_price, row.entry_price) is not None
            and row.flat_five.return_pct is not None
        ),
        Decimal("0"),
    )
    lines.append(
        f"| Total | {len(missed)}/{len([row for row in rows if row.gradable])} | - | - | - | {pct(total_cost)} |"
    )
    return "\n".join(lines)


def write_csv(path: Path, rows: Sequence[ComparisonRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "day",
        "symbol",
        "group",
        "entry_time_et",
        "entry_price",
        "entry_source",
        "first_five_at_et",
        "first_five_bid",
        "pullback_reference_bid",
        "pullback_started_at_et",
        "pullback_giveback_points",
        "pullback_giveback_pct",
        "pullback_trough_at_et",
        "recovered_at_et",
        "pullback_duration_seconds",
        "came_back",
        "halt_count",
        "gradable",
        "gradability_reason",
        "rule",
        "armed_at_et",
        "exit_at_et",
        "exit_price",
        "return_pct",
        "flat_return_pct",
        "difference_vs_flat",
        "price_at_1000",
        "price_at_1000_time_et",
        "qualification",
    )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            for result in row.ladders:
                writer.writerow(
                    {
                        "day": row.entry.day,
                        "symbol": row.entry.symbol,
                        "group": row.entry.group,
                        "entry_time_et": clock(row.entry_at),
                        "entry_price": row.entry_price or "",
                        "entry_source": row.entry_source,
                        "first_five_at_et": clock(row.pullback.touch_at),
                        "first_five_bid": row.pullback.touch_price or "",
                        "pullback_reference_bid": row.pullback.reference_price or "",
                        "pullback_started_at_et": clock(row.pullback.started_at),
                        "pullback_giveback_points": row.pullback.giveback_points or "",
                        "pullback_giveback_pct": row.pullback.giveback_pct or "",
                        "pullback_trough_at_et": clock(row.pullback.trough_at),
                        "recovered_at_et": clock(row.pullback.recovered_at),
                        "pullback_duration_seconds": row.pullback.duration_seconds or "",
                        "came_back": row.pullback.recovered_at is not None,
                        "halt_count": row.halt_count,
                        "gradable": row.gradable,
                        "gradability_reason": row.gradability_reason,
                        "rule": result.rule,
                        "armed_at_et": clock(result.armed_at),
                        "exit_at_et": clock(result.exit_at),
                        "exit_price": result.exit_price or "",
                        "return_pct": result.return_pct or "",
                        "flat_return_pct": row.flat_five.return_pct or "",
                        "difference_vs_flat": difference(
                            result, row.flat_five, gradable=row.gradable
                        )
                        or "",
                        "price_at_1000": row.ten_price or "",
                        "price_at_1000_time_et": clock(row.ten_at),
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
    print(DISCLOSURE)
    print(
        "Executable price is NBBO bid. Halts use the shared 285-second print-gap detector; "
        "halted quotes cannot arm, update a high, trigger a floor, or price an exit. The first "
        "executable quote after reopening resumes evaluation."
    )
    print(
        "TOUCH arms on the first executable bid at +5%. MINUTE_CLOSE arms only at the next "
        "minute boundary when that completed minute's final executable bid is >= +5%. Floors "
        "start at +3% or +4%; thereafter they trail the post-arm high by 5pp or 3pp."
    )
    print(
        "After the first +5% touch, the pullback reference follows bids upward until the first "
        "downtick. Recovery is the first later executable bid strictly above that reference; "
        "duration is measured from the first downtick to recovery."
    )
    print("\n" + render_pullbacks(primary, "Post-+5% pullback: primary population"))
    print("\n\n" + render_pullback_summary(primary, "Primary pullback distribution"))
    print("\n\n" + render_comparison(primary, "Revised ladder comparison: primary population"))
    print("\n\n" + render_summary(primary, "Primary summary"))
    print("\n\n" + render_close_arm_cost(primary, "Cost of touch without a +5% minute close"))
    print("\n\nPPCB separate\n")
    print(render_pullbacks(separate, "Post-+5% pullback: PPCB"))
    print("\n\n" + render_pullback_summary(separate, "PPCB pullback distribution"))
    print("\n\n" + render_comparison(separate, "Revised ladder comparison: PPCB"))
    print("\n\n" + render_summary(separate, "PPCB summary"))
    if args.csv:
        write_csv(args.csv, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
