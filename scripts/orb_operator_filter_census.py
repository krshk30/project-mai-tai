#!/usr/bin/env python3
"""Census ORB break bars against the operator's proposed entry filters.

This is selection-only. It does not simulate an entry, fill, exit, or return.
Every decision uses only completed Schwab 1-minute bars available when the
breaking bar closes. The report is intended for operator sign-off before any
stop sweep is run.
"""

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

from orb_momentum_turn_report import BarPoint, admissible_seed_bars
from project_mai_tai.backtest.dot_entry import fast_stoch_k, rsi_wilders
from project_mai_tai.backtest.watch_start import WatchWindow, build_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.market_halts import HALT_MIN_PRINT_GAP, HaltWindow, confirmed_halt_window
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy, V2Indicators

EASTERN = ZoneInfo("America/New_York")
DISCLOSURE = "SIMULATED | NO REALISED CONTROL | NOT SIZE-QUALIFIED"
MACD_PARAMETERS = (12, 26, 9)
RSI_LENGTH = 14
STOCH_LENGTH = 10
VOLUME_LOOKBACK = 20
CHOP_LOOKBACK = 5
CHOP_MIN_EFFICIENCY = Decimal("0.35")
CHOP_MAX_REVERSALS = 2
DB_SEED_BAR_LIMIT = 250


@dataclass(frozen=True)
class TradePoint:
    at: datetime
    price: Decimal


@dataclass(frozen=True)
class QuotePoint:
    at: datetime
    bid: Decimal
    ask: Decimal


@dataclass(frozen=True)
class IndicatorSnapshot:
    atr_state: str | None
    atr_level: Decimal | None
    macd: Decimal | None
    signal: Decimal | None
    histogram: Decimal | None
    prior_histogram: Decimal | None
    volume_average: Decimal | None
    rsi: Decimal | None
    prior_rsi: Decimal | None
    stoch_k: Decimal | None
    prior_stoch_k: Decimal | None


@dataclass(frozen=True)
class BreakRow:
    day: date
    symbol: str
    break_number: int
    opening_high: Decimal
    bar: BarPoint
    atr_0930_state: str | None
    atr_entry_state: str | None
    body_pct: Decimal | None
    volume_ratio: Decimal | None
    macd_bullish: bool
    rsi_bullish: bool
    stoch_bullish: bool
    red_0925_0929: int | None
    efficiency: Decimal | None
    reversals: int | None
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DayReport:
    day: date
    watched_symbols: tuple[str, ...]
    no_break_symbols: tuple[str, ...]
    no_level_symbols: tuple[str, ...]
    rows: tuple[BreakRow, ...]


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def at_et(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), EASTERN).astimezone(UTC)


def decision_at(bar: BarPoint) -> datetime:
    """A minute bar labelled 09:30 is observable at 09:31:00."""
    return bar.at + timedelta(minutes=1)


def body_percent(bar: BarPoint) -> Decimal | None:
    span = bar.high - bar.low
    return abs(bar.close - bar.open) / span if span > 0 else None


def choppiness(bars: Sequence[BarPoint], index: int) -> tuple[Decimal | None, int | None, bool]:
    """Return five-close efficiency, direction reversals, and the choppy verdict."""
    if index + 1 < CHOP_LOOKBACK:
        return None, None, True
    closes = [bar.close for bar in bars[index - CHOP_LOOKBACK + 1 : index + 1]]
    changes = [current - previous for previous, current in zip(closes, closes[1:], strict=False)]
    travel = sum((abs(change) for change in changes), Decimal("0"))
    efficiency = abs(closes[-1] - closes[0]) / travel if travel > 0 else Decimal("0")
    signs = [1 if change > 0 else -1 for change in changes if change != 0]
    reversals = sum(left != right for left, right in zip(signs, signs[1:], strict=False))
    return efficiency, reversals, efficiency < CHOP_MIN_EFFICIENCY or reversals > CHOP_MAX_REVERSALS


def break_indices(bars: Sequence[BarPoint], level: Decimal) -> list[int]:
    """All completed bars that cross the fixed level from at/below to above."""
    indices: list[int] = []
    for index, bar in enumerate(bars):
        if not (at_et(bar.at.astimezone(EASTERN).date(), 9, 30) <= bar.at < at_et(bar.at.astimezone(EASTERN).date(), 10, 0)):
            continue
        prior_close = bars[index - 1].close if index else bar.open
        crossed_intrabar = bar.low <= level < bar.high
        gapped_across = prior_close <= level < bar.open
        if crossed_intrabar or gapped_across:
            indices.append(index)
    return indices


