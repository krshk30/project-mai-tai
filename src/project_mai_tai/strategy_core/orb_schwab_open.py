"""Pure three-bar ORB order decisions; the broker remains authoritative for fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from project_mai_tai.strategy_core.orb_intrabar import OrbBar


@dataclass(frozen=True)
class OrbSchwabAction:
    kind: Literal["place", "reprice", "cancel"]
    level: Decimal | None = None
    reason: str = ""


@dataclass
class OrbSchwabOpeningOrder:
    """One session's decisions. A reprice never authorizes a second buy."""

    opening_minute: datetime
    bars: dict[datetime, OrbBar] = field(default_factory=dict)
    placed: bool = False
    cancelled: bool = False
    last_requested_level: Decimal | None = None

    def on_closed_bar(
        self,
        bar: OrbBar,
        *,
        observed_at: datetime,
        macd_allowed: bool,
        macd_reason: str,
    ) -> OrbSchwabAction | None:
        first = self.opening_minute - timedelta(minutes=5)
        index = int((bar.timestamp - first).total_seconds() / 60)
        if index not in (2, 3, 4) or bar.timestamp != first + timedelta(minutes=index):
            return None
        if bar.timestamp in self.bars or self.cancelled:
            return None
        self.bars[bar.timestamp] = bar
        # A delayed stream must not place or adjust a live order using old prices.
        close_at = bar.timestamp + timedelta(minutes=1)
        if observed_at.tzinfo is None or not timedelta(0) <= observed_at - close_at <= timedelta(seconds=5):
            if self.placed:
                self.cancelled = True
                return OrbSchwabAction("cancel", reason="late_bar")
            return None
        if not macd_allowed:
            if self.placed:
                self.cancelled = True
                return OrbSchwabAction("cancel", reason=f"macd_{macd_reason}")
            return None
        expected = {first + timedelta(minutes=i) for i in range(index + 1)}
        if not expected.issubset(self.bars):
            if self.placed:
                self.cancelled = True
                return OrbSchwabAction("cancel", reason="missing_opening_bar")
            return None
        qualified = [
            Decimal(str(item.breakout_high))
            for minute, item in self.bars.items()
            if minute in expected and item.breakout_high is not None and item.breakout_high > 0
        ]
        if not qualified:
            return None
        level = max(qualified)
        if not self.placed:
            if index != 2:
                return None
            self.placed = True
            self.last_requested_level = level
            return OrbSchwabAction("place", level, "third_bar_complete")
        if self.last_requested_level is not None and level > self.last_requested_level:
            self.last_requested_level = level
            return OrbSchwabAction("reprice", level, "higher_completed_bar")
        return None
