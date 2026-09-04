#!/usr/bin/env python3
"""Sweep -1% through -8% stops against flat +5% and the deployed ATR SELL."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from sqlalchemy import text

from orb_exit_ladder_comparison import (
    DISCLOSURE,
    PRIMARY_POPULATION,
    SEPARATE_POPULATION,
    PopulationEntry,
    QuotePoint,
    TradePoint,
    at_et,
    clock,
    detect_halts,
    executable_quotes,
    load_market,
    money,
    pct,
    percent,
    resolve_entry_price,
)
from orb_momentum_turn_report import (
    DB_SEED_BAR_LIMIT,
    BarPoint,
    admissible_seed_bars,
    assert_indicator_parameters,
    utc,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.market_halts import HaltWindow
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

STOP_LEVELS = tuple(Decimal(value) for value in range(1, 9))
TARGET_PCT = Decimal("5")
SESSION_END = time(16, 0)


@dataclass(frozen=True)
class AtrTrigger:
    bar_minute: datetime
    decision_at: datetime
    exit_at: datetime | None
    exit_price: Decimal | None


@dataclass(frozen=True)
class StopResult:
    stop_pct: Decimal
    target_at: datetime | None
    target_price: Decimal | None
    stop_at: datetime | None
    stop_price: Decimal | None
    atr_bar_minute: datetime | None
    atr_exit_at: datetime | None
    atr_exit_price: Decimal | None
    exit_rule: str
    exit_at: datetime | None
    exit_price: Decimal | None
    return_pct: Decimal | None
    recovered_after_stop: bool | None
    stop_on_reopen: bool
    target_answerable: bool
    stop_answerable: bool
    answer: str = ""


@dataclass(frozen=True)
class SweepRow:
    entry: PopulationEntry
    entry_at: datetime
    entry_price: Decimal | None
    entry_source: str
    results: tuple[StopResult, ...]
    quote_complete: bool


def load_bars(session_factory, entry: PopulationEntry) -> list[BarPoint]:
    session_start = at_et(entry.day, time(4, 0))
    session_end = at_et(entry.day, SESSION_END)
    with session_factory() as session:
        seed_newest_first = [
            BarPoint(
                utc(row[0]),
                Decimal(str(row[1])),
                Decimal(str(row[2])),
                Decimal(str(row[3])),
                Decimal(str(row[4])),
                int(row[5] or 0),
                str(row[6] or ""),
            )
            for row in session.execute(
                text(
                    "SELECT bar_time,open_price,high_price,low_price,close_price,volume,source "
                    "FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' "
                    "AND symbol=:symbol AND interval_secs=60 AND bar_time<:start "
                    "ORDER BY bar_time DESC LIMIT :limit"
                ),
                {
                    "symbol": entry.symbol,
                    "start": session_start,
                    "limit": DB_SEED_BAR_LIMIT,
                },
            )
        ]
        seed = admissible_seed_bars(session, seed_newest_first, entry.day)
        current = [
            BarPoint(
                utc(row[0]),
                Decimal(str(row[1])),
                Decimal(str(row[2])),
                Decimal(str(row[3])),
                Decimal(str(row[4])),
                int(row[5] or 0),
                str(row[6] or ""),
            )
            for row in session.execute(
                text(
                    "SELECT bar_time,open_price,high_price,low_price,close_price,volume,source "
                    "FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' "
                    "AND symbol=:symbol AND interval_secs=60 AND bar_time>=:start "
                    "AND bar_time<:end ORDER BY bar_time"
                ),
                {"symbol": entry.symbol, "start": session_start, "end": session_end},
            )
        ]
    return seed + current


def replay_atr_triggers(
    *,
    settings,
    entry: PopulationEntry,
    bars: Sequence[BarPoint],
    path: Sequence[QuotePoint],
) -> list[AtrTrigger]:
    strategy = SchwabV2Strategy(settings)
    assert_indicator_parameters(strategy)
    state = strategy.watchlist_state(entry.symbol)
    entry_at = at_et(entry.day, entry.entry_time)
    triggers: list[AtrTrigger] = []
    for bar in bars:
        atr = strategy._update_atr_state(  # noqa: SLF001
            state,
            OHLCVBar(
                timestamp_ms=int(bar.at.timestamp() * 1000),
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=bar.volume,
            ),
            observation_phase="replay",
        )
        decision_at = bar.at + timedelta(minutes=1)
        if decision_at <= entry_at or not atr or atr.get("flip") != "SELL":
            continue
        exit_quote = next((quote for quote in path if quote.at >= decision_at), None)
        triggers.append(
            AtrTrigger(
                bar_minute=bar.at,
                decision_at=decision_at,
                exit_at=exit_quote.at if exit_quote else None,
                exit_price=exit_quote.bid if exit_quote else None,
            )
        )
    return triggers


def quote_after_reopen(
    quote: QuotePoint | None,
    path: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> bool:
    if quote is None:
        return False
    for halt in halts:
        first = next((item for item in path if item.at >= halt.reopen_print_at), None)
        if first is not None and first.at == quote.at:
            return True
    return False


def evaluate_stop(
    *,
    entry_price: Decimal,
    stop_pct: Decimal,
    path: Sequence[QuotePoint],
    atr_triggers: Sequence[AtrTrigger],
    halts: Sequence[HaltWindow],
    quote_complete: bool = True,
) -> StopResult:
    target_price = entry_price * Decimal("1.05")
    stop_price = entry_price * (Decimal("1") - stop_pct / Decimal("100"))
    target = next((quote for quote in path if quote.bid >= target_price), None)
    stop = next((quote for quote in path if quote.bid <= stop_price), None)
    atr = next((item for item in atr_triggers if item.exit_at is not None), None)
    candidates: list[tuple[datetime, str, Decimal, object]] = []
    if target is not None:
        candidates.append((target.at, "+5%", target.bid, target))
    if stop is not None:
        candidates.append((stop.at, "STOP", stop.bid, stop))
    if atr is not None and atr.exit_at is not None and atr.exit_price is not None:
        candidates.append((atr.exit_at, "ATR", atr.exit_price, atr))
    if not candidates:
        return StopResult(
            stop_pct,
            target.at if target else None,
            target.bid if target else None,
            stop.at if stop else None,
            stop.bid if stop else None,
            atr_triggers[0].bar_minute if atr_triggers else None,
            atr_triggers[0].exit_at if atr_triggers else None,
            atr_triggers[0].exit_price if atr_triggers else None,
            "UNANSWERABLE",
            None,
            None,
            None,
            None,
            False,
            quote_complete,
            quote_complete,
            "no target, stop, or executable ATR exit observed by 16:00",
        )
    first_at = min(item[0] for item in candidates)
    first = [item for item in candidates if item[0] == first_at]
    if len(first) != 1:
        return StopResult(
            stop_pct,
            target.at if target else None,
            target.bid if target else None,
            stop.at if stop else None,
            stop.bid if stop else None,
            atr.bar_minute if atr else None,
            atr.exit_at if atr else None,
            atr.exit_price if atr else None,
            "UNANSWERABLE",
            None,
            None,
            None,
            None,
            False,
            target is not None or quote_complete,
            stop is not None or quote_complete,
            f"timestamp tie at {clock(first_at)} cannot be ordered across feeds",
        )
    exit_at, rule, exit_price, source = first[0]
    recovered = None
    if rule == "STOP":
        later_target = any(quote.at > exit_at and quote.bid >= target_price for quote in path)
        recovered = True if later_target else False if quote_complete else None
    return StopResult(
        stop_pct,
        target.at if target else None,
        target.bid if target else None,
        stop.at if stop else None,
        stop.bid if stop else None,
        atr.bar_minute if atr else None,
        atr.exit_at if atr else None,
        atr.exit_price if atr else None,
        rule,
        exit_at,
        exit_price,
        percent(exit_price, entry_price),
        recovered,
        rule == "STOP" and quote_after_reopen(source, path, halts),
        target is not None or quote_complete,
        stop is not None or quote_complete,
    )


def evaluate_entry(
    *,
    settings,
    session_factory,
    entry: PopulationEntry,
    trades: Sequence[TradePoint],
    quotes: Sequence[QuotePoint],
) -> SweepRow:
    entry_at = at_et(entry.day, entry.entry_time)
    halts = detect_halts(trades, quotes)
    entry_price, source = resolve_entry_price(entry, entry_at, quotes, halts)
    path = executable_quotes(quotes, halts, entry_at)
    quote_complete = bool(path and path[-1].at >= at_et(entry.day, time(15, 59)))
    if entry_price is None:
        return SweepRow(entry, entry_at, None, source, (), quote_complete)
    atr = replay_atr_triggers(
        settings=settings,
        entry=entry,
        bars=load_bars(session_factory, entry),
        path=path,
    )
    return SweepRow(
        entry,
        entry_at,
        entry_price,
        source,
        tuple(
            evaluate_stop(
                entry_price=entry_price,
                stop_pct=stop,
                path=path,
                atr_triggers=atr,
                halts=halts,
                quote_complete=quote_complete,
            )
            for stop in STOP_LEVELS
        ),
        quote_complete,
    )


def render_result(result: StopResult) -> str:
    target = (
        f"Y {clock(result.target_at)}"
        if result.target_at
        else "N"
        if result.target_answerable
        else "UNKNOWN"
    )
    stop = (
        f"Y {clock(result.stop_at)}"
        if result.stop_at
        else "N"
        if result.stop_answerable
        else "UNKNOWN"
    )
    if result.exit_rule == "UNANSWERABLE":
        return f"T:{target}; S:{stop}; UNANSWERABLE ({result.answer})"
    label = result.exit_rule + ("@REOPEN" if result.stop_on_reopen else "")
    atr = (
        f"; ATR {clock(result.atr_bar_minute)} bar/{clock(result.atr_exit_at)} bid"
        if result.atr_bar_minute
        else ""
    )
    recovery = (
        "; later +5:Y"
        if result.exit_rule == "STOP" and result.recovered_after_stop
        else "; later +5:N"
        if result.exit_rule == "STOP" and result.recovered_after_stop is False
        else "; later +5:UNANSWERABLE"
        if result.exit_rule == "STOP"
        else ""
    )
    return (
        f"T:{target}; S:{stop}; {label} {clock(result.exit_at)} "
        f"{money(result.exit_price)} {pct(result.return_pct)}{recovery}{atr}"
    )


def render_rows(rows: Sequence[SweepRow], title: str) -> str:
    lines = [
        title,
        "",
        "| name | n | -1% | -2% | -3% | -4% | -5% | -6% | -7% | -8% |",
        "|---|---:|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        cells = [render_result(result) for result in row.results]
        n = f"{sum(result.return_pct is not None for result in row.results)}/8"
        lines.append(
            f"| {row.entry.symbol} {row.entry.day:%m-%d} | {n} | " + " | ".join(cells) + " |"
        )
        if not row.quote_complete and any(result.target_at is None for result in row.results):
            lines.append(
                f"| {row.entry.symbol} note | 0/1 full-day quote coverage | "
                + " | ".join(["quotes end before 16:00"] * 8)
                + " |"
            )
    return "\n".join(lines)


def render_summary(rows: Sequence[SweepRow], title: str) -> str:
    lines = [
        title,
        "",
        "| stop | n | pooled | stopped before +5 | stopped then later +5 | names | recovery unknown | ATR exits |",
        "|---:|---:|---:|---:|---:|---|---|---:|",
    ]
    for index, stop in enumerate(STOP_LEVELS):
        results = [(row, row.results[index]) for row in rows if row.results]
        gradable = [(row, result) for row, result in results if result.return_pct is not None]
        stopped = [(row, result) for row, result in gradable if result.exit_rule == "STOP"]
        recovery_gradable = [
            (row, result) for row, result in stopped if result.recovered_after_stop is not None
        ]
        recovered = [
            f"{row.entry.symbol} {row.entry.day:%m-%d}"
            for row, result in recovery_gradable
            if result.recovered_after_stop is True
        ]
        recovery_unknown = [
            f"{row.entry.symbol} {row.entry.day:%m-%d}"
            for row, result in stopped
            if result.recovered_after_stop is None
        ]
        atr_count = sum(result.exit_rule == "ATR" for _, result in gradable)
        total = sum((result.return_pct for _, result in gradable), Decimal("0"))
        lines.append(
            f"| -{stop}% | {len(gradable)}/{len(rows)} | {pct(total)} | "
            f"{len(stopped)}/{len(gradable)} | {len(recovered)}/{len(recovery_gradable)} | "
            f"{', '.join(recovered) or '-'} | {', '.join(recovery_unknown) or '-'} | "
            f"{atr_count}/{len(gradable)} |"
        )
    return "\n".join(lines)


def write_csv(path: Path, rows: Sequence[SweepRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "day",
        "symbol",
        "population",
        "entry_time_et",
        "entry_price",
        "entry_source",
        "stop_pct",
        "target_at_et",
        "target_price",
        "stop_at_et",
        "stop_price",
        "atr_bar_minute_et",
        "atr_exit_at_et",
        "atr_exit_price",
        "exit_rule",
        "exit_at_et",
        "exit_price",
        "return_pct",
        "recovered_after_stop",
        "target_answerable",
        "stop_answerable",
        "stop_on_reopen",
        "answer",
        "qualification",
    )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            for result in row.results:
                writer.writerow(
                    {
                        "day": row.entry.day,
                        "symbol": row.entry.symbol,
                        "population": row.entry.group,
                        "entry_time_et": row.entry.entry_time,
                        "entry_price": row.entry_price or "",
                        "entry_source": row.entry_source,
                        "stop_pct": -result.stop_pct,
                        "target_at_et": clock(result.target_at),
                        "target_price": result.target_price or "",
                        "stop_at_et": clock(result.stop_at),
                        "stop_price": result.stop_price or "",
                        "atr_bar_minute_et": clock(result.atr_bar_minute),
                        "atr_exit_at_et": clock(result.atr_exit_at),
                        "atr_exit_price": result.atr_exit_price or "",
                        "exit_rule": result.exit_rule,
                        "exit_at_et": clock(result.exit_at),
                        "exit_price": result.exit_price or "",
                        "return_pct": result.return_pct or "",
                        "recovered_after_stop": result.recovered_after_stop,
                        "target_answerable": result.target_answerable,
                        "stop_answerable": result.stop_answerable,
                        "stop_on_reopen": result.stop_on_reopen,
                        "answer": result.answer,
                        "qualification": DISCLOSURE,
                    }
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    session_factory = build_session_factory(settings)
    rows = []
    for entry in (*PRIMARY_POPULATION, *SEPARATE_POPULATION):
        trades, quotes = load_market(session_factory, entry.day, entry.symbol)
        rows.append(
            evaluate_entry(
                settings=settings,
                session_factory=session_factory,
                entry=entry,
                trades=trades,
                quotes=quotes,
            )
        )
    primary = [row for row in rows if row.entry.group == "PRIMARY"]
    separate = [row for row in rows if row.entry.group == "PPCB_SEPARATE"]
    print(DISCLOSURE)
    print(
        "Target and stops use timestamped executable NBBO bids. A target/stop quote inside a "
        "confirmed halt is ineligible; a gap-through stop exits at the first executable reopening "
        "bid. ATR SELL is computed by deployed v2 WILDERS(5), factor 3.5 on Schwab 1-minute bars; "
        "the bar label is shown and its decision becomes actionable at the following minute boundary. "
        "10:00 blocks new entries only; open rows continue through 16:00."
    )
    print("\n\n" + render_rows(primary, "Primary population"))
    print("\n\n" + render_summary(primary, "Primary summary"))
    print("\n\n" + render_rows(separate, "PPCB separate"))
    print("\n\n" + render_summary(separate, "PPCB summary"))
    if args.csv:
        write_csv(args.csv, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
