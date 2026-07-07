"""Backtest data layer — captured market data (ground truth) + faithful bar-building.

FAITHFULNESS: the live ORB builds its 1-min decision bars from the market-data gateway
TRADE stream via ``OrbTickAggregator``; ``market_capture_trades`` captures that SAME stream,
so building bars from it with the SAME aggregator reproduces the bars the live bot saw
(exact logic mirror). ``market_capture_bars`` are INDEPENDENT post-close REST aggregates —
used only as a two-source PARITY cross-check on the trade-built bars (the "two independent
implementations must agree" principle at the data layer), never as the decision source.

Stdlib + SQLAlchemy only (no pandas — matches the live strategy path). A ``MarketDataSource``
protocol has a DB impl (production) and will get a fixture impl (gzipped CSV) for CI.
"""
from __future__ import annotations

import csv
import gzip
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    MarketCaptureBar,
    MarketCaptureQuote,
    MarketCaptureTrade,
)
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.orb_tick_aggregator import OrbTickAggregator


@dataclass(frozen=True)
class Trade:
    ts: datetime
    price: float
    size: float


@dataclass(frozen=True)
class Quote:
    ts: datetime
    bid: float
    ask: float


@dataclass(frozen=True)
class CapturedBar:
    """A pre-built market_capture_bars row (REST aggregate) — parity reference only."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketDataSource(Protocol):
    def trades(self, symbol: str, start: datetime, end: datetime) -> list[Trade]: ...
    def quotes(self, symbol: str, start: datetime, end: datetime) -> list[Quote]: ...
    def captured_bars(self, symbol: str, start: datetime, end: datetime) -> list[CapturedBar]: ...


class DbMarketDataSource:
    """Production source — reads captured data from Postgres via the repo models."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def trades(self, symbol: str, start: datetime, end: datetime) -> list[Trade]:
        with self._sf() as s:
            rows = s.execute(
                select(MarketCaptureTrade.event_ts, MarketCaptureTrade.price, MarketCaptureTrade.size)
                .where(
                    MarketCaptureTrade.symbol == symbol,
                    MarketCaptureTrade.event_ts >= start,
                    MarketCaptureTrade.event_ts < end,
                )
                .order_by(MarketCaptureTrade.event_ts, MarketCaptureTrade.id)
            ).all()
        return [Trade(ts=ts, price=float(p), size=float(sz or 0)) for ts, p, sz in rows]

    def quotes(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        with self._sf() as s:
            rows = s.execute(
                select(MarketCaptureQuote.event_ts, MarketCaptureQuote.bid_price, MarketCaptureQuote.ask_price)
                .where(
                    MarketCaptureQuote.symbol == symbol,
                    MarketCaptureQuote.event_ts >= start,
                    MarketCaptureQuote.event_ts < end,
                )
                .order_by(MarketCaptureQuote.event_ts, MarketCaptureQuote.id)
            ).all()
        return [Quote(ts=ts, bid=float(b), ask=float(a)) for ts, b, a in rows]

    def captured_bars(self, symbol: str, start: datetime, end: datetime) -> list[CapturedBar]:
        with self._sf() as s:
            rows = s.execute(
                select(
                    MarketCaptureBar.event_ts, MarketCaptureBar.open, MarketCaptureBar.high,
                    MarketCaptureBar.low, MarketCaptureBar.close, MarketCaptureBar.volume,
                )
                .where(
                    MarketCaptureBar.symbol == symbol,
                    MarketCaptureBar.interval_secs == 60,
                    MarketCaptureBar.event_ts >= start,
                    MarketCaptureBar.event_ts < end,
                )
                .order_by(MarketCaptureBar.event_ts)
            ).all()
        return [
            CapturedBar(ts=ts, open=float(o), high=float(h), low=float(lo), close=float(c), volume=float(v or 0))
            for ts, o, h, lo, c, v in rows
        ]


class FixtureMarketDataSource:
    """Reads committed gzipped-CSV golden fixtures (NO DB) so the golden-case suite runs in CI.

    Fixtures are named ``{SYMBOL}_{YYYYMMDD}_{trades|quotes}.csv.gz`` under ``fixtures_dir``
    (exported from market_capture_* — the same stream-trades ground truth the engine uses in
    production). Rows filtered to [start, end)."""

    def __init__(self, fixtures_dir: str | Path) -> None:
        self._dir = Path(fixtures_dir)

    def _read(self, symbol, start, end, kind, ctor):
        path = self._dir / f"{symbol}_{start:%Y%m%d}_{kind}.csv.gz"
        out = []
        with gzip.open(path, "rt", newline="") as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            for row in reader:
                ts = datetime.fromisoformat(row[0])
                if start <= ts < end:
                    out.append(ctor(ts, row))
        return out

    def trades(self, symbol: str, start: datetime, end: datetime) -> list[Trade]:
        return self._read(symbol, start, end, "trades", lambda ts, r: Trade(ts, float(r[1]), float(r[2])))

    def quotes(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        return self._read(symbol, start, end, "quotes", lambda ts, r: Quote(ts, float(r[1]), float(r[2])))

    def captured_bars(self, symbol: str, start: datetime, end: datetime) -> list[CapturedBar]:
        return []  # REST-aggregate parity reference is DB-only; not needed for the golden gates


def build_bars(trades: Sequence[Trade], session_open: datetime) -> list[OrbBar]:
    """Build 1-min bars from trades using the LIVE OrbTickAggregator (exact mirror of how
    the ORB service builds its decision bars). `session_open` anchors the VWAP (09:30 ET)."""
    agg = OrbTickAggregator(session_open=session_open)
    bars: list[OrbBar] = []
    for t in trades:
        completed = agg.add_tick(t.ts, t.price, t.size)
        if completed is not None:
            bars.append(completed)
    tail = agg.flush()
    if tail is not None:
        bars.append(tail)
    return bars


def bar_parity(built: Sequence[OrbBar], captured: Sequence[CapturedBar], *, rel_tol: float = 0.001):
    """Two-source parity: compare trade-built bars to the independent REST-aggregate bars,
    per minute, on OHLC. Returns (matched, mismatches) — mismatches are (minute, field,
    built, captured, rel_diff) for any bar present in BOTH sources whose OHLC diverges > tol.
    (Minutes present in only one source are reported separately as coverage gaps.)"""
    cap_by_min = {b.ts.replace(second=0, microsecond=0): b for b in captured}
    built_by_min = {b.timestamp.replace(second=0, microsecond=0): b for b in built}
    common = sorted(set(cap_by_min) & set(built_by_min))
    mismatches: list = []
    for m in common:
        bb, cb = built_by_min[m], cap_by_min[m]
        for field, bv, cv in (
            ("open", bb.open, cb.open), ("high", bb.high, cb.high),
            ("low", bb.low, cb.low), ("close", bb.close, cb.close),
        ):
            denom = max(abs(cv), 1e-9)
            rel = abs(bv - cv) / denom
            if rel > rel_tol:
                mismatches.append((m, field, bv, cv, rel))
    only_built = sorted(set(built_by_min) - set(cap_by_min))
    only_captured = sorted(set(cap_by_min) - set(built_by_min))
    return {
        "common": len(common),
        "matched": len(common) - len({m for m, *_ in mismatches}),
        "mismatches": mismatches,
        "only_built": only_built,
        "only_captured": only_captured,
    }
