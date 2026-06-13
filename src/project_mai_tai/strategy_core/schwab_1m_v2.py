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
    # Extra bars beyond the strict MACD math minimum (macd_slow +
    # macd_signal = 35) before `_evaluate_completed_bar` touches
    # indicators, runs cross detection, or updates the prev_* memo.
    # Empirical: the simple-average EMA seed bias decays below TOS's
    # 4-decimal display precision around n_bars ≈ 135 — validated against
    # CPSH 2026-05-22 15:46–16:00 ET via `[V2-MACD-PROBE]` (see Day 2
    # entry in `docs/session-handoff-schwab-1m-v2.md`). With this
    # allowance, crosses detected during the warmup batch use settled
    # EMAs, so the memo handed off to live evaluation reflects truth
    # rather than seed-biased noise.
    macd_warmup_settling_bars: int = 100
    # C2: when a MACD-cross or VWAP-breakout cross is detected on a bar
    # that's then suppressed by the `MAX_BAR_AGE_SECONDS_FOR_EMIT`
    # freshness guard, carry the cross forward as `pending_*` on the
    # SymbolState so the next FRESH bar can still consume it. This field
    # is the maximum allowed gap (in seconds) between the cross-bar's
    # timestamp and the fresh bar that would consume it — beyond this,
    # the pending cross expires and the signal is discarded. Default
    # 180s = 3 bars at 1m, matching the existing emit-freshness window.
    # Setting to 0 effectively disables pending carryforward (cross
    # only fires if the cross-bar itself is fresh).
    pending_cross_max_gap_secs: int = 180


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
    # C2: pending-cross carryforward. When a cross is detected on a bar
    # that's then suppressed by the MAX_BAR_AGE_SECONDS_FOR_EMIT
    # freshness guard, these fields hold the signal so the next fresh
    # bar can consume it. Cleared after the consuming fresh bar runs
    # (whether it actually fired or not). Expires per
    # SchwabV2Config.pending_cross_max_gap_secs.
    pending_path_macd: bool = False
    pending_path_vwap: bool = False
    pending_cross_bar_ts_ms: int = 0
    # --- Track 1: ATR-Flip touch entry (P3-B). All ATR-scoped, reset at the
    # 04:00-ET session anchor (the same anchor VWAP uses). These are
    # WRITE-DISJOINT from every field above — the ATR path never reads or
    # mutates prev_macd/prev_signal/prev_close/prev_vwap, the VWAP
    # accumulators, or the pending-cross fields; it only shares the read-only
    # `bars` deque and the `position_qty`/`cooldown_bars_remaining` gates. See
    # docs/schwab-1m-v2-atr-flip-entry-design.md §3.
    atr_session_anchor_ms: int = 0
    atr_hl: Deque[float] = field(default_factory=lambda: deque(maxlen=5))
    atr_prev_bar: OHLCVBar | None = None       # previous SESSION bar (TR needs it)
    atr_wilders: float | None = None           # running Wilders(TR, period)
    atr_tr_seed: list = field(default_factory=list)  # first ≤period session TRs (SMA seed)
    atr_state: str | None = None               # 'long' | 'short' | None
    atr_trail: float | None = None             # current trail level
    atr_prev_trail: float | None = None        # prior bar's trail (touch compares HIGH vs this)
    atr_prev_state: str | None = None          # prior bar's state (touch only while short)
    atr_state_age: int = 0                     # bars since last flip
    atr_fired_in_short_seg: bool = False       # one-entry-per-short-segment guard (offline B `break`)


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
        # `_macd_probe_*` is a cached parse of
        # settings.strategy_schwab_1m_v2_macd_probe_symbols. "*" enables
        # probe for every symbol; an empty/missing setting disables probe
        # entirely. Diagnostic-only — never gates strategy behavior.
        raw_probe = str(
            getattr(self.settings, "strategy_schwab_1m_v2_macd_probe_symbols", "") or ""
        ).strip()
        self._macd_probe_all = raw_probe == "*"
        self._macd_probe_symbols: set[str] = (
            set()
            if self._macd_probe_all or not raw_probe
            else {s.strip().upper() for s in raw_probe.split(",") if s.strip()}
        )
        # --- Track 1: ATR-Flip config (read from settings; see settings.py). The
        # indicator state is computed every bar to stay warm; emit is gated by the
        # enable flag, so OFF = dormant (state computed, nothing emitted).
        self._atr_enabled = bool(
            getattr(self.settings, "strategy_schwab_1m_v2_atr_flip_enabled", False)
        )
        self._atr_variant = str(
            getattr(self.settings, "strategy_schwab_1m_v2_atr_flip_variant", "B") or "B"
        ).strip().upper()
        self._atr_qty = max(
            1, int(getattr(self.settings, "strategy_schwab_1m_v2_atr_flip_quantity", 10))
        )
        self._atr_vol_floor = int(
            getattr(self.settings, "strategy_schwab_1m_v2_atr_flip_vol_floor", 5000)
        )
        self._atr_period = max(
            1, int(getattr(self.settings, "strategy_schwab_1m_v2_atr_flip_period", 5))
        )
        self._atr_factor = float(
            getattr(self.settings, "strategy_schwab_1m_v2_atr_flip_factor", 3.5)
        )
        raw_atr_probe = str(
            getattr(self.settings, "strategy_schwab_1m_v2_atr_flip_probe_symbols", "") or ""
        ).strip()
        self._atr_probe_all = raw_atr_probe == "*"
        self._atr_probe_symbols: set[str] = (
            set()
            if self._atr_probe_all or not raw_atr_probe
            else {s.strip().upper() for s in raw_atr_probe.split(",") if s.strip()}
        )

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
        # Safety drop: `state.bars` is an append-only deque (no sorted
        # insert). An out-of-order bar would corrupt every indicator
        # that iterates the deque (MACD EMAs, VWAP, rel_vol). The bot
        # service's buffer-and-replay path in `_handle_bar_from_rest`
        # is the primary ordering guarantee; this drop catches any
        # regression that bypasses it.
        if state.bars and ohlcv.timestamp_ms < state.bars[-1].timestamp_ms:
            logger.warning(
                "schwab_1m_v2 dropping out-of-order bar for %s "
                "(incoming ts=%d, latest deque ts=%d)",
                symbol,
                ohlcv.timestamp_ms,
                state.bars[-1].timestamp_ms,
            )
            return None
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

    # ------------------------------------------------------ ATR Flip (Track 1)

    def _update_atr_state(self, state: SymbolState, cur: OHLCVBar) -> dict | None:
        """Advance the ATR-trailing-stop flip state by one bar; return this
        bar's signal {touch, touch_price, flip, trail, loss, state, state_age}
        or None until the trail is defined.

        Incremental port of analysis/atr_flip.py::compute_atr_trail (modified
        true range, Wilders(period), ATRFactor). State resets at the 04:00-ET
        session anchor — the SAME anchor VWAP uses — so the live series matches
        the validated session-sliced backtest. Pinned to that offline function
        as an oracle by the determinism test. Only mutates atr_* fields (write-
        disjoint from Paths 1/2; see docs/schwab-1m-v2-atr-flip-entry-design.md).
        """
        period = self._atr_period
        factor = self._atr_factor

        # Session reset (mirror VWAP's anchor roll → reproduces fetch_day's slice).
        anchor = session_start_ts_ms(cur.timestamp_ms)
        if anchor != state.atr_session_anchor_ms:
            state.atr_session_anchor_ms = anchor
            state.atr_hl = deque(maxlen=period)
            state.atr_prev_bar = None
            state.atr_wilders = None
            state.atr_tr_seed = []
            state.atr_state = None
            state.atr_trail = None
            state.atr_prev_trail = None
            state.atr_prev_state = None
            state.atr_state_age = 0
            state.atr_fired_in_short_seg = False

        # --- modified true range (needs prior SESSION bar + SMA(high-low, period)) ---
        hl_cur = cur.high - cur.low
        state.atr_hl.append(hl_cur)
        prev = state.atr_prev_bar
        tr: float | None = None
        if len(state.atr_hl) == period and prev is not None:
            s = sum(state.atr_hl) / period
            hilo = min(hl_cur, 1.5 * s)
            href = (
                (cur.high - prev.close)
                if cur.low <= prev.high
                else (cur.high - prev.close) - 0.5 * (cur.low - prev.high)
            )
            lref = (
                (prev.close - cur.low)
                if cur.high >= prev.low
                else (prev.close - cur.low) - 0.5 * (prev.low - cur.high)
            )
            tr = max(hilo, href, lref)

        # --- Wilders(tr, period) seeded with the SMA of the first `period` valid TRs ---
        if tr is not None:
            if state.atr_wilders is None:
                state.atr_tr_seed.append(tr)
                if len(state.atr_tr_seed) == period:
                    state.atr_wilders = sum(state.atr_tr_seed) / period
            else:
                state.atr_wilders = state.atr_wilders + (tr - state.atr_wilders) / period
        loss = factor * state.atr_wilders if state.atr_wilders is not None else None

        # Roll prev bar AFTER computing TR (next bar's TR needs THIS bar as prev).
        state.atr_prev_bar = cur

        if loss is None:
            return None  # trail not defined yet

        # --- touch detection (variant B): while the PRIOR bar was short, the
        # first bar whose HIGH reaches the prior resting trail. Bar-close
        # approximation of the intrabar stop-buy; entry = that trail level.
        # One entry per short segment (offline B `break`).
        touch = False
        touch_price: float | None = None
        if (
            state.atr_prev_state == "short"
            and state.atr_prev_trail is not None
            and cur.high >= state.atr_prev_trail
            and not state.atr_fired_in_short_seg
        ):
            touch = True
            touch_price = state.atr_prev_trail
            state.atr_fired_in_short_seg = True

        # --- flip state machine (close vs PRIOR trail) ---
        flip: str | None = None
        close = cur.close
        if state.atr_state is None:
            state.atr_state, state.atr_trail, state.atr_state_age = "long", close - loss, 0
        else:
            state.atr_state_age += 1
            if state.atr_state == "long":
                if close > state.atr_trail:
                    state.atr_trail = max(state.atr_trail, close - loss)
                else:
                    state.atr_state, state.atr_trail = "short", close + loss
                    flip, state.atr_state_age = "SELL", 0
                    state.atr_fired_in_short_seg = False  # a fresh short segment opens
            else:  # short
                if close < state.atr_trail:
                    state.atr_trail = min(state.atr_trail, close + loss)
                else:
                    state.atr_state, state.atr_trail = "long", close - loss
                    flip, state.atr_state_age = "BUY", 0

        # Roll prev trail/state to THIS bar's values for the next bar's touch test.
        state.atr_prev_trail = state.atr_trail
        state.atr_prev_state = state.atr_state

        if self._atr_probe_all or state.symbol in self._atr_probe_symbols:
            logger.info(
                "[V2-ATR-PROBE] sym=%s ts_ms=%d close=%.6f high=%.6f low=%.6f "
                "tr=%s loss=%.6f trail=%.6f state=%s age=%d touch=%s flip=%s "
                "vol=%d fired_seg=%s",
                state.symbol, cur.timestamp_ms, cur.close, cur.high, cur.low,
                "none" if tr is None else f"{tr:.6f}", loss, state.atr_trail,
                state.atr_state, state.atr_state_age, str(touch).lower(),
                flip or "none", int(cur.volume),
                str(state.atr_fired_in_short_seg).lower(),
            )

        return {
            "touch": touch,
            "touch_price": touch_price,
            "flip": flip,
            "trail": state.atr_trail,
            "loss": loss,
            "state": state.atr_state,
            "state_age": state.atr_state_age,
        }

    def _maybe_atr_emit(
        self,
        state: SymbolState,
        cur: OHLCVBar,
        atr_signal: dict | None,
        bar_is_fresh: bool,
    ) -> TradeIntentDraft | None:
        """Emit an "ATR Flip" open intent if the flag is on and this bar produced
        an entry signal for the configured variant. Reached only when flat + no
        cooldown + Paths 1/2 didn't fire (shared gates). The liquidity floor is
        the ONLY filter (operator's "just the script"). OFF flag = dormant: the
        indicator state above is still computed every bar, but nothing emits."""
        if not self._atr_enabled or atr_signal is None:
            return None
        if not bar_is_fresh:
            return None  # never fire on a replayed historical touch
        if cur.volume <= self._atr_vol_floor:
            return None  # the only filter: bar volume > floor

        # Variant B (default, validated): intrabar touch of the resting trail;
        # entry at that trail level (== the backtest's `tp`, by construction, so
        # live sim and replay agree). Variant A (default-off, for live A/B
        # comparison): confirmed BUY flip; entry at the flip bar close.
        if self._atr_variant == "A":
            if atr_signal.get("flip") != "BUY":
                return None
            entry = float(cur.close)
        else:  # "B"
            if not atr_signal.get("touch") or atr_signal.get("touch_price") is None:
                return None
            entry = float(atr_signal["touch_price"])

        state.last_entry_price = entry
        trail = atr_signal.get("trail")
        loss = atr_signal.get("loss")
        return TradeIntentDraft(
            symbol=state.symbol,
            side="buy",
            intent_type="open",
            quantity=Decimal(str(self._atr_qty)),
            reason=f"schwab_1m_v2 ATR Flip {self._atr_variant}",
            metadata={
                "path": "ATR Flip",
                "entry_price": f"{entry:.4f}",
                # reference_price = the fill level (SimulatedBrokerAdapter fills
                # at it). Variant B fills at the touched trail (idealized, no
                # slippage) — identical to the backtest entry by construction;
                # variant A fills at the flip bar close. See design §4.
                "reference_price": f"{entry:.4f}",
                "atr_variant": self._atr_variant,
                "atr_trail": f"{trail:.4f}" if trail is not None else "",
                "atr_loss": f"{loss:.6f}" if loss is not None else "",
                "atr_state": str(atr_signal.get("state")),
                "atr_state_age": str(atr_signal.get("state_age")),
                "bar_high": f"{cur.high:.4f}",
                "volume": str(cur.volume),
                "source": "schwab_1m_v2",
                "strategy_version": STRATEGY_VERSION,
                "bar_time_ms": str(cur.timestamp_ms),
            },
        )

    # ------------------------------------------------------------- evaluate

    def _evaluate_completed_bar(
        self, state: SymbolState, *, is_new_bar: bool
    ) -> TradeIntentDraft | None:
        # Only re-evaluate when a NEW minute lands; bar revisions of the
        # same timestamp are a noop for signaling (the cross-detection
        # state would double-fire otherwise).
        if not is_new_bar:
            return None

        # Track 1: update the ATR-Flip indicator state on EVERY new bar, BEFORE
        # the MACD-warmup guard below. The ATR indicator has its own short
        # warmup (~period+1 session bars) and must stay continuous + warm
        # independent of the 135-bar MACD settling allowance, so a Monday
        # flag-flip fires on a correct series immediately. This only mutates the
        # write-disjoint atr_* fields (no effect on Paths 1/2); the resulting
        # touch/flip signal is consumed in the emit region below, and only when
        # the enable flag is on. Returns the per-bar signal (or None).
        atr_signal = self._update_atr_state(state, state.bars[-1])

        # Bootstrap: need enough history for the slowest indicator chain
        # PLUS the settling allowance — see `macd_warmup_settling_bars`
        # docstring on SchwabV2Config for the empirical motivation. The
        # raw MACD math is well-defined at 35 bars but seed bias makes
        # the values unreliable until ≈135 bars, so we don't evaluate or
        # update the prev_* memo until the EMAs have converged.
        min_bars = max(
            self.cfg.macd_slow_length
            + self.cfg.macd_signal_length
            + max(0, int(self.cfg.macd_warmup_settling_bars)),
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

        # Path 1 — MACD Cross. (NATIVE = detected on THIS bar's data.)
        path_macd_native = (
            macd_cross_above
            and macd_increasing
            and vwap_filter_path1
            and base_filters
        )
        # Path 2 — VWAP Breakout. (NATIVE = detected on THIS bar's data.)
        path_vwap_native = (
            vwap_cross_above
            and macd_above_signal
            and macd_increasing
            and base_filters
        )

        # Freshness guard precomputed here so the probe log can include it.
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        bar_age_secs = (now_ms - cur.timestamp_ms) / 1000.0
        bar_is_fresh = bar_age_secs <= MAX_BAR_AGE_SECONDS_FOR_EMIT

        # Diagnostic probe — emit one INFO line per evaluated bar for
        # symbols in the probe set. Captures every input needed to
        # cross-check the bot's MACD chain against TOS for the same minute.
        # Logged BEFORE the memo update so `prev_*` reflects the state
        # cross-detection actually consumed for this bar.
        if self._macd_probe_all or state.symbol in self._macd_probe_symbols:
            rel_vol_ratio = cur.volume / avg_vol if avg_vol > 0 else 0.0
            logger.info(
                "[V2-MACD-PROBE] sym=%s ts_ms=%d close=%.6f "
                "macd=%.6f sig=%.6f hist=%.6f hist_pct=%.4f "
                "prev_macd=%s prev_sig=%s prev_close=%s prev_vwap=%s "
                "vwap=%.6f ema%d=%.6f stoch%d=%.4f "
                "vol=%d avg_vol_%d=%.4f rel_vol_x=%.4f "
                "cross_macd_above=%s cross_vwap_above=%s "
                "macd_above_sig=%s macd_inc=%s green=%s "
                "n_bars=%d age_s=%.2f pos_qty=%d cooldown=%d",
                state.symbol,
                cur.timestamp_ms,
                cur.close,
                macd_line,
                signal_line,
                histogram,
                hist_pct,
                "none" if state.prev_macd is None else f"{state.prev_macd:.6f}",
                "none" if state.prev_signal is None else f"{state.prev_signal:.6f}",
                "none" if state.prev_close is None else f"{state.prev_close:.6f}",
                "none" if state.prev_vwap is None else f"{state.prev_vwap:.6f}",
                vwap,
                self.cfg.ema_trend_length,
                ema_trend,
                self.cfg.stoch_length,
                stoch_k,
                int(cur.volume),
                self.cfg.rel_vol_length,
                avg_vol,
                rel_vol_ratio,
                str(macd_cross_above).lower(),
                str(vwap_cross_above).lower(),
                str(macd_above_signal).lower(),
                str(macd_increasing).lower(),
                str(cur.close > cur.open).lower(),
                len(state.bars),
                bar_age_secs,
                state.position_qty,
                state.cooldown_bars_remaining,
            )

        # C2: if a NATIVE cross is detected on this bar AND the bar is
        # stale (would be suppressed by the freshness guard below),
        # stash it as a pending cross so the next FRESH bar can still
        # consume it. The memo updates either way (indicator continuity
        # requires it), but the SIGNAL is preserved across the
        # suppressed bar via pending_*.
        if not bar_is_fresh:
            if path_macd_native:
                if not state.pending_path_macd:
                    logger.info(
                        "[V2-PENDING-CROSS-SET] sym=%s path=macd "
                        "cross_bar_ts_ms=%d bar_age_s=%.1f",
                        state.symbol,
                        cur.timestamp_ms,
                        bar_age_secs,
                    )
                state.pending_path_macd = True
                state.pending_cross_bar_ts_ms = cur.timestamp_ms
            if path_vwap_native:
                if not state.pending_path_vwap:
                    logger.info(
                        "[V2-PENDING-CROSS-SET] sym=%s path=vwap "
                        "cross_bar_ts_ms=%d bar_age_s=%.1f",
                        state.symbol,
                        cur.timestamp_ms,
                        bar_age_secs,
                    )
                state.pending_path_vwap = True
                state.pending_cross_bar_ts_ms = cur.timestamp_ms

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
        if not bar_is_fresh:
            return None

        # Bar IS fresh. Resolve effective paths = native ∪ valid-pending.
        # C2: a pending cross can be consumed by THIS fresh bar if the
        # gap from the original cross-bar is within the configured cap
        # AND the cross condition still holds on this bar (macd still
        # above signal — not reversed) AND the filters still pass.
        path_macd = path_macd_native
        path_vwap = path_vwap_native
        if state.pending_path_macd or state.pending_path_vwap:
            pending_gap_secs = (
                cur.timestamp_ms - state.pending_cross_bar_ts_ms
            ) / 1000.0
            max_gap = float(self.cfg.pending_cross_max_gap_secs)
            if pending_gap_secs > max_gap:
                logger.info(
                    "[V2-PENDING-CROSS-EXPIRED] sym=%s gap_s=%.1f "
                    "cap_s=%.1f had_pending_macd=%s had_pending_vwap=%s",
                    state.symbol,
                    pending_gap_secs,
                    max_gap,
                    str(state.pending_path_macd).lower(),
                    str(state.pending_path_vwap).lower(),
                )
            else:
                # Pending still within window — promote to effective path
                # if the validating conditions hold on the fresh bar.
                if (
                    state.pending_path_macd
                    and macd_above_signal
                    and vwap_filter_path1
                    and base_filters
                ):
                    path_macd = True
                    logger.info(
                        "[V2-PENDING-CROSS-CONSUMED] sym=%s path=macd "
                        "gap_s=%.1f cross_bar_ts_ms=%d fresh_bar_ts_ms=%d",
                        state.symbol,
                        pending_gap_secs,
                        state.pending_cross_bar_ts_ms,
                        cur.timestamp_ms,
                    )
                if (
                    state.pending_path_vwap
                    and macd_above_signal
                    and base_filters
                ):
                    path_vwap = True
                    logger.info(
                        "[V2-PENDING-CROSS-CONSUMED] sym=%s path=vwap "
                        "gap_s=%.1f cross_bar_ts_ms=%d fresh_bar_ts_ms=%d",
                        state.symbol,
                        pending_gap_secs,
                        state.pending_cross_bar_ts_ms,
                        cur.timestamp_ms,
                    )
            # Always clear pending after the consuming fresh bar has
            # evaluated — whether we promoted, expired, or the cross
            # condition no longer held. The cross's window of opportunity
            # is one fresh bar.
            state.pending_path_macd = False
            state.pending_path_vwap = False
            state.pending_cross_bar_ts_ms = 0

        # State-machine gate (entry side): flat + no cooldown + raw entry.
        if state.position_qty > 0:
            return None
        if state.cooldown_bars_remaining > 0:
            return None
        if not (path_macd or path_vwap):
            # Paths 1/2 did not fire. Consider the ATR-Flip path (precedence
            # MACD > VWAP > ATR Flip — ATR is only reached when neither fired,
            # so it can never alter a Paths-1/2 outcome). The flat + no-cooldown
            # gates above are SHARED, so an ATR entry respects them identically.
            return self._maybe_atr_emit(state, cur, atr_signal, bar_is_fresh)

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
                # reference_price is consumed by the execution sink (the
                # SimulatedBrokerAdapter fills at it; without it the order is
                # rejected "missing reference_price"). v2 uses the signal bar
                # CLOSE (== entry_price): deterministic and reproducible from
                # stored bars, so live sim fills and the replay study agree by
                # construction. Idealized by design (exact signal price, no
                # slippage) — realism comes from the future tick-based slippage
                # sink, not this field. This is execution metadata, NOT a gate;
                # the entry decision above is unchanged.
                "reference_price": f"{cur.close:.4f}",
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
