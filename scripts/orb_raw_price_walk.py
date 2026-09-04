#!/usr/bin/env python3
"""Extract a rule-free minute walk around a fixed opening-high break."""

from __future__ import annotations

import argparse
import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
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
)
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
)

EASTERN = ZoneInfo("America/New_York")
DISCLOSURE = "SIMULATED | NOT SIZE-QUALIFIED"
DEFAULT_SYMBOLS = ("DAIC", "VCIG", "MSS", "CRE", "YYGH")


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
class AtrBar:
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str


@dataclass(frozen=True)
class AtrSnapshot:
    state: str | None
    trail: Decimal | None
    source: str


@dataclass(frozen=True)
class MinuteRow:
    minute: datetime
    price: Decimal | None
    level_pct: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    spread_pct: Decimal | None
    halted: bool


@dataclass(frozen=True)
class SymbolWalk:
    symbol: str
    opening_high: Decimal | None
    break_at: datetime | None
    atr: AtrSnapshot
    rows: tuple[MinuteRow, ...]


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def at_et(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), EASTERN).astimezone(UTC)


def minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def dedupe_trades(rows: Iterable[TradePoint]) -> list[TradePoint]:
    seen: set[tuple[datetime, Decimal, int, str, str]] = set()
    result: list[TradePoint] = []
    for row in sorted(rows, key=lambda item: item.at):
        key = (row.at, row.price, row.size, row.exchange, row.conditions)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def opening_high(day: date, trades: Sequence[TradePoint]) -> Decimal | None:
    start = at_et(day, 9, 25)
    end = at_et(day, 9, 30)
    prices = [trade.price for trade in trades if start <= trade.at < end]
    return max(prices) if prices else None


def first_break(
    day: date, level: Decimal, trades: Sequence[TradePoint]
) -> datetime | None:
    start = at_et(day, 9, 30)
    end = at_et(day, 10, 1)
    return next(
        (trade.at for trade in trades if start <= trade.at < end and trade.price > level),
        None,
    )


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
        window.last_print_at < end and window.reopen_print_at > minute for window in halts
    )


def percent(value: Decimal | None, basis: Decimal | None) -> Decimal | None:
    if value is None or basis is None or basis <= 0:
        return None
    return (value / basis - Decimal("1")) * Decimal("100")


def spread_percent(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    midpoint = (bid + ask) / Decimal("2")
    return (ask - bid) / midpoint * Decimal("100") if midpoint > 0 else None


def minute_walk(
    *,
    day: date,
    level: Decimal,
    break_at: datetime,
    trades: Sequence[TradePoint],
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
) -> tuple[MinuteRow, ...]:
    start = minute_floor(break_at)
    final = at_et(day, 10, 0)
    rows: list[MinuteRow] = []
    minute = start
    while minute <= final:
        end = minute + timedelta(minutes=1)
        minute_trades = [trade for trade in trades if minute <= trade.at < end]
        minute_quotes = [quote for quote in quotes if minute <= quote.at < end]
        price = minute_trades[-1].price if minute_trades else None
        quote = minute_quotes[-1] if minute_quotes else None
        bid = quote.bid if quote is not None else None
        ask = quote.ask if quote is not None else None
        rows.append(
            MinuteRow(
                minute=minute,
                price=price,
                level_pct=percent(price, level),
                bid=bid,
                ask=ask,
                spread_pct=spread_percent(bid, ask),
                halted=minute_overlaps_halt(minute, halts),
            )
        )
        minute = end
    return tuple(rows)


def replay_atr_at_break(
    settings,
    symbol: str,
    break_at: datetime,
    bars: Sequence[AtrBar],
) -> AtrSnapshot:
    """Replay the deployed v2 ATR implementation through the break-minute close."""
    break_minute = minute_floor(break_at)
    eligible = [bar for bar in bars if bar.at <= break_minute]
    if not eligible:
        return AtrSnapshot(None, None, "UNAVAILABLE: no persisted Schwab bars")
    strategy = SchwabV2Strategy(settings)
    state = strategy.watchlist_state(symbol)
    signal = None
    for bar in eligible:
        signal = strategy._update_atr_state(  # noqa: SLF001
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
    if eligible[-1].at != break_minute:
        return AtrSnapshot(None, None, "UNAVAILABLE: break-minute Schwab bar missing")
    if signal is None:
        return AtrSnapshot(None, None, "UNAVAILABLE: ATR warmup incomplete")
    sources = "+".join(sorted({bar.source or "unknown" for bar in eligible}))
    return AtrSnapshot(
        str(signal["state"]).upper(),
        Decimal(str(signal["trail"])),
        "DERIVED: deployed v2 ATR replay on persisted Schwab 1m bars "
        f"(sources={sources}) at break-bar close",
    )


def load_market(
    session_factory, day: date, symbol: str
) -> tuple[list[TradePoint], list[QuotePoint]]:
    start = at_et(day, 9, 20)
    end = at_et(day, 16, 0)
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
                    "SELECT event_ts,price,size,exchange,conditions "
                    "FROM market_capture_trades "
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
                    "AND bid_price IS NOT NULL AND ask_price IS NOT NULL "
                    "ORDER BY event_ts,id"
                ),
                {"symbol": symbol, "start": start, "end": end},
            )
        ]
    return dedupe_trades(trades), quotes


