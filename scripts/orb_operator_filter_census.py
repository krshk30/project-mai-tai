#!/usr/bin/env python3
"""Compare the legacy ORB filter census with the settled operator rules.

The settled census is a per-name/day state machine. It emits exactly three
decision kinds: a once-only day gate, live arm/pull/fill decisions, and the
post-fill body sell. It never publishes an intent or calls a broker.
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
from typing import Literal, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text

from orb_momentum_turn_report import BarPoint, admissible_seed_bars
from project_mai_tai.backtest.dot_entry import fast_stoch_k, rsi_wilders
from project_mai_tai.backtest.watch_start import WatchWindow, build_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.market_halts import (
    HALT_MIN_PRINT_GAP,
    HaltWindow,
    confirmed_halt_window,
    timestamp_is_halted,
)
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
DAY_GATE = "DAY_GATE"
LIVE = "LIVE"
POST_FILL_SELL = "POST_FILL_SELL"
LOOKBACK_VOLUME_BASELINE = "volume-baseline"
LOOKBACK_ABSOLUTE_LEVELS = "absolute-levels"
LOOKBACK_ASSIGNMENTS = (LOOKBACK_VOLUME_BASELINE, LOOKBACK_ABSOLUTE_LEVELS)
DEFAULT_BODY_THRESHOLD_PCT = Decimal("45")
DEFAULT_BODY_MEASUREMENT_DELAY = "bar-close"

LookbackAssignment = Literal["volume-baseline", "absolute-levels"]


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


@dataclass(frozen=True)
class GateDecision:
    day: date
    symbol: str
    assignment: LookbackAssignment
    evaluated_at: datetime
    opening_high: Decimal | None
    atr_state: str | None
    atr_level: Decimal | None
    close: Decimal | None
    red_0925_0929: int | None
    status: str
    checks: tuple[str, ...]
    kind: str = DAY_GATE


@dataclass(frozen=True)
class LiveDecision:
    day: date
    symbol: str
    assignment: LookbackAssignment
    evaluated_at: datetime
    action: str
    opening_high: Decimal
    bar_at: datetime
    volume_ratio: Decimal | None
    macd_green: bool | None
    rsi_up: bool | None
    stoch_up: bool | None
    checks: tuple[str, ...]
    fill_at: datetime | None = None
    fill_price: Decimal | None = None
    kind: str = LIVE


@dataclass(frozen=True)
class PostFillDecision:
    day: date
    symbol: str
    assignment: LookbackAssignment
    evaluated_at: datetime
    action: str
    opening_high: Decimal
    fill_at: datetime
    fill_price: Decimal
    body_pct: Decimal | None
    threshold_pct: Decimal
    delay_label: str
    exit_at: datetime | None
    exit_bid: Decimal | None
    return_pct: Decimal | None
    checks: tuple[str, ...]
    kind: str = POST_FILL_SELL


@dataclass(frozen=True)
class SettledNameDay:
    day: date
    symbol: str
    assignment: LookbackAssignment
    gate: GateDecision
    live: tuple[LiveDecision, ...]
    post_fill: PostFillDecision | None


@dataclass(frozen=True)
class SettledDayReport:
    day: date
    watched_symbols: tuple[str, ...]
    no_level_symbols: tuple[str, ...]
    legacy_rows: tuple[BreakRow, ...]
    runs: tuple[SettledNameDay, ...]


@dataclass(frozen=True)
class QuoteCoverage:
    first_day: date | None
    last_day: date | None


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


def evaluate_day_gate(
    *,
    day: date,
    symbol: str,
    assignment: LookbackAssignment,
    bars: Sequence[BarPoint],
    indicators: Sequence[IndicatorSnapshot],
    opening_high: Decimal | None,
) -> GateDecision:
    """Evaluate the only rules allowed to end a name's session."""
    evaluated_at = at_et(day, 9, 31)
    gate_index = next(
        (index for index, item in enumerate(bars) if item.at == at_et(day, 9, 30)),
        None,
    )
    opening = [item for item in bars if at_et(day, 9, 25) <= item.at < at_et(day, 9, 30)]
    red_count = sum(item.close < item.open for item in opening) if len(opening) == 5 else None
    current = indicators[gate_index] if gate_index is not None else None
    bar_0930 = bars[gate_index] if gate_index is not None else None
    checks: list[str] = []
    killed = False
    unanswerable = False

    if current is None or bar_0930 is None or current.atr_level is None:
        checks.append("DAY_GATE:R1=UNANSWERABLE ATR_0930_TRAIL_MISSING")
        unanswerable = True
    elif bar_0930.close <= current.atr_level:
        checks.append("DAY_GATE:R1=FAIL CLOSE_NOT_ABOVE_ATR_TRAIL_AT_0930")
        killed = True
    else:
        checks.append("DAY_GATE:R1=PASS CLOSE_ABOVE_ATR_TRAIL_AT_0930")

    if red_count is None:
        checks.append("DAY_GATE:R5=UNANSWERABLE FIVE_BAR_RUNUP_MISSING")
        unanswerable = True
    elif red_count >= 4:
        checks.append("DAY_GATE:R5=FAIL FOUR_OF_FIVE_RED")
        killed = True
    else:
        checks.append("DAY_GATE:R5=PASS FEWER_THAN_FOUR_OF_FIVE_RED")

    if assignment == LOOKBACK_VOLUME_BASELINE:
        if current is None or current.volume_average is None:
            checks.append("DAY_GATE:R3_LOOKBACK=UNANSWERABLE VOLUME_BASELINE_MISSING")
            unanswerable = True
        else:
            checks.append("DAY_GATE:R3_LOOKBACK=PASS VOLUME_BASELINE_AVAILABLE")
    elif assignment == LOOKBACK_ABSOLUTE_LEVELS:
        if current is None or current.rsi is None or current.stoch_k is None:
            checks.append("DAY_GATE:R3_LOOKBACK=UNANSWERABLE RSI_OR_STOCH_LEVEL_MISSING")
            unanswerable = True
        elif current.rsi < 50 or current.stoch_k < 50:
            checks.append("DAY_GATE:R3_LOOKBACK=FAIL RSI_OR_STOCH_BELOW_50_AT_0930")
            killed = True
        else:
            checks.append("DAY_GATE:R3_LOOKBACK=PASS RSI_AND_STOCH_AT_LEAST_50_AT_0930")
    else:  # pragma: no cover - argparse and the Literal contract prevent this
        raise ValueError(f"unknown look-back assignment: {assignment}")

    if opening_high is None:
        checks.append("DAY_GATE:OPENING_RANGE=UNANSWERABLE HIGH_0925_0929_MISSING")
        unanswerable = True
    else:
        checks.append("DAY_GATE:OPENING_RANGE=PASS HIGH_0925_0929_FIXED")

    status = "KILLED" if killed else "UNANSWERABLE" if unanswerable else "ELIGIBLE"
    return GateDecision(
        day=day,
        symbol=symbol,
        assignment=assignment,
        evaluated_at=evaluated_at,
        opening_high=opening_high,
        atr_state=current.atr_state if current else None,
        atr_level=current.atr_level if current else None,
        close=bar_0930.close if bar_0930 else None,
        red_0925_0929=red_count,
        status=status,
        checks=tuple(checks),
    )


