"""ORB (P6 "OPEN") opening-range-breakout — execution leaf (import-clean).

Pure, feed-agnostic logic for the settled ORB path. Holds NO I/O and NO global
state; the hot-path wiring (strategy_engine_app.py + oms/service.py) imports
these helpers behind the ``orb_enabled`` flag (default OFF → inert). Mirrors the
parity-proven backtest (``scripts/orb_exit_backtest.py``); ``BAR_CLOSE`` mode is
byte-identical to the canonical bar-close ORB (proven 159/159, 25 days).

Settled config (see ``docs/orb-opening-range-exit-research.md``):
  ENTRY = 5-min OR from 09:30 · close > OR_high · vol >= 1.5x OR_avg ·
          close > VWAP · close > EMA9 · skip if OR_width% > 12% · cutoff 10:30 ·
          one trade per symbol · ONLY pre-09:25-confirmed names (universe guard).
  EXIT  = TRAIL-8% (ratchets up 8% below the high-water-mark, never down).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class ExecutionMode(str, Enum):
    """``BAR_CLOSE`` = fill the entry at the breakout bar's close (the backtested,
    parity-proven behaviour). ``INTRABAR`` = fill at the breakout level (OR_high)
    the moment price crosses it, instead of waiting for the 1-min bar to close."""

    BAR_CLOSE = "bar_close"
    INTRABAR = "intrabar"


@dataclass(frozen=True)
class OrbConfig:
    or_minutes: int = 5
    vol_mult: float = 1.5
    width_max_pct: float = 12.0
    width_min_pct: float = 2.0
    cutoff_minutes: int = 60          # last entry = session_open + 60m = 10:30 ET
    trail_pct: float = 8.0
    require_vwap: bool = True
    require_ema9: bool = True
    universe_lead_minutes: int = 5    # must be confirmed by session_open - 5m = 09:25


@dataclass(frozen=True)
class OrbBar:
    """Minimal bar view the leaf needs. ``vwap``/``ema9`` are session-anchored
    values already computed upstream (None outside RTH)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    ema9: float | None = None


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    avg_volume: float

    @property
    def width_pct(self) -> float:
        return (self.high - self.low) / self.low * 100.0 if self.low > 0 else float("inf")


# ---------------------------------------------------------------------------
# Universe guard — the binding rule (operator decision 2026-06-18):
# ORB only considers names that were on the scanner list / confirmed BEFORE
# 09:25. A name that confirms DURING the 09:25-10:00 window is OUT OF SCOPE by
# design (no clean opening range; typically coming off a downtrend) — not a
# missed trade. ARM only in-time names; SKIP the rest.
# ---------------------------------------------------------------------------
def in_pre_open_universe(
    last_confirmed_at: datetime | None,
    session_open: datetime,
    *,
    lead_minutes: int = 5,
) -> bool:
    if last_confirmed_at is None:
        return False
    return last_confirmed_at <= session_open - timedelta(minutes=lead_minutes)


def build_opening_range(or_bars: list[OrbBar], config: OrbConfig) -> OpeningRange | None:
    """The first ``or_minutes`` bars from session open. Returns None when coverage
    is insufficient (fewer than ``or_minutes`` bars — the in-time-coverage guard)
    or the range is outside the chop band (skip-this-symbol)."""
    if len(or_bars) < config.or_minutes:
        return None
    high = max(b.high for b in or_bars)
    low = min(b.low for b in or_bars)
    avg_volume = sum(b.volume for b in or_bars) / len(or_bars)
    opening_range = OpeningRange(high=high, low=low, avg_volume=avg_volume)
    if not (config.width_min_pct <= opening_range.width_pct <= config.width_max_pct):
        return None
    return opening_range


def bar_confirms_breakout(opening_range: OpeningRange, bar: OrbBar, config: OrbConfig) -> bool:
    """The settled entry filter, evaluated at bar close: close above the frozen
    OR_high (not a wick), volume spike, and above VWAP/EMA9."""
    if not bar.close > opening_range.high:
        return False
    if not bar.volume >= config.vol_mult * opening_range.avg_volume:
        return False
    if config.require_vwap and not (bar.vwap is not None and bar.close > bar.vwap):
        return False
    if config.require_ema9 and not (bar.ema9 is not None and bar.close > bar.ema9):
        return False
    return True


def entry_fill_price(opening_range: OpeningRange, bar: OrbBar, mode: ExecutionMode) -> float:
    """BAR_CLOSE → the breakout bar close (parity). INTRABAR → the breakout level
    (OR_high), the price at which the cross is detected intrabar."""
    return bar.close if mode is ExecutionMode.BAR_CLOSE else opening_range.high


@dataclass
class TrailingStop:
    """TRAIL-8% hard stop. ``ratchet`` is called per tick/bar high; the stop only
    rises. ``breached`` is checked against the bar low / live bid. Default
    ``trail_pct=0`` → inert (never ratchets, never breaches above entry stop)."""

    entry_price: float
    trail_pct: float
    high_water_mark: float
    stop_price: float

    @classmethod
    def arm(cls, entry_price: float, trail_pct: float) -> "TrailingStop":
        return cls(
            entry_price=entry_price,
            trail_pct=trail_pct,
            high_water_mark=entry_price,
            stop_price=entry_price * (1.0 - trail_pct / 100.0),
        )

    def ratchet(self, price: float) -> None:
        if self.trail_pct <= 0:
            return
        if price > self.high_water_mark:
            self.high_water_mark = price
            self.stop_price = max(self.stop_price, self.high_water_mark * (1.0 - self.trail_pct / 100.0))

    def breached(self, low_or_bid: float) -> bool:
        return low_or_bid <= self.stop_price
