"""Read-only selection census for confirmed-stock ATR BUY flips.

The study is deliberately separate from live strategy code. It replays the
canonical ATR oracle over stored Schwab bars, measures only information that
existed before each BUY-flip bar, and asks whether the subsequent bars reached
+5% before the next SELL flip (16:00 ET backstop).

Pre-registered pass criterion
-----------------------------
The one candidate chosen before reading population results blocks an event when
the latest session high is older than 90 minutes *and* the last pre-flip close
is more than 5% below that high. It passes only when the blocked hit rate is
strictly less than half the kept hit rate, both groups contain at least 60
measured flips, and the same test survives dropping every one symbol and every
one session day. Decile gradients are descriptive and cannot move this bar.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.backtest.data import Trade, build_bars
from project_mai_tai.backtest.metrics import kaufman_efficiency_ratio
from project_mai_tai.strategy_core.time_utils import is_fillable_et_session

ET = ZoneInfo("America/New_York")
SESSION_ANCHOR = time(4)
LEGACY_FEATURE_ANCHOR = time(7)
ENTRY_START = time(7)
SESSION_END = time(16)
PRE0700_CAPTURE_START = date(2026, 9, 1)
TARGET_PCT = 5.0
MIN_CANDIDATE_GROUP = 60
CANDIDATE_HIGH_AGE_MINUTES = 90.0
CANDIDATE_BELOW_HIGH_PCT = 5.0
FEATURE_NAMES = (
    "minutes_since_high",
    "below_session_high_pct",
    "lower_high_streak_30m",
    "kaufman_efficiency_60",
    "prior_120m_range_pct",
    "prior_120m_swing_count",
    "volume_trend_30_over_60",
    "change_since_confirm_pct",
    "confirm_change_pct",
)
_LEAVE_EVENTS = {"FADE", "RETENTION_DROP"}
_ENV_FILE = Path("/etc/project-mai-tai/project-mai-tai.env")


class EvidenceUnknown(RuntimeError):
    """A required input could not be established without guessing."""


@dataclass(frozen=True)
class ConfirmEvent:
    event_type: str
    at: datetime
    price: float | None = None
    change_pct: float | None = None
    confirm_path: str | None = None


@dataclass(frozen=True)
class LiveEntry:
    logical_id: str
    filled_at: datetime
    accounts: tuple[str, ...]


@dataclass(frozen=True)
class SymbolDayInput:
    day: date
    symbol: str
    scanner_events: tuple[ConfirmEvent, ...]
    schwab_bars: tuple[Bar, ...]
    pre0700_bars: tuple[Bar, ...] = ()
    pre0700_available: bool = False
    live_entries: tuple[LiveEntry, ...] = ()


@dataclass(frozen=True)
class FlipObservation:
    day: date
    symbol: str
    flip_at: datetime
    flip_close: float
    sell_at: datetime | None
    hit_plus5: bool | None
    mfe_pct: float | None
    mae_pct: float | None
    feature_anchor_et: str
    pre0700_available: bool
    minutes_since_high: float | None
    below_session_high_pct: float | None
    lower_high_streak_30m: int | None
    kaufman_efficiency_60: float | None
    prior_120m_range_pct: float | None
    prior_120m_swing_count: int | None
    prior_120m_bar_count: int
    volume_trend_30_over_60: float | None
    change_since_confirm_pct: float | None
    confirm_change_pct: float | None
    confirm_path: str | None
    live_entry_ids: tuple[str, ...]


@dataclass(frozen=True)
class DecileRow:
    feature: str
    decile: int
    value_min: float
    value_max: float
    flips: int
    hits: int
    hit_rate_pct: float


@dataclass(frozen=True)
class FeatureSummary:
    feature: str
    measured: int
    total_outcomes: int
    unknown: int
    monotonic: bool
    direction: str
    survives_drop_one_symbol: bool
    survives_drop_one_day: bool
    retained: bool
    max_day: str | None
    max_day_flips: int
    deciles: tuple[DecileRow, ...]


@dataclass(frozen=True)
class CandidateResult:
    eligible: int
    total_outcomes: int
    blocked: int
    blocked_hits: int
    blocked_hit_rate_pct: float | None
    kept: int
    kept_hits: int
    kept_hit_rate_pct: float | None
    count_floor_met: bool
    rate_separation_met: bool
    survives_drop_one_symbol: bool
    survives_drop_one_day: bool
    max_blocked_day: str | None
    max_blocked_day_flips: int
    pass_criterion: bool


@dataclass(frozen=True)
class LiveSubsetSummary:
    logical_entries: int
    matched_entries: int
    unmatched_entries: int
    matched_flips: int
    hits: int
    hit_rate_pct: float | None


@dataclass(frozen=True)
class CensusReport:
    start: date
    end: date
    confirmed_symbol_days: int
    bar_measurable_symbol_days: int
    no_live_bar_symbol_days: int
    confirm_memberships: int
    path_c_memberships: int
    pre0700_measurable_symbol_days: int
    observations: tuple[FlipObservation, ...]
    features: tuple[FeatureSummary, ...]
    candidate: CandidateResult
    live_subset: LiveSubsetSummary


class SelectionDataSource(Protocol):
    def load(self, start: date, end: date) -> list[SymbolDayInput]: ...


def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=ET)


def _bar_at(bar: Bar) -> datetime:
    return datetime.fromtimestamp(bar.ts / 1000, UTC).astimezone(ET)


def _opening_confirms(events: Sequence[ConfirmEvent]) -> tuple[ConfirmEvent, ...]:
    """Collapse repeated scanner snapshots into membership-opening CONFIRMs."""

    openings: list[ConfirmEvent] = []
    active = False
    for event in sorted(events, key=lambda item: item.at):
        if event.event_type == "CONFIRM":
            if not active:
                openings.append(event)
                active = True
        elif event.event_type in _LEAVE_EVENTS:
            active = False
    return tuple(openings)


def _confirmed_keys(
    events: dict[tuple[date, str], list[ConfirmEvent]],
) -> list[tuple[date, str]]:
    """Select only symbol-days carrying a positive CONFIRM event."""

    return sorted(
        key for key, rows in events.items() if any(row.event_type == "CONFIRM" for row in rows)
    )


def _confirm_before(events: Sequence[ConfirmEvent], at: datetime) -> ConfirmEvent | None:
    return next((event for event in reversed(_opening_confirms(events)) if event.at <= at), None)


def _count_five_pct_swings(closes: Sequence[float], threshold_pct: float = 5.0) -> int | None:
    """Count non-overlapping close-to-close 5% legs, resetting the anchor after each leg."""

    if len(closes) < 2:
        return None
    anchor = closes[0]
    count = 0
    for close in closes[1:]:
        if anchor <= 0:
            return None
        move = (close / anchor - 1.0) * 100.0
        if abs(move) >= threshold_pct:
            count += 1
            anchor = close
    return count


def _lower_high_streak(bars: Sequence[Bar], flip_at: datetime, anchor: datetime) -> int | None:
    """Consecutive completed 30-minute blocks whose high is below the preceding block."""

    completed = int((flip_at - anchor).total_seconds() // 1800)
    if completed < 2:
        return None
    highs: dict[int, float] = {}
    for bar in bars:
        at = _bar_at(bar)
        index = int((at - anchor).total_seconds() // 1800)
        if 0 <= index < completed:
            highs[index] = max(highs.get(index, float("-inf")), float(bar.high))
    newest = completed - 1
    if newest not in highs or newest - 1 not in highs:
        return None
    streak = 0
    index = newest
    while index > 0 and index in highs and index - 1 in highs:
        if highs[index] >= highs[index - 1]:
            break
        streak += 1
        index -= 1
    return streak


def _feature_bars(item: SymbolDayInput) -> tuple[list[Bar], datetime]:
    anchor_clock = SESSION_ANCHOR if item.pre0700_available else LEGACY_FEATURE_ANCHOR
    anchor = _at(item.day, anchor_clock)
    rows = [*item.pre0700_bars, *item.schwab_bars]
    by_ts = {bar.ts: bar for bar in rows if anchor <= _bar_at(bar) < _at(item.day, SESSION_END)}
    return [by_ts[key] for key in sorted(by_ts)], anchor


def _features(item: SymbolDayInput, flip_bar: Bar) -> dict[str, float | int | str | None]:
    flip_at = _bar_at(flip_bar)
    feature_bars, anchor = _feature_bars(item)
    prior = [bar for bar in feature_bars if _bar_at(bar) < flip_at]
    prior_close = float(prior[-1].close) if prior else None

    high_bar = max(prior, key=lambda bar: (float(bar.high), bar.ts), default=None)
    session_high = float(high_bar.high) if high_bar is not None else None
    minutes_since_high = (
        (flip_at - _bar_at(high_bar)).total_seconds() / 60.0 if high_bar is not None else None
    )
    below_high = (
        (session_high - prior_close) / session_high * 100.0
        if session_high and prior_close is not None
        else None
    )

    closes_60 = [Decimal(str(bar.close)) for bar in prior[-60:]]
    efficiency = kaufman_efficiency_ratio(closes_60) if len(closes_60) == 60 else None
    prior_120 = [bar for bar in prior if _bar_at(bar) >= flip_at - timedelta(minutes=120)]
    range_pct = None
    if len(prior_120) >= 2:
        low = min(float(bar.low) for bar in prior_120)
        high = max(float(bar.high) for bar in prior_120)
        if prior_close and prior_close > 0:
            range_pct = (high - low) / prior_close * 100.0
    swings = _count_five_pct_swings([float(bar.close) for bar in prior_120])

    volume_window = [bar for bar in prior if _bar_at(bar) >= flip_at - timedelta(minutes=90)]
    feed_boundary = _at(item.day, ENTRY_START)
    mixed_volume_units = any(_bar_at(bar) < feed_boundary for bar in volume_window) and any(
        _bar_at(bar) >= feed_boundary for bar in volume_window
    )
    recent_volume = sum(
        bar.volume for bar in volume_window if _bar_at(bar) >= flip_at - timedelta(minutes=30)
    )
    baseline_volume = sum(
        bar.volume for bar in volume_window if _bar_at(bar) < flip_at - timedelta(minutes=30)
    )
    volume_trend = (
        recent_volume / (baseline_volume / 2.0)
        if baseline_volume > 0 and not mixed_volume_units
        else None
    )

    confirm = _confirm_before(item.scanner_events, flip_at)
    change_since_confirm = None
    if confirm is not None and confirm.price and prior_close is not None:
        change_since_confirm = (prior_close / confirm.price - 1.0) * 100.0
    return {
        "feature_anchor_et": anchor.strftime("%H:%M"),
        "minutes_since_high": minutes_since_high,
        "below_session_high_pct": below_high,
        "lower_high_streak_30m": _lower_high_streak(prior, flip_at, anchor),
        "kaufman_efficiency_60": float(efficiency) if efficiency is not None else None,
        "prior_120m_range_pct": range_pct,
        "prior_120m_swing_count": swings,
        "prior_120m_bar_count": len(prior_120),
        "volume_trend_30_over_60": volume_trend,
        "change_since_confirm_pct": change_since_confirm,
        "confirm_change_pct": confirm.change_pct if confirm is not None else None,
        "confirm_path": confirm.confirm_path if confirm is not None else None,
    }


def _outcome(
    bars: Sequence[Bar], rows: Sequence[dict[str, object]], index: int
) -> tuple[datetime | None, bool | None, float | None, float | None]:
    flip = bars[index]
    end = next(
        (future for future in range(index + 1, len(rows)) if rows[future].get("flip") == "SELL"),
        len(rows) - 1,
    )
    observed = list(bars[index + 1 : end + 1])
    sell_at = _bar_at(bars[end]) if end > index and rows[end].get("flip") == "SELL" else None
    if not observed:
        return sell_at, None, None, None
    entry = float(flip.close)
    mfe = max((float(bar.high) / entry - 1.0) * 100.0 for bar in observed)
    mae = min((float(bar.low) / entry - 1.0) * 100.0 for bar in observed)
    return sell_at, mfe >= TARGET_PCT, mfe, mae


def _live_entries_for_flip(entries: Sequence[LiveEntry], flip_at: datetime) -> tuple[str, ...]:
    end = flip_at + timedelta(minutes=2)
    return tuple(entry.logical_id for entry in entries if flip_at <= entry.filled_at < end)


def evaluate_symbol_day(item: SymbolDayInput) -> list[FlipObservation]:
    bars = [
        bar
        for bar in sorted(item.schwab_bars, key=lambda row: row.ts)
        if _at(item.day, SESSION_ANCHOR) <= _bar_at(bar) < _at(item.day, SESSION_END)
    ]
    rows = compute_atr_trail(bars, seed="sma5", period=5, factor=3.5)
    observations: list[FlipObservation] = []
    for index, row in enumerate(rows):
        if row.get("flip") != "BUY" or _bar_at(bars[index]) < _at(item.day, ENTRY_START):
            continue
        flip = bars[index]
        sell_at, hit, mfe, mae = _outcome(bars, rows, index)
        values = _features(item, flip)
        observations.append(
            FlipObservation(
                day=item.day,
                symbol=item.symbol,
                flip_at=_bar_at(flip),
                flip_close=float(flip.close),
                sell_at=sell_at,
                hit_plus5=hit,
                mfe_pct=mfe,
                mae_pct=mae,
                pre0700_available=item.pre0700_available,
                live_entry_ids=_live_entries_for_flip(item.live_entries, _bar_at(flip)),
                **values,
            )
        )
    return observations


def _feature_value(row: FlipObservation, feature: str) -> float | None:
    value = getattr(row, feature)
    return float(value) if value is not None else None


def _deciles(rows: Sequence[FlipObservation], feature: str) -> tuple[DecileRow, ...]:
    measured = sorted(
        (
            (float(value), row)
            for row in rows
            if (value := _feature_value(row, feature)) is not None
        ),
        key=lambda item: (item[0], item[1].day, item[1].symbol, item[1].flip_at),
    )
    if not measured:
        return ()
    grouped: dict[int, list[tuple[float, FlipObservation]]] = defaultdict(list)
    total = len(measured)
    start = 0
    while start < total:
        end = start + 1
        while end < total and measured[end][0] == measured[start][0]:
            end += 1
        midpoint = (start + end - 1) / 2.0
        decile = min(10, int(midpoint * 10 / total) + 1)
        grouped[decile].extend(measured[start:end])
        start = end
    output: list[DecileRow] = []
    for decile, bucket in sorted(grouped.items()):
        hits = sum(row.hit_plus5 is True for _, row in bucket)
        output.append(
            DecileRow(
                feature=feature,
                decile=decile,
                value_min=min(value for value, _ in bucket),
                value_max=max(value for value, _ in bucket),
                flips=len(bucket),
                hits=hits,
                hit_rate_pct=100.0 * hits / len(bucket),
            )
        )
    return tuple(output)


def _monotonic(deciles: Sequence[DecileRow]) -> tuple[bool, str]:
    rates = [row.hit_rate_pct for row in deciles]
    if len(rates) < 2:
        return False, "insufficient_buckets"
    increasing = all(left <= right for left, right in zip(rates, rates[1:], strict=False))
    decreasing = all(left >= right for left, right in zip(rates, rates[1:], strict=False))
    if increasing and decreasing:
        return False, "flat"
    if increasing:
        return True, "increasing"
    if decreasing:
        return True, "decreasing"
    return False, "non_monotonic"


def _survives_drop_one(
    rows: Sequence[FlipObservation], feature: str, direction: str, attribute: str
) -> bool:
    values = sorted({getattr(row, attribute) for row in rows})
    if not values:
        return False
    for value in values:
        subset = [row for row in rows if getattr(row, attribute) != value]
        monotonic, subset_direction = _monotonic(_deciles(subset, feature))
        if not monotonic or subset_direction != direction:
            return False
    return True


def summarize_feature(rows: Sequence[FlipObservation], feature: str) -> FeatureSummary:
    outcomes = [row for row in rows if row.hit_plus5 is not None]
    measured = [row for row in outcomes if _feature_value(row, feature) is not None]
    deciles = _deciles(measured, feature)
    monotonic, direction = _monotonic(deciles)
    survives_symbols = monotonic and _survives_drop_one(measured, feature, direction, "symbol")
    survives_days = monotonic and _survives_drop_one(measured, feature, direction, "day")
    by_day: dict[date, int] = defaultdict(int)
    for row in measured:
        by_day[row.day] += 1
    max_day = max(by_day, key=by_day.get) if by_day else None
    return FeatureSummary(
        feature=feature,
        measured=len(measured),
        total_outcomes=len(outcomes),
        unknown=len(outcomes) - len(measured),
        monotonic=monotonic,
        direction=direction,
        survives_drop_one_symbol=survives_symbols,
        survives_drop_one_day=survives_days,
        retained=monotonic and survives_symbols and survives_days,
        max_day=max_day.isoformat() if max_day else None,
        max_day_flips=by_day.get(max_day, 0) if max_day else 0,
        deciles=deciles,
    )


def _candidate_groups(
    rows: Sequence[FlipObservation],
) -> tuple[list[FlipObservation], list[FlipObservation]]:
    eligible = [
        row
        for row in rows
        if row.hit_plus5 is not None
        and row.minutes_since_high is not None
        and row.below_session_high_pct is not None
    ]
    blocked = [
        row
        for row in eligible
        if row.minutes_since_high > CANDIDATE_HIGH_AGE_MINUTES
        and row.below_session_high_pct > CANDIDATE_BELOW_HIGH_PCT
    ]
    blocked_ids = {id(row) for row in blocked}
    return blocked, [row for row in eligible if id(row) not in blocked_ids]


def _criterion(rows: Sequence[FlipObservation]) -> bool:
    blocked, kept = _candidate_groups(rows)
    if len(blocked) < MIN_CANDIDATE_GROUP or len(kept) < MIN_CANDIDATE_GROUP:
        return False
    blocked_rate = sum(row.hit_plus5 is True for row in blocked) / len(blocked)
    kept_rate = sum(row.hit_plus5 is True for row in kept) / len(kept)
    return blocked_rate < kept_rate / 2.0


def evaluate_candidate(rows: Sequence[FlipObservation]) -> CandidateResult:
    outcomes = [row for row in rows if row.hit_plus5 is not None]
    blocked, kept = _candidate_groups(outcomes)
    blocked_hits = sum(row.hit_plus5 is True for row in blocked)
    kept_hits = sum(row.hit_plus5 is True for row in kept)
    floor_met = len(blocked) >= MIN_CANDIDATE_GROUP and len(kept) >= MIN_CANDIDATE_GROUP
    blocked_rate = blocked_hits / len(blocked) if blocked else None
    kept_rate = kept_hits / len(kept) if kept else None
    separated = (
        blocked_rate is not None and kept_rate is not None and blocked_rate < kept_rate / 2.0
    )
    eligible = [*blocked, *kept]
    symbols = sorted({row.symbol for row in eligible})
    days = sorted({row.day for row in eligible})
    survives_symbols = bool(symbols) and all(
        _criterion([row for row in outcomes if row.symbol != symbol]) for symbol in symbols
    )
    survives_days = bool(days) and all(
        _criterion([row for row in outcomes if row.day != day]) for day in days
    )
    blocked_by_day: dict[date, int] = defaultdict(int)
    for row in blocked:
        blocked_by_day[row.day] += 1
    max_day = max(blocked_by_day, key=blocked_by_day.get) if blocked_by_day else None
    passed = floor_met and separated and survives_symbols and survives_days
    return CandidateResult(
        eligible=len(blocked) + len(kept),
        total_outcomes=len(outcomes),
        blocked=len(blocked),
        blocked_hits=blocked_hits,
        blocked_hit_rate_pct=blocked_rate * 100.0 if blocked_rate is not None else None,
        kept=len(kept),
        kept_hits=kept_hits,
        kept_hit_rate_pct=kept_rate * 100.0 if kept_rate is not None else None,
        count_floor_met=floor_met,
        rate_separation_met=separated,
        survives_drop_one_symbol=survives_symbols,
        survives_drop_one_day=survives_days,
        max_blocked_day=max_day.isoformat() if max_day else None,
        max_blocked_day_flips=blocked_by_day.get(max_day, 0) if max_day else 0,
        pass_criterion=passed,
    )


def _live_subset(
    items: Sequence[SymbolDayInput], rows: Sequence[FlipObservation]
) -> LiveSubsetSummary:
    entries = [entry for item in items for entry in item.live_entries]
    matched_ids = {entry_id for row in rows for entry_id in row.live_entry_ids}
    matched_rows = [row for row in rows if row.live_entry_ids and row.hit_plus5 is not None]
    hits = sum(row.hit_plus5 is True for row in matched_rows)
    return LiveSubsetSummary(
        logical_entries=len(entries),
        matched_entries=len(matched_ids),
        unmatched_entries=len(entries) - len(matched_ids),
        matched_flips=len(matched_rows),
        hits=hits,
        hit_rate_pct=100.0 * hits / len(matched_rows) if matched_rows else None,
    )


def run_census(source: SelectionDataSource, start: date, end: date) -> CensusReport:
    items = source.load(start, end)
    rows = tuple(row for item in items for row in evaluate_symbol_day(item))
    openings = [event for item in items for event in _opening_confirms(item.scanner_events)]
    bar_items = [item for item in items if item.schwab_bars]
    return CensusReport(
        start=start,
        end=end,
        confirmed_symbol_days=len(items),
        bar_measurable_symbol_days=len(bar_items),
        no_live_bar_symbol_days=len(items) - len(bar_items),
        confirm_memberships=len(openings),
        path_c_memberships=sum(event.confirm_path == "PATH_C_EXTREME_MOVER" for event in openings),
        pre0700_measurable_symbol_days=sum(item.pre0700_available for item in bar_items),
        observations=rows,
        features=tuple(summarize_feature(rows, feature) for feature in FEATURE_NAMES),
        candidate=evaluate_candidate(rows),
        live_subset=_live_subset(items, rows),
    )


class DbSelectionDataSource:
    """Production SELECT-only adapter; loads one session at a time to bound memory."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def load(self, start: date, end: date) -> list[SymbolDayInput]:
        with self._sf() as session:
            event_rows = session.execute(
                text(
                    "SELECT trade_date,symbol,event_type,event_at,price,change_pct,confirm_path "
                    "FROM scanner_confirmed_events WHERE trade_date BETWEEN :start AND :end "
                    "ORDER BY trade_date,symbol,event_at,id"
                ),
                {"start": start, "end": end},
            ).all()
            entry_rows = session.execute(
                text(
                    "SELECT (f.filled_at AT TIME ZONE 'America/New_York')::date AS day, "
                    "f.symbol,coalesce(bo.payload->>'fanout_slot_id',bo.client_order_id) AS logical_id, "
                    "min(f.filled_at) AS filled_at,array_agg(DISTINCT ba.name ORDER BY ba.name) AS accounts "
                    "FROM fills f JOIN broker_orders bo ON bo.id=f.order_id "
                    "JOIN broker_accounts ba ON ba.id=f.broker_account_id "
                    "WHERE f.side='buy' AND ba.name IN ('live:schwab_1m_v2','live:orb') "
                    "AND bo.client_order_id LIKE 'schwab_1m_v2-%-open-%' "
                    "AND (f.filled_at AT TIME ZONE 'America/New_York')::date BETWEEN :start AND :end "
                    "GROUP BY day,f.symbol,logical_id ORDER BY day,f.symbol,filled_at"
                ),
                {"start": start, "end": end},
            ).all()

        events: dict[tuple[date, str], list[ConfirmEvent]] = defaultdict(list)
        for day, symbol, event_type, at, price, change_pct, path in event_rows:
            events[(day, str(symbol).upper())].append(
                ConfirmEvent(
                    str(event_type),
                    at.astimezone(ET),
                    float(price) if price is not None else None,
                    float(change_pct) if change_pct is not None else None,
                    str(path) if path else None,
                )
            )
        confirmed_keys = _confirmed_keys(events)
        confirmed_key_set = set(confirmed_keys)
        entries: dict[tuple[date, str], list[LiveEntry]] = defaultdict(list)
        for day, symbol, logical_id, filled_at, accounts in entry_rows:
            key = (day, str(symbol).upper())
            if key in confirmed_key_set:
                entries[key].append(
                    LiveEntry(str(logical_id), filled_at.astimezone(ET), tuple(accounts or ()))
                )

        output: list[SymbolDayInput] = []
        keys_by_day: dict[date, list[str]] = defaultdict(list)
        for day, symbol in confirmed_keys:
            keys_by_day[day].append(symbol)
        for day in sorted(keys_by_day):
            symbols = keys_by_day[day]
            schwab = self._schwab_bars(day, symbols)
            pre0700_available = day >= PRE0700_CAPTURE_START
            pre0700 = self._pre0700_bars(day, symbols) if pre0700_available else {}
            for symbol in symbols:
                output.append(
                    SymbolDayInput(
                        day=day,
                        symbol=symbol,
                        scanner_events=tuple(events[(day, symbol)]),
                        schwab_bars=tuple(schwab.get(symbol, ())),
                        pre0700_bars=tuple(pre0700.get(symbol, ())),
                        pre0700_available=pre0700_available,
                        live_entries=tuple(entries.get((day, symbol), ())),
                    )
                )
        return output

    def _schwab_bars(self, day: date, symbols: Sequence[str]) -> dict[str, list[Bar]]:
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
                    "lo": _at(day, SESSION_ANCHOR),
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

    def _pre0700_bars(self, day: date, symbols: Sequence[str]) -> dict[str, list[Bar]]:
        with self._sf() as session:
            rows = session.execute(
                text(
                    "SELECT symbol,event_ts,price,size,provider FROM market_capture_trades "
                    "WHERE symbol=ANY(:symbols) AND event_ts>=:lo AND event_ts<:hi "
                    "ORDER BY symbol,event_ts,id"
                ),
                {
                    "symbols": list(symbols),
                    "lo": _at(day, SESSION_ANCHOR),
                    "hi": _at(day, ENTRY_START),
                },
            ).all()
        trades: dict[str, list[Trade]] = defaultdict(list)
        for symbol, at, price, size, provider in rows:
            if str(provider).lower() != "massive":
                raise EvidenceUnknown(f"{day} {symbol}: pre-07 trade provider is {provider!r}")
            trades[str(symbol).upper()].append(Trade(at, float(price), float(size or 0)))
        result: dict[str, list[Bar]] = defaultdict(list)
        for symbol, points in trades.items():
            for bar in build_bars(points, _at(day, SESSION_ANCHOR)):
                result[symbol].append(
                    Bar(
                        ts=int(bar.timestamp.timestamp() * 1000),
                        open=float(bar.open),
                        high=float(bar.high),
                        low=float(bar.low),
                        close=float(bar.close),
                        volume=int(bar.volume),
                    )
                )
        return result


