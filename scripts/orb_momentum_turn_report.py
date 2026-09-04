#!/usr/bin/env python3
"""Report Schwab 1-minute MACD, volume, and ATR state for fixed ORB names."""

from __future__ import annotations

import argparse
import csv
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.market_halts import HALT_MIN_PRINT_GAP, HaltWindow, confirmed_halt_window
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
    V2Indicators,
)

EASTERN = ZoneInfo("America/New_York")
DISCLOSURE = "SIMULATED | NOT SIZE-QUALIFIED"
MACD_PARAMETERS = (12, 26, 9)
ATR_PERIOD = 5
ATR_FACTOR = Decimal("3.5")
VOLUME_AVERAGE_BARS = 20
DB_SEED_BAR_LIMIT = 250
DB_SEED_GAP_PROBE_MIN = timedelta(hours=2)


@dataclass(frozen=True)
class PopulationName:
    day: date
    symbol: str
    entry_time: time
    entry_price: Decimal
    group: str


POPULATION = (
    PopulationName(date(2026, 8, 25), "DAIC", time(9, 30, 21), Decimal("3.66"), "A"),
    PopulationName(date(2026, 8, 28), "QNRX", time(9, 30, 10), Decimal("7.25"), "A"),
    PopulationName(date(2026, 9, 1), "SSM", time(9, 37, 41), Decimal("3.87"), "A"),
    PopulationName(date(2026, 9, 2), "VIVK", time(9, 30, 29), Decimal("1.11"), "B"),
    PopulationName(date(2026, 8, 26), "CRE", time(9, 30, 42), Decimal("6.37"), "B"),
    PopulationName(date(2026, 8, 31), "YDDL", time(9, 34, 26), Decimal("2.49"), "B"),
    PopulationName(date(2026, 8, 31), "AEHL", time(9, 30, 13), Decimal("6.76"), "B"),
    PopulationName(date(2026, 8, 26), "DAIC", time(9, 30, 4), Decimal("6.11"), "B"),
    PopulationName(date(2026, 8, 31), "RDHL", time(9, 30, 53), Decimal("1.39"), "B"),
)


@dataclass(frozen=True)
class BarPoint:
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str


@dataclass(frozen=True)
class TradePoint:
    at: datetime
    price: Decimal


@dataclass(frozen=True)
class QuotePoint:
    at: datetime
    bid: Decimal


@dataclass(frozen=True)
class IndicatorRow:
    minute: datetime
    close: Decimal | None
    return_pct: Decimal | None
    macd: Decimal | None
    signal: Decimal | None
    histogram: Decimal | None
    macd_cross_down: bool
    volume: int | None
    average_volume: Decimal | None
    volume_ratio: Decimal | None
    atr_state: str | None
    atr_level: Decimal | None
    atr_sell_flip: bool
    halted: bool
    quote_available: bool


@dataclass(frozen=True)
class NameReport:
    name: PopulationName
    rows: tuple[IndicatorRow, ...]
    high_at: datetime | None
    high_price: Decimal | None
    low_at: datetime | None
    low_price: Decimal | None
    macd_cross_at: datetime | None
    atr_flip_at: datetime | None
    cross_before_low: bool | None
    complete_through_ten: bool


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def at_et(day: date, value: time) -> datetime:
    return datetime.combine(day, value, EASTERN).astimezone(UTC)


def percent(value: Decimal | None, basis: Decimal) -> Decimal | None:
    if value is None or basis <= 0:
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


def minute_overlaps_halt(minute: datetime, halts: Sequence[HaltWindow]) -> bool:
    boundary = minute + timedelta(minutes=1)
    return any(halt.last_print_at < boundary and halt.reopen_print_at > minute for halt in halts)


def has_intervening_strategy_session(session, older: datetime, newer: datetime) -> bool:
    """Match v2's zero-tolerance missed-session test for historical replay."""
    older_date = older.astimezone(EASTERN).date()
    newer_date = newer.astimezone(EASTERN).date()
    lo = datetime.combine(older_date + timedelta(days=1), time.min, EASTERN)
    hi = datetime.combine(newer_date, time.min, EASTERN)
    if lo >= hi:
        return False
    return bool(
        session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM strategy_bar_history "
                "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 "
                "AND bar_time>=:lo AND bar_time<:hi)"
            ),
            {"lo": lo, "hi": hi},
        ).scalar()
    )


