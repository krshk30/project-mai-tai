"""Read-only PRE07 shadow report for the v2 ATR initialization blind window.

This module measures what a Massive-trade ATR shadow would have observed before
the live Schwab CHART_EQUITY ATR becomes available.  It never publishes an
intent, changes strategy state, or writes to PostgreSQL.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import statistics
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.backtest.atr_oracle import Bar as AtrBar
from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.data import (
    DbMarketDataSource,
    SchwabBar,
    Trade,
    build_bars,
)
from project_mai_tai.db.models import MarketTradeTick
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.time_utils import is_fillable_et_session

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

ATR_PERIOD = 5
ATR_FACTOR = 3.5
RESTING_BAND_PCT_DEFAULT = 0.5
FIDELITY_REL_TOL = 0.001
FIDELITY_REQUIRED_BARS = 7
OVERLAP_EXPECTED_BARS = 9
COVERAGE_EXPECTED_MINUTES = 188
MAX_LIVE_PROBE_LAG_SECONDS = 300

PRE07_START = time(4, 0)
ENTRY_START = time(7, 0)
BLIND_END = time(7, 8)
SESSION_END = time(16, 0)

_PROBE_RE = re.compile(
    r"\[V2-ATR-PROBE\]\s+sym=(?P<symbol>[A-Z0-9.\-]+)\s+"
    r"ts_ms=(?P<ts_ms>\d+).*?\btrail=(?P<trail>-?\d+(?:\.\d+)?)\s+"
    r"state=(?P<state>long|short)\b"
)
_LOG_CLOCK_RE = re.compile(
    r"^(?P<clock>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"(?:[,.](?P<fraction>\d{1,6}))?"
)
_ENV_FILE = Path("/etc/project-mai-tai/project-mai-tai.env")
_LOG_DIR = Path("/var/log/project-mai-tai")


class EvidenceUnknown(RuntimeError):
    """The report could not establish a required input without guessing."""


@dataclass(frozen=True)
class LevelOneTick:
    ts: datetime
    price: float


@dataclass(frozen=True)
class ProbeReading:
    symbol: str
    bar_at: datetime
    state: str
    trail: float
    observed_at: datetime | None = None
    conflicting: bool = False


@dataclass(frozen=True)
class FlipEvent:
    at: datetime
    side: str
    close: float
    trail: float
    bucket: str


@dataclass(frozen=True)
class TouchEvent:
    at: datetime
    close: float
    prior_trail: float
    band_limit: float


@dataclass(frozen=True)
class Excursion:
    flip_at: datetime
    end_at: datetime | None
    mfe_pct: float | None
    mae_pct: float | None
    note: str = "excursion, not the live exit rules"


@dataclass(frozen=True)
class FidelityBar:
    minute: datetime
    present_massive: bool
    present_schwab: bool
    close_rel_delta: float | None
    high_abs_delta: float | None
    low_abs_delta: float | None
    agrees: bool


@dataclass(frozen=True)
class FidelityResult:
    agree: int
    expected: int
    common: int
    bars: tuple[FidelityBar, ...]

    @property
    def low(self) -> bool:
        return self.agree < FIDELITY_REQUIRED_BARS


@dataclass(frozen=True)
class LevelOneResult:
    agree: int
    compared: int
    massive_minutes: int
    levelone_minutes: int
    minutes: tuple["LevelOneMinute", ...]


@dataclass(frozen=True)
class LevelOneMinute:
    minute: datetime
    massive_close: float | None
    levelone_last: float | None
    close_rel_delta: float | None
    agrees: bool


@dataclass(frozen=True)
class ShadowEvents:
    rows: tuple[dict[str, object], ...]
    pile_a_flips: tuple[FlipEvent, ...]
    pile_b_buy_flips: tuple[FlipEvent, ...]
    pile_b_touches: tuple[TouchEvent, ...]


@dataclass(frozen=True)
class SymbolDayInput:
    day: date
    symbol: str
    massive_trades: tuple[Trade, ...]
    schwab_bars: tuple[SchwabBar, ...]
    levelone_ticks: tuple[LevelOneTick, ...]
    probe: ProbeReading | None


@dataclass(frozen=True)
class SymbolDayReport:
    day: date
    symbol: str
    coverage_minutes: int
    coverage_expected: int
    first_trade_et: datetime | None
    unknown: bool
    pile_a_flips: tuple[FlipEvent, ...]
    pile_b_buy_flips: tuple[FlipEvent, ...]
    pile_b_touches: tuple[TouchEvent, ...]
    shadow_state_0708: str | None
    shadow_trail_0708: float | None
    live_probe_state_0708: str | None
    live_probe_trail_0708: float | None
    schwab_oracle_state_0708: str | None
    schwab_oracle_trail_0708: float | None
    instrument_mismatch: bool
    instrument_detail: str
    fidelity: FidelityResult
    levelone: LevelOneResult
    excursions: tuple[Excursion, ...]

    @property
    def status(self) -> str:
        if self.instrument_mismatch:
            return "INSTRUMENT_MISMATCH"
        if self.unknown:
            return "UNKNOWN"
        if self.fidelity.low:
            return "FIDELITY_LOW"
        return "FIDELITY_OK"


@dataclass(frozen=True)
class RangeSummary:
    range_days: int
    days_with_population: int
    symbol_days: int
    instrument_valid_symbol_days: int
    fidelity_ok_symbol_days: int
    unknown: int
    fidelity_low: int
    instrument_mismatch: int
    pile_a_flips: int
    pile_b_buy_flips: int
    fidelity_ok_pile_b_buy_flips: int
    pile_b_touches: int
    excursion_count: int
    median_mfe_pct: float | None
    median_mae_pct: float | None


class Pre0700DataSource(Protocol):
    def population(self, day: date) -> list[str]: ...

    def massive_providers(self, symbol: str, start: datetime, end: datetime) -> set[str]: ...

    def massive_trades(self, symbol: str, start: datetime, end: datetime) -> list[Trade]: ...

    def schwab_bars(self, symbol: str, start: datetime, end: datetime) -> list[SchwabBar]: ...

    def levelone_ticks(self, symbol: str, start: datetime, end: datetime) -> list[LevelOneTick]: ...


class DbPre0700DataSource:
    """SELECT-only production adapter composed around the shared backtest loader."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory
        self._market = DbMarketDataSource(session_factory)

    def population(self, day: date) -> list[str]:
        start = _at(day, ENTRY_START)
        end = _at(day, time(7, 9))
        with self._sf() as session:
            rows = session.execute(
                text(
                    "SELECT DISTINCT symbol FROM strategy_bar_history "
                    "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 "
                    "AND source='live' AND bar_time>=:lo AND bar_time<:hi ORDER BY symbol"
                ),
                {"lo": start, "hi": end},
            ).all()
        return [str(row[0]).upper() for row in rows]

    def massive_providers(self, symbol: str, start: datetime, end: datetime) -> set[str]:
        with self._sf() as session:
            rows = session.execute(
                text(
                    "SELECT DISTINCT provider FROM market_capture_trades "
                    "WHERE symbol=:symbol AND event_ts>=:lo AND event_ts<:hi"
                ),
                {"symbol": symbol, "lo": start, "hi": end},
            ).all()
        return {str(row[0]).lower() for row in rows}

    def massive_trades(self, symbol: str, start: datetime, end: datetime) -> list[Trade]:
        # DbMarketDataSource is the validated engine loader. Provider purity is checked by the
        # caller before these rows are used, because this historical table predates a provider arg.
        return self._market.trades(symbol, start, end)

    def schwab_bars(self, symbol: str, start: datetime, end: datetime) -> list[SchwabBar]:
        with self._sf() as session:
            rows = session.execute(
                text(
                    "SELECT extract(epoch from bar_time)*1000 AS ts, open_price, high_price, "
                    "low_price, close_price, volume FROM strategy_bar_history "
                    "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND source='live' "
                    "AND symbol=:symbol AND bar_time>=:lo AND bar_time<:hi ORDER BY bar_time"
                ),
                {"symbol": symbol, "lo": start, "hi": end},
            ).all()
        return [
            SchwabBar(int(ts), float(o), float(h), float(low), float(c), int(volume or 0))
            for ts, o, h, low, c, volume in rows
        ]

    def levelone_ticks(self, symbol: str, start: datetime, end: datetime) -> list[LevelOneTick]:
        with self._sf() as session:
            rows = session.execute(
                select(MarketTradeTick.event_ts, MarketTradeTick.price)
                .where(
                    MarketTradeTick.provider == "schwab",
                    MarketTradeTick.service == "LEVELONE_EQUITIES",
                    MarketTradeTick.symbol == symbol,
                    MarketTradeTick.event_ts >= start,
                    MarketTradeTick.event_ts < end,
                )
                .order_by(MarketTradeTick.event_ts, MarketTradeTick.id)
            ).all()
        return [LevelOneTick(ts=ts, price=float(price)) for ts, price in rows]