def evaluate_live_momentum(
    *,
    day: date,
    symbol: str,
    assignment: LookbackAssignment,
    opening_high: Decimal,
    bar: BarPoint,
    current: IndicatorSnapshot,
) -> LiveDecision:
    """Arm or pull for the next interval; a pull never kills the name-day."""
    volume_ratio = (
        Decimal(bar.volume) / current.volume_average
        if current.volume_average is not None and current.volume_average > 0
        else None
    )
    macd_green = current.histogram > 0 if current.histogram is not None else None
    rsi_up = (
        current.rsi > current.prior_rsi
        if current.rsi is not None and current.prior_rsi is not None
        else None
    )
    stoch_up = (
        current.stoch_k > current.prior_stoch_k
        if current.stoch_k is not None and current.prior_stoch_k is not None
        else None
    )
    checks = (
        f"LIVE:R3_MACD_HISTOGRAM={'PASS' if macd_green else 'FAIL' if macd_green is False else 'UNANSWERABLE'}",
        f"LIVE:R3_VOLUME={'PASS' if volume_ratio is not None and volume_ratio >= 1 else 'FAIL' if volume_ratio is not None else 'UNANSWERABLE'}",
        f"LIVE:R3_RSI_DIRECTION={'PASS' if rsi_up else 'FAIL' if rsi_up is False else 'UNANSWERABLE'}",
        f"LIVE:R3_STOCH_DIRECTION={'PASS' if stoch_up else 'FAIL' if stoch_up is False else 'UNANSWERABLE'}",
    )
    action = (
        "ARM"
        if macd_green is True
        and volume_ratio is not None
        and volume_ratio >= 1
        and rsi_up is True
        and stoch_up is True
        else "PULL"
    )
    return LiveDecision(
        day=day,
        symbol=symbol,
        assignment=assignment,
        evaluated_at=decision_at(bar),
        action=action,
        opening_high=opening_high,
        bar_at=bar.at,
        volume_ratio=volume_ratio,
        macd_green=macd_green,
        rsi_up=rsi_up,
        stoch_up=stoch_up,
        checks=checks,
    )


