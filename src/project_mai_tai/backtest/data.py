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

from sqlalchemy import select, text
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
    last: float | None = None   # populated for Schwab LEVELONE quotes (hold-confirm uses last/mid)


@dataclass(frozen=True)
class SchwabBar:
    """A strategy_bar_history (Schwab CHART_EQUITY) 1-min bar. Duck-compatible with
    analysis.atr_flip.Bar (ts=epoch ms, o/h/l/c, volume) for compute_atr_trail."""
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: int


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

    def schwab_bars(self, symbol: str, start: datetime, end: datetime) -> list[SchwabBar]:
        """v2/ATR decision bars — Schwab CHART_EQUITY 1-min from strategy_bar_history (the feed
        v2 actually saw; the anchor confirmed 1.1887 is Schwab, not massive)."""
        with self._sf() as s:
            rows = s.execute(
                text(
                    "SELECT extract(epoch from bar_time)*1000 AS ts, open_price, high_price, "
                    "low_price, close_price, volume FROM strategy_bar_history "
                    "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND symbol=:sym "
                    "AND bar_time>=:lo AND bar_time<:hi ORDER BY bar_time"
                ),
                {"sym": symbol, "lo": start, "hi": end},
            ).all()
        return [SchwabBar(int(ts), float(o), float(h), float(lo), float(c), int(v or 0))
                for ts, o, h, lo, c, v in rows]

    def schwab_quotes(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        """v2 entry-fill + hold-window feed — Schwab LEVELONE NBBO (provider schwab). Sparse
        (why the live hold-confirm mostly hits fallback_thin). Distinct from massive `quotes()`."""
        with self._sf() as s:
            rows = s.execute(
                text(
                    "SELECT event_ts, bid_price, ask_price, last_price FROM market_quote_ticks "
                    "WHERE provider='schwab' AND symbol=:sym AND event_ts>=:lo AND event_ts<:hi "
                    "ORDER BY event_ts, id"
                ),
                {"sym": symbol, "lo": start, "hi": end},
            ).all()
        # LEVELONE snapshots can carry NULL bid/ask (trade-only updates) — skip those.
        return [Quote(ts=ts, bid=float(b), ask=float(a), last=float(lp) if lp is not None else None)
                for ts, b, a, lp in rows if b is not None and a is not None]


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

    def schwab_bars(self, symbol: str, start: datetime, end: datetime) -> list[SchwabBar]:
        out = []
        with gzip.open(self._dir / f"{symbol}_{start:%Y%m%d}_schwab_bars.csv.gz", "rt", newline="") as fh:
            reader = csv.reader(fh)
            next(reader, None)
            for r in reader:
                ts = int(r[0])
                if int(start.timestamp() * 1000) <= ts < int(end.timestamp() * 1000):
                    out.append(SchwabBar(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5])))
        return out

    def schwab_quotes(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        def ctor(ts, r):
            return Quote(ts, float(r[1]), float(r[2]), float(r[3]) if r[3] not in ("", "None") else None)
        return self._read(symbol, start, end, "schwab_quotes", ctor)


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