def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=ET)


def _minute(value: datetime) -> datetime:
    return value.astimezone(ET).replace(second=0, microsecond=0)


def _bar_at(bar: OrbBar | SchwabBar) -> datetime:
    if isinstance(bar, SchwabBar):
        return datetime.fromtimestamp(bar.ts / 1000, UTC).astimezone(ET)
    return bar.timestamp.astimezone(ET)


def _bar_value(bar: OrbBar | SchwabBar, field: str) -> float:
    return float(getattr(bar, field))


def _oracle_bars(bars: Sequence[OrbBar | SchwabBar]) -> list[AtrBar]:
    return [
        AtrBar(
            ts=int(_bar_at(bar).timestamp() * 1000),
            open=_bar_value(bar, "open"),
            high=_bar_value(bar, "high"),
            low=_bar_value(bar, "low"),
            close=_bar_value(bar, "close"),
            volume=int(_bar_value(bar, "volume")),
        )
        for bar in bars
    ]


def _row_at(row: dict[str, object]) -> datetime:
    return datetime.fromtimestamp(int(row["ts"]) / 1000, UTC).astimezone(ET)


def analyze_shadow_trades(
    trades: Sequence[Trade],
    day: date,
    *,
    resting_band_pct: float = RESTING_BAND_PCT_DEFAULT,
) -> ShadowEvents:
    """Build Massive bars through the live aggregator and bucket canonical ATR events."""

    bars = build_bars(trades, _at(day, PRE07_START))
    rows = compute_atr_trail(
        _oracle_bars(bars), seed="sma5", period=ATR_PERIOD, factor=ATR_FACTOR
    )
    pile_a: list[FlipEvent] = []
    pile_b: list[FlipEvent] = []
    touches: list[TouchEvent] = []

    for index, row in enumerate(rows):
        at = _row_at(row)
        flip = str(row["flip"] or "")
        prior_trail = rows[index - 1]["trail"] if index else None
        if PRE07_START <= at.timetz().replace(tzinfo=None) < ENTRY_START and flip:
            if prior_trail is None:
                raise EvidenceUnknown(f"{at:%Y-%m-%d %H:%M}: flip has no prior trail")
            pile_a.append(
                FlipEvent(at, flip, float(row["close"]), float(prior_trail), "04:00-06:59")
            )
        if ENTRY_START <= at.timetz().replace(tzinfo=None) < BLIND_END and flip == "BUY":
            if prior_trail is None:
                raise EvidenceUnknown(f"{at:%Y-%m-%d %H:%M}: BUY flip has no prior trail")
            pile_b.append(
                FlipEvent(at, flip, float(row["close"]), float(prior_trail), "07:00-07:07")
            )
        if index == 0 or not (ENTRY_START <= at.timetz().replace(tzinfo=None) < BLIND_END):
            continue
        previous = rows[index - 1]
        prior_trail = previous["trail"]
        if previous["state"] != "short" or prior_trail is None:
            continue
        close = float(row["close"])
        lower = float(prior_trail)
        upper = lower * (1.0 + resting_band_pct / 100.0)
        if lower <= close <= upper:
            touches.append(TouchEvent(at, close, lower, upper))

    return ShadowEvents(tuple(rows), tuple(pile_a), tuple(pile_b), tuple(touches))