def quote_is_known_by(
    quotes: Sequence[QuotePoint],
    *,
    start: datetime,
    at: datetime,
) -> bool:
    """Require positive NBBO evidence no later than the event being judged."""
    return any(start <= quote.at <= at and quote.bid > 0 and quote.ask > 0 for quote in quotes)


def first_break_while_armed(
    *,
    opening_high: Decimal,
    trades: Sequence[TradePoint],
    start: datetime,
    end: datetime,
    halts: Sequence[HaltWindow],
) -> TradePoint | None:
    return next(
        (
            trade
            for trade in trades
            if start <= trade.at < end
            and trade.price > opening_high
            and not timestamp_is_halted(trade.at, list(halts))
        ),
        None,
    )


def body_measurement_at(fill_at: datetime, delay: timedelta | None) -> datetime:
    if delay is None:
        return fill_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return fill_at + delay


def measured_body_percent(
    *,
    fill: TradePoint,
    fill_bar: BarPoint,
    trades: Sequence[TradePoint],
    measurement_at: datetime,
    delay: timedelta | None,
) -> Decimal | None:
    if delay is None:
        return body_percent(fill_bar)
    visible = [
        trade.price
        for trade in trades
        if fill_bar.at <= trade.at <= measurement_at
    ]
    if not visible:
        return None
    open_price = fill_bar.open
    high = max([open_price, *visible])
    low = min([open_price, *visible])
    span = high - low
    return abs(visible[-1] - open_price) / span if span > 0 else None


def first_executable_bid(
    quotes: Sequence[QuotePoint],
    *,
    at: datetime,
    halts: Sequence[HaltWindow],
) -> QuotePoint | None:
    return next(
        (
            quote
            for quote in quotes
            if quote.at >= at
            and quote.bid > 0
            and not timestamp_is_halted(quote.at, list(halts))
        ),
        None,
    )


def evaluate_post_fill(
    *,
    day: date,
    symbol: str,
    assignment: LookbackAssignment,
    opening_high: Decimal,
    fill: TradePoint,
    fill_bar: BarPoint,
    trades: Sequence[TradePoint],
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
    body_threshold_pct: Decimal,
    body_delay: timedelta | None,
    delay_label: str,
) -> PostFillDecision:
    measurement_at = body_measurement_at(fill.at, body_delay)
    body = measured_body_percent(
        fill=fill,
        fill_bar=fill_bar,
        trades=trades,
        measurement_at=measurement_at,
        delay=body_delay,
    )
    if body is None:
        return PostFillDecision(
            day, symbol, assignment, measurement_at, "UNANSWERABLE", opening_high,
            fill.at, opening_high, None, body_threshold_pct, delay_label,
            None, None, None,
            ("POST_FILL_SELL:R4=UNANSWERABLE BODY_MEASUREMENT_MISSING",),
        )
    if body * 100 >= body_threshold_pct:
        return PostFillDecision(
            day, symbol, assignment, measurement_at, "HOLD", opening_high,
            fill.at, opening_high, body * 100, body_threshold_pct, delay_label,
            None, None, None,
            ("POST_FILL_SELL:R4=PASS BODY_AT_OR_ABOVE_THRESHOLD",),
        )
    exit_quote = first_executable_bid(quotes, at=measurement_at, halts=halts)
    if exit_quote is None:
        return PostFillDecision(
            day, symbol, assignment, measurement_at, "SELL_UNANSWERABLE", opening_high,
            fill.at, opening_high, body * 100, body_threshold_pct, delay_label,
            None, None, None,
            (
                "POST_FILL_SELL:R4=FAIL BODY_BELOW_THRESHOLD",
                "POST_FILL_SELL:EXECUTION=UNANSWERABLE NO_EXECUTABLE_BID",
            ),
        )
    return PostFillDecision(
        day, symbol, assignment, measurement_at, "SELL", opening_high,
        fill.at, opening_high, body * 100, body_threshold_pct, delay_label,
        exit_quote.at, exit_quote.bid,
        (exit_quote.bid / opening_high - Decimal("1")) * Decimal("100"),
        (
            "POST_FILL_SELL:R4=FAIL BODY_BELOW_THRESHOLD",
            "POST_FILL_SELL:EXECUTION=PASS FIRST_EXECUTABLE_BID",
        ),
    )


