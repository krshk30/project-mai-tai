"""Pure state for the broker-disconnected ORB paper lifecycle."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from project_mai_tai.strategy_core.orb_intrabar import OrbBar


PAPER_LIFECYCLE_VERSION = 1
PAPER_TARGET_PCT = 5.0
PAPER_STOP_PCT = 8.0
PAPER_MIN_BREAK_BODY_PCT = 45.0
PAPER_ATR_PERIOD = 5
PAPER_ATR_FACTOR = 3.5
PAPER_ATR_MAX_BAR_GAP_SECONDS = 90
_ET = ZoneInfo("America/New_York")


@dataclass
class OrbPaperPosition:
    """One modeled long position; it has no account, adapter, or order handle."""

    entry_event_key: str
    symbol: str
    entry_time: datetime
    entry_price: float
    quantity: float
    mode: str
    target_pct: float = PAPER_TARGET_PCT
    stop_pct: float = PAPER_STOP_PCT
    current_bid: float | None = None
    current_bid_at: datetime | None = None
    peak_bid: float | None = None
    break_body_pct: float | None = None
    body_exit_pending: bool = False
    atr_exit_pending: bool = False
    atr_decision_at: datetime | None = None
    atr_trail: float | None = None
    exit_pending: bool = False

    @property
    def target_price(self) -> float:
        return self.entry_price * (1.0 + self.target_pct / 100.0)

    @property
    def stop_price(self) -> float:
        return self.entry_price * (1.0 - self.stop_pct / 100.0)

    @property
    def peak_profit_pct(self) -> float:
        if self.peak_bid is None or self.entry_price <= 0:
            return 0.0
        return (self.peak_bid / self.entry_price - 1.0) * 100.0

    def observe_bid(self, bid: float, observed_at: datetime) -> None:
        self.current_bid = bid
        self.current_bid_at = observed_at
        self.peak_bid = bid if self.peak_bid is None else max(self.peak_bid, bid)


def forming_bar_body_pct(*, open_price: float, high: float, low: float, close: float) -> float:
    """Return the real-body share of the forming bar's range, in percentage points."""
    bar_range = high - low
    if bar_range <= 0:
        return 0.0
    return abs(close - open_price) / bar_range * 100.0


def paper_atr_session_key(timestamp: datetime) -> str:
    et = timestamp.astimezone(_ET)
    if et.hour < 4:
        et -= timedelta(days=1)
    return et.date().isoformat()


def compute_paper_atr_trail(
    bars: Sequence[OrbBar],
    *,
    period: int = PAPER_ATR_PERIOD,
    factor: float = PAPER_ATR_FACTOR,
) -> list[dict[str, float | str | None]]:
    """Mirror v2's live 5/3.5 modified-TR Wilders state, including its gap guard.

    State resets at 04:00 ET. A non-adjacent bar contributes only its capped
    intrabar range, so an outage or an illiquid minute never turns the whole gap
    into one true-range sample.
    """
    rows: list[dict[str, float | str | None]] = []
    session_key: str | None = None
    high_lows: deque[float] = deque(maxlen=period)
    previous: OrbBar | None = None
    wilders: float | None = None
    seed: list[float] = []
    state: str | None = None
    trail: float | None = None

    for bar in bars:
        current_session = paper_atr_session_key(bar.timestamp)
        if current_session != session_key:
            session_key = current_session
            high_lows = deque(maxlen=period)
            previous = None
            wilders = None
            seed = []
            state = None
            trail = None

        high_low = bar.high - bar.low
        high_lows.append(high_low)
        true_range: float | None = None
        if len(high_lows) == period and previous is not None:
            capped_range = min(high_low, 1.5 * (sum(high_lows) / period))
            gap_seconds = (bar.timestamp - previous.timestamp).total_seconds()
            if gap_seconds > PAPER_ATR_MAX_BAR_GAP_SECONDS:
                true_range = capped_range
            else:
                high_reference = (
                    bar.high - previous.close
                    if bar.low <= previous.high
                    else (bar.high - previous.close) - 0.5 * (bar.low - previous.high)
                )
                low_reference = (
                    previous.close - bar.low
                    if bar.high >= previous.low
                    else (previous.close - bar.low) - 0.5 * (previous.low - bar.high)
                )
                true_range = max(capped_range, high_reference, low_reference)

        if true_range is not None:
            if wilders is None:
                seed.append(true_range)
                if len(seed) == period:
                    wilders = sum(seed) / period
            else:
                wilders = wilders + (true_range - wilders) / period
        previous = bar

        loss = factor * wilders if wilders is not None else None
        flip: str | None = None
        if loss is not None:
            if state is None:
                state, trail = "long", bar.close - loss
            elif state == "long":
                if bar.close > float(trail):
                    trail = max(float(trail), bar.close - loss)
                else:
                    state, trail, flip = "short", bar.close + loss, "SELL"
            elif bar.close < float(trail):
                trail = min(float(trail), bar.close + loss)
            else:
                state, trail, flip = "long", bar.close - loss, "BUY"

        rows.append(
            {
                "state": state,
                "trail": trail,
                "flip": flip,
                "loss": loss,
                "session": session_key,
            }
        )
    return rows