def confirmed_halts(trades: Sequence[TradePoint], quotes: Sequence[QuotePoint]) -> list[HaltWindow]:
    quote_times = [quote.at for quote in quotes]
    result: list[HaltWindow] = []
    for previous, current in zip(trades, trades[1:], strict=False):
        if current.at - previous.at < HALT_MIN_PRINT_GAP:
            continue
        updates = bisect_left(quote_times, current.at) - bisect_right(quote_times, previous.at)
        window = confirmed_halt_window(
            last_print_at=previous.at,
            reopen_print_at=current.at,
            quote_updates=updates,
        )
        if window is not None:
            result.append(window)
    return result


def overlaps_halt(bar: BarPoint, halts: Sequence[HaltWindow]) -> bool:
    end = decision_at(bar)
    return any(halt.last_print_at < end and halt.reopen_print_at > bar.at for halt in halts)


def has_executable_nbbo(bar: BarPoint, quotes: Sequence[QuotePoint]) -> bool:
    return any(bar.at <= quote.at < decision_at(bar) and quote.bid > 0 and quote.ask > 0 for quote in quotes)


def replay_indicators(settings, symbol: str, bars: Sequence[BarPoint]) -> list[IndicatorSnapshot]:
    strategy = SchwabV2Strategy(settings)
    if (
        strategy.cfg.macd_fast_length,
        strategy.cfg.macd_slow_length,
        strategy.cfg.macd_signal_length,
    ) != MACD_PARAMETERS:
        raise RuntimeError("deployed MACD parameters no longer match 12/26/9")
    state = strategy.watchlist_state(symbol)
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    closes = [float(bar.close) for bar in bars]
    rsi_values = rsi_wilders(closes, RSI_LENGTH)
    stoch_values = fast_stoch_k(highs, lows, closes, STOCH_LENGTH)
    histories: deque[float] = deque(maxlen=300)
    histograms: list[Decimal | None] = []
    atr_values: list[tuple[str | None, Decimal | None]] = []
    for bar in bars:
        histories.append(float(bar.close))
        macd_result = V2Indicators.macd(list(histories), *MACD_PARAMETERS)
        histograms.append(Decimal(str(macd_result[2])) if macd_result else None)
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
        atr_values.append(
            (
                str(atr["state"]).upper() if atr else None,
                Decimal(str(atr["trail"])) if atr else None,
            )
        )

    snapshots: list[IndicatorSnapshot] = []
    for index, bar in enumerate(bars):
        macd_result = V2Indicators.macd(closes[: index + 1], *MACD_PARAMETERS)
        volume_history = bars[max(0, index - VOLUME_LOOKBACK) : index]
        volume_average = (
            Decimal(sum(item.volume for item in volume_history)) / Decimal(VOLUME_LOOKBACK)
            if len(volume_history) == VOLUME_LOOKBACK
            else None
        )
        rsi = rsi_values[index] if index < len(rsi_values) else float("nan")
        prior_rsi = rsi_values[index - 1] if index else float("nan")
        stoch = stoch_values[index] if index < len(stoch_values) else float("nan")
        prior_stoch = stoch_values[index - 1] if index else float("nan")
        snapshots.append(
            IndicatorSnapshot(
                atr_state=atr_values[index][0],
                atr_level=atr_values[index][1],
                macd=Decimal(str(macd_result[0])) if macd_result else None,
                signal=Decimal(str(macd_result[1])) if macd_result else None,
                histogram=histograms[index],
                prior_histogram=histograms[index - 1] if index else None,
                volume_average=volume_average,
                rsi=Decimal(str(rsi)) if rsi == rsi else None,
                prior_rsi=Decimal(str(prior_rsi)) if prior_rsi == prior_rsi else None,
                stoch_k=Decimal(str(stoch)) if stoch == stoch else None,
                prior_stoch_k=Decimal(str(prior_stoch)) if prior_stoch == prior_stoch else None,
            )
        )
    return snapshots


