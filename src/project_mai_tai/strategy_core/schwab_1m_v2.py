"""Dedicated strategy module for the isolated `schwab_1m_v2` bot.

Owns bar storage, indicator math, strategy decision, and intent emission for
the v2 bot. NO imports from `schwab_native_30s.py`, `bar_builder.py`,
`indicators.py`, `entry.py`, `exit.py`, or `strategy_engine_app.py`.

Inputs (from `market_data/schwab_v2_rest_client.py`):
- ChartBar: closed 1-minute OHLCV candles, REST-polled
- Quote: bid/ask/last + cumulative volume, REST-polled, used for intrabar
  freshness + entry/exit price decisions

Output (to OMS via Redis `strategy-intents` stream):
- TradeIntentEvent in the existing shape (see `events.py::TradeIntentPayload`)

Strategy body is a PLACEHOLDER pending operator's spec. The scaffolding
records bars + quotes, builds indicator state, but emits zero intents.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Deque, Iterable
from uuid import UUID

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
    bars: Deque[OHLCVBar] = field(default_factory=lambda: deque(maxlen=200))
    last_quote: Quote | None = None
    position_qty: int = 0
    last_entry_price: float | None = None


class V2Indicators:
    """Inline indicator math. Kept simple — extend in-place as the strategy
    spec arrives.
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
    def macd(
        closes: Iterable[float], fast: int = 12, slow: int = 26, signal: int = 9
    ) -> tuple[float, float, float] | None:
        seq = list(closes)
        if len(seq) < slow + signal:
            return None
        ema_fast = V2Indicators.ema(seq, fast)
        ema_slow = V2Indicators.ema(seq, slow)
        if ema_fast is None or ema_slow is None:
            return None
        macd_line = ema_fast - ema_slow
        # signal line: EMA of MACD over `signal` periods. For scaffolding
        # we approximate using a single most-recent MACD value (the strategy
        # spec will likely refine this).
        signal_line = macd_line  # PLACEHOLDER until spec arrives
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    @staticmethod
    def rsi(closes: Iterable[float], period: int = 14) -> float | None:
        seq = list(closes)
        if len(seq) < period + 1:
            return None
        gains = 0.0
        losses = 0.0
        for prev, curr in zip(seq[-period - 1 : -1], seq[-period:]):
            delta = curr - prev
            if delta > 0:
                gains += delta
            else:
                losses -= delta
        if losses == 0:
            return 100.0
        rs = (gains / period) / (losses / period)
        return 100.0 - (100.0 / (1.0 + rs))


class SchwabV2Strategy:
    """Bar/quote-driven strategy for schwab_1m_v2.

    Strategy decision body is a placeholder. Inputs and intent shape are
    locked in so the strategy can be filled in via a follow-up edit to ONLY
    this file.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._symbol_states: dict[str, SymbolState] = {}

    def watchlist_state(self, symbol: str) -> SymbolState:
        state = self._symbol_states.get(symbol)
        if state is None:
            state = SymbolState(symbol=symbol)
            self._symbol_states[symbol] = state
        return state

    def drop_symbol(self, symbol: str) -> None:
        self._symbol_states.pop(symbol, None)

    def on_bar(self, symbol: str, bar: ChartBar) -> "TradeIntentDraft | None":
        state = self.watchlist_state(symbol)
        ohlcv = OHLCVBar(
            timestamp_ms=bar.timestamp_ms,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        if state.bars and state.bars[-1].timestamp_ms == ohlcv.timestamp_ms:
            state.bars[-1] = ohlcv
        else:
            state.bars.append(ohlcv)
        return self._evaluate_completed_bar(state)

    def on_quote(self, symbol: str, quote: Quote) -> "TradeIntentDraft | None":
        state = self.watchlist_state(symbol)
        state.last_quote = quote
        return self._evaluate_intrabar(state, quote)

    # ----- strategy decision body (PLACEHOLDER) ----------------------------

    def _evaluate_completed_bar(
        self, state: SymbolState
    ) -> "TradeIntentDraft | None":
        # PLACEHOLDER — operator's strategy spec pending (2026-05-22). When
        # the spec arrives, implement entry/exit logic using `state.bars`
        # and V2Indicators. Return TradeIntentDraft to emit; return None to
        # take no action.
        return None

    def _evaluate_intrabar(
        self, state: SymbolState, quote: Quote
    ) -> "TradeIntentDraft | None":
        # PLACEHOLDER — operator's intrabar spec pending. Typical use: tight
        # stop trigger, take-profit at quote level, scale-out at price target.
        return None


@dataclass
class TradeIntentDraft:
    """Shape returned by the strategy to the engine; the emitter converts to
    a TradeIntentEvent + DB row."""

    symbol: str
    side: str  # "buy" | "sell"
    intent_type: str  # "open" | "scale" | "close" | "cancel"
    quantity: Decimal
    reason: str
    metadata: dict[str, str] = field(default_factory=dict)


class SchwabV2IntentEmitter:
    """Writes intents to Redis `strategy-intents` stream in the shape OMS
    already expects (`TradeIntentEvent` from `events.py`).

    NOTE: in the existing strategy-engine flow, `trade_intents` table writes
    are handled by OMS on stream consumption. We rely on that. If the v2 bot
    needs to persist intents to the DB itself for any reason (e.g., OMS down,
    forensic trail), that's a follow-up edit to ONLY this class.
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
            "schwab_v2 emitted intent symbol=%s side=%s type=%s qty=%s reason=%s",
            draft.symbol,
            draft.side,
            draft.intent_type,
            draft.quantity,
            draft.reason,
        )
        return event.event_id


def utc_now_isoformat() -> str:
    return datetime.now(UTC).isoformat()


# Re-export so the engine module has a single import surface.
__all__ = [
    "STRATEGY_CODE",
    "SERVICE_NAME",
    "OHLCVBar",
    "SymbolState",
    "V2Indicators",
    "SchwabV2Strategy",
    "TradeIntentDraft",
    "SchwabV2IntentEmitter",
    "utc_now_isoformat",
]