def admissible_seed_bars(
    session,
    seed_newest_first: Sequence[BarPoint],
    target_day: date,
) -> list[BarPoint]:
    """Apply v2 continuity, substituting the historical target for wall-clock today."""
    if not seed_newest_first:
        return []
    target = at_et(target_day, time.min)
    if has_intervening_strategy_session(session, seed_newest_first[0].at, target):
        return []
    kept: list[BarPoint] = []
    previous: BarPoint | None = None
    for row in seed_newest_first:
        if (
            previous is not None
            and previous.at - row.at >= DB_SEED_GAP_PROBE_MIN
            and has_intervening_strategy_session(session, row.at, previous.at)
        ):
            break
        kept.append(row)
        previous = row
    kept.reverse()
    return kept


def assert_indicator_parameters(strategy: SchwabV2Strategy) -> None:
    actual_macd = (
        strategy.cfg.macd_fast_length,
        strategy.cfg.macd_slow_length,
        strategy.cfg.macd_signal_length,
    )
    if actual_macd != MACD_PARAMETERS:
        raise RuntimeError(
            f"MACD parameter mismatch: expected {MACD_PARAMETERS}, got {actual_macd}"
        )
    if strategy._atr_period != ATR_PERIOD:  # noqa: SLF001
        raise RuntimeError(
            f"ATR period mismatch: expected {ATR_PERIOD}, got {strategy._atr_period}"  # noqa: SLF001
        )
    if Decimal(str(strategy._atr_factor)) != ATR_FACTOR:  # noqa: SLF001
        raise RuntimeError(
            f"ATR factor mismatch: expected {ATR_FACTOR}, got {strategy._atr_factor}"  # noqa: SLF001
        )


def replay_rows(
    *,
    settings,
    name: PopulationName,
    bars: Sequence[BarPoint],
    halts: Sequence[HaltWindow],
    quotes: Sequence[QuotePoint],
) -> tuple[IndicatorRow, ...]:
    strategy = SchwabV2Strategy(settings)
    assert_indicator_parameters(strategy)
    state = strategy.watchlist_state(name.symbol)
    closes: deque[float] = deque(maxlen=300)
    volumes: deque[int] = deque(maxlen=300)
    computed: dict[datetime, IndicatorRow] = {}
    previous_macd: Decimal | None = None
    previous_signal: Decimal | None = None
    for bar in bars:
        closes.append(float(bar.close))
        volumes.append(bar.volume)
        macd_result = V2Indicators.macd(closes, *MACD_PARAMETERS)
        macd = signal = histogram = None
        if macd_result is not None:
            macd, signal, histogram = (Decimal(str(value)) for value in macd_result)
        cross_down = (
            previous_macd is not None
            and previous_signal is not None
            and macd is not None
            and signal is not None
            and previous_macd >= previous_signal
            and macd < signal
        )
        if macd is not None and signal is not None:
            previous_macd, previous_signal = macd, signal
        average = V2Indicators.avg_volume(list(volumes), VOLUME_AVERAGE_BARS)
        average_volume = Decimal(str(average)) if average is not None else None
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
        if at_et(name.day, time(9, 25)) <= bar.at <= at_et(name.day, time(10, 0)):
            quote_available = any(
                bar.at <= quote.at < bar.at + timedelta(minutes=1) and quote.bid > 0
                for quote in quotes
            )
            computed[bar.at] = IndicatorRow(
                minute=bar.at,
                close=bar.close,
                return_pct=percent(bar.close, name.entry_price),
                macd=macd,
                signal=signal,
                histogram=histogram,
                macd_cross_down=cross_down,
                volume=bar.volume,
                average_volume=average_volume,
                volume_ratio=(Decimal(bar.volume) / average_volume)
                if average_volume and average_volume > 0
                else None,
                atr_state=str(atr["state"]).upper() if atr else None,
                atr_level=Decimal(str(atr["trail"])) if atr else None,
                atr_sell_flip=bool(atr and atr.get("flip") == "SELL"),
                halted=minute_overlaps_halt(bar.at, halts),
                quote_available=quote_available,
            )

    minute = at_et(name.day, time(9, 25))
    end = at_et(name.day, time(10, 0))
    result: list[IndicatorRow] = []
    while minute <= end:
        row = computed.get(minute)
        if row is None:
            row = IndicatorRow(
                minute=minute,
                close=None,
                return_pct=None,
                macd=None,
                signal=None,
                histogram=None,
                macd_cross_down=False,
                volume=None,
                average_volume=None,
                volume_ratio=None,
                atr_state=None,
                atr_level=None,
                atr_sell_flip=False,
                halted=minute_overlaps_halt(minute, halts),
                quote_available=any(
                    minute <= quote.at < minute + timedelta(minutes=1) and quote.bid > 0
                    for quote in quotes
                ),
            )
        result.append(row)
        minute += timedelta(minutes=1)
    return tuple(result)