def compare_overlap(
    massive_bars: Sequence[OrbBar],
    schwab_bars: Sequence[SchwabBar],
    day: date,
    *,
    rel_tol: float = FIDELITY_REL_TOL,
) -> FidelityResult:
    """Compare the expected 07:00-07:08 overlap, counting missing minutes as disagreement."""

    massive = {_minute(_bar_at(bar)): bar for bar in massive_bars}
    schwab = {_minute(_bar_at(bar)): bar for bar in schwab_bars}
    rows: list[FidelityBar] = []
    agree = 0
    common = 0
    for offset in range(OVERLAP_EXPECTED_BARS):
        minute = _at(day, ENTRY_START) + timedelta(minutes=offset)
        mb = massive.get(minute)
        sb = schwab.get(minute)
        if mb is None or sb is None:
            rows.append(FidelityBar(minute, mb is not None, sb is not None, None, None, None, False))
            continue
        common += 1
        close_rel = abs(float(mb.close) - float(sb.close)) / max(abs(float(sb.close)), 1e-9)
        high_abs = abs(float(mb.high) - float(sb.high))
        low_abs = abs(float(mb.low) - float(sb.low))
        high_rel = high_abs / max(abs(float(sb.high)), 1e-9)
        low_rel = low_abs / max(abs(float(sb.low)), 1e-9)
        matches = close_rel <= rel_tol and high_rel <= rel_tol and low_rel <= rel_tol
        agree += int(matches)
        rows.append(FidelityBar(minute, True, True, close_rel, high_abs, low_abs, matches))
    return FidelityResult(agree, OVERLAP_EXPECTED_BARS, common, tuple(rows))


