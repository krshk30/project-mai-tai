"""Initial ORB-Schwab entry gate using the exact completed-bar exit ATR."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite

from project_mai_tai.orb_paper_lifecycle import compute_paper_atr_trail
from project_mai_tai.orb_schwab_exits import ATR_SOURCE, schwab_completed_atr_bars
from project_mai_tai.strategy_core.orb_intrabar import OrbBar


@dataclass(frozen=True)
class AtrEntryGate:
    verdict: str
    reason: str
    bar_at: datetime | None = None
    close: float | None = None
    trail: float | None = None

    def evidence(self) -> dict:
        return {"source": ATR_SOURCE, "verdict": self.verdict, "reason": self.reason,
                "bar_at": self.bar_at.isoformat() if self.bar_at else None,
                "close": self.close, "trail": self.trail}


def completed_atr_entry_gate(bars: list[OrbBar], status: str, now: datetime) -> AtrEntryGate:
    if status != "complete":
        return AtrEntryGate("unknown", status)
    if now.tzinfo is None:
        return AtrEntryGate("unknown", "invalid_evaluation_time")
    last = now.astimezone(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    if not bars or bars[-1].timestamp != last:
        return AtrEntryGate("unknown", "missing_last_closed_schwab_bar")
    if any(bar.timestamp.tzinfo is None or bar.timestamp > last for bar in bars):
        return AtrEntryGate("unknown", "invalid_completed_schwab_bars")
    if any(right.timestamp <= left.timestamp for left, right in zip(bars, bars[1:])):
        return AtrEntryGate("unknown", "unordered_schwab_bars")
    result = compute_paper_atr_trail(bars)[-1]
    close, trail = bars[-1].close, result["trail"]
    if (trail is None or result["state"] not in {"long", "short"}
            or not isfinite(close) or close <= 0 or not isfinite(trail)):
        return AtrEntryGate("unknown", "insufficient_schwab_atr_history")
    allowed = result["state"] == "long" and close >= trail
    return AtrEntryGate("allowed" if allowed else "below_line",
                        "prior_close_at_or_above_atr" if allowed else "prior_close_under_atr",
                        bars[-1].timestamp, close, trail)


def schwab_atr_entry_gate(factory, symbol: str, now: datetime) -> AtrEntryGate:
    if now.tzinfo is None:
        return AtrEntryGate("unknown", "invalid_evaluation_time")
    bars, status = schwab_completed_atr_bars(factory, symbol, now)
    return completed_atr_entry_gate(bars, status, now)
