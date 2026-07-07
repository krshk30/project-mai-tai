"""ORB running-high entry / break detection — EXACT mirror of the live logic.

Mirrors `services/orb_app.py:_on_bar_running_high` (lines 525-557). The one rule that
matters most (the CELZ-bug fix): the running-high level advances ONCE PER CLOSED BAR to
`max(running_high, bar.high)` — it is NEVER a per-tick stale level. The prior throwaway
backtest evaluated per-tick against a bar-lagged level, so a symbol ranging at its high
re-crossed the same stale level every tick → 93 phantom "breaks" on CELZ. This bar-close
tracker is structurally incapable of that (≤ one break per closed bar).

DETECTION only (this component): produces the ordered list of entry-eligible breaks. The
re-entry/cap/traded gating (the 2-attempt cap, hold-until-flat, reclaim) is Component 3;
the strategy-EDGE study (the operator's 15-name-day table, e.g. CELZ 23) counts genuine
breaks without the live risk-cap, to measure the entry thesis. Live-behavior mode (cap)
reproduces the real broker fills (KIDZ). This component feeds both.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from project_mai_tai.strategy_core.orb_intrabar import OrbBar

# Live defaults (services/orb_app.py): gap-cap 1.5% (:174), window 30min, observe 09:25 ET.
DEFAULT_GAP_CAP_PCT = 1.5
DEFAULT_WINDOW_MIN = 30


@dataclass(frozen=True)
class Break:
    """One completed bar whose high exceeded the prior running high (a break candidate)."""
    bar_ts: datetime
    level: float        # the running-high that was broken (the intended entry level)
    bar_open: float
    bar_high: float
    fill_ref: float     # level, or bar_open if the bar GAPPED open above the level
    gap_ok: bool        # fill_ref <= level*(1+gap_cap) — entry-eligible per the live gap-cap
    running_high_before: float


class RunningHighTracker:
    """Bar-close running-high tracker (exact mirror of orb_app.py:525-557).

    Feed completed bars in order via `on_bar`; returns a Break when THIS bar's high exceeds
    the running high (evaluated in-window), then advances the running high. `observe_open`
    (09:25 ET) seeds; entries only inside [session_open, cutoff] (09:30–10:00 ET)."""

    def __init__(
        self,
        *,
        observe_open: datetime,   # 09:25 ET
        session_open: datetime,   # 09:30 ET
        cutoff: datetime,         # session_open + window
        gap_cap_pct: float = DEFAULT_GAP_CAP_PCT,
    ) -> None:
        self.observe_open = observe_open
        self.session_open = session_open
        self.cutoff = cutoff
        self.gap_cap_pct = gap_cap_pct
        self.running_high: float | None = None

    def on_bar(self, bar: OrbBar) -> Break | None:
        if bar.timestamp < self.observe_open:
            return None
        if self.running_high is None:
            self.running_high = bar.high          # seed on the first observed bar (no entry)
            return None
        brk: Break | None = None
        rh_before = self.running_high
        in_window = self.session_open <= bar.timestamp <= self.cutoff
        if in_window and bar.high > rh_before:    # break test vs the OLD running high
            level = rh_before
            fill_ref = level if bar.open <= level else bar.open   # gap-up fills at open
            gap_ok = fill_ref <= level * (1.0 + self.gap_cap_pct / 100.0)
            brk = Break(bar.timestamp, level, bar.open, bar.high, fill_ref, gap_ok, rh_before)
        self.running_high = max(self.running_high, bar.high)      # advance EVERY bar (:557)
        return brk


def detect_breaks(bars, *, observe_open, session_open, cutoff, gap_cap_pct=DEFAULT_GAP_CAP_PCT):
    """Run the tracker over ordered bars; return the list of Breaks (all in-window breaks)."""
    t = RunningHighTracker(
        observe_open=observe_open, session_open=session_open, cutoff=cutoff, gap_cap_pct=gap_cap_pct
    )
    out: list[Break] = []
    for b in bars:
        brk = t.on_bar(b)
        if brk is not None:
            out.append(brk)
    return out