def load_atr_bars(session_factory, day: date, symbol: str) -> list[AtrBar]:
    start = at_et(day, 4, 0)
    end = at_et(day, 10, 1)
    with session_factory() as session:
        return [
            AtrBar(
                at=utc(row[0]),
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=int(row[5] or 0),
                source=str(row[6] or ""),
            )
            for row in session.execute(
                text(
                    "SELECT bar_time,open_price,high_price,low_price,close_price,volume,source "
                    "FROM strategy_bar_history "
                    "WHERE strategy_code='schwab_1m_v2' AND symbol=:symbol "
                    "AND interval_secs=60 AND bar_time>=:start AND bar_time<:end "
                    "ORDER BY bar_time"
                ),
                {"symbol": symbol, "start": start, "end": end},
            )
        ]


def extract_symbol(session_factory, settings, day: date, symbol: str) -> SymbolWalk:
    trades, quotes = load_market(session_factory, day, symbol)
    level = opening_high(day, trades)
    if level is None:
        return SymbolWalk(
            symbol, None, None, AtrSnapshot(None, None, "UNAVAILABLE: no opening prints"), ()
        )
    break_at = first_break(day, level, trades)
    if break_at is None:
        return SymbolWalk(
            symbol, level, None, AtrSnapshot(None, None, "UNAVAILABLE: no break"), ()
        )
    halts = detect_halts(trades, quotes)
    atr = replay_atr_at_break(
        settings, symbol, break_at, load_atr_bars(session_factory, day, symbol)
    )
    return SymbolWalk(
        symbol=symbol,
        opening_high=level,
        break_at=break_at,
        atr=atr,
        rows=minute_walk(
            day=day,
            level=level,
            break_at=break_at,
            trades=trades,
            quotes=quotes,
            halts=halts,
        ),
    )


def money(value: Decimal | None) -> str:
    return "NO DATA" if value is None else f"${value:.4f}"


def pct(value: Decimal | None) -> str:
    return "NO DATA" if value is None else f"{value:+.2f}%"


def clock(value: datetime | None, seconds: bool = False) -> str:
    if value is None:
        return "NO DATA"
    return value.astimezone(EASTERN).strftime("%H:%M:%S" if seconds else "%H:%M")


def render(walks: Sequence[SymbolWalk]) -> str:
    lines = [
        DISCLOSURE,
        "Price = final trade print in the minute; bid/ask = final NBBO quote in the minute; "
        "spread % = (ask - bid) / midpoint. Missing minutes are retained.",
        "ATR = break-bar-close state from the deployed v2 ATR implementation replayed over "
        "persisted Schwab 1-minute bars; bar provenance is retained and it is the only "
        "derived field.",
    ]
    for walk in walks:
        lines.extend(["", walk.symbol])
        if walk.opening_high is None or walk.break_at is None:
            lines.append(
                f"Opening high: {money(walk.opening_high)} | Break: {clock(walk.break_at)} | "
                f"ATR: {walk.atr.source}"
            )
            continue
        atr_value = (
            f"{walk.atr.state} @ {money(walk.atr.trail)}"
            if walk.atr.state is not None
            else walk.atr.source
        )
        lines.extend(
            [
                f"Opening high: {money(walk.opening_high)} | Break: {clock(walk.break_at)} ET "
                f"({clock(walk.break_at, seconds=True)}) | ATR at break-bar close: {atr_value}",
                "",
                "| minute | price | % from level | bid | ask | spread % |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in walk.rows:
            minute_label = f"{clock(row.minute)} HALT" if row.halted else clock(row.minute)
            lines.append(
                "| "
                + " | ".join(
                    [
                        minute_label,
                        money(row.price) if row.price is not None else "NO TRADE",
                        pct(row.level_pct),
                        money(row.bid) if row.bid is not None else "NO QUOTE",
                        money(row.ask) if row.ask is not None else "NO QUOTE",
                        pct(row.spread_pct),
                    ]
                )
                + " |"
            )
    return "\n".join(lines)


def write_csv(path: Path, walks: Sequence[SymbolWalk]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "day",
                "symbol",
                "opening_high",
                "break_time_et",
                "atr_state",
                "atr_trail",
                "atr_source",
                "minute_et",
                "price",
                "pct_from_level",
                "bid",
                "ask",
                "spread_pct",
                "halted",
                "qualification",
            ),
        )
        writer.writeheader()
        for walk in walks:
            for row in walk.rows:
                writer.writerow(
                    {
                        "day": row.minute.astimezone(EASTERN).date().isoformat(),
                        "symbol": walk.symbol,
                        "opening_high": walk.opening_high,
                        "break_time_et": clock(walk.break_at, seconds=True),
                        "atr_state": walk.atr.state or "",
                        "atr_trail": walk.atr.trail or "",
                        "atr_source": walk.atr.source,
                        "minute_et": clock(row.minute),
                        "price": row.price or "",
                        "pct_from_level": row.level_pct if row.level_pct is not None else "",
                        "bid": row.bid or "",
                        "ask": row.ask or "",
                        "spread_pct": row.spread_pct if row.spread_pct is not None else "",
                        "halted": row.halted,
                        "qualification": DISCLOSURE,
                    }
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--csv", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    session_factory = build_session_factory(settings)
    walks = [
        extract_symbol(session_factory, settings, args.date, symbol.upper())
        for symbol in args.symbols
    ]
    print(render(walks))
    if args.csv is not None:
        write_csv(args.csv, walks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