def compare_levelone(
    massive_bars: Sequence[OrbBar],
    ticks: Sequence[LevelOneTick],
    day: date,
    *,
    rel_tol: float = FIDELITY_REL_TOL,
) -> LevelOneResult:
    """Compare LEVELONE's last update per minute with the Massive minute close."""

    start = _at(day, PRE07_START)
    end = _at(day, BLIND_END)
    massive = {
        _minute(bar.timestamp): float(bar.close)
        for bar in massive_bars
        if start <= bar.timestamp.astimezone(ET) < end
    }
    levelone: dict[datetime, float] = {}
    for tick in ticks:
        at = tick.ts.astimezone(ET)
        if start <= at < end:
            levelone[_minute(at)] = tick.price
    minute_rows: list[LevelOneMinute] = []
    agree = 0
    for minute in sorted(set(massive) | set(levelone)):
        massive_close = massive.get(minute)
        levelone_last = levelone.get(minute)
        if massive_close is None or levelone_last is None:
            minute_rows.append(
                LevelOneMinute(minute, massive_close, levelone_last, None, False)
            )
            continue
        rel_delta = abs(massive_close - levelone_last) / max(abs(massive_close), 1e-9)
        matches = rel_delta <= rel_tol
        agree += int(matches)
        minute_rows.append(
            LevelOneMinute(minute, massive_close, levelone_last, rel_delta, matches)
        )
    compared = sum(
        row.massive_close is not None and row.levelone_last is not None for row in minute_rows
    )
    return LevelOneResult(agree, compared, len(massive), len(levelone), tuple(minute_rows))


def parse_probe_lines(lines: Iterable[str]) -> dict[tuple[date, str], ProbeReading]:
    """Parse timely 07:08 probes, preserving disagreement between live observations."""

    readings: dict[tuple[date, str], ProbeReading] = {}
    for line in lines:
        match = _PROBE_RE.search(line)
        clock_match = _LOG_CLOCK_RE.match(line)
        if match is None or clock_match is None:
            continue
        at = datetime.fromtimestamp(int(match.group("ts_ms")) / 1000, UTC).astimezone(ET)
        if at.hour != 7 or at.minute != 8:
            continue
        fraction = (clock_match.group("fraction") or "").ljust(6, "0")
        observed_at = datetime.strptime(clock_match.group("clock"), "%Y-%m-%d %H:%M:%S").replace(
            microsecond=int(fraction or "0"),
            tzinfo=UTC,
        )
        lag_seconds = (observed_at - at).total_seconds()
        if not 0 <= lag_seconds <= MAX_LIVE_PROBE_LAG_SECONDS:
            continue
        reading = ProbeReading(
            symbol=match.group("symbol"),
            bar_at=at,
            state=match.group("state"),
            trail=float(match.group("trail")),
            observed_at=observed_at,
        )
        key = (at.date(), reading.symbol)
        previous = readings.get(key)
        if previous is None:
            readings[key] = reading
            continue
        conflicting = previous.state != reading.state or round(previous.trail, 4) != round(
            reading.trail, 4
        )
        if conflicting:
            readings[key] = ProbeReading(
                symbol=reading.symbol,
                bar_at=reading.bar_at,
                state=reading.state,
                trail=reading.trail,
                observed_at=reading.observed_at,
                conflicting=True,
            )
    return readings


def _canonical_0708(
    bars: Sequence[SchwabBar], day: date
) -> tuple[str | None, float | None, str]:
    expected = {_at(day, ENTRY_START) + timedelta(minutes=i) for i in range(9)}
    selected = [bar for bar in bars if _minute(_bar_at(bar)) in expected]
    by_minute = {_minute(_bar_at(bar)): bar for bar in selected}
    if set(by_minute) != expected:
        return None, None, f"Schwab overlap has {len(by_minute)}/9 expected bars"
    rows = compute_atr_trail(
        _oracle_bars([by_minute[m] for m in sorted(by_minute)]),
        seed="sma5",
        period=ATR_PERIOD,
        factor=ATR_FACTOR,
    )
    row = rows[-1]
    if row["state"] is None or row["trail"] is None:
        return None, None, "Schwab oracle did not initialize on 07:08"
    return str(row["state"]), float(row["trail"]), ""