def summarize(name: PopulationName, rows: Sequence[IndicatorRow]) -> NameReport:
    entry_minute = at_et(name.day, name.entry_time).replace(second=0, microsecond=0)
    available = [row for row in rows if row.minute >= entry_minute and row.close is not None]
    high = max(available, key=lambda row: row.close, default=None)
    after_high = [row for row in available if high is not None and row.minute >= high.minute]
    low = min(after_high, key=lambda row: row.close, default=None)
    cross = next((row for row in available if row.macd_cross_down), None)
    atr_flip = next((row for row in available if row.atr_sell_flip), None)
    assessed = [row for row in rows if row.minute >= entry_minute]
    complete = bool(assessed) and all(row.close is not None or row.halted for row in assessed)
    return NameReport(
        name=name,
        rows=tuple(rows),
        high_at=high.minute if high else None,
        high_price=high.close if high else None,
        low_at=low.minute if low else None,
        low_price=low.close if low else None,
        macd_cross_at=cross.minute if cross else None,
        atr_flip_at=atr_flip.minute if atr_flip else None,
        cross_before_low=(cross.minute < low.minute) if complete and cross and low else None,
        complete_through_ten=complete,
    )


def load_data(session_factory, name: PopulationName):
    bars_start = at_et(name.day, time(4, 0))
    market_start = at_et(name.day, time(9, 20))
    bars_end = at_et(name.day, time(10, 1))
    market_end = at_et(name.day, time(16, 1))
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
                    "symbol": name.symbol,
                    "start": bars_start,
                    "limit": DB_SEED_BAR_LIMIT,
                },
            )
        ]
        seed = admissible_seed_bars(session, seed_newest_first, name.day)
        bars = [
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
                {"symbol": name.symbol, "start": bars_start, "end": bars_end},
            )
        ]
        trades = [
            TradePoint(utc(row[0]), Decimal(str(row[1])))
            for row in session.execute(
                text(
                    "SELECT event_ts,price FROM market_capture_trades WHERE symbol=:symbol "
                    "AND event_ts>=:start AND event_ts<:end AND price>0 ORDER BY event_ts,id"
                ),
                {"symbol": name.symbol, "start": market_start, "end": market_end},
            )
        ]
        quotes = [
            QuotePoint(utc(row[0]), Decimal(str(row[1])))
            for row in session.execute(
                text(
                    "SELECT event_ts,bid_price FROM market_capture_quotes WHERE symbol=:symbol "
                    "AND event_ts>=:start AND event_ts<:end AND bid_price IS NOT NULL "
                    "ORDER BY event_ts,id"
                ),
                {"symbol": name.symbol, "start": market_start, "end": market_end},
            )
        ]
    return seed + bars, trades, quotes


def build_reports(session_factory, settings) -> list[NameReport]:
    reports: list[NameReport] = []
    for name in POPULATION:
        bars, trades, quotes = load_data(session_factory, name)
        halts = detect_halts(trades, quotes)
        reports.append(
            summarize(
                name,
                replay_rows(
                    settings=settings,
                    name=name,
                    bars=bars,
                    halts=halts,
                    quotes=quotes,
                ),
            )
        )
    return reports