def run_settled_name_day(
    *,
    day: date,
    symbol: str,
    assignment: LookbackAssignment,
    bars: Sequence[BarPoint],
    indicators: Sequence[IndicatorSnapshot],
    trades: Sequence[TradePoint],
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
    opening_high: Decimal | None,
    body_threshold_pct: Decimal = DEFAULT_BODY_THRESHOLD_PCT,
    body_delay: timedelta | None = None,
    delay_label: str = DEFAULT_BODY_MEASUREMENT_DELAY,
) -> SettledNameDay:
    gate = evaluate_day_gate(
        day=day,
        symbol=symbol,
        assignment=assignment,
        bars=bars,
        indicators=indicators,
        opening_high=opening_high,
    )
    if gate.status != "ELIGIBLE" or opening_high is None:
        return SettledNameDay(day, symbol, assignment, gate, (), None)

    session_bars = [
        (index, bar)
        for index, bar in enumerate(bars)
        if at_et(day, 9, 30) <= bar.at < at_et(day, 10, 0)
    ]
    live_rows: list[LiveDecision] = []
    armed = False
    interval_start: datetime | None = None
    armed_from: LiveDecision | None = None
    for index, bar in session_bars:
        close_at = decision_at(bar)
        if armed and interval_start is not None:
            fill = first_break_while_armed(
                opening_high=opening_high,
                trades=trades,
                start=interval_start,
                end=min(close_at, at_et(day, 10, 0)),
                halts=halts,
            )
            if fill is not None:
                nbbo_known = quote_is_known_by(
                    quotes,
                    start=fill.at.replace(second=0, microsecond=0),
                    at=fill.at,
                )
                live_rows.append(
                    LiveDecision(
                        day=day,
                        symbol=symbol,
                        assignment=assignment,
                        evaluated_at=fill.at,
                        action="FILL" if nbbo_known else "FILL_UNANSWERABLE",
                        opening_high=opening_high,
                        bar_at=fill.at.replace(second=0, microsecond=0),
                        volume_ratio=armed_from.volume_ratio if armed_from else None,
                        macd_green=armed_from.macd_green if armed_from else None,
                        rsi_up=armed_from.rsi_up if armed_from else None,
                        stoch_up=armed_from.stoch_up if armed_from else None,
                        checks=(
                            "LIVE:FILL=PASS BREAK_WHILE_ARMED_AT_OWN_PRICE",
                            (
                                "LIVE:NBBO=PASS KNOWN_NO_LATER_THAN_FILL"
                                if nbbo_known
                                else "LIVE:NBBO=UNANSWERABLE NO_QUOTE_BY_FILL"
                            ),
                        ),
                        fill_at=fill.at,
                        fill_price=opening_high if nbbo_known else None,
                    )
                )
                if not nbbo_known:
                    return SettledNameDay(day, symbol, assignment, gate, tuple(live_rows), None)
                fill_bar = next(
                    (item for _, item in session_bars if item.at == fill.at.replace(second=0, microsecond=0)),
                    None,
                )
                if fill_bar is None:
                    return SettledNameDay(day, symbol, assignment, gate, tuple(live_rows), None)
                post_fill = evaluate_post_fill(
                    day=day,
                    symbol=symbol,
                    assignment=assignment,
                    opening_high=opening_high,
                    fill=fill,
                    fill_bar=fill_bar,
                    trades=trades,
                    quotes=quotes,
                    halts=halts,
                    body_threshold_pct=body_threshold_pct,
                    body_delay=body_delay,
                    delay_label=delay_label,
                )
                return SettledNameDay(day, symbol, assignment, gate, tuple(live_rows), post_fill)

        if close_at >= at_et(day, 10, 0):
            break
        momentum = evaluate_live_momentum(
            day=day,
            symbol=symbol,
            assignment=assignment,
            opening_high=opening_high,
            bar=bar,
            current=indicators[index],
        )
        live_rows.append(momentum)
        armed = momentum.action == "ARM"
        armed_from = momentum if armed else None
        interval_start = close_at
    return SettledNameDay(day, symbol, assignment, gate, tuple(live_rows), None)


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


