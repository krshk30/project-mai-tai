"""ORB tick -> 1-min bar aggregator (import-clean leaf).

The ORB service drains trade ticks (price, size) from the market-data gateway and
feeds them here; this emits completed 1-min ``OrbBar``s (OHLCV + session VWAP +
EMA9) for the ORB entry logic (slice 3b). Pure / feed-agnostic / no I/O.

Session VWAP is cumulative typical-price*volume anchored at ``session_open``
(09:30); pre-open ticks don't contribute to it. EMA9 is computed over completed
bar closes. A bar completes when a tick arrives in a later 1-min bucket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from project_mai_tai.strategy_core.orb_intrabar import OrbBar


@dataclass
class OrbTickAggregator:
    session_open: datetime | None = None
    ema_period: int = 9

    _bucket: datetime | None = field(default=None, init=False)
    _o: float = field(default=0.0, init=False)
    _h: float = field(default=0.0, init=False)
    _l: float = field(default=0.0, init=False)
    _c: float = field(default=0.0, init=False)
    _v: float = field(default=0.0, init=False)
    _cum_pv: float = field(default=0.0, init=False)
    _cum_v: float = field(default=0.0, init=False)
    _ema: float | None = field(default=None, init=False)

    @staticmethod
    def _floor_minute(ts: datetime) -> datetime:
        return ts.replace(second=0, microsecond=0)

    def add_tick(self, timestamp: datetime, price: float, size: float) -> OrbBar | None:
        """Add one trade tick. Returns a completed OrbBar when the tick rolls into
        a new 1-min bucket (i.e. the *prior* minute closed), else None."""
        bucket = self._floor_minute(timestamp)
        completed: OrbBar | None = None
        if self._bucket is not None and bucket > self._bucket:
            completed = self._finalize()
            self._start(bucket, price, size)
        elif self._bucket is None:
            self._start(bucket, price, size)
        elif bucket == self._bucket:
            self._h = max(self._h, price)
            self._l = min(self._l, price)
            self._c = price
            self._v += size
        # ticks with bucket < current (late/out-of-order) are ignored
        return completed

    def flush(self) -> OrbBar | None:
        """Finalize the in-progress bucket (e.g. at the cutoff). Returns the bar or
        None; the aggregator is left with no open bucket."""
        if self._bucket is None:
            return None
        bar = self._finalize()
        self._bucket = None
        return bar

    def _start(self, bucket: datetime, price: float, size: float) -> None:
        self._bucket = bucket
        self._o = self._h = self._l = self._c = price
        self._v = size

    def _finalize(self) -> OrbBar:
        typical = (self._h + self._l + self._c) / 3.0
        if self.session_open is None or self._bucket >= self.session_open:
            self._cum_pv += typical * self._v
            self._cum_v += self._v
        vwap = self._cum_pv / self._cum_v if self._cum_v > 0 else self._c
        k = 2.0 / (self.ema_period + 1)
        self._ema = self._c if self._ema is None else self._c * k + self._ema * (1.0 - k)
        return OrbBar(
            timestamp=self._bucket,
            open=self._o,
            high=self._h,
            low=self._l,
            close=self._c,
            volume=self._v,
            vwap=vwap,
            ema9=self._ema,
        )