def canonical_agreement(
    bars: Sequence[SchwabBar], day: date, probe: ProbeReading | None
) -> tuple[bool, str | None, float | None, str]:
    """Require the log probe and the independent oracle replay to agree at 07:08."""

    state, trail, problem = _canonical_0708(bars, day)
    if problem:
        return False, state, trail, problem
    if probe is None:
        return False, state, trail, "07:08 live probe is missing"
    if probe.conflicting:
        return False, state, trail, "07:08 live probes conflict"
    if probe.bar_at.date() != day or probe.bar_at.hour != 7 or probe.bar_at.minute != 8:
        return False, state, trail, "live probe is not the requested 07:08 bar"
    if probe.state != state or round(probe.trail, 4) != round(float(trail), 4):
        return (
            False,
            state,
            trail,
            f"probe={probe.state}/{probe.trail:.6f} oracle={state}/{trail:.4f}",
        )
    return True, state, trail, "probe and Schwab oracle agree"


def _shadow_0708(rows: Sequence[dict[str, object]], day: date) -> tuple[str | None, float | None]:
    target = _at(day, BLIND_END)
    for row in rows:
        if _minute(_row_at(row)) == target:
            state = str(row["state"]) if row["state"] is not None else None
            trail = float(row["trail"]) if row["trail"] is not None else None
            return state, trail
    return None, None


def _coverage(trades: Sequence[Trade], day: date) -> tuple[int, datetime | None]:
    start = _at(day, PRE07_START)
    end = _at(day, BLIND_END)
    in_window = [trade.ts.astimezone(ET) for trade in trades if start <= trade.ts.astimezone(ET) < end]
    return len({_minute(ts) for ts in in_window}), min(in_window, default=None)


def _next_shadow_sell(
    rows: Sequence[dict[str, object]], flip_at: datetime, day: date
) -> datetime | None:
    session_end = _at(day, SESSION_END)
    return next(
        (
            _row_at(row)
            for row in rows
            if _row_at(row) > flip_at
            and _row_at(row) < session_end
            and row["flip"] == "SELL"
        ),
        None,
    )


def _excursions(
    flips: Sequence[FlipEvent],
    shadow_rows: Sequence[dict[str, object]],
    schwab_bars: Sequence[SchwabBar],
    day: date,
) -> tuple[Excursion, ...]:
    live_start = _at(day, BLIND_END)
    session_end = _at(day, SESSION_END)
    out: list[Excursion] = []
    for flip in flips:
        sell_at = _next_shadow_sell(shadow_rows, flip.at, day)
        observed = [
            bar
            for bar in schwab_bars
            if max(live_start, flip.at) <= _bar_at(bar) < session_end
            and (sell_at is None or _bar_at(bar) <= sell_at)
        ]
        if not observed:
            out.append(Excursion(flip.at, sell_at, None, None))
            continue
        entry = flip.close
        mfe = max(0.0, max((float(bar.high) / entry - 1.0) * 100.0 for bar in observed))
        mae = min(0.0, min((float(bar.low) / entry - 1.0) * 100.0 for bar in observed))
        out.append(Excursion(flip.at, sell_at, mfe, mae))
    return tuple(out)


def evaluate_symbol_day(
    item: SymbolDayInput,
    *,
    resting_band_pct: float = RESTING_BAND_PCT_DEFAULT,
) -> SymbolDayReport:
    start = _at(item.day, PRE07_START)
    end = _at(item.day, SESSION_END)
    trades = tuple(trade for trade in item.massive_trades if start <= trade.ts.astimezone(ET) < end)
    coverage, first_trade = _coverage(trades, item.day)
    unknown = coverage == 0
    shadow = analyze_shadow_trades(trades, item.day, resting_band_pct=resting_band_pct)
    massive_bars = build_bars(trades, start)
    fidelity = compare_overlap(massive_bars, item.schwab_bars, item.day)
    levelone = compare_levelone(massive_bars, item.levelone_ticks, item.day)
    canonical_ok, oracle_state, oracle_trail, instrument_detail = canonical_agreement(
        item.schwab_bars, item.day, item.probe
    )
    shadow_state, shadow_trail = _shadow_0708(shadow.rows, item.day)
    return SymbolDayReport(
        day=item.day,
        symbol=item.symbol,
        coverage_minutes=coverage,
        coverage_expected=COVERAGE_EXPECTED_MINUTES,
        first_trade_et=first_trade,
        unknown=unknown,
        pile_a_flips=shadow.pile_a_flips,
        pile_b_buy_flips=shadow.pile_b_buy_flips,
        pile_b_touches=shadow.pile_b_touches,
        shadow_state_0708=shadow_state,
        shadow_trail_0708=shadow_trail,
        live_probe_state_0708=item.probe.state if item.probe else None,
        live_probe_trail_0708=item.probe.trail if item.probe else None,
        schwab_oracle_state_0708=oracle_state,
        schwab_oracle_trail_0708=oracle_trail,
        instrument_mismatch=not canonical_ok,
        instrument_detail=instrument_detail,
        fidelity=fidelity,
        levelone=levelone,
        excursions=_excursions(
            shadow.pile_b_buy_flips, shadow.rows, item.schwab_bars, item.day
        ),
    )


