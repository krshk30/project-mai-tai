"""Dedicated strategy module for the isolated `schwab_1m_v2` bot.

Implements the MACD Momentum v1.32 design (entry side only). Exits, scaled
exits, and hard stops are handled by OMS — this module only emits open
intents.

Inputs (from `market_data/schwab_v2_rest_client.py`):
- ChartBar: closed 1-minute OHLCV candles, REST-polled
- Quote: bid/ask/last + cumulative volume, REST-polled

Output (to OMS via Redis `strategy-intents` stream):
- TradeIntentEvent in the existing shape (see `events.py::TradeIntentPayload`)

Strategy spec sources (in this file, intentionally not imported from
elsewhere — every fix to this strategy must touch ONLY this file):
- Two entry paths: "MACD Cross" (path 1) and "VWAP Breakout" (path 2)
- Both require macd_line > signal_line AND macd_line > prev macd_line
- Seven base filter gates; each "off" toggle = pass-through
- Per-symbol cooldown after OMS closes the position
- Bar-close evaluation only (intrabar quotes update freshness, not signals)

NO imports from: `schwab_native_30s.py`, `bar_builder.py`, `indicators.py`,
`entry.py`, `exit.py`, `strategy_engine_app.py`, `polygon_30s.py`.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Deque, Iterable
from uuid import UUID
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from project_mai_tai.events import (
    TradeIntentEvent,
    TradeIntentPayload,
    stream_name,
)
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote
from project_mai_tai.settings import Settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "schwab-1m-v2"
STRATEGY_CODE = "schwab_1m_v2"
STRATEGY_VERSION = "v1.32"
EASTERN_TZ = ZoneInfo("America/New_York")

# VWAP session anchor: 04:00 ET each day, matching scanner-session-roll.
# Bars from 04:00 ET through next-day 03:59 ET share a VWAP cumulator.
VWAP_SESSION_HOUR_ET = 4

# Max age (seconds) a bar can be relative to wall-clock and still emit a
# signal. The REST client feeds the full 24h candle window on cold-start
# for indicator warmup; this guard prevents historical bars from firing
# stale signals while indicators back-fill.
MAX_BAR_AGE_SECONDS_FOR_EMIT = 180.0


@dataclass(frozen=True)
class SchwabV2Config:
    """All MACD Momentum v1.32 inputs. Defaults match the design doc
    (Section 3) exactly. Trade size is read separately from settings so
    the operator can resize from the env file without code edits.
    """

    # MACD (Section 3.1)
    macd_fast_length: int = 12
    macd_slow_length: int = 26
    macd_signal_length: int = 9
    # Stochastic (Section 3.1)
    stoch_length: int = 5
    # Volume (Section 3.1, 3.3)
    volume_threshold: int = 5000
    rel_vol_multiple: float = 1.5
    rel_vol_length: int = 20
    require_rel_volume: bool = True
    # VWAP (Section 3.2)
    require_vwap_filter: bool = True
    allow_vwap_cross_entry: bool = True
    # Cooldown (Section 3.2)
    cooldown_bars: int = 5
    # Trend EMA (Section 3.3)
    require_uptrend: bool = True
    ema_trend_length: int = 9
    # MACD strength (Section 3.3)
    require_macd_strength: bool = True
    macd_hist_min_pct: float = 0.02
    # Overbought (Section 3.3 — disabled by default in spec)
    block_overbought: bool = False
    stoch_max_at_entry: float = 90.0
    # Green bar (Section 3.3)
    require_green_bar: bool = True
    # Dead zone (Section 3.2 — disabled by default; both 0)
    dead_zone_start: int = 0
    dead_zone_end: int = 0


@dataclass
class OHLCVBar:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class SymbolState:
    symbol: str
    bars: Deque[OHLCVBar] = field(default_factory=lambda: deque(maxlen=300))
    last_quote: Quote | None = None
    position_qty: int = 0
    last_entry_price: float | None = None
    # Indicator memo for cross detection (v1.32 needs prev_* to fire only
    # on transitions, not while a condition continues to hold true).
    prev_macd: float | None = None
    prev_signal: float | None = None
    prev_close: float | None = None
    prev_vwap: float | None = None
    # State machine (entry side — exits are OMS).
    cooldown_bars_remaining: int = 0
    # Session-VWAP accumulators (reset at each 04:00 ET anchor).
    vwap_session_anchor_ms: int = 0
    vwap_sum_pv: float = 0.0
    vwap_sum_v: float = 0.0


@dataclass
class TradeIntentDraft:
    symbol: str
    side: str  # "buy" | "sell"
    intent_type: str  # "open" | "scale" | "close" | "cancel"
    quantity: Decimal
    reason: str
    metadata: dict[str, str] = field(default_factory=dict)


def session_start_ts_ms(bar_ts_ms: int) -> int:
    """Return the 04:00 ET session anchor (ms UTC) for a bar at bar_ts_ms.

    Bars at or after 04:00 ET on a given calendar day share the same anchor.
    A bar at 03:30 ET belongs to the previous day's 04:00 ET anchor.
    """
    bar_utc = datetime.fromtimestamp(bar_ts_ms / 1000.0, UTC)
    bar_et = bar_utc.astimezone(EASTERN_TZ)
    anchor_et = bar_et.replace(
        hour=VWAP_SESSION_HOUR_ET, minute=0, second=0, microsecond=0
    )
    if bar_et < anchor_et:
        anchor_et = anchor_et - timedelta(days=1)
    return int(anchor_et.astimezone(UTC).timestamp() * 1000)


class V2Indicators:
    """Inline indicator math for the v1.32 strategy. Kept tightly scoped
    to what the spec actually needs.
    """

    @staticmethod
    def ema(values: Iterable[float], period: int) -> float | None:
        seq = list(values)
        if len(seq) < period:
            return None
        multiplier = 2.0 / (period + 1)
        ema_value = sum(seq[:period]) / period
        for price in seq[period:]:
            ema_value = (price - ema_value) * multiplier + ema_value
        return ema_value

    @staticmethod
    def ema_series(values: list[float], period: int) -> list[float]:
        """Full EMA series. Output length = len(values) - period + 1.
        Each output[i] is the EMA value at input position (period - 1 + i).
        """
        if len(values) < period:
            return []
        multiplier = 2.0 / (period + 1)
        ema_value = sum(values[:period]) / period
        result = [ema_value]
        for price in values[period:]:
            ema_value = (price - ema_value) * multiplier + ema_value
            result.append(ema_value)
        return result

    @staticmethod
    def macd(
        closes: Iterable[float], fast: int = 12, slow: int = 26, signal: int = 9
    ) -> tuple[float, float, float] | None:
        """Return (macd_line, signal_line, histogram).

        macd_line   = ema(closes, fast) - ema(closes, slow)
        signal_line = ema(macd_line_series, signal)
        histogram   = macd_line - signal_line

        Signal line requires a series of MACD values; computed by running
        EMA across the full macd_line history derivable from `closes`.
        """
        seq = list(closes)
        if len(seq) < slow + signal:
            return None
        fast_series = V2Indicators.ema_series(seq, fast)
        slow_series = V2Indicators.ema_series(seq, slow)
        if not fast_series or not slow_series:
            return None
        offset = len(fast_series) - len(slow_series)
        macd_series = [
            fast_series[i + offset] - slow_series[i] for i in range(len(slow_series))
        ]
        if len(macd_series) < signal:
            return None
        signal_series = V2Indicators.ema_series(macd_series, signal)
        if not signal_series:
            return None
        macd_now = macd_series[-1]
        signal_now = signal_series[-1]
        histogram = macd_now - signal_now
        return macd_now, signal_now, histogram

    @staticmethod
    def stochastic_k(
        highs: list[float], lows: list[float], closes: list[float], length: int = 5
    ) -> float | None:
        """FullK with no extra smoothing (smoothK=1 in v1.32 inputs)."""
        if min(len(highs), len(lows), len(closes)) < length:
            return None
        high_n = max(highs[-length:])
        low_n = min(lows[-length:])
        if high_n == low_n:
            # Flat range — return neutral 50 so neither the overbought
            # ceiling nor the exit trigger fires on a coincidence.
            return 50.0
        return (closes[-1] - low_n) / (high_n - low_n) * 100.0

    @staticmethod
    def avg_volume(volumes: list[int], length: int = 20) -> float | None:
        if len(volumes) < length:
            return None
        return sum(volumes[-length:]) / length


class SchwabV2Strategy:
    """MACD Momentum v1.32 — entry side.

    Per-symbol state machine emits one "open" intent per momentum move.
    OMS owns all exits (MACD-cross-down / stochastic-exit / quick-stop /
    scaled / hard-stop) — we never emit close/scale/cancel intents.

    The engine calls update_position(symbol, qty) on a 5s poll so we can
    track True→False transitions and arm the cooldown.
    """

    def __init__(
        self, settings: Settings, config: SchwabV2Config | None = None
    ) -> None:
        self.settings = settings
        self.cfg = config or SchwabV2Config()
        self._symbol_states: dict[str, SymbolState] = {}

    def watchlist_state(self, symbol: str) -> SymbolState:
        state = self._symbol_states.get(symbol)
        if state is None:
            state = SymbolState(symbol=symbol)
            self._symbol_states[symbol] = state
        return state

    def drop_symbol(self, symbol: str) -> None:
        self._symbol_states.pop(symbol, None)

    def update_position(self, symbol: str, qty: int) -> None:
        """Called by the engine each position-poll cycle. On a True→False
        transition (OMS just closed our position), arm the cooldown so we
        don't re-enter immediately on the same bar.
        """
        state = self.watchlist_state(symbol)
        prev = state.position_qty
        state.position_qty = max(0, int(qty))
        if prev > 0 and state.position_qty == 0:
            state.cooldown_bars_remaining = self.cfg.cooldown_bars
            logger.info(
                "schwab_1m_v2 cooldown armed for %s (bars=%d) "
                "after OMS closed the position",
                symbol,
                self.cfg.cooldown_bars,
            )

    def on_bar(self, symbol: str, bar: ChartBar) -> TradeIntentDraft | None:
        state = self.watchlist_state(symbol)
        ohlcv = OHLCVBar(
            timestamp_ms=bar.timestamp_ms,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        is_new_bar = (
            not state.bars or state.bars[-1].timestamp_ms != ohlcv.timestamp_ms
        )
        if state.bars and state.bars[-1].timestamp_ms == ohlcv.timestamp_ms:
            state.bars[-1] = ohlcv
        else:
            state.bars.append(ohlcv)

        # Only update VWAP accumulators on genuinely new bars; revisions
        # to the same minute must not double-count volume.
        if is_new_bar:
            self._update_vwap_accumulators(state, ohlcv)

        return self._evaluate_completed_bar(state, is_new_bar=is_new_bar)

    def on_quote(self, symbol: str, quote: Quote) -> TradeIntentDraft | None:
        # v1.32 spec: bar-close evaluation only. Quotes update freshness
        # but never fire entry signals.
        state = self.watchlist_state(symbol)
        state.last_quote = quote
        return None

    # ---------------------------------------------------------------- VWAP

    def _update_vwap_accumulators(self, state: SymbolState, bar: OHLCVBar) -> None:
        anchor = session_start_ts_ms(bar.timestamp_ms)
        if anchor != state.vwap_session_anchor_ms:
            state.vwap_session_anchor_ms = anchor
            state.vwap_sum_pv = 0.0
            state.vwap_sum_v = 0.0
        typical = (bar.high + bar.low + bar.close) / 3.0
        state.vwap_sum_pv += typical * float(bar.volume)
        state.vwap_sum_v += float(bar.volume)

    def _current_vwap(self, state: SymbolState, fallback: float) -> float:
        if state.vwap_sum_v > 0:
            return state.vwap_sum_pv / state.vwap_sum_v
        return fallback

    # ------------------------------------------------------------- evaluate

    def _evaluate_completed_bar(
        self, state: SymbolState, *, is_new_bar: bool
    ) -> TradeIntentDraft | None:
        # Only re-evaluate when a NEW minute lands; bar revisions of the
        # same timestamp are a noop for signaling (the cross-detection
        # state would double-fire otherwise).
        if not is_new_bar:
            return None

        # Bootstrap: need enough history for the slowest indicator chain.
        min_bars = max(
            self.cfg.macd_slow_length + self.cfg.macd_signal_length,
            self.cfg.rel_vol_length + 1,
            self.cfg.ema_trend_length + 1,
            self.cfg.stoch_length,
        )
        if len(state.bars) < min_bars:
            return None

        # Decrement cooldown on every new bar (independent of whether we
        # would have signaled). v1.32 spec: cooldown ticks down each bar.
        if state.cooldown_bars_remaining > 0:
            state.cooldown_bars_remaining -= 1

        closes = [b.close for b in state.bars]
        highs = [b.high for b in state.bars]
        lows = [b.low for b in state.bars]
        vols = [b.volume for b in state.bars]
        cur = state.bars[-1]

        macd_result = V2Indicators.macd(
            closes,
            self.cfg.macd_fast_length,
            self.cfg.macd_slow_length,
            self.cfg.macd_signal_length,
        )
        if macd_result is None:
            return None
        macd_line, signal_line, histogram = macd_result

        stoch_k = V2Indicators.stochastic_k(highs, lows, closes, self.cfg.stoch_length)
        ema_trend = V2Indicators.ema(closes, self.cfg.ema_trend_length)
        avg_vol = V2Indicators.avg_volume(vols, self.cfg.rel_vol_length)
        vwap = self._current_vwap(state, fallback=cur.close)
        if stoch_k is None or ema_trend is None or avg_vol is None:
            return None

        # Cross detection — fires only on the single transition bar.
        macd_cross_above = (
            state.prev_macd is not None
            and state.prev_signal is not None
            and state.prev_macd <= state.prev_signal
            and macd_line > signal_line
        )
        macd_above_signal = macd_line > signal_line
        macd_increasing = (
            state.prev_macd is not None and macd_line > state.prev_macd
        )
        vwap_cross_above = (
            state.prev_close is not None
            and state.prev_vwap is not None
            and state.prev_close <= state.prev_vwap
            and cur.close > vwap
        )

        # Filter gates. Disabled toggle = gate passes (per Section 5).
        hist_pct = (histogram / cur.close * 100.0) if cur.close > 0 else 0.0
        trend_ok = (not self.cfg.require_uptrend) or (cur.close > ema_trend)
        macd_strength_ok = (not self.cfg.require_macd_strength) or (
            hist_pct >= self.cfg.macd_hist_min_pct
        )
        stoch_not_chase = (not self.cfg.block_overbought) or (
            stoch_k < self.cfg.stoch_max_at_entry
        )
        green_bar_ok = (not self.cfg.require_green_bar) or (cur.close > cur.open)
        rel_vol_ok = (not self.cfg.require_rel_volume) or (
            avg_vol > 0 and cur.volume > avg_vol * self.cfg.rel_vol_multiple
        )
        vol_abs_ok = cur.volume > self.cfg.volume_threshold
        time_allowed = self._time_allowed(cur.timestamp_ms)

        base_filters = (
            trend_ok
            and macd_strength_ok
            and stoch_not_chase
            and green_bar_ok
            and rel_vol_ok
            and vol_abs_ok
            and time_allowed
        )

        # VWAP filter for Path 1 (Section 6.1): above VWAP, OR a fresh
        # cross above when allow_vwap_cross_entry is enabled.
        vwap_filter_path1 = (
            (not self.cfg.require_vwap_filter)
            or cur.close > vwap
            or (self.cfg.allow_vwap_cross_entry and vwap_cross_above)
        )

        # Path 1 — MACD Cross.
        path_macd = (
            macd_cross_above
            and macd_increasing
            and vwap_filter_path1
            and base_filters
        )
        # Path 2 — VWAP Breakout.
        path_vwap = (
            vwap_cross_above
            and macd_above_signal
            and macd_increasing
            and base_filters
        )

        # Persist memo BEFORE any early return so cross-detection stays
        # consistent across bars. Memo MUST update on every bar including
        # historical warmup feeds, otherwise prev_* is stale and crosses
        # are missed when live bars start arriving.
        state.prev_macd = macd_line
        state.prev_signal = signal_line
        state.prev_close = cur.close
        state.prev_vwap = vwap

        # Freshness guard: only the live tail of any batch can emit. Old
        # bars (replayed from the 24h REST window on cold-start) update
        # indicators above but never fire intents. This prevents emitting
        # an "open" intent on a MACD cross that happened hours ago.
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        bar_age_secs = (now_ms - cur.timestamp_ms) / 1000.0
        if bar_age_secs > MAX_BAR_AGE_SECONDS_FOR_EMIT:
            return None

        # State-machine gate (entry side): flat + no cooldown + raw entry.
        if state.position_qty > 0:
            return None
        if state.cooldown_bars_remaining > 0:
            return None
        if not (path_macd or path_vwap):
            return None

        path_name = "MACD Cross" if path_macd else "VWAP Breakout"
        quantity = max(
            1, int(self.settings.strategy_schwab_1m_v2_default_quantity)
        )
        state.last_entry_price = cur.close
        rel_vol_ratio = cur.volume / avg_vol if avg_vol > 0 else 0.0
        return TradeIntentDraft(
            symbol=state.symbol,
            side="buy",
            intent_type="open",
            quantity=Decimal(str(quantity)),
            reason=f"schwab_1m_v2 {path_name}",
            metadata={
                "path": path_name,
                "entry_price": f"{cur.close:.4f}",
                "macd_value": f"{macd_line:.6f}",
                "macd_signal": f"{signal_line:.6f}",
                "macd_hist": f"{histogram:.6f}",
                "macd_hist_pct": f"{hist_pct:.4f}",
                "stoch_k": f"{stoch_k:.2f}",
                "rel_vol_ratio": f"{rel_vol_ratio:.2f}",
                "ema_trend": f"{ema_trend:.4f}",
                "vwap": f"{vwap:.4f}",
                "volume": str(cur.volume),
                "avg_volume": f"{avg_vol:.2f}",
                "source": "schwab_1m_v2",
                "strategy_version": STRATEGY_VERSION,
                "bar_time_ms": str(cur.timestamp_ms),
            },
        )

    def _time_allowed(self, ts_ms: int) -> bool:
        """Dead-zone check. Both bounds=0 disables the window entirely."""
        if self.cfg.dead_zone_start == 0 and self.cfg.dead_zone_end == 0:
            return True
        bar_et = datetime.fromtimestamp(ts_ms / 1000.0, UTC).astimezone(EASTERN_TZ)
        hhmm = bar_et.hour * 100 + bar_et.minute
        start = self.cfg.dead_zone_start
        end = self.cfg.dead_zone_end
        if start <= end:
            return not (start <= hhmm < end)
        # Wraps midnight: e.g. start=2200, end=0400 blocks 22:00-23:59 and
        # 00:00-03:59. Outside the window = allowed.
        return not (hhmm >= start or hhmm < end)


class SchwabV2IntentEmitter:
    """Writes intents to Redis `strategy-intents` stream in the shape OMS
    already expects. OMS persists to trade_intents on stream consumption.
    """

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        broker_account_name: str,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.broker_account_name = broker_account_name
        self.stream = stream_name(settings.redis_stream_prefix, "strategy-intents")

    async def emit(
        self,
        draft: TradeIntentDraft,
        *,
        correlation_id: UUID | None = None,
    ) -> UUID:
        payload = TradeIntentPayload(
            strategy_code=STRATEGY_CODE,
            broker_account_name=self.broker_account_name,
            symbol=draft.symbol,
            side=draft.side,  # type: ignore[arg-type]
            quantity=draft.quantity,
            intent_type=draft.intent_type,  # type: ignore[arg-type]
            reason=draft.reason,
            metadata=dict(draft.metadata),
        )
        event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            correlation_id=correlation_id,
            payload=payload,
        )
        await self.redis.xadd(
            self.stream,
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_strategy_intent_stream_maxlen,
            approximate=True,
        )
        logger.info(
            "schwab_1m_v2 emitted intent symbol=%s side=%s type=%s qty=%s "
            "reason=%s",
            draft.symbol,
            draft.side,
            draft.intent_type,
            draft.quantity,
            draft.reason,
        )
        return event.event_id


def utc_now_isoformat() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "STRATEGY_CODE",
    "STRATEGY_VERSION",
    "SERVICE_NAME",
    "OHLCVBar",
    "SchwabV2Config",
    "SymbolState",
    "V2Indicators",
    "SchwabV2Strategy",
    "TradeIntentDraft",
    "SchwabV2IntentEmitter",
    "session_start_ts_ms",
    "utc_now_isoformat",
]