def clock(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone(EASTERN).strftime("%H:%M")


def number(value: Decimal | None, places: int = 5) -> str:
    return "NO DATA" if value is None else f"{value:.{places}f}"


def money(value: Decimal | None) -> str:
    return "NO DATA" if value is None else f"${value:.4f}"


def pct(value: Decimal | None) -> str:
    return "NO DATA" if value is None else f"{value:+.2f}%"


def render(reports: Sequence[NameReport]) -> str:
    lines = [
        DISCLOSURE,
        "Schwab 1-minute completed bars. MACD=12/26/9 using deployed v2 EMA math. "
        "ATR=modified TR, WILDERS(5), factor 3.5 using deployed v2 state machine. "
        "Volume average=v2 trailing 20 bars including the current bar; ratio=volume/average.",
        "Price is the Schwab bar close; % is versus the frozen operator-approved entry. "
        "A timestamp names the minute whose close produced the indicator. Halts are flagged, "
        "not removed. Missing bars remain explicit NO DATA rows.",
    ]
    for report in reports:
        name = report.name
        lines.extend(
            [
                "",
                f"Group {name.group} | {name.symbol} {name.day} | entry {name.entry_time} ET @ {money(name.entry_price)} | n=1/1",
                "",
                "| min | price | from entry | MACD | signal | hist | volume | avg20 | vol/avg | ATR state | ATR level | markers |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|",
            ]
        )
        for row in report.rows:
            markers: list[str] = []
            if row.macd_cross_down:
                markers.append("MACD XDOWN")
            if row.atr_sell_flip:
                markers.append("ATR SELL")
            if row.halted:
                markers.append("HALT")
            if not row.quote_available:
                markers.append("NO QUOTE")
            if row.close is None:
                markers.append("NO BAR")
            lines.append(
                f"| {clock(row.minute)} | {money(row.close)} | {pct(row.return_pct)} | "
                f"{number(row.macd)} | {number(row.signal)} | {number(row.histogram)} | "
                f"{row.volume if row.volume is not None else 'NO DATA'} | "
                f"{number(row.average_volume, 0)} | {number(row.volume_ratio, 2)} | "
                f"{row.atr_state or 'NO DATA'} | {money(row.atr_level)} | "
                f"{', '.join(markers) or '-'} |"
            )
    return "\n".join(lines)


def order_text(cross: datetime | None, atr: datetime | None) -> str:
    if cross is None and atr is None:
        return "NEITHER"
    if cross is None:
        return "ATR ONLY"
    if atr is None:
        return "MACD ONLY"
    if cross < atr:
        return f"MACD FIRST by {(atr - cross).total_seconds() / 60:.0f}m"
    if atr < cross:
        return f"ATR FIRST by {(cross - atr).total_seconds() / 60:.0f}m"
    return "SAME MINUTE"


def render_summary(reports: Sequence[NameReport]) -> str:
    lines = [
        "Cross timing summary",
        "",
        "High is the maximum available Schwab minute close from entry through 10:00; low is "
        "the minimum available close from that high through 10:00.",
        "",
        "| group | name | n | high min / px | MACD down | ATR SELL | first | low min / px | MACD before low |",
        "|---|---|---:|---|---|---|---|---|---|",
    ]
    for report in reports:
        lines.append(
            f"| {report.name.group} | {report.name.symbol} {report.name.day:%m-%d} | 1/1 | "
            f"{clock(report.high_at)} / {money(report.high_price)} | "
            f"{clock(report.macd_cross_at)} | {clock(report.atr_flip_at)} | "
            f"{order_text(report.macd_cross_at, report.atr_flip_at)} | "
            f"{clock(report.low_at)} / {money(report.low_price)} | "
            f"{'YES' if report.cross_before_low else 'NO' if report.cross_before_low is False else 'UNANSWERABLE'} |"
        )
    lines.append("")
    for group in ("A", "B"):
        group_rows = [report for report in reports if report.name.group == group]
        answered = [report for report in group_rows if report.cross_before_low is not None]
        count = sum(report.cross_before_low is True for report in answered)
        lines.append(
            f"Group {group}: MACD crossed down before the low on {count}/{len(answered)} gradable names "
            f"({len(answered)}/{len(group_rows)} population gradable)."
        )
    return "\n".join(lines)


def write_csv(path: Path, reports: Sequence[NameReport]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "group",
        "day",
        "symbol",
        "entry_time_et",
        "entry_price",
        "minute_et",
        "price",
        "return_pct",
        "macd",
        "signal",
        "histogram",
        "macd_cross_down",
        "volume",
        "average_volume_20",
        "volume_ratio",
        "atr_state",
        "atr_level",
        "atr_sell_flip",
        "halted",
        "quote_available",
        "qualification",
    )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for report in reports:
            for row in report.rows:
                writer.writerow(
                    {
                        "group": report.name.group,
                        "day": report.name.day,
                        "symbol": report.name.symbol,
                        "entry_time_et": report.name.entry_time,
                        "entry_price": report.name.entry_price,
                        "minute_et": clock(row.minute),
                        "price": row.close or "",
                        "return_pct": row.return_pct if row.return_pct is not None else "",
                        "macd": row.macd if row.macd is not None else "",
                        "signal": row.signal if row.signal is not None else "",
                        "histogram": row.histogram if row.histogram is not None else "",
                        "macd_cross_down": row.macd_cross_down,
                        "volume": row.volume if row.volume is not None else "",
                        "average_volume_20": row.average_volume or "",
                        "volume_ratio": row.volume_ratio or "",
                        "atr_state": row.atr_state or "",
                        "atr_level": row.atr_level or "",
                        "atr_sell_flip": row.atr_sell_flip,
                        "halted": row.halted,
                        "quote_available": row.quote_available,
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
    reports = build_reports(build_session_factory(settings), settings)
    print(render(reports))
    print("\n\n" + render_summary(reports))
    if args.csv:
        write_csv(args.csv, reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
