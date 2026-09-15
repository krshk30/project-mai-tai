"""Measure what follows a confirmed stock's first genuine ATR trade loss.

This is a read-only signal study.  A trade begins only on a canonical ATR BUY
bar close, so an intrabar resting-order touch that never confirms a BUY flip is
excluded.  The common entry reference is that BUY bar's close; exact broker
fills are a separately labelled subset because quote retention does not cover
the full study month.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.backtest.confirmed_selection_census import (
    ConfirmEvent,
    EvidenceUnknown,
    SelectionDataSource,
    SymbolDayInput,
    _assert_after_close,
    _bar_at,
    _confirmed_keys,
    _load_project_environment,
    _parse_date,
    _read_only_session_factory,
)
from project_mai_tai.backtest.watch_start import WatchWindow, build_windows

ENTRY_START = time(7)
SESSION_END = time(16)
TARGET_PCT = 5.0
STOP_PCT = 8.0
MIN_LATER_OPPORTUNITIES = 30
MIN_FIRST_STOP_DAYS_WITH_LATER = 20
_LEAVE_EVENTS = {"FADE", "RETENTION_DROP"}
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class AtrOpportunity:
    day: date
    symbol: str
    sequence: int
    flip_at: datetime
    decision_at: datetime
    entry_price: float
    scanner_window_start: datetime
    exit_kind: str
    exit_at: datetime | None
    exit_price: float | None
    return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    guaranteed_mfe_before_stop_pct: float | None
    possible_mfe_before_stop_pct: float | None


@dataclass(frozen=True)
class SymbolDayStudy:
    day: date
    symbol: str
    bar_count: int
    bar_gap_count: int
    opportunities: tuple[AtrOpportunity, ...]
    post_first_stop_mfe_pct: float | None
    post_first_stop_reached_original_target: bool | None


@dataclass(frozen=True)
class OutcomeGroup:
    first_outcome: str
    symbol_days: int
    with_later_opportunity: int
    later_opportunities: int
    later_gradable: int
    later_targets: int
    later_stops: int
    later_atr_sells: int
    later_session_closes: int
    later_unknown: int
    later_target_rate_pct: float | None
    later_return_sum_pct_points: float | None
    days_with_any_later_target: int


@dataclass(frozen=True)
class RuleAssessment:
    first_stop_symbol_days: int
    first_stop_days_with_later: int
    later_opportunities: int
    later_gradable: int
    later_return_sum_pct_points: float | None
    sample_floor_met: bool
    later_pnl_negative: bool
    survives_drop_one_symbol: bool
    survives_drop_one_day: bool
    pass_criterion: bool


@dataclass(frozen=True)
class StudyReport:
    start: date
    end: date
    confirmed_symbol_days: int
    bar_measurable_symbol_days: int
    no_live_bar_symbol_days: int
    symbol_days_with_eligible_flip: int
    symbol_days_without_eligible_flip: int
    opportunities: int
    first_outcomes: dict[str, int]
    first_decisive_outcomes: dict[str, int]
    first_decisive_stop_group: OutcomeGroup
    groups: tuple[OutcomeGroup, ...]
    rule: RuleAssessment
    symbol_days: tuple[SymbolDayStudy, ...]


def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=ET)


class DbFirstLossDataSource:
    """Load only scanner events and Schwab bars needed by this study."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def load(self, start: date, end: date) -> list[SymbolDayInput]:
        with self._sf() as session:
            rows = session.execute(
                text(
                    "SELECT trade_date,symbol,event_type,event_at,price,change_pct,confirm_path "
                    "FROM scanner_confirmed_events WHERE trade_date BETWEEN :start AND :end "
                    "ORDER BY trade_date,symbol,event_at,id"
                ),
                {"start": start, "end": end},
            ).all()
        events: dict[tuple[date, str], list[ConfirmEvent]] = defaultdict(list)
        for day, symbol, event_type, at, price, change_pct, path in rows:
            events[(day, str(symbol).upper())].append(
                ConfirmEvent(
                    str(event_type),
                    at.astimezone(ET),
                    float(price) if price is not None else None,
                    float(change_pct) if change_pct is not None else None,
                    str(path) if path else None,
                )
            )
        confirmed = _confirmed_keys(events)
        by_day: dict[date, list[str]] = defaultdict(list)
        for day, symbol in confirmed:
            by_day[day].append(symbol)

        output: list[SymbolDayInput] = []
        for day in sorted(by_day):
            bars = self._bars(day, by_day[day])
            for symbol in by_day[day]:
                output.append(
                    SymbolDayInput(
                        day,
                        symbol,
                        tuple(events[(day, symbol)]),
                        tuple(bars.get(symbol, ())),
                    )
                )
        return output

    def _bars(self, day: date, symbols: Sequence[str]) -> dict[str, list[Bar]]:
        with self._sf() as session:
            rows = session.execute(
                text(
                    "SELECT symbol,bar_time,open_price,high_price,low_price,close_price,volume "
                    "FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' "
                    "AND interval_secs=60 AND source='live' AND symbol=ANY(:symbols) "
                    "AND bar_time>=:lo AND bar_time<:hi ORDER BY symbol,bar_time"
                ),
                {
                    "symbols": list(symbols),
                    "lo": _at(day, time(4)),
                    "hi": _at(day, SESSION_END),
                },
            ).all()
        result: dict[str, list[Bar]] = defaultdict(list)
        for symbol, at, open_, high, low, close, volume in rows:
            result[str(symbol).upper()].append(
                Bar(
                    ts=int(at.timestamp() * 1000),
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=int(volume or 0),
                )
            )
        return result