def _fmt(value: float | None, digits: int = 2) -> str:
    return "UNKNOWN" if value is None else f"{value:.{digits}f}"


def render_markdown(report: CensusReport) -> str:
    outcomes = [row for row in report.observations if row.hit_plus5 is not None]
    hits = sum(row.hit_plus5 is True for row in outcomes)
    overall_rate = 100.0 * hits / len(outcomes) if outcomes else None
    lines = [
        "# Confirmed-stock ATR selection census",
        "",
        f"Range: {report.start} through {report.end}, ET. Read-only; no scanner or strategy changes.",
        "",
        "## Population",
        "",
        "| Measure | Result | Denominator |",
        "|---|---:|---|",
        f"| Confirmed symbol-days | {report.confirmed_symbol_days} | distinct days/names with a CONFIRM row |",
        f"| Bar-measurable symbol-days | {report.bar_measurable_symbol_days}/{report.confirmed_symbol_days} | confirmed symbol-days |",
        f"| No live Schwab bars | {report.no_live_bar_symbol_days}/{report.confirmed_symbol_days} | confirmed symbol-days |",
        f"| Confirmation memberships | {report.confirm_memberships} | repeated snapshots collapsed |",
        f"| PATH_C_EXTREME_MOVER | {report.path_c_memberships}/{report.confirm_memberships} | confirmation memberships |",
        f"| Pre-07:00 features measurable | {report.pre0700_measurable_symbol_days}/{report.bar_measurable_symbol_days} | bar-measurable symbol-days |",
        f"| Canonical BUY flips | {len(report.observations)} | {report.bar_measurable_symbol_days} bar-measurable symbol-days |",
        f"| Outcome measured | {len(outcomes)}/{len(report.observations)} | canonical BUY flips |",
        f"| Reached +5% | {hits}/{len(outcomes)} ({_fmt(overall_rate)}%) | measured outcomes |",
        "",
        "Pre-09-01 session-high features begin at 07:00 ET. From 09-01 onward they include Massive trade-built bars from 04:00-06:59 ET. Missing features remain UNKNOWN, never zero.",
        "",
        "## Pre-registered candidate",
        "",
        "Block only when high age >90 minutes AND the last pre-flip close is >5% below that high. PASS requires blocked hit rate < half kept hit rate, at least 60 flips in each group, and survival after dropping every one name and every one day.",
        "",
        "| Result | Blocked | Kept | Count floor | Separation | Drop-one name | Drop-one day | Verdict |",
        "|---|---:|---:|---|---|---|---|---|",
        f"| +5 hit rate | {report.candidate.blocked_hits}/{report.candidate.blocked} ({_fmt(report.candidate.blocked_hit_rate_pct)}%) | {report.candidate.kept_hits}/{report.candidate.kept} ({_fmt(report.candidate.kept_hit_rate_pct)}%) | {report.candidate.count_floor_met} | {report.candidate.rate_separation_met} | {report.candidate.survives_drop_one_symbol} | {report.candidate.survives_drop_one_day} | {'PASS' if report.candidate.pass_criterion else 'FAIL'} |",
        f"| Eligibility | {report.candidate.eligible}/{report.candidate.total_outcomes} | candidate-measurable / measured outcomes | - | - | - | - | - |",
        f"| Largest blocked day | {report.candidate.max_blocked_day or 'UNKNOWN'}: {report.candidate.max_blocked_day_flips}/{report.candidate.blocked} | blocked flips | - | - | - | - | - |",
        "",
        "## Feature coverage",
        "",
        "| Feature | Measured | UNKNOWN | Gradient / buckets | Drop-one name | Drop-one day | Largest day | Retained |",
        "|---|---:|---:|---|---|---|---:|---|",
    ]
    for feature in report.features:
        lines.append(
            f"| {feature.feature} | {feature.measured}/{feature.total_outcomes} | {feature.unknown}/{feature.total_outcomes} | {feature.direction} / {len(feature.deciles)} | {feature.survives_drop_one_symbol} | {feature.survives_drop_one_day} | {feature.max_day or 'UNKNOWN'}: {feature.max_day_flips}/{feature.measured} | {feature.retained} |"
        )
    lines.extend(
        [
            "",
            "## Deciles",
            "",
            "| Feature | Decile | Value range | Hits | Hit rate |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for feature in report.features:
        for row in feature.deciles:
            lines.append(
                f"| {row.feature} | {row.decile} | {row.value_min:.4f} to {row.value_max:.4f} | {row.hits}/{row.flips} | {row.hit_rate_pct:.2f}% |"
            )
    lines.extend(
        [
            "",
            "## Live-entry subset",
            "",
            "This table labels actual logical entry fills but does not select thresholds. A fill matches the BUY-flip minute or the following minute, covering an intrabar resting trigger and its immediately following fill window.",
            "",
            "| Logical fills | Matched fills | Unmatched fills | Matched flips | +5 hits |",
            "|---:|---:|---:|---:|---:|",
            f"| {report.live_subset.logical_entries} | {report.live_subset.matched_entries}/{report.live_subset.logical_entries} | {report.live_subset.unmatched_entries}/{report.live_subset.logical_entries} | {report.live_subset.matched_flips} | {report.live_subset.hits}/{report.live_subset.matched_flips} ({_fmt(report.live_subset.hit_rate_pct)}%) |",
            "",
            "| Day | Symbol | Flip ET | Entry IDs | +5 | MFE | MAE | High age | Below high |",
            "|---|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in report.observations:
        if not row.live_entry_ids:
            continue
        lines.append(
            f"| {row.day} | {row.symbol} | {row.flip_at:%H:%M} | {', '.join(row.live_entry_ids)} | {row.hit_plus5} | {_fmt(row.mfe_pct)}% | {_fmt(row.mae_pct)}% | {_fmt(row.minutes_since_high)}m | {_fmt(row.below_session_high_pct)}% |"
        )
    lines.extend(
        [
            "",
            "## Interpretation caveats",
            "",
            "- Outcome entry is the BUY-flip bar close. Live resting entries occur at the ATR trail below that close, so +5% from the close is stricter than the live entry geometry.",
            "- Integer features can collapse into only a few tie-preserving buckets. The feature table prints the actual bucket count next to every gradient.",
            "- The canonical oracle has no bar-gap guard. Results describe the stored Schwab series as it exists; they do not prove continuity across a missing bar.",
            "",
            "## Existing production facts",
            "",
            "The scanner removes a confirmed name below +30% day change. While a name remains confirmed, feed-retention evaluation resets it active. This census changes neither behavior.",
        ]
    )
    return "\n".join(lines) + "\n"


def report_json(report: CensusReport) -> dict[str, object]:
    def encode(value: object) -> object:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, tuple):
            return [encode(item) for item in value]
        if hasattr(value, "__dataclass_fields__"):
            return {field.name: encode(getattr(value, field.name)) for field in fields(value)}
        return value

    return encode(report)  # type: ignore[return-value]


def _load_project_environment() -> dict[str, str]:
    if _ENV_FILE.exists() and os.access(_ENV_FILE, os.R_OK):
        raw = _ENV_FILE.read_text(encoding="utf-8")
    else:
        result = subprocess.run(
            ["sudo", "-n", "cat", "--", str(_ENV_FILE)],
            check=False,
            capture_output=True,
            text=True,
        )
        raw = result.stdout if result.returncode == 0 else ""
    values: dict[str, str] = {}
    for line in raw.splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("#") and "=" in cleaned:
            key, value = cleaned.removeprefix("export ").split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    values.update({key: value for key, value in os.environ.items() if key.startswith("MAI_TAI_")})
    return values


def _read_only_session_factory(database_url: str) -> sessionmaker[Session]:
    connect_args: dict[str, str] = {}
    if database_url.startswith("postgresql"):
        connect_args["options"] = "-c default_transaction_read_only=on -c statement_timeout=300000"
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ET date: {raw}") from exc


def _assert_after_close(end: date, now: datetime | None = None) -> None:
    current = now or datetime.now(UTC)
    if end >= current.astimezone(ET).date() and is_fillable_et_session(current, 7, 16):
        raise EvidenceUnknown("selection census is after-close only for a range containing today")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m project_mai_tai.backtest.confirmed_selection_census"
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
        source = DbSelectionDataSource(_read_only_session_factory(database_url))
        report = run_census(source, start, end)
    except (EvidenceUnknown, OSError, SQLAlchemyError, ValueError) as exc:
        print(f"CANNOT_TELL: {exc}")
        return 2
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