def evaluate_break(
    *,
    day: date,
    symbol: str,
    break_number: int,
    bars: Sequence[BarPoint],
    index: int,
    indicators: Sequence[IndicatorSnapshot],
    opening_high: Decimal,
    halts: Sequence[HaltWindow],
    quotes: Sequence[QuotePoint],
    accepted_before: int = 0,
) -> BreakRow:
    bar = bars[index]
    current = indicators[index]
    at_0930 = next(
        (indicators[i] for i, item in enumerate(bars) if item.at == at_et(day, 9, 30)),
        None,
    )
    atr_path = [
        indicators[i].atr_state
        for i, item in enumerate(bars)
        if at_et(day, 9, 30) <= item.at <= bar.at
    ]
    opening_bars = [item for item in bars if at_et(day, 9, 25) <= item.at < at_et(day, 9, 30)]
    red_count = sum(item.close < item.open for item in opening_bars) if len(opening_bars) == 5 else None
    body = body_percent(bar)
    volume_ratio = (
        Decimal(bar.volume) / current.volume_average
        if current.volume_average is not None and current.volume_average > 0
        else None
    )
    macd_bullish = bool(
        current.macd is not None
        and current.signal is not None
        and current.histogram is not None
        and current.prior_histogram is not None
        and current.macd > current.signal
        and current.histogram > current.prior_histogram
    )
    rsi_bullish = bool(
        current.rsi is not None
        and current.prior_rsi is not None
        and current.rsi >= 50
        and current.rsi > current.prior_rsi
    )
    stoch_bullish = bool(
        current.stoch_k is not None
        and current.prior_stoch_k is not None
        and current.stoch_k >= 50
        and current.stoch_k > current.prior_stoch_k
    )
    efficiency, reversals, is_choppy = choppiness(bars, index)
    reasons: list[str] = []
    if at_0930 is None or at_0930.atr_state is None or any(state is None for state in atr_path):
        reasons.append("UNANSWERABLE ATR_STATE")
    elif at_0930.atr_state != "LONG" or any(state != "LONG" for state in atr_path):
        reasons.append("R1 ATR_NOT_CONTINUOUSLY_LONG")
    if bar.close < bar.open:
        reasons.append("R2 RED_BREAK_BAR")
    stack_available = all(
        value is not None
        for value in (
            current.macd,
            current.signal,
            current.histogram,
            current.prior_histogram,
            volume_ratio,
            current.rsi,
            current.prior_rsi,
            current.stoch_k,
            current.prior_stoch_k,
        )
    )
    if not stack_available:
        reasons.append("UNANSWERABLE STACK_WARMUP")
    elif not (
        bar.close > bar.open
        and macd_bullish
        and volume_ratio >= 1
        and rsi_bullish
        and stoch_bullish
    ):
        reasons.append("R3 STACK_DISAGREES")
    if body is None or body < Decimal("0.45"):
        reasons.append("R4 BODY_LT_45PCT")
    if red_count is None or red_count >= 4:
        reasons.append("R5 FOUR_OF_FIVE_RED")
    if is_choppy:
        reasons.append("R6 CHOPPY")
    if overlaps_halt(bar, halts):
        reasons.append("UNANSWERABLE HALT")
    if not has_executable_nbbo(bar, quotes):
        reasons.append("UNANSWERABLE NO_NBBO")
    if not reasons and accepted_before >= 2:
        reasons.append("R7 TWO_ACCEPTED_TRADES_ALREADY")
    status = (
        "UNANSWERABLE"
        if any(reason.startswith("UNANSWERABLE") for reason in reasons)
        else "REJECT"
        if reasons
        else "PASS"
    )
    return BreakRow(
        day=day,
        symbol=symbol,
        break_number=break_number,
        opening_high=opening_high,
        bar=bar,
        atr_0930_state=at_0930.atr_state if at_0930 else None,
        atr_entry_state=current.atr_state,
        body_pct=body,
        volume_ratio=volume_ratio,
        macd_bullish=macd_bullish,
        rsi_bullish=rsi_bullish,
        stoch_bullish=stoch_bullish,
        red_0925_0929=red_count,
        efficiency=efficiency,
        reversals=reversals,
        status=status,
        reasons=tuple(reasons),
    )


def symbol_is_watched(windows: Sequence[WatchWindow], at: datetime, cutoff: datetime) -> bool:
    at_ms = int(at.timestamp() * 1000)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    return any(window.start_ms <= cutoff_ms and window.contains(at_ms) for window in windows)


def load_day_events(session, day: date) -> dict[str, list[tuple[str, int]]]:
    grouped: dict[str, list[tuple[str, int]]] = {}
    for symbol, event_type, event_at in session.execute(
        text(
            "SELECT symbol,event_type,event_at FROM scanner_confirmed_events "
            "WHERE trade_date=:day ORDER BY symbol,event_at,id"
        ),
        {"day": day},
    ):
        grouped.setdefault(str(symbol).upper(), []).append(
            (str(event_type), int(utc(event_at).timestamp() * 1000))
        )
    return grouped