def _scanner_windows(item: SymbolDayInput) -> tuple[WatchWindow, ...]:
    events = [
        (event.event_type, int(event.at.timestamp() * 1000))
        for event in item.scanner_events
        if event.event_type == "CONFIRM" or event.event_type in _LEAVE_EVENTS
    ]
    return tuple(build_windows(events))


def _eligible_window(
    windows: Sequence[WatchWindow], flip_at: datetime, decision_at: datetime
) -> WatchWindow | None:
    """Require membership for the whole flip bar and a pre-existing watch.

    Live caps an arm whose bar timestamp is at or before the watch start.  A
    symbol that leaves before the close is no longer present when the BUY is
    knowable, so both bar start and decision time must be inside one window.
    """

    flip_ms = int(flip_at.timestamp() * 1000)
    decision_ms = int(decision_at.timestamp() * 1000)
    return next(
        (
            window
            for window in windows
            if window.start_ms < flip_ms
            and window.contains(flip_ms)
            and window.contains(decision_ms)
        ),
        None,
    )


def _pct(price: float, entry: float) -> float:
    return (price / entry - 1.0) * 100.0


def evaluate_opportunity(
    *,
    day: date,
    symbol: str,
    sequence: int,
    bars: Sequence[Bar],
    atr_rows: Sequence[dict[str, object]],
    index: int,
    window: WatchWindow,
) -> AtrOpportunity:
    flip = bars[index]
    flip_at = _bar_at(flip)
    decision_at = flip_at + timedelta(minutes=1)
    entry = float(flip.close)
    target = entry * (1.0 + TARGET_PCT / 100.0)
    stop = entry * (1.0 - STOP_PCT / 100.0)
    observed_highs: list[float] = []
    observed_lows: list[float] = []
    previous_at = flip_at

    for future in range(index + 1, len(bars)):
        bar = bars[future]
        at = _bar_at(bar)
        if at >= _at(day, SESSION_END):
            break
        if at - previous_at > timedelta(minutes=1):
            window_start = datetime.fromtimestamp(window.start_ms / 1000, UTC).astimezone(
                flip_at.tzinfo
            )
            return AtrOpportunity(
                day,
                symbol,
                sequence,
                flip_at,
                decision_at,
                entry,
                window_start,
                "UNKNOWN_BAR_GAP",
                at,
                None,
                None,
                max((_pct(value, entry) for value in observed_highs), default=None),
                min((_pct(value, entry) for value in observed_lows), default=None),
                None,
                None,
            )
        previous_at = at
        high = float(bar.high)
        low = float(bar.low)
        target_hit = high >= target
        stop_hit = low <= stop
        prior_mfe = max((_pct(value, entry) for value in observed_highs), default=0.0)
        possible_mfe = max(prior_mfe, _pct(high, entry))
        observed_highs.append(high)
        observed_lows.append(low)
        mfe = max(_pct(value, entry) for value in observed_highs)
        mae = min(_pct(value, entry) for value in observed_lows)

        if target_hit and stop_hit:
            return AtrOpportunity(
                day,
                symbol,
                sequence,
                flip_at,
                decision_at,
                entry,
                datetime.fromtimestamp(window.start_ms / 1000, UTC).astimezone(flip_at.tzinfo),
                "UNKNOWN_INTRABAR_ORDER",
                at,
                None,
                None,
                mfe,
                mae,
                None,
                None,
            )
        if target_hit:
            return AtrOpportunity(
                day,
                symbol,
                sequence,
                flip_at,
                decision_at,
                entry,
                datetime.fromtimestamp(window.start_ms / 1000, UTC).astimezone(flip_at.tzinfo),
                "TARGET_5",
                at,
                target,
                TARGET_PCT,
                mfe,
                mae,
                None,
                None,
            )
        if stop_hit:
            return AtrOpportunity(
                day,
                symbol,
                sequence,
                flip_at,
                decision_at,
                entry,
                datetime.fromtimestamp(window.start_ms / 1000, UTC).astimezone(flip_at.tzinfo),
                "STOP_8",
                at,
                stop,
                -STOP_PCT,
                mfe,
                mae,
                max(0.0, prior_mfe),
                max(0.0, possible_mfe),
            )
        if atr_rows[future].get("flip") == "SELL":
            close = float(bar.close)
            return AtrOpportunity(
                day,
                symbol,
                sequence,
                flip_at,
                decision_at,
                entry,
                datetime.fromtimestamp(window.start_ms / 1000, UTC).astimezone(flip_at.tzinfo),
                "ATR_SELL",
                at + timedelta(minutes=1),
                close,
                _pct(close, entry),
                mfe,
                mae,
                None,
                None,
            )

    window_start = datetime.fromtimestamp(window.start_ms / 1000, UTC).astimezone(flip_at.tzinfo)
    if not observed_highs:
        return AtrOpportunity(
            day,
            symbol,
            sequence,
            flip_at,
            decision_at,
            entry,
            window_start,
            "UNKNOWN_NO_FUTURE_BAR",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    last = next(
        (bar for bar in reversed(bars[index + 1 :]) if _bar_at(bar) < _at(day, SESSION_END)),
        None,
    )
    assert last is not None
    mfe = max(_pct(value, entry) for value in observed_highs)
    mae = min(_pct(value, entry) for value in observed_lows)
    if _bar_at(last).time() < time(15, 59):
        return AtrOpportunity(
            day,
            symbol,
            sequence,
            flip_at,
            decision_at,
            entry,
            window_start,
            "UNKNOWN_INCOMPLETE_SESSION",
            _bar_at(last) + timedelta(minutes=1),
            None,
            None,
            mfe,
            mae,
            None,
            None,
        )
    close = float(last.close)
    return AtrOpportunity(
        day,
        symbol,
        sequence,
        flip_at,
        decision_at,
        entry,
        window_start,
        "SESSION_CLOSE",
        _bar_at(last) + timedelta(minutes=1),
        close,
        _pct(close, entry),
        mfe,
        mae,
        None,
        None,
    )


def _bar_gaps(bars: Sequence[Bar]) -> int:
    ordered = sorted(bars, key=lambda bar: bar.ts)
    return sum(
        max(0, int((right.ts - left.ts) // 60_000) - 1)
        for left, right in zip(ordered, ordered[1:], strict=False)
    )


def _post_stop_recovery(
    bars: Sequence[Bar], first: AtrOpportunity
) -> tuple[float | None, bool | None]:
    assert first.exit_at is not None
    later = [bar for bar in bars if _bar_at(bar) > first.exit_at]
    if not later:
        return None, None
    mfe = max(_pct(float(bar.high), first.entry_price) for bar in later)
    if mfe >= TARGET_PCT:
        return mfe, True
    timestamps = [_bar_at(bar) for bar in later]
    complete = (
        timestamps[0] == first.exit_at + timedelta(minutes=1)
        and timestamps[-1].time() >= time(15, 59)
        and all(
            right - left <= timedelta(minutes=1)
            for left, right in zip(timestamps, timestamps[1:], strict=False)
        )
    )
    return mfe, False if complete else None


def evaluate_symbol_day(item: SymbolDayInput) -> SymbolDayStudy:
    bars = tuple(
        bar
        for bar in sorted(item.schwab_bars, key=lambda row: row.ts)
        if _at(item.day, time(4)) <= _bar_at(bar) < _at(item.day, SESSION_END)
    )
    atr_rows = compute_atr_trail(bars, seed="sma5", period=5, factor=3.5)
    windows = _scanner_windows(item)
    opportunities: list[AtrOpportunity] = []
    for index, atr in enumerate(atr_rows):
        flip_at = _bar_at(bars[index])
        if atr.get("flip") != "BUY" or flip_at.time() < ENTRY_START:
            continue
        decision_at = flip_at + timedelta(minutes=1)
        window = _eligible_window(windows, flip_at, decision_at)
        if window is None:
            continue
        opportunities.append(
            evaluate_opportunity(
                day=item.day,
                symbol=item.symbol,
                sequence=len(opportunities) + 1,
                bars=bars,
                atr_rows=atr_rows,
                index=index,
                window=window,
            )
        )

    post_stop_mfe = None
    post_stop_target = None
    if opportunities and opportunities[0].exit_kind == "STOP_8":
        first = opportunities[0]
        post_stop_mfe, post_stop_target = _post_stop_recovery(bars, first)
    return SymbolDayStudy(
        item.day,
        item.symbol,
        len(bars),
        _bar_gaps(bars),
        tuple(opportunities),
        post_stop_mfe,
        post_stop_target,
    )


def _summarize_selected(selected: Sequence[tuple[SymbolDayStudy, int]], label: str) -> OutcomeGroup:
    later = [
        opportunity
        for day, selected_index in selected
        for opportunity in day.opportunities[selected_index + 1 :]
    ]
    gradable = [opportunity for opportunity in later if opportunity.return_pct is not None]
    targets = sum(opportunity.exit_kind == "TARGET_5" for opportunity in later)
    total_return = sum(opportunity.return_pct for opportunity in gradable)
    return OutcomeGroup(
        first_outcome=label,
        symbol_days=len(selected),
        with_later_opportunity=sum(
            len(day.opportunities) > selected_index + 1 for day, selected_index in selected
        ),
        later_opportunities=len(later),
        later_gradable=len(gradable),
        later_targets=targets,
        later_stops=sum(opportunity.exit_kind == "STOP_8" for opportunity in later),
        later_atr_sells=sum(opportunity.exit_kind == "ATR_SELL" for opportunity in later),
        later_session_closes=sum(opportunity.exit_kind == "SESSION_CLOSE" for opportunity in later),
        later_unknown=len(later) - len(gradable),
        later_target_rate_pct=100.0 * targets / len(gradable) if gradable else None,
        later_return_sum_pct_points=total_return if gradable else None,
        days_with_any_later_target=sum(
            any(
                opportunity.exit_kind == "TARGET_5"
                for opportunity in day.opportunities[selected_index + 1 :]
            )
            for day, selected_index in selected
        ),
    )


def _group(days: Sequence[SymbolDayStudy], first_outcome: str) -> OutcomeGroup:
    selected = [(day, 0) for day in days if day.opportunities[0].exit_kind == first_outcome]
    return _summarize_selected(selected, first_outcome)


def _first_decisive(day: SymbolDayStudy) -> tuple[int, AtrOpportunity] | None:
    """Find the first +5/-8 result without promoting a trade past unknown evidence."""

    for index, opportunity in enumerate(day.opportunities):
        if opportunity.exit_kind.startswith("UNKNOWN"):
            return None
        if opportunity.exit_kind in {"TARGET_5", "STOP_8"}:
            return index, opportunity
    return None


def _later_return(days: Sequence[SymbolDayStudy]) -> tuple[int, float | None]:
    gradable = [
        opportunity
        for day in days
        for opportunity in day.opportunities[1:]
        if opportunity.return_pct is not None
    ]
    return len(gradable), sum(
        opportunity.return_pct for opportunity in gradable
    ) if gradable else None


def _negative_after_drop_one(days: Sequence[SymbolDayStudy], attr: str) -> bool:
    values = sorted({getattr(day, attr) for day in days})
    if not values:
        return False
    for value in values:
        count, total = _later_return([day for day in days if getattr(day, attr) != value])
        if count == 0 or total is None or total >= 0:
            return False
    return True


def _rule_assessment(days: Sequence[SymbolDayStudy]) -> RuleAssessment:
    stopped = [day for day in days if day.opportunities[0].exit_kind == "STOP_8"]
    later = [opportunity for day in stopped for opportunity in day.opportunities[1:]]
    gradable_count, total = _later_return(stopped)
    with_later = sum(bool(day.opportunities[1:]) for day in stopped)
    sample_floor = (
        gradable_count >= MIN_LATER_OPPORTUNITIES and with_later >= MIN_FIRST_STOP_DAYS_WITH_LATER
    )
    negative = total is not None and total < 0
    by_symbol = _negative_after_drop_one(stopped, "symbol")
    by_day = _negative_after_drop_one(stopped, "day")
    return RuleAssessment(
        first_stop_symbol_days=len(stopped),
        first_stop_days_with_later=with_later,
        later_opportunities=len(later),
        later_gradable=gradable_count,
        later_return_sum_pct_points=total,
        sample_floor_met=sample_floor,
        later_pnl_negative=negative,
        survives_drop_one_symbol=by_symbol,
        survives_drop_one_day=by_day,
        pass_criterion=sample_floor and negative and by_symbol and by_day,
    )


def run_study(source: SelectionDataSource, start: date, end: date) -> StudyReport:
    inputs = source.load(start, end)
    measured = [item for item in inputs if item.schwab_bars]
    days = tuple(evaluate_symbol_day(item) for item in measured)
    with_flip = [day for day in days if day.opportunities]
    first_counts = Counter(day.opportunities[0].exit_kind for day in with_flip)
    decisive = [
        (day, selected) for day in with_flip if (selected := _first_decisive(day)) is not None
    ]
    decisive_counts = Counter(selected[1].exit_kind for _day, selected in decisive)
    decisive_stops = [
        (day, selected[0]) for day, selected in decisive if selected[1].exit_kind == "STOP_8"
    ]
    group_order = ("STOP_8", "TARGET_5", "ATR_SELL", "SESSION_CLOSE")
    extra = sorted(set(first_counts) - set(group_order))
    return StudyReport(
        start=start,
        end=end,
        confirmed_symbol_days=len(inputs),
        bar_measurable_symbol_days=len(measured),
        no_live_bar_symbol_days=len(inputs) - len(measured),
        symbol_days_with_eligible_flip=len(with_flip),
        symbol_days_without_eligible_flip=len(measured) - len(with_flip),
        opportunities=sum(len(day.opportunities) for day in with_flip),
        first_outcomes=dict(sorted(first_counts.items())),
        first_decisive_outcomes=dict(sorted(decisive_counts.items())),
        first_decisive_stop_group=_summarize_selected(decisive_stops, "FIRST_DECISIVE_STOP_8"),
        groups=tuple(_group(with_flip, kind) for kind in (*group_order, *extra)),
        rule=_rule_assessment(with_flip),
        symbol_days=days,
    )


def _fmt(value: float | None, digits: int = 2) -> str:
    return "UNKNOWN" if value is None else f"{value:.{digits}f}"


def _clock(value: datetime | None) -> str:
    return "UNKNOWN" if value is None else value.strftime("%H:%M")


def render_markdown(report: StudyReport) -> str:
    with_flip = [day for day in report.symbol_days if day.opportunities]
    stopped = [day for day in with_flip if day.opportunities[0].exit_kind == "STOP_8"]
    decisive_rows = [
        (day, selected) for day in with_flip if (selected := _first_decisive(day)) is not None
    ]
    decisive_stopped = [
        (day, selected_index, opportunity)
        for day, (selected_index, opportunity) in decisive_rows
        if opportunity.exit_kind == "STOP_8"
    ]
    guaranteed = [
        day.opportunities[0].guaranteed_mfe_before_stop_pct
        for day in stopped
        if day.opportunities[0].guaranteed_mfe_before_stop_pct is not None
    ]
    possible = [
        day.opportunities[0].possible_mfe_before_stop_pct
        for day in stopped
        if day.opportunities[0].possible_mfe_before_stop_pct is not None
    ]
    recovery_measured = [
        day for day in stopped if day.post_first_stop_reached_original_target is not None
    ]
    recovered = sum(
        day.post_first_stop_reached_original_target is True for day in recovery_measured
    )
    lines = [
        "# First ATR loss and rest-of-day follow-through",
        "",
        f"Range: {report.start} through {report.end}, ET. Read-only; no scanner or trading changes.",
        "",
        "## Population",
        "",
        "| Measure | Result | Denominator |",
        "|---|---:|---|",
        f"| Confirmed symbol-days | {report.confirmed_symbol_days} | distinct day/name pairs with CONFIRM |",
        f"| Bar-measurable | {report.bar_measurable_symbol_days}/{report.confirmed_symbol_days} | confirmed symbol-days |",
        f"| No live Schwab bars | {report.no_live_bar_symbol_days}/{report.confirmed_symbol_days} | confirmed symbol-days |",
        f"| At least one eligible real BUY flip | {report.symbol_days_with_eligible_flip}/{report.bar_measurable_symbol_days} | bar-measurable symbol-days |",
        f"| No eligible BUY flip | {report.symbol_days_without_eligible_flip}/{report.bar_measurable_symbol_days} | bar-measurable symbol-days |",
        f"| Eligible BUY opportunities | {report.opportunities} | {report.symbol_days_with_eligible_flip} symbol-days with a flip |",
        "",
        "An eligible flip is a canonical ATR BUY bar that began and closed while the symbol was in one scanner-confirmed membership window, and whose bar began after the watch start. Intrabar touches with no BUY close are false flips and do not enter the denominator.",
        "",
        "## Literal first eligible BUY",
        "",
        "| Outcome | Count | Denominator |",
        "|---|---:|---|",
    ]
    for kind, count in report.first_outcomes.items():
        lines.append(
            f"| {kind} | {count}/{report.symbol_days_with_eligible_flip} | symbol-days with an eligible BUY flip |"
        )
    decisive_total = sum(report.first_decisive_outcomes.values())
    lines.extend(
        [
            "",
            "## First decisive +5/-8 outcome (secondary)",
            "",
            "This descriptive view skips known ATR-sell outcomes until the first +5/-8 result, matching the operator's wording and bringing MYSZ's 09:34 ET stop into view. It never skips an UNKNOWN. The pre-registered verdict below still uses the literal first eligible BUY and was not rewritten after results were seen.",
            "",
            "| Outcome | Count | Denominator |",
            "|---|---:|---|",
        ]
    )
    for kind, count in report.first_decisive_outcomes.items():
        lines.append(
            f"| {kind} | {count}/{decisive_total} | symbol-days with a gradable first decisive outcome |"
        )
    decisive_group = report.first_decisive_stop_group
    decisive_mfe = [
        opportunity.guaranteed_mfe_before_stop_pct
        for _day, _selected_index, opportunity in decisive_stopped
        if opportunity.guaranteed_mfe_before_stop_pct is not None
    ]
    lines.extend(
        [
            f"| First decisive STOP_8 with a later BUY | {decisive_group.with_later_opportunity}/{decisive_group.symbol_days} | first-decisive-stop symbol-days |",
            f"| Median guaranteed upside before the stop | {_fmt(statistics.median(decisive_mfe) if decisive_mfe else None)}% | {len(decisive_mfe)}/{decisive_group.symbol_days} first-decisive-stop rows |",
            f"| Guaranteed upside >= +3% before the stop | {sum(value >= 3 for value in decisive_mfe)}/{len(decisive_mfe)} | first-decisive-stop rows with measurable MFE |",
            f"| Guaranteed upside >= +4% before the stop | {sum(value >= 4 for value in decisive_mfe)}/{len(decisive_mfe)} | first-decisive-stop rows with measurable MFE |",
            f"| Later +5 targets after first decisive STOP_8 | {decisive_group.later_targets}/{decisive_group.later_gradable} | gradable later opportunities |",
            f"| Days with any later +5 target | {decisive_group.days_with_any_later_target}/{decisive_group.with_later_opportunity} | first-decisive-stop days with a later BUY |",
            f"| Later equal-weight return sum | {_fmt(decisive_group.later_return_sum_pct_points)} pts | {decisive_group.later_gradable} gradable later opportunities |",
            "",
            "## Literal first -8% loss: upside before and after",
            "",
            "The guaranteed MFE excludes the stop bar because one-minute OHLC cannot tell whether that bar's high came before or after its low. The possible MFE includes it. This prevents a +3% or +4% print after the stop from being reported as pre-stop opportunity.",
            "",
            "| Measure | Result | Denominator |",
            "|---|---:|---|",
            f"| First trade stopped -8% | {len(stopped)}/{report.symbol_days_with_eligible_flip} | symbol-days with an eligible BUY flip |",
            f"| Median guaranteed pre-stop MFE | {_fmt(statistics.median(guaranteed) if guaranteed else None)}% | {len(guaranteed)}/{len(stopped)} first-stop rows |",
            f"| Median possible pre-stop MFE | {_fmt(statistics.median(possible) if possible else None)}% | {len(possible)}/{len(stopped)} first-stop rows |",
            f"| Guaranteed pre-stop MFE >= +1% | {sum(value >= 1 for value in guaranteed)}/{len(guaranteed)} | first-stop rows with measurable MFE |",
            f"| Guaranteed pre-stop MFE >= +2% | {sum(value >= 2 for value in guaranteed)}/{len(guaranteed)} | first-stop rows with measurable MFE |",
            f"| Guaranteed pre-stop MFE >= +3% | {sum(value >= 3 for value in guaranteed)}/{len(guaranteed)} | first-stop rows with measurable MFE |",
            f"| Guaranteed pre-stop MFE >= +4% | {sum(value >= 4 for value in guaranteed)}/{len(guaranteed)} | first-stop rows with measurable MFE |",
            f"| Possible pre-stop MFE >= +1% | {sum(value >= 1 for value in possible)}/{len(possible)} | first-stop rows with measurable MFE |",
            f"| Possible pre-stop MFE >= +2% | {sum(value >= 2 for value in possible)}/{len(possible)} | first-stop rows with measurable MFE |",
            f"| Possible pre-stop MFE >= +3% | {sum(value >= 3 for value in possible)}/{len(possible)} | first-stop rows with measurable MFE |",
            f"| Possible pre-stop MFE >= +4% | {sum(value >= 4 for value in possible)}/{len(possible)} | first-stop rows with measurable MFE |",
            f"| Reached original +5% after the stop | {recovered}/{len(recovery_measured)} | first-stop rows with complete negative evidence or a visible recovery |",
            f"| Post-stop recovery UNKNOWN | {len(stopped) - len(recovery_measured)}/{len(stopped)} | first-stop symbol-days |",
            "",
            "## What happened later that day",
            "",
            "| First outcome | Symbol-days | Days with later flip | Later opportunities | Later targets | Later stops | ATR sells | 16:00 closes | UNKNOWN | Target rate | Return sum | Days with later target |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group in report.groups:
        lines.append(
            f"| {group.first_outcome} | {group.symbol_days} | {group.with_later_opportunity}/{group.symbol_days} | {group.later_opportunities} | {group.later_targets}/{group.later_gradable} | {group.later_stops}/{group.later_gradable} | {group.later_atr_sells}/{group.later_gradable} | {group.later_session_closes}/{group.later_gradable} | {group.later_unknown}/{group.later_opportunities} | {_fmt(group.later_target_rate_pct)}% | {_fmt(group.later_return_sum_pct_points)} pts | {group.days_with_any_later_target}/{group.with_later_opportunity} |"
        )
    rule = report.rule
    lines.extend(
        [
            "",
            "## Pre-registered rule test",
            "",
            "A block-after-first-stop rule passes only with at least 30 gradable later opportunities across at least 20 first-stop symbol-days, negative equal-weight later return, and negative return after dropping every one symbol and every one session day.",
            "",
            "| Later gradable | First-stop days with later flip | Return | Drop-one symbol | Drop-one day | Verdict |",
            "|---:|---:|---:|---|---|---|",
            f"| {rule.later_gradable}/{rule.later_opportunities} | {rule.first_stop_days_with_later}/{rule.first_stop_symbol_days} | {_fmt(rule.later_return_sum_pct_points)} pts | {rule.survives_drop_one_symbol} | {rule.survives_drop_one_day} | {'PASS' if rule.pass_criterion else 'FAIL'} |",
            "",
            "## First-stop detail",
            "",
            "| Day | Symbol | First BUY ET | Stop ET | Entry | Guaranteed MFE | Possible MFE | Post-stop day MFE | Original +5 later | Later flips | Later outcomes | Later return | Bar gaps |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|---:|---|---:|---:|",
        ]
    )
    for day in stopped:
        first = day.opportunities[0]
        later = day.opportunities[1:]
        later_return = sum(
            opportunity.return_pct for opportunity in later if opportunity.return_pct is not None
        )
        outcomes = (
            ", ".join(
                f"{opportunity.flip_at:%H:%M} {opportunity.exit_kind}" for opportunity in later
            )
            or "none"
        )
        lines.append(
            f"| {day.day} | {day.symbol} | {first.flip_at:%H:%M} | {_clock(first.exit_at)} | {first.entry_price:.4f} | {_fmt(first.guaranteed_mfe_before_stop_pct)}% | {_fmt(first.possible_mfe_before_stop_pct)}% | {_fmt(day.post_first_stop_mfe_pct)}% | {day.post_first_stop_reached_original_target} | {len(later)} | {outcomes} | {later_return:.2f} pts | {day.bar_gap_count} |"
        )
    lines.extend(
        [
            "",
            "## First-decisive-stop detail (secondary)",
            "",
            "| Day | Symbol | Decisive trade # | Earlier known outcomes | Stop BUY ET | Stop ET | Guaranteed MFE | Later flips | Later targets | Later outcomes | Later return |",
            "|---|---|---:|---|---:|---:|---:|---:|---:|---|---:|",
        ]
    )
    for day, selected_index, first in decisive_stopped:
        earlier = (
            ", ".join(opportunity.exit_kind for opportunity in day.opportunities[:selected_index])
            or "none"
        )
        later = day.opportunities[selected_index + 1 :]
        later_return = sum(
            opportunity.return_pct for opportunity in later if opportunity.return_pct is not None
        )
        outcomes = (
            ", ".join(
                f"{opportunity.flip_at:%H:%M} {opportunity.exit_kind}" for opportunity in later
            )
            or "none"
        )
        lines.append(
            f"| {day.day} | {day.symbol} | {selected_index + 1} | {earlier} | {first.flip_at:%H:%M} | {_clock(first.exit_at)} | {_fmt(first.guaranteed_mfe_before_stop_pct)}% | {len(later)} | {sum(opportunity.exit_kind == 'TARGET_5' for opportunity in later)} | {outcomes} | {later_return:.2f} pts |"
        )
    lines.extend(
        [
            "",
            "## Limits",
            "",
            "- Entry is the canonical BUY-flip bar close. Live resting fills occur near the pre-flip ATR trail and can differ by spread and the 0.5% stop-limit band. This full-month table tests signal sequence, not exact broker execution.",
            "- Target and stop are first-touch on one-minute Schwab OHLC. A bar touching both is UNKNOWN. Exact tick order is never invented.",
            "- The canonical oracle reproduces the stored Schwab series and has no pre-entry bar-gap guard. Once a modeled trade is open, the first missing minute makes its outcome UNKNOWN_BAR_GAP. Every symbol-day's gap count is preserved in the JSON and shown for first-stop rows.",
            "- Percent returns are equal-weight percentage points, not dollars and not a portfolio simulation.",
        ]
    )
    return "\n".join(lines) + "\n"


def report_json(report: StudyReport) -> dict[str, object]:
    def encode(value: object) -> object:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, tuple):
            return [encode(item) for item in value]
        if isinstance(value, dict):
            return {str(key): encode(item) for key, item in value.items()}
        if hasattr(value, "__dataclass_fields__"):
            return {field.name: encode(getattr(value, field.name)) for field in fields(value)}
        return value

    return encode(report)  # type: ignore[return-value]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m project_mai_tai.backtest.atr_first_loss_followthrough"
    )
    parser.add_argument(
        "--range", nargs=2, required=True, metavar=("START", "END"), type=_parse_date
    )
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args(argv)
    start, end = args.range
    if end < start:
        parser.error("range end precedes range start")
    try:
        _assert_after_close(end)
        database_url = _load_project_environment().get("MAI_TAI_DATABASE_URL", "").strip()
        if not database_url:
            raise EvidenceUnknown("MAI_TAI_DATABASE_URL is unavailable")
        source = DbFirstLossDataSource(_read_only_session_factory(database_url))
        report = run_study(source, start, end)
    except (EvidenceUnknown, OSError, SQLAlchemyError, ValueError) as exc:
        print(f"CANNOT_TELL: {exc}")
        return 2
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(report_json(report), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    print(f"JSON: {args.json}")
    print(f"Markdown: {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