def summarize(reports: Sequence[SymbolDayReport], start: date, end: date) -> RangeSummary:
    instrument_valid = [report for report in reports if not report.instrument_mismatch]
    fidelity_ok = [
        report
        for report in instrument_valid
        if not report.unknown and not report.fidelity.low
    ]
    excursions = [
        event
        for report in fidelity_ok
        for event in report.excursions
        if event.mfe_pct is not None
    ]
    return RangeSummary(
        range_days=(end - start).days + 1,
        days_with_population=len({report.day for report in reports}),
        symbol_days=len(reports),
        instrument_valid_symbol_days=len(instrument_valid),
        fidelity_ok_symbol_days=len(fidelity_ok),
        unknown=sum(report.unknown for report in reports),
        fidelity_low=sum(report.fidelity.low for report in reports),
        instrument_mismatch=sum(report.instrument_mismatch for report in reports),
        pile_a_flips=sum(len(report.pile_a_flips) for report in instrument_valid),
        pile_b_buy_flips=sum(len(report.pile_b_buy_flips) for report in instrument_valid),
        fidelity_ok_pile_b_buy_flips=sum(
            len(report.pile_b_buy_flips) for report in fidelity_ok
        ),
        pile_b_touches=sum(len(report.pile_b_touches) for report in instrument_valid),
        excursion_count=len(excursions),
        median_mfe_pct=(statistics.median(e.mfe_pct for e in excursions) if excursions else None),
        median_mae_pct=(statistics.median(e.mae_pct for e in excursions) if excursions else None),
    )


def _fmt_number(value: float | None, digits: int = 4) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _fmt_flip(events: Sequence[FlipEvent]) -> str:
    if not events:
        return "0"
    return "; ".join(
        f"{event.at:%H:%M} {event.side} c={event.close:.4f} t={event.trail:.4f}"
        for event in events
    )


def _fmt_touch(events: Sequence[TouchEvent]) -> str:
    if not events:
        return "0"
    return "; ".join(
        f"{event.at:%H:%M} c={event.close:.4f} band={event.prior_trail:.4f}-{event.band_limit:.4f}"
        for event in events
    )


def _fmt_excursions(events: Sequence[Excursion]) -> str:
    if not events:
        return "0"
    return "; ".join(
        f"{event.flip_at:%H:%M} MFE={_fmt_number(event.mfe_pct, 2)}% "
        f"MAE={_fmt_number(event.mae_pct, 2)}%"
        for event in events
    )


def render_symbol_table(reports: Sequence[SymbolDayReport]) -> str:
    lines = [
        "| Day | Symbol | Massive coverage | First trade ET | Pile A flips | Pile B BUY flips | "
        "Pile B touches | 07:08 shadow / live / oracle | Fidelity | LEVELONE | Excursions | Status |",
        "|---|---|---:|---|---|---|---|---|---:|---:|---|---|",
    ]
    for report in reports:
        shadow = f"{report.shadow_state_0708 or '-'} {_fmt_number(report.shadow_trail_0708)}"
        live = f"{report.live_probe_state_0708 or '-'} {_fmt_number(report.live_probe_trail_0708)}"
        oracle = (
            f"{report.schwab_oracle_state_0708 or '-'} "
            f"{_fmt_number(report.schwab_oracle_trail_0708)}"
        )
        status = report.status
        if report.instrument_mismatch:
            status = f"{status}: {report.instrument_detail}"
        levelone = (
            f"{report.levelone.agree}/{report.levelone.compared}"
            if report.levelone.compared
            else "UNKNOWN(0 paired)"
        )
        lines.append(
            f"| {report.day} | {report.symbol} | {report.coverage_minutes}/"
            f"{report.coverage_expected} | "
            f"{report.first_trade_et.strftime('%H:%M:%S') if report.first_trade_et else 'UNKNOWN'} | "
            f"{_fmt_flip(report.pile_a_flips)} | {_fmt_flip(report.pile_b_buy_flips)} | "
            f"{_fmt_touch(report.pile_b_touches)} | {shadow} / {live} / {oracle} | "
            f"{report.fidelity.agree}/{report.fidelity.expected} | "
            f"{levelone} | "
            f"{_fmt_excursions(report.excursions)} | {status} |"
        )
    return "\n".join(lines)