def load_symbol_data(session, day: date, symbol: str) -> tuple[list[BarPoint], list[TradePoint], list[QuotePoint]]:
    start = at_et(day, 4, 0)
    end = at_et(day, 10, 1)
    seed_newest = [
        BarPoint(utc(row[0]), Decimal(str(row[1])), Decimal(str(row[2])), Decimal(str(row[3])), Decimal(str(row[4])), int(row[5] or 0), str(row[6] or ""))
        for row in session.execute(
            text(
                "SELECT bar_time,open_price,high_price,low_price,close_price,volume,source "
                "FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND symbol=:symbol "
                "AND interval_secs=60 AND bar_time<:start ORDER BY bar_time DESC LIMIT :limit"
            ),
            {"symbol": symbol, "start": start, "limit": DB_SEED_BAR_LIMIT},
        )
    ]
    seed = admissible_seed_bars(session, seed_newest, day)
    bars = [
        BarPoint(utc(row[0]), Decimal(str(row[1])), Decimal(str(row[2])), Decimal(str(row[3])), Decimal(str(row[4])), int(row[5] or 0), str(row[6] or ""))
        for row in session.execute(
            text(
                "SELECT bar_time,open_price,high_price,low_price,close_price,volume,source "
                "FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND symbol=:symbol "
                "AND interval_secs=60 AND bar_time>=:start AND bar_time<:end ORDER BY bar_time"
            ),
            {"symbol": symbol, "start": start, "end": end},
        )
    ]
    market_start = at_et(day, 9, 20)
    trades = [
        TradePoint(utc(row[0]), Decimal(str(row[1])))
        for row in session.execute(
            text(
                "SELECT event_ts,price FROM market_capture_trades WHERE symbol=:symbol "
                "AND event_ts>=:start AND event_ts<:end AND price>0 ORDER BY event_ts,id"
            ),
            {"symbol": symbol, "start": market_start, "end": end},
        )
    ]
    quotes = [
        QuotePoint(utc(row[0]), Decimal(str(row[1])), Decimal(str(row[2])))
        for row in session.execute(
            text(
                "SELECT event_ts,bid_price,ask_price FROM market_capture_quotes WHERE symbol=:symbol "
                "AND event_ts>=:start AND event_ts<:end AND bid_price>0 AND ask_price>0 "
                "ORDER BY event_ts,id"
            ),
            {"symbol": symbol, "start": market_start, "end": end},
        )
    ]
    return seed + bars, trades, quotes


def build_day_report(session_factory, settings, day: date) -> DayReport:
    with session_factory() as session:
        events = load_day_events(session, day)
        cutoff = at_et(day, 9, 25)
        windows_by_symbol = {
            symbol: build_windows(rows)
            for symbol, rows in events.items()
            if any(event_type == "CONFIRM" and at_ms <= int(cutoff.timestamp() * 1000) for event_type, at_ms in rows)
        }
        report_rows: list[BreakRow] = []
        broke: set[str] = set()
        watched: set[str] = set()
        no_level: set[str] = set()
        for symbol, windows in sorted(windows_by_symbol.items()):
            if not any(
                window.start_ms <= int(cutoff.timestamp() * 1000)
                and (window.end_ms is None or window.end_ms > int(at_et(day, 9, 30).timestamp() * 1000))
                for window in windows
            ):
                continue
            watched.add(symbol)
            bars, trades, quotes = load_symbol_data(session, day, symbol)
            session_bars = [bar for bar in bars if at_et(day, 9, 25) <= bar.at < at_et(day, 10, 0)]
            opening = [bar for bar in session_bars if bar.at < at_et(day, 9, 30)]
            if len(opening) != 5:
                no_level.add(symbol)
                continue
            level = max(bar.high for bar in opening)
            indicators = replay_indicators(settings, symbol, bars)
            halts = confirmed_halts(trades, quotes)
            accepted = 0
            for number, index in enumerate(break_indices(bars, level), start=1):
                bar = bars[index]
                if not symbol_is_watched(windows, decision_at(bar), cutoff):
                    continue
                broke.add(symbol)
                row = evaluate_break(
                    day=day,
                    symbol=symbol,
                    break_number=number,
                    bars=bars,
                    index=index,
                    indicators=indicators,
                    opening_high=level,
                    halts=halts,
                    quotes=quotes,
                    accepted_before=accepted,
                )
                report_rows.append(row)
                if row.status == "PASS":
                    accepted += 1
    return DayReport(
        day=day,
        watched_symbols=tuple(sorted(watched)),
        no_break_symbols=tuple(sorted(watched - broke - no_level)),
        no_level_symbols=tuple(sorted(no_level)),
        rows=tuple(sorted(report_rows, key=lambda row: (row.bar.at, row.symbol, row.break_number))),
    )


def trading_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def clock(value: datetime) -> str:
    return value.astimezone(EASTERN).strftime("%H:%M")