def load_quote_coverage(session_factory) -> QuoteCoverage:
    with session_factory() as session:
        first_at, last_at = session.execute(
            text("SELECT min(event_ts),max(event_ts) FROM market_capture_quotes")
        ).one()
    return QuoteCoverage(
        first_day=utc(first_at).astimezone(EASTERN).date() if first_at else None,
        last_day=utc(last_at).astimezone(EASTERN).date() if last_at else None,
    )


def build_settled_day_report(
    session_factory,
    settings,
    day: date,
    *,
    assignments: Sequence[LookbackAssignment],
    body_threshold_pct: Decimal,
    body_delay: timedelta | None,
    delay_label: str,
) -> SettledDayReport:
    with session_factory() as session:
        events = load_day_events(session, day)
        cutoff = at_et(day, 9, 25)
        windows_by_symbol = {
            symbol: build_windows(rows)
            for symbol, rows in events.items()
            if any(
                event_type == "CONFIRM" and at_ms <= int(cutoff.timestamp() * 1000)
                for event_type, at_ms in rows
            )
        }
        watched: set[str] = set()
        no_level: set[str] = set()
        legacy_rows: list[BreakRow] = []
        runs: list[SettledNameDay] = []
        for symbol, windows in sorted(windows_by_symbol.items()):
            if not any(
                window.start_ms <= int(cutoff.timestamp() * 1000)
                and (
                    window.end_ms is None
                    or window.end_ms > int(at_et(day, 9, 30).timestamp() * 1000)
                )
                for window in windows
            ):
                continue
            watched.add(symbol)
            bars, trades, quotes = load_symbol_data(session, day, symbol)
            opening = [bar for bar in bars if at_et(day, 9, 25) <= bar.at < at_et(day, 9, 30)]
            opening_high = max((bar.high for bar in opening), default=None) if len(opening) == 5 else None
            if opening_high is None:
                no_level.add(symbol)
            indicators = replay_indicators(settings, symbol, bars)
            halts = confirmed_halts(trades, quotes)

            if opening_high is not None:
                accepted = 0
                for number, index in enumerate(break_indices(bars, opening_high), start=1):
                    bar = bars[index]
                    if not symbol_is_watched(windows, decision_at(bar), cutoff):
                        continue
                    legacy = evaluate_break(
                        day=day,
                        symbol=symbol,
                        break_number=number,
                        bars=bars,
                        index=index,
                        indicators=indicators,
                        opening_high=opening_high,
                        halts=halts,
                        quotes=quotes,
                        accepted_before=accepted,
                    )
                    legacy_rows.append(legacy)
                    if legacy.status == "PASS":
                        accepted += 1

            for assignment in assignments:
                runs.append(
                    run_settled_name_day(
                        day=day,
                        symbol=symbol,
                        assignment=assignment,
                        bars=bars,
                        indicators=indicators,
                        trades=trades,
                        quotes=quotes,
                        halts=halts,
                        opening_high=opening_high,
                        body_threshold_pct=body_threshold_pct,
                        body_delay=body_delay,
                        delay_label=delay_label,
                    )
                )
    return SettledDayReport(
        day=day,
        watched_symbols=tuple(sorted(watched)),
        no_level_symbols=tuple(sorted(no_level)),
        legacy_rows=tuple(
            sorted(legacy_rows, key=lambda row: (row.bar.at, row.symbol, row.break_number))
        ),
        runs=tuple(sorted(runs, key=lambda run: (run.assignment, run.symbol))),
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


def settled_decisions(run: SettledNameDay) -> list[GateDecision | LiveDecision | PostFillDecision]:
    decisions: list[GateDecision | LiveDecision | PostFillDecision] = [run.gate, *run.live]
    if run.post_fill is not None:
        decisions.append(run.post_fill)
    return decisions


def render_settled(
    reports: Sequence[SettledDayReport],
    *,
    coverage: QuoteCoverage,
    requested_days: Sequence[date],
    unreachable_days: Sequence[date],
    assignments: Sequence[LookbackAssignment],
    body_threshold_pct: Decimal,
    delay_label: str,
) -> str:
    coverage_label = (
        f"{coverage.first_day.isoformat()} through {coverage.last_day.isoformat()}"
        if coverage.first_day is not None and coverage.last_day is not None
        else "UNANSWERABLE"
    )
    legacy_rows = [row for report in reports for row in report.legacy_rows]
    lines = [
        DISCLOSURE,
        f"Executable quote coverage: {coverage_label}; denominator {len(requested_days)} requested trading days. Unreachable: {', '.join(day.isoformat() for day in unreachable_days) if unreachable_days else 'none'}.",
        "Kinds: DAY_GATE is evaluated once at the 09:30 bar close and is the only kind that can kill a name-day; LIVE arms or pulls and a pull never kills the day; POST_FILL_SELL evaluates only after a fill.",
        "Timing: a bar labelled 09:30 becomes visible at 09:31:00 ET. A LIVE decision can affect only later prints. Fill = first non-halt print above the fixed 09:25-09:29 high while armed, at that fixed price, with positive NBBO known no later than the fill.",
        "LIVE R3: MACD(12,26,9) histogram > 0; bar volume >= its prior-20-bar average; RSI(14,Wilder) rising; Fast Stoch K(10) rising. Candle colour and absolute RSI/Stoch floors are not LIVE checks.",
        "Look-back assignment A (volume-baseline): the 09:30 DAY_GATE requires the prior-20 volume baseline to exist. Assignment B (absolute-levels): the 09:30 DAY_GATE requires RSI and Stoch K >= 50. Both are reported; neither is silently selected.",
        f"POST_FILL_SELL parameters: body threshold {body_threshold_pct}% and measurement delay {delay_label}. Re-entry is disabled for this census after the first fill; that is a live-behaviour assumption, not an entry filter.",
        f"Legacy comparison denominator: {len(legacy_rows)} break rows on the same loaded tape; legacy PASS {sum(row.status == 'PASS' for row in legacy_rows)}/{len(legacy_rows)}.",
        "",
        "| assignment | legacy PASS / break rows | gate eligible / name-days | fill candidates / eligible | priced / candidates | unanswerable / candidates | post-fill SELL / priced | HOLD / priced | coverage |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    all_runs = [run for report in reports for run in report.runs]
    for assignment in assignments:
        runs = [run for run in all_runs if run.assignment == assignment]
        eligible = [run for run in runs if run.gate.status == "ELIGIBLE"]
        candidates = [
            run
            for run in eligible
            if any(row.action in {"FILL", "FILL_UNANSWERABLE"} for row in run.live)
        ]
        fills = [run for run in candidates if any(row.action == "FILL" for row in run.live)]
        unanswerable = [
            run
            for run in candidates
            if any(row.action == "FILL_UNANSWERABLE" for row in run.live)
        ]
        sells = [run for run in fills if run.post_fill is not None and run.post_fill.action == "SELL"]
        holds = [run for run in fills if run.post_fill is not None and run.post_fill.action == "HOLD"]
        lines.append(
            f"| {assignment} | {sum(row.status == 'PASS' for row in legacy_rows)}/{len(legacy_rows)} | "
            f"{len(eligible)}/{len(runs)} | {len(candidates)}/{len(eligible)} | "
            f"{len(fills)}/{len(candidates)} | {len(unanswerable)}/{len(candidates)} | "
            f"{len(sells)}/{len(fills)} | {len(holds)}/{len(fills)} | {coverage_label} |"
        )

    lines.extend(["", "DAIC 2026-08-25 control, same tape:"])
    daic_runs = [
        run
        for run in all_runs
        if run.day == date(2026, 8, 25) and run.symbol == "DAIC"
    ]
    if not daic_runs:
        lines.append(f"- UNANSWERABLE: DAIC is absent; denominator 0/{len(assignments)} assignments.")
    for run in daic_runs:
        fill = next((row for row in run.live if row.action == "FILL"), None)
        post = run.post_fill
        lines.append(
            f"- {run.assignment}: gate {run.gate.status}; fill "
            f"{clock(fill.fill_at) if fill and fill.fill_at else '-'} at {number(fill.fill_price, 4) if fill else '-'}; "
            f"post-fill {post.action if post else '-'}; denominator 1 name-day."
        )

    for report in reports:
        for assignment in assignments:
            runs = [run for run in report.runs if run.assignment == assignment]
            decisions = [decision for run in runs for decision in settled_decisions(run)]
            lines.extend(
                [
                    "",
                    f"{report.day.isoformat()} | assignment {assignment} | quote coverage {coverage_label} | denominator {len(decisions)} typed decisions across {len(runs)} watched name-days.",
                    "",
                    "| n | kind | sym | at ET | action | level | state | checks |",
                    "|---:|---|---|---:|---|---:|---|---|",
                ]
            )
            for index, decision in enumerate(decisions, start=1):
                if isinstance(decision, GateDecision):
                    action = decision.status
                    level = number(decision.opening_high, 4)
                    state = (
                        f"ATR={decision.atr_state or '-'}@{number(decision.atr_level, 4)} "
                        f"close={number(decision.close, 4)} red5={decision.red_0925_0929 if decision.red_0925_0929 is not None else '-'}"
                    )
                    at = decision.evaluated_at
                    checks = decision.checks
                elif isinstance(decision, LiveDecision):
                    action = decision.action
                    level = number(decision.opening_high, 4)
                    state = (
                        f"vol={number(decision.volume_ratio)} macd={decision.macd_green} "
                        f"rsi_up={decision.rsi_up} stoch_up={decision.stoch_up}"
                    )
                    if decision.fill_price is not None:
                        state += f" fill={number(decision.fill_price, 4)}"
                    at = decision.evaluated_at
                    checks = decision.checks
                else:
                    action = decision.action
                    level = number(decision.opening_high, 4)
                    state = (
                        f"body={number(decision.body_pct)}% threshold={number(decision.threshold_pct)}% "
                        f"exit={number(decision.exit_bid, 4)} return={number(decision.return_pct)}%"
                    )
                    at = decision.evaluated_at
                    checks = decision.checks
                lines.append(
                    f"| {index}/{len(decisions)} | {decision.kind} | {decision.symbol} | "
                    f"{clock(at)} | {action} | {level} | {state} | {'; '.join(checks)} |"
                )
    return "\n".join(lines)


def write_settled_csv(path: Path, reports: Sequence[SettledDayReport]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "day", "assignment", "kind", "symbol", "evaluated_at_et", "action",
        "opening_high", "fill_at_et", "fill_price", "exit_at_et", "exit_bid",
        "return_pct", "atr_state", "atr_level", "close", "red_0925_0929",
        "volume_ratio", "macd_green", "rsi_up", "stoch_up", "body_pct",
        "body_threshold_pct", "body_delay", "checks", "qualification",
    )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for report in reports:
            for run in report.runs:
                for decision in settled_decisions(run):
                    row = {field: "" for field in fields}
                    row.update(
                        {
                            "day": run.day,
                            "assignment": run.assignment,
                            "kind": decision.kind,
                            "symbol": run.symbol,
                            "evaluated_at_et": decision.evaluated_at.astimezone(EASTERN).isoformat(),
                            "action": decision.status if isinstance(decision, GateDecision) else decision.action,
                            "opening_high": decision.opening_high or "",
                            "checks": "; ".join(decision.checks),
                            "qualification": DISCLOSURE,
                        }
                    )
                    if isinstance(decision, GateDecision):
                        row.update(
                            {
                                "atr_state": decision.atr_state or "",
                                "atr_level": decision.atr_level or "",
                                "close": decision.close or "",
                                "red_0925_0929": decision.red_0925_0929 if decision.red_0925_0929 is not None else "",
                            }
                        )
                    elif isinstance(decision, LiveDecision):
                        row.update(
                            {
                                "fill_at_et": decision.fill_at.astimezone(EASTERN).isoformat() if decision.fill_at else "",
                                "fill_price": decision.fill_price or "",
                                "volume_ratio": decision.volume_ratio or "",
                                "macd_green": decision.macd_green if decision.macd_green is not None else "",
                                "rsi_up": decision.rsi_up if decision.rsi_up is not None else "",
                                "stoch_up": decision.stoch_up if decision.stoch_up is not None else "",
                            }
                        )
                    else:
                        row.update(
                            {
                                "fill_at_et": decision.fill_at.astimezone(EASTERN).isoformat(),
                                "fill_price": decision.fill_price,
                                "exit_at_et": decision.exit_at.astimezone(EASTERN).isoformat() if decision.exit_at else "",
                                "exit_bid": decision.exit_bid or "",
                                "return_pct": decision.return_pct if decision.return_pct is not None else "",
                                "body_pct": decision.body_pct if decision.body_pct is not None else "",
                                "body_threshold_pct": decision.threshold_pct,
                                "body_delay": decision.delay_label,
                            }
                        )
                    writer.writerow(row)


def parse_body_delay(value: str) -> tuple[timedelta | None, str]:
    if value == DEFAULT_BODY_MEASUREMENT_DELAY:
        return None, value
    try:
        seconds = Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError("body delay must be 'bar-close' or seconds") from exc
    if seconds < 0:
        raise argparse.ArgumentTypeError("body delay seconds must be non-negative")
    return timedelta(seconds=float(seconds)), f"{seconds.normalize()}s-after-fill"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--csv", type=Path)
    parser.add_argument(
        "--lookback-assignment",
        choices=("both", *LOOKBACK_ASSIGNMENTS),
        default="both",
    )
    parser.add_argument(
        "--body-threshold-pct",
        type=Decimal,
        default=DEFAULT_BODY_THRESHOLD_PCT,
    )
    parser.add_argument(
        "--body-measurement-delay",
        type=parse_body_delay,
        default=(None, DEFAULT_BODY_MEASUREMENT_DELAY),
        metavar="bar-close|SECONDS",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start_date > args.end_date:
        raise SystemExit("start date must not be after end date")
    if not Decimal("0") <= args.body_threshold_pct <= Decimal("100"):
        raise SystemExit("body threshold must be between 0 and 100 percent")
    settings = get_settings()
    session_factory = build_session_factory(settings)
    coverage = load_quote_coverage(session_factory)
    requested_days = trading_days(args.start_date, args.end_date)
    reachable_days = [
        day
        for day in requested_days
        if coverage.first_day is not None
        and coverage.last_day is not None
        and coverage.first_day <= day <= coverage.last_day
    ]
    unreachable_days = [day for day in requested_days if day not in reachable_days]
    assignments: tuple[LookbackAssignment, ...] = (
        LOOKBACK_ASSIGNMENTS
        if args.lookback_assignment == "both"
        else (args.lookback_assignment,)
    )
    body_delay, delay_label = args.body_measurement_delay
    reports = [
        build_settled_day_report(
            session_factory,
            settings,
            day,
            assignments=assignments,
            body_threshold_pct=args.body_threshold_pct,
            body_delay=body_delay,
            delay_label=delay_label,
        )
        for day in reachable_days
    ]
    print(
        render_settled(
            reports,
            coverage=coverage,
            requested_days=requested_days,
            unreachable_days=unreachable_days,
            assignments=assignments,
            body_threshold_pct=args.body_threshold_pct,
            delay_label=delay_label,
        )
    )
    if args.csv:
        write_settled_csv(args.csv, reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