def render_fidelity_details(reports: Sequence[SymbolDayReport]) -> str:
    lines = [
        "| Day | Symbol | Minute ET | Massive | Schwab | Close rel delta | High abs delta | "
        "Low abs delta | Agree |",
        "|---|---|---|---|---|---:|---:|---:|---|",
    ]
    for report in reports:
        for row in report.fidelity.bars:
            lines.append(
                f"| {report.day} | {report.symbol} | {row.minute:%H:%M} | "
                f"{'yes' if row.present_massive else 'no'} | "
                f"{'yes' if row.present_schwab else 'no'} | "
                f"{_fmt_number(row.close_rel_delta, 6)} | "
                f"{_fmt_number(row.high_abs_delta, 6)} | "
                f"{_fmt_number(row.low_abs_delta, 6)} | "
                f"{'yes' if row.agrees else 'no'} |"
            )
    return "\n".join(lines)


def render_summary(summary: RangeSummary) -> str:
    symbol_denominator = summary.symbol_days
    valid_denominator = summary.instrument_valid_symbol_days
    fidelity_ok_denominator = summary.fidelity_ok_symbol_days
    excursion_denominator = summary.excursion_count
    return "\n".join(
        [
            "| Measure | Result | Denominator |",
            "|---|---:|---|",
            f"| Days with population | {summary.days_with_population}/{summary.range_days} | "
            "calendar dates queried |",
            f"| Population | {summary.symbol_days} | symbol-days |",
            f"| Instrument-valid population | {valid_denominator}/{symbol_denominator} | "
            "population symbol-days |",
            f"| FIDELITY_OK population | {fidelity_ok_denominator}/{symbol_denominator} | "
            "population symbol-days |",
            f"| UNKNOWN | {summary.unknown}/{symbol_denominator} | population symbol-days |",
            f"| FIDELITY_LOW | {summary.fidelity_low}/{symbol_denominator} | population symbol-days |",
            f"| INSTRUMENT_MISMATCH | {summary.instrument_mismatch}/{symbol_denominator} | "
            "population symbol-days |",
            f"| Pile A flips | {summary.pile_a_flips} | {valid_denominator} instrument-valid "
            "symbol-days |",
            f"| Pile B BUY flips | {summary.pile_b_buy_flips} | {valid_denominator} "
            "instrument-valid symbol-days |",
            f"| Pile B BUY flips, FIDELITY_OK | {summary.fidelity_ok_pile_b_buy_flips} | "
            f"{fidelity_ok_denominator} fidelity-ok symbol-days |",
            f"| Pile B touches | {summary.pile_b_touches} | {valid_denominator} "
            "instrument-valid symbol-days |",
            f"| Median MFE | {_fmt_number(summary.median_mfe_pct, 2)}% | "
            f"{excursion_denominator} measured excursions |",
            f"| Median MAE | {_fmt_number(summary.median_mae_pct, 2)}% | "
            f"{excursion_denominator} measured excursions |",
        ]
    )


