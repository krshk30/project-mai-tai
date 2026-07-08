"""ORB tick-driven entry engine (production leaf) — the approved V1 entry decision, shared by the
live orb_app.py tick handler AND the backtest engine so the back-test validates the real code.

Decision (per docs/orb-tick-exit-design.md, APPROVED+LOCKED):
  - CONTINUOUS running-high (advances every trade tick, so a stale bar-lagged level can't be
    re-crossed — the CELZ-phantom fix).
  - ENTER on the break TICK (price > running-high) inside the 09:30-cutoff window (not at bar close).
  - HIGH-ATR gate (optional, causal): only enter if the period-5 ATR% over the ORB-window bars formed
    SO FAR is >= `atr_gate_pct` (slow names whipsaw on the 2% trail — gate them out). None = no gate.
  - gap-cap / fill / exit / attempt-cap are the CALLER's job (kept out so this stays a pure decision
    and reproduces the validated backtest `simulate_intrabar` exactly when the gate is off).

Pure, no I/O. `observe_tick` returns the broken running-high level (the intended entry level) or None.
The exit is the OMS tick-driven 2% ratcheting trail (already live) — not this module's concern.

The self-contained ATR% (`atr_pct5`) is pinned to the vendored oracle by
tests/backtest/test_orb_tick_entry.py (strategy_core must not import backtest/ or analysis/).
"""
from __future__ import annotations

from datetime import datetime
from statistics import median

from project_mai_tai.strategy_core.orb_intrabar import OrbBar

ATR_PERIOD = 5


def atr_pct5(bars: list[OrbBar]) -> float | None:
    """Median period-5 Wilders ATR% (ATR/close*100) over `bars` — the volatility measure the study
    classified names by. modified-TR + Wilders(period, seed=sma5), matching analysis/atr_flip. Returns
    None if fewer than PERIOD+1 usable bars. Self-contained (no backtest/analysis import)."""
    n = len(bars)
    if n < ATR_PERIOD + 1:
        return None
    hl = [b.high - b.low for b in bars]
    tr: list[float | None] = [None] * n
    for i in range(1, n):
        if i < ATR_PERIOD - 1:
            continue
        s = sum(hl[i - ATR_PERIOD + 1:i + 1]) / ATR_PERIOD
        prev, cur = bars[i - 1], bars[i]
        hilo = min(hl[i], 1.5 * s)
        href = (cur.high - prev.close) if cur.low <= prev.high \
            else (cur.high - prev.close) - 0.5 * (cur.low - prev.high)
        lref = (prev.close - cur.low) if cur.high >= prev.low \
            else (prev.close - cur.low) - 0.5 * (prev.low - cur.high)
        tr[i] = max(hilo, href, lref)
    valid = [i for i in range(n) if tr[i] is not None]
    if len(valid) < ATR_PERIOD:
        return None
    w: list[float | None] = [None] * n
    seed = valid[:ATR_PERIOD]
    prev_w = sum(tr[i] for i in seed) / ATR_PERIOD
    start = seed[-1]
    w[start] = prev_w
    for i in range(start + 1, n):
        if tr[i] is None:
            continue
        prev_w = prev_w + (tr[i] - prev_w) / ATR_PERIOD
        w[i] = prev_w
    pcts = [w[i] / bars[i].close * 100.0 for i in range(n) if w[i] is not None and bars[i].close > 0]
    return median(pcts) if pcts else None


class OrbTickEntry:
    """Streaming ORB tick-entry decision. Feed closed bars via `observe_bar` (for the causal ATR gate)
    and every trade tick via `observe_tick`; the latter returns the broken level when this tick is a
    qualifying entry break, else None. `advance` is the cheap running-high bump used while holding."""

    def __init__(self, *, observe_open: datetime, session_open: datetime, cutoff: datetime,
                 atr_gate_pct: float | None = None) -> None:
        self._observe_open = observe_open
        self._session_open = session_open
        self._cutoff = cutoff
        self._atr_gate_pct = atr_gate_pct
        self.running_high: float | None = None
        self._bars: list[OrbBar] = []

    def observe_bar(self, bar: OrbBar) -> None:
        """Feed a CLOSED 1-min ORB-window bar (causal: only bars closed before a tick inform its gate)."""
        self._bars.append(bar)

    def _gate_passes(self) -> bool:
        if self._atr_gate_pct is None:
            return True
        v = atr_pct5(self._bars)
        return v is not None and v >= self._atr_gate_pct

    def observe_tick(self, ts: datetime, price: float) -> float | None:
        """FLAT-state tick. Advance the continuous running-high; return the broken level if this tick
        breaks it inside the window AND the high-ATR gate passes, else None. Gap-cap/fill/attempt-cap
        are the caller's. Byte-identical to simulate_intrabar's running-high/break when gate is off."""
        if ts < self._observe_open:
            return None
        if self.running_high is None:
            self.running_high = price          # first observed tick seeds the reference
            return None
        level: float | None = None
        if self._session_open <= ts <= self._cutoff and price > self.running_high and self._gate_passes():
            level = self.running_high
        self.running_high = max(self.running_high, price)
        return level

    def advance(self, price: float) -> None:
        """HOLDING-state tick: advance the running-high only (no break/gate) — so a re-entry after the
        exit needs a genuinely higher high (mirrors simulate_intrabar's hold-advance loop)."""
        if self.running_high is not None:
            self.running_high = max(self.running_high, price)