def number(value: Decimal | None, places: int = 2) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def render(reports: Sequence[DayReport], *, coverage_note: str) -> str:
    rows = [row for report in reports for row in report.rows]
    passing = sum(row.status == "PASS" for row in rows)
    lines = [
        DISCLOSURE,
        coverage_note,
        "Decision convention: a bar labelled 09:30 is evaluated at its 09:31 close; no later bar informs it. Break = a completed Schwab 1m bar crossing the fixed max high of the 09:25-09:29 Schwab bars. No entry or fill is simulated in Step 1.",
        "R3 STACK_AGREES: green close; MACD(12,26,9) above signal with rising histogram; entry-bar volume >= prior-20-bar average; RSI(14,Wilder) >=50 and rising; Fast Stoch K(10) >=50 and rising. All five must pass.",
        "R6 CHOPPY: last five visible closes have directional efficiency <0.35 OR more than two direction reversals. No future close is read.",
        f"Overall: {passing}/{len(rows)} break rows PASS; denominator {len(rows)} break rows.",
    ]
    for report in reports:
        day_pass = sum(row.status == "PASS" for row in report.rows)
        lines.extend(
            [
                "",
                f"{report.day.isoformat()}: watched {len(report.watched_symbols)}; broke {len(set(row.symbol for row in report.rows))}/{len(report.watched_symbols)} names; PASS {day_pass}/{len(report.rows)} break rows.",
                f"No break: {', '.join(report.no_break_symbols) if report.no_break_symbols else '-'}",
                f"UNANSWERABLE opening range: {', '.join(report.no_level_symbols) if report.no_level_symbols else '-'}",
                "",
                "| n | sym | # | high | break | ATR 09:30/entry | body | vol x | M/R/S | red5 | eff/rev | result |",
                "|---:|---|---:|---:|---:|---|---:|---:|---|---:|---|---|",
            ]
        )
        for row in report.rows:
            lines.append(
                f"| {rows.index(row) + 1}/{len(rows)} | {row.symbol} | {row.break_number} | "
                f"{row.opening_high:.4f} | {clock(row.bar.at)} | "
                f"{row.atr_0930_state or '-'}/{row.atr_entry_state or '-'} | "
                f"{number(row.body_pct * 100 if row.body_pct is not None else None, 0)}% | "
                f"{number(row.volume_ratio)} | "
                f"{'Y' if row.macd_bullish else 'N'}/{'Y' if row.rsi_bullish else 'N'}/{'Y' if row.stoch_bullish else 'N'} | "
                f"{row.red_0925_0929 if row.red_0925_0929 is not None else '-'} | "
                f"{number(row.efficiency)}/{row.reversals if row.reversals is not None else '-'} | "
                f"{row.status if row.status == 'PASS' else '; '.join(row.reasons)} |"
            )
    return "\n".join(lines)


def write_csv(path: Path, reports: Sequence[DayReport]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "day", "symbol", "break_number", "opening_high", "break_minute_et",
                "atr_0930", "atr_entry", "body_pct", "volume_ratio", "macd_bullish",
                "rsi_bullish", "stoch_bullish", "red_0925_0929", "efficiency",
                "reversals", "status", "reasons", "qualification",
            ),
        )
        writer.writeheader()
        for report in reports:
            for row in report.rows:
                writer.writerow(
                    {
                        "day": row.day,
                        "symbol": row.symbol,
                        "break_number": row.break_number,
                        "opening_high": row.opening_high,
                        "break_minute_et": clock(row.bar.at),
                        "atr_0930": row.atr_0930_state or "",
                        "atr_entry": row.atr_entry_state or "",
                        "body_pct": row.body_pct or "",
                        "volume_ratio": row.volume_ratio or "",
                        "macd_bullish": row.macd_bullish,
                        "rsi_bullish": row.rsi_bullish,
                        "stoch_bullish": row.stoch_bullish,
                        "red_0925_0929": row.red_0925_0929 if row.red_0925_0929 is not None else "",
                        "efficiency": row.efficiency or "",
                        "reversals": row.reversals if row.reversals is not None else "",
                        "status": row.status,
                        "reasons": "; ".join(row.reasons),
                        "qualification": DISCLOSURE,
                    }
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--coverage-note", default="Coverage supplied by operator run.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start_date > args.end_date:
        raise SystemExit("start date must not be after end date")
    settings = get_settings()
    session_factory = build_session_factory(settings)
    reports = [build_day_report(session_factory, settings, day) for day in trading_days(args.start_date, args.end_date)]
    print(render(reports, coverage_note=args.coverage_note))
    if args.csv:
        write_csv(args.csv, reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