def _json_value(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def report_json(
    reports: Sequence[SymbolDayReport], summary: RangeSummary
) -> dict[str, object]:
    return {
        "summary": _json_value(asdict(summary)),
        "symbol_days": [_json_value(asdict(report)) for report in reports],
    }


def _load_project_environment() -> dict[str, str]:
    values: dict[str, str] = {}
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
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, value = cleaned.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    values.update({key: value for key, value in os.environ.items() if key.startswith("MAI_TAI_")})
    return values


def _read_only_session_factory(database_url: str) -> sessionmaker[Session]:
    connect_args: dict[str, str] = {}
    if database_url.startswith("postgresql"):
        connect_args["options"] = "-c default_transaction_read_only=on -c statement_timeout=120000"
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _log_paths(log_dir: Path) -> list[Path]:
    if log_dir.exists() and os.access(log_dir, os.R_OK):
        return sorted(log_dir.glob("schwab-1m-v2.log*"))
    result = subprocess.run(
        [
            "sudo",
            "-n",
            "find",
            str(log_dir),
            "-maxdepth",
            "1",
            "-type",
            "f",
            "-name",
            "schwab-1m-v2.log*",
            "-print",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise EvidenceUnknown("cannot enumerate v2 logs")
    return sorted(Path(line) for line in result.stdout.splitlines() if line.strip())


def _probe_log_lines(log_dir: Path) -> list[str]:
    lines: list[str] = []
    for path in _log_paths(log_dir):
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                    lines.extend(line for line in handle if "[V2-ATR-PROBE]" in line)
            else:
                lines.extend(
                    line
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if "[V2-ATR-PROBE]" in line
                )
            continue
        except PermissionError:
            pass
        result = subprocess.run(
            ["sudo", "-n", "zgrep", "-h", "-F", "[V2-ATR-PROBE]", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):
            raise EvidenceUnknown(f"cannot read v2 probe log {path.name}")
        lines.extend(result.stdout.splitlines())
    return lines


def _load_symbol_day(
    source: Pre0700DataSource,
    probes: dict[tuple[date, str], ProbeReading],
    day: date,
    symbol: str,
) -> SymbolDayInput:
    start = _at(day, PRE07_START)
    end = _at(day, SESSION_END)
    providers = source.massive_providers(symbol, start, end)
    if providers - {"massive"}:
        raise EvidenceUnknown(
            f"{day} {symbol}: market_capture_trades contains non-Massive providers"
        )
    return SymbolDayInput(
        day=day,
        symbol=symbol,
        massive_trades=tuple(source.massive_trades(symbol, start, end)),
        schwab_bars=tuple(source.schwab_bars(symbol, _at(day, ENTRY_START), end)),
        levelone_ticks=tuple(source.levelone_ticks(symbol, start, _at(day, BLIND_END))),
        probe=probes.get((day, symbol)),
    )


def run_range(
    source: Pre0700DataSource,
    probes: dict[tuple[date, str], ProbeReading],
    start: date,
    end: date,
    *,
    resting_band_pct: float = RESTING_BAND_PCT_DEFAULT,
) -> list[SymbolDayReport]:
    reports: list[SymbolDayReport] = []
    day = start
    while day <= end:
        for symbol in source.population(day):
            reports.append(
                evaluate_symbol_day(
                    _load_symbol_day(source, probes, day, symbol),
                    resting_band_pct=resting_band_pct,
                )
            )
        day += timedelta(days=1)
    return reports


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ET date: {raw}") from exc


def _assert_after_close(now: datetime | None = None) -> None:
    current = now or datetime.now(UTC)
    if is_fillable_et_session(current, 7, 16):
        raise EvidenceUnknown("PRE07 is after-close only; refusing a production query before 16:00 ET")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m project_mai_tai.backtest.pre0700_shadow")
    parser.add_argument("date", nargs="?", type=_parse_date, help="single ET session date")
    parser.add_argument(
        "--range",
        dest="date_range",
        nargs=2,
        metavar=("START", "END"),
        type=_parse_date,
        help="inclusive ET date range",
    )
    parser.add_argument("--json", type=Path, help="also write the full report as JSON")
    args = parser.parse_args(argv)
    if args.date_range:
        if args.date is not None:
            parser.error("use a single date or --range, not both")
        start, end = args.date_range
    elif args.date is not None:
        start = end = args.date
    else:
        parser.error("provide YYYY-MM-DD or --range START END")
    if end < start:
        parser.error("range end precedes range start")

    try:
        _assert_after_close()
        env = _load_project_environment()
        database_url = env.get("MAI_TAI_DATABASE_URL", "").strip()
        if not database_url:
            raise EvidenceUnknown("MAI_TAI_DATABASE_URL is unavailable")
        band_pct = float(
            env.get(
                "MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_RESTING_ENTRY_BAND_PCT",
                RESTING_BAND_PCT_DEFAULT,
            )
        )
        source = DbPre0700DataSource(_read_only_session_factory(database_url))
        log_dir = Path(os.environ.get("PRE0700_V2_LOG_DIR", _LOG_DIR))
        probes = parse_probe_lines(_probe_log_lines(log_dir))
        reports = run_range(source, probes, start, end, resting_band_pct=band_pct)
        range_summary = summarize(reports, start, end)
    except (EvidenceUnknown, OSError, SQLAlchemyError, ValueError) as exc:
        print(f"CANNOT_TELL: {exc}")
        return 2

    print(render_summary(range_summary))
    print()
    print(render_symbol_table(reports))
    print("\nOverlap fidelity details (07:00-07:08 ET)\n")
    print(render_fidelity_details(reports))
    if args.json:
        args.json.write_text(
            json.dumps(report_json(reports, range_summary), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nJSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
