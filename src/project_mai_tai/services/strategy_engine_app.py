from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    DashboardSnapshot,
    ScannerBlacklistEntry,
    Strategy,
    StrategyBarHistory,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    HistoricalBarsEvent,
    LiveBarEvent,
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    MarketSnapshotPayload,
    OrderEventEvent,
    QuoteTickEvent,
    SnapshotBatchEvent,
    StrategyBotStatePayload,
    StrategyStateSnapshotEvent,
    StrategyStateSnapshotPayload,
    TradeIntentEvent,
    TradeIntentPayload,
    TradeTickEvent,
    stream_name,
)
from project_mai_tai.log import configure_logging
from project_mai_tai.runtime_registry import strategy_registration_map
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.market_data.models import QuoteTickRecord, TradeTickRecord
from project_mai_tai.market_data.massive_indicator_provider import MassiveIndicatorProvider
from project_mai_tai.market_data.schwab_tick_archive import SchwabTickArchive
from project_mai_tai.market_data.schwab_streamer import SchwabStreamerClient
from project_mai_tai.market_data.taapi_indicator_provider import TaapiIndicatorProvider
from project_mai_tai.strategy_core import (
    CatalystAiConfig,
    CatalystAiEvaluator,
    CatalystConfig,
    CatalystEngine,
    DaySnapshot,
    EntryEngine,
    ExitEngine,
    FivePillarsConfig,
    IndicatorConfig,
    IndicatorEngine,
    LastTrade,
    MarketSnapshot,
    MinuteSnapshot,
    MomentumAlertConfig,
    MomentumAlertEngine,
    MomentumConfirmedConfig,
    MomentumConfirmedScanner,
    OHLCVBar,
    PositionTracker,
    QuoteSnapshot,
    ReferenceData,
    RunnerStrategyRuntime,
    SchwabNativeBarBuilderManager,
    SchwabNativeEntryEngine,
    SchwabNativeIndicatorEngine,
    TopGainersConfig,
    TopGainersTracker,
    TradingConfig,
    apply_five_pillars,
)
from project_mai_tai.strategy_core.time_utils import now_eastern
from project_mai_tai.strategy_core.time_utils import session_day_eastern_str
from project_mai_tai.strategy_core.bar_builder import BarBuilderManager

logger = logging.getLogger(__name__)

SERVICE_NAME = "strategy-engine"
EASTERN_TZ = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _format_limit_price(value: float | str | Decimal | None) -> str | None:
    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except Exception:
        return None


def order_routing_metadata(*, price: str, side: str, now: datetime | None = None) -> dict[str, str]:
    current = (now or utcnow()).astimezone(EASTERN_TZ)
    regular_open = current.replace(hour=9, minute=30, second=0, microsecond=0)
    regular_close = current.replace(hour=16, minute=0, second=0, microsecond=0)
    if regular_open <= current < regular_close:
        return {}
    return {
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": "true",
        "limit_price": price,
        "reference_price": price,
        "price_source": "ask" if side == "buy" else "bid",
    }


def current_scanner_session_start_utc(now: datetime | None = None) -> datetime:
    current = now or utcnow()
    current_et = current.astimezone(EASTERN_TZ)
    session_start_et = current_et.replace(hour=4, minute=0, second=0, microsecond=0)
    if current_et < session_start_et:
        session_start_et -= timedelta(days=1)
    return session_start_et.astimezone(UTC)


@dataclass(frozen=True)
class StrategyDefinition:
    code: str
    display_name: str
    account_name: str
    interval_secs: int
    trading_config: TradingConfig
    indicator_config: IndicatorConfig


class StrategyBotRuntime:
    def __init__(
        self,
        definition: StrategyDefinition,
        *,
        now_provider: Callable[[], datetime] | None = None,
        session_factory: sessionmaker[Session] | None = None,
        use_live_aggregate_bars: bool = False,
        live_aggregate_fallback_enabled: bool = True,
        live_aggregate_stale_after_seconds: int = 3,
        indicator_overlay_provider: MassiveIndicatorProvider | TaapiIndicatorProvider | None = None,
        builder_manager: BarBuilderManager | SchwabNativeBarBuilderManager | None = None,
        indicator_engine: IndicatorEngine | SchwabNativeIndicatorEngine | None = None,
        entry_engine: EntryEngine | SchwabNativeEntryEngine | None = None,
    ):
        self.definition = definition
        self.now_provider = now_provider or now_eastern
        self.builder_manager = builder_manager or BarBuilderManager(
            interval_secs=definition.interval_secs,
            time_provider=self._builder_time_provider,
        )
        self.indicator_engine = indicator_engine or IndicatorEngine(definition.indicator_config)
        self.entry_engine = entry_engine or EntryEngine(
            definition.trading_config,
            name=definition.display_name,
            now_provider=self.now_provider,
        )
        self.exit_engine = ExitEngine(definition.trading_config)
        self.positions = PositionTracker(
            definition.trading_config,
            positions_file=self._positions_file_for_strategy(definition.code),
            closed_file_prefix=self._closed_trade_prefix_for_strategy(definition.code),
        )
        self.positions.load_closed_trades()
        self._active_day = session_day_eastern_str(self.now_provider())
        self.watchlist: set[str] = set()
        self.last_indicators: dict[str, dict[str, object]] = {}
        self.latest_quotes: dict[str, dict[str, float]] = {}
        self.pending_open_symbols: set[str] = set()
        self.pending_close_symbols: set[str] = set()
        self.pending_scale_levels: set[tuple[str, str]] = set()
        self.exit_retry_blocked_until: dict[str, datetime] = {}
        self.scale_retry_blocked_until: dict[tuple[str, str], datetime] = {}
        self._applied_fill_quantity_by_order: dict[str, Decimal] = {}
        self.recent_decisions: list[dict[str, str]] = []
        self.session_factory = session_factory
        self.use_live_aggregate_bars = use_live_aggregate_bars
        self.live_aggregate_fallback_enabled = live_aggregate_fallback_enabled
        self.live_aggregate_stale_after_seconds = max(0, int(live_aggregate_stale_after_seconds))
        self.indicator_overlay_provider = indicator_overlay_provider
        self._last_live_bar_received_at: dict[str, datetime] = {}

    @staticmethod
    def _positions_file_for_strategy(strategy_code: str) -> str:
        return f"data/cache/positions_{strategy_code}.json"

    @staticmethod
    def _closed_trade_prefix_for_strategy(strategy_code: str) -> str:
        if strategy_code == "macd_30s":
            return "macdbot"
        return strategy_code

    def set_watchlist(self, symbols: Iterable[str]) -> None:
        self.watchlist = {str(symbol).upper() for symbol in symbols if str(symbol).strip()}
        self._prune_runtime_state()

    def restore_position(
        self,
        *,
        symbol: str,
        quantity: int,
        average_price: float,
        path: str = "",
    ) -> None:
        normalized = symbol.upper()
        if quantity <= 0 or average_price <= 0:
            return
        self.positions.open_position(normalized, average_price, quantity=quantity, path=path)

    def restore_pending_open(self, symbol: str) -> None:
        if symbol:
            self.pending_open_symbols.add(symbol.upper())

    def restore_pending_close(self, symbol: str) -> None:
        if symbol:
            self.pending_close_symbols.add(symbol.upper())

    def restore_pending_scale(self, symbol: str, level: str) -> None:
        if symbol and level:
            self.pending_scale_levels.add((symbol.upper(), level))

    def _builder_time_provider(self) -> float:
        current = self.now_provider()
        if current.tzinfo is None:
            current = current.replace(tzinfo=EASTERN_TZ)
        return current.timestamp()

    def update_market_snapshots(self, snapshots: Sequence[MarketSnapshot]) -> None:
        for snapshot in snapshots:
            if snapshot.last_quote is None:
                continue
            quote: dict[str, float] = {}
            if snapshot.last_quote.bid_price is not None and snapshot.last_quote.bid_price > 0:
                quote["bid"] = float(snapshot.last_quote.bid_price)
            if snapshot.last_quote.ask_price is not None and snapshot.last_quote.ask_price > 0:
                quote["ask"] = float(snapshot.last_quote.ask_price)
            if quote:
                self.latest_quotes[snapshot.ticker.upper()] = quote

    def handle_quote_tick(
        self,
        symbol: str,
        *,
        bid_price: float | None,
        ask_price: float | None,
    ) -> None:
        quote: dict[str, float] = {}
        if bid_price is not None and bid_price > 0:
            quote["bid"] = float(bid_price)
        if ask_price is not None and ask_price > 0:
            quote["ask"] = float(ask_price)
        if quote:
            self.latest_quotes[symbol.upper()] = quote

    def seed_bars(self, symbol: str, bars: Sequence[dict[str, float | int]]) -> None:
        builder = self.builder_manager.get_or_create(symbol)
        builder.reset()

        hydrated = [
            OHLCVBar(
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=int(bar["volume"]),
                timestamp=float(bar["timestamp"]),
                trade_count=int(bar.get("trade_count", 1)),
            )
            for bar in bars
        ]
        if not hydrated:
            return

        builder.bars = hydrated[:-1][-builder.max_bars :]
        builder._bar_count = len(builder.bars)
        builder._current_bar = hydrated[-1]
        builder._current_bar_start = hydrated[-1].timestamp

        historical_indicators: list[dict[str, float | bool]] = []
        closed_bars = hydrated[:-1]
        for index in range(len(closed_bars)):
            indicators = self.indicator_engine.calculate(closed_bars[: index + 1])
            if indicators is None:
                continue
            historical_indicators.append(indicators)
        self.entry_engine.seed_recent_bars(symbol, historical_indicators)

    def handle_trade_tick(
        self,
        symbol: str,
        price: float,
        size: int,
        timestamp_ns: int | None = None,
        cumulative_volume: int | None = None,
    ) -> list[TradeIntentEvent]:
        self._roll_day_if_needed()
        intents: list[TradeIntentEvent] = []

        position = self.positions.get_position(symbol)
        if position is not None:
            position.update_price(price)
            hard_stop = self.exit_engine.check_hard_stop(position, price)
            if (
                hard_stop
                and symbol not in self.pending_close_symbols
                and not self._is_exit_retry_blocked(symbol)
            ):
                intents.append(self._emit_close_intent(hard_stop))
            elif symbol not in self.pending_close_symbols:
                intrabar_exit = self.exit_engine.check_intrabar_exit(position)
                if intrabar_exit is not None:
                    if intrabar_exit["action"] == "SCALE":
                        level = str(intrabar_exit["level"])
                        if (
                            (symbol, level) not in self.pending_scale_levels
                            and not self._is_scale_retry_blocked(symbol, level)
                        ):
                            intents.append(self._emit_scale_intent(intrabar_exit))
                    elif not self._is_exit_retry_blocked(symbol):
                        intents.append(self._emit_close_intent(intrabar_exit))

        if symbol not in self.watchlist and position is None:
            return intents

        if self.use_live_aggregate_bars and not self._should_fallback_to_trade_ticks(symbol):
            return intents

        completed_bars = self.builder_manager.on_trade(
            symbol,
            price,
            size,
            timestamp_ns or 0,
            cumulative_volume,
        )
        for _bar in completed_bars:
            intents.extend(self._evaluate_completed_bar(symbol))

        return intents

    def handle_live_bar(
        self,
        *,
        symbol: str,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: int,
        timestamp: float,
        trade_count: int = 1,
    ) -> list[TradeIntentEvent]:
        self._roll_day_if_needed()
        intents: list[TradeIntentEvent] = []

        position = self.positions.get_position(symbol)
        if position is not None:
            position.update_price(close_price)

        if symbol not in self.watchlist and position is None:
            return intents

        if not self.use_live_aggregate_bars:
            return intents

        self._last_live_bar_received_at[symbol] = self._normalize_now(self.now_provider())

        completed_bars = self.builder_manager.on_bar(
            symbol,
            OHLCVBar(
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                timestamp=timestamp,
                trade_count=trade_count,
            ),
        )
        for _bar in completed_bars:
            intents.extend(self._evaluate_completed_bar(symbol))

        return intents

    def _should_fallback_to_trade_ticks(self, symbol: str) -> bool:
        if not self.live_aggregate_fallback_enabled:
            return False
        last_live_bar_at = self._last_live_bar_received_at.get(symbol)
        if last_live_bar_at is None:
            return True
        now = self._normalize_now(self.now_provider())
        return (now - last_live_bar_at).total_seconds() > self.live_aggregate_stale_after_seconds

    @staticmethod
    def _normalize_now(current: datetime) -> datetime:
        if current.tzinfo is None:
            return current.replace(tzinfo=EASTERN_TZ)
        return current

    def flush_completed_bars(self) -> tuple[list[TradeIntentEvent], int]:
        self._roll_day_if_needed()
        intents: list[TradeIntentEvent] = []
        completed = self.builder_manager.check_all_bar_closes()
        for symbol, _bar in completed:
            intents.extend(self._evaluate_completed_bar(symbol))
        return intents, len(completed)

    def apply_execution_fill(
        self,
        *,
        client_order_id: str,
        symbol: str,
        intent_type: str,
        status: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        level: str | None = None,
        path: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._roll_day_if_needed()
        incremental_quantity = self._incremental_fill_quantity(client_order_id, quantity)
        if incremental_quantity <= 0:
            return

        qty = int(incremental_quantity)
        fill_price = float(price)
        position = self.positions.get_position(symbol)

        if intent_type == "open" and side == "buy":
            self.pending_open_symbols.discard(symbol)
            if position is None:
                self.positions.open_position(symbol, fill_price, quantity=qty, path=path or "")
                return

            total_qty = position.quantity + qty
            if total_qty <= 0:
                return

            weighted_cost = position.entry_price * position.quantity + fill_price * qty
            position.entry_price = weighted_cost / total_qty
            position.quantity = total_qty
            position.original_quantity += qty
            position.update_price(fill_price)
            return

        if intent_type == "close" and side == "sell":
            if position is None:
                self.pending_close_symbols.discard(symbol)
                return

            if status == "filled" or qty >= position.quantity:
                self.pending_close_symbols.discard(symbol)
                close_reason = (reason or "").strip() or "OMS_FILL"
                self.positions.close_position(symbol, fill_price, reason=close_reason)
                bar_index = self.builder_manager.get_or_create(symbol).get_bar_count()
                self.entry_engine.record_exit(symbol, bar_index)
                return

            position.scale_pnl += (fill_price - position.entry_price) * qty
            position.quantity -= qty
            return

        if intent_type == "scale" and side == "sell" and level and position is not None:
            self.pending_scale_levels.discard((symbol, level))
            position.apply_scale(level, qty, fill_price)

    def _incremental_fill_quantity(self, client_order_id: str, cumulative_quantity: Decimal) -> Decimal:
        if not client_order_id:
            return cumulative_quantity
        already_applied = self._applied_fill_quantity_by_order.get(client_order_id, Decimal("0"))
        incremental_quantity = cumulative_quantity - already_applied
        if incremental_quantity > 0:
            self._applied_fill_quantity_by_order[client_order_id] = cumulative_quantity
        return incremental_quantity

    def apply_order_status(
        self,
        *,
        symbol: str,
        intent_type: str,
        status: str,
        level: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._roll_day_if_needed()
        if status not in {"rejected", "cancelled"}:
            return

        normalized_reason = (reason or "").strip().lower()

        if intent_type == "open":
            self.pending_open_symbols.discard(symbol)
            self.entry_engine.cancel_pending(symbol)
            return

        if intent_type == "close":
            self.pending_close_symbols.discard(symbol)
            if self._is_no_position_reason(normalized_reason):
                self.positions.drop_position(symbol)
                bar_index = self.builder_manager.get_or_create(symbol).get_bar_count()
                self.entry_engine.record_exit(symbol, bar_index)
                return
            if (
                "duplicate_exit_in_flight" in normalized_reason
                or "broker quantity already reserved for pending exits" in normalized_reason
            ):
                self.exit_retry_blocked_until[symbol] = utcnow() + timedelta(seconds=2)
                return
            if "rate limit exceeded" in normalized_reason:
                self.exit_retry_blocked_until[symbol] = utcnow() + timedelta(seconds=5)
            return

        if intent_type == "scale" and level:
            self.pending_scale_levels.discard((symbol, level))
            if self._is_no_position_reason(normalized_reason):
                self.positions.drop_position(symbol)
                bar_index = self.builder_manager.get_or_create(symbol).get_bar_count()
                self.entry_engine.record_exit(symbol, bar_index)
                return
            if (
                "duplicate_exit_in_flight" in normalized_reason
                or "broker quantity already reserved for pending exits" in normalized_reason
            ):
                self.scale_retry_blocked_until[(symbol, level)] = utcnow() + timedelta(seconds=2)
                return
            if "rate limit exceeded" in normalized_reason:
                self.scale_retry_blocked_until[(symbol, level)] = utcnow() + timedelta(seconds=5)

    def summary(self) -> dict[str, object]:
        self._roll_day_if_needed()
        return {
            "strategy": self.definition.code,
            "account_name": self.definition.account_name,
            "interval_secs": self.definition.interval_secs,
            "watchlist": sorted(self.watchlist),
            "positions": self.positions.get_all_positions(),
            "pending_open_symbols": sorted(self.pending_open_symbols),
            "pending_close_symbols": sorted(self.pending_close_symbols),
            "pending_scale_levels": sorted(f"{symbol}:{level}" for symbol, level in self.pending_scale_levels),
            "daily_pnl": self.positions.get_daily_pnl(),
            "closed_today": self.positions.get_closed_today(),
            "recent_decisions": list(self.recent_decisions),
            "indicator_snapshots": self._indicator_snapshots(),
        }

    def _roll_day_if_needed(self) -> None:
        current_day = session_day_eastern_str(self.now_provider())
        if current_day == self._active_day:
            return
        self.positions.reset()
        self.positions.load_closed_trades()
        self.entry_engine.reset()
        self.last_indicators.clear()
        self.latest_quotes.clear()
        self.builder_manager.reset()
        self._applied_fill_quantity_by_order.clear()
        self._last_live_bar_received_at.clear()
        self._active_day = current_day

    def has_position(self, ticker: str) -> bool:
        return self.positions.has_position(ticker) or ticker in self.pending_open_symbols

    def active_symbols(self) -> set[str]:
        active = set(self.watchlist)
        active.update(self.pending_open_symbols)
        active.update(self.pending_close_symbols)
        active.update(symbol for symbol, _level in self.pending_scale_levels)
        active.update(position["ticker"] for position in self.positions.get_all_positions())
        return active

    def _evaluate_completed_bar(self, symbol: str) -> list[TradeIntentEvent]:
        builder = self.builder_manager.get_builder(symbol)
        if builder is None:
            return []

        bars = builder.get_bars_as_dicts()
        if not bars:
            return []

        local_indicators = self.indicator_engine.calculate(bars)
        if local_indicators is None:
            return []

        indicators = self._decorate_indicators(symbol, local_indicators)
        self.last_indicators[symbol] = indicators
        intents: list[TradeIntentEvent] = []

        position = self.positions.get_position(symbol)
        if position is not None:
            position.increment_bars()
            probe_signal = None
            decision = None
            if self.definition.trading_config.entry_logic_mode in {"pretrigger_probe", "pretrigger_reclaim"}:
                probe_signal = self.entry_engine.check_entry(symbol, indicators, builder.get_bar_count(), self)
                decision = self._capture_entry_decision(symbol, indicators)
                if probe_signal is not None and probe_signal.get("action") == "SELL":
                    if symbol not in self.pending_close_symbols and not self._is_exit_retry_blocked(symbol):
                        intents.append(self._emit_close_intent(probe_signal))
                    return self._finalize_completed_bar(symbol, indicators, intents, decision=decision)

            exit_signal = self.exit_engine.check_exit(position, indicators)
            if exit_signal:
                if exit_signal["action"] == "SCALE":
                    level = str(exit_signal["level"])
                    if (symbol, level) not in self.pending_scale_levels and not self._is_scale_retry_blocked(symbol, level):
                        intents.append(self._emit_scale_intent(exit_signal))
                elif symbol not in self.pending_close_symbols and not self._is_exit_retry_blocked(symbol):
                    intents.append(self._emit_close_intent(exit_signal))
                return self._finalize_completed_bar(
                    symbol,
                    indicators,
                    intents,
                    decision=decision
                    or self._build_persisted_decision(
                        symbol=symbol,
                        status="position_open",
                        reason="position open",
                        indicators=indicators,
                    ),
                )

            if probe_signal is not None and probe_signal.get("action") == "BUY":
                if symbol not in self.pending_open_symbols:
                    intents.append(self._emit_open_intent(probe_signal))
                return self._finalize_completed_bar(symbol, indicators, intents, decision=decision)
            return self._finalize_completed_bar(
                symbol,
                indicators,
                intents,
                decision=decision
                or self._build_persisted_decision(
                    symbol=symbol,
                    status="position_open",
                    reason="position open",
                    indicators=indicators,
                ),
            )

        if symbol in self.pending_open_symbols:
            return self._finalize_completed_bar(
                symbol,
                indicators,
                [],
                decision=self._build_persisted_decision(
                    symbol=symbol,
                    status="pending_open",
                    reason="awaiting open fill",
                    indicators=indicators,
                ),
            )

        can_open, _reason = self.positions.can_open_position(symbol)
        if not can_open:
            decision = self._record_decision(
                symbol=symbol,
                status="blocked",
                reason=str(_reason),
                indicators=indicators,
            )
            return self._finalize_completed_bar(symbol, indicators, [], decision=decision)

        signal = self.entry_engine.check_entry(symbol, indicators, builder.get_bar_count(), self)
        decision = self._capture_entry_decision(symbol, indicators)
        if signal is None:
            return self._finalize_completed_bar(symbol, indicators, [], decision=decision)

        intents.append(self._emit_open_intent(signal))
        return self._finalize_completed_bar(symbol, indicators, intents, decision=decision)

    def _indicator_snapshots(self) -> list[dict[str, object]]:
        snapshots: list[dict[str, object]] = []
        for symbol, indicators in sorted(self.last_indicators.items()):
            builder = self.builder_manager.get_builder(symbol)
            if builder is None or not builder.bars:
                continue

            last_bar = builder.bars[-1]
            snapshots.append(
                {
                    "symbol": symbol,
                    "interval_secs": self.definition.interval_secs,
                    "bar_count": builder.get_bar_count(),
                    "last_bar_at": datetime.fromtimestamp(last_bar.timestamp, UTC).astimezone(EASTERN_TZ).isoformat(),
                    "close": float(indicators.get("price", 0) or 0),
                    "ema9": float(indicators.get("ema9", 0) or 0),
                    "ema20": float(indicators.get("ema20", 0) or 0),
                    "macd": float(indicators.get("macd", 0) or 0),
                    "signal": float(indicators.get("signal", 0) or 0),
                    "histogram": float(indicators.get("histogram", 0) or 0),
                    "vwap": float(indicators.get("vwap", 0) or 0),
                    "macd_above_signal": bool(indicators.get("macd_above_signal", False)),
                    "price_above_vwap": bool(indicators.get("price_above_vwap", False)),
                    "price_above_ema9": bool(indicators.get("price_above_ema9", False)),
                    "price_above_ema20": bool(indicators.get("price_above_ema20", False)),
                    "provider_source": str(indicators.get("provider_source", "")),
                    "provider_status": str(indicators.get("provider_status", "")),
                    "provider_last_bar_at": str(indicators.get("provider_last_bar_at", "")),
                    "provider_macd": float(indicators.get("provider_macd", 0) or 0),
                    "provider_signal": float(indicators.get("provider_signal", 0) or 0),
                    "provider_histogram": float(indicators.get("provider_histogram", 0) or 0),
                    "provider_ema9": float(indicators.get("provider_ema9", 0) or 0),
                    "provider_ema20": float(indicators.get("provider_ema20", 0) or 0),
                    "provider_stoch_k": float(indicators.get("provider_stoch_k", 0) or 0),
                    "provider_stoch_d": float(indicators.get("provider_stoch_d", 0) or 0),
                    "provider_vwap": float(indicators.get("provider_vwap", 0) or 0),
                    "provider_open": float(indicators.get("provider_open", 0) or 0),
                    "provider_high": float(indicators.get("provider_high", 0) or 0),
                    "provider_low": float(indicators.get("provider_low", 0) or 0),
                    "provider_close": float(indicators.get("provider_close", 0) or 0),
                    "provider_volume": float(indicators.get("provider_volume", 0) or 0),
                    "provider_macd_diff": float(indicators.get("provider_macd_diff", 0) or 0),
                    "provider_signal_diff": float(indicators.get("provider_signal_diff", 0) or 0),
                    "provider_histogram_diff": float(indicators.get("provider_histogram_diff", 0) or 0),
                    "provider_ema9_diff": float(indicators.get("provider_ema9_diff", 0) or 0),
                    "provider_ema20_diff": float(indicators.get("provider_ema20_diff", 0) or 0),
                    "provider_stoch_k_diff": float(indicators.get("provider_stoch_k_diff", 0) or 0),
                    "provider_stoch_d_diff": float(indicators.get("provider_stoch_d_diff", 0) or 0),
                    "provider_vwap_diff": float(indicators.get("provider_vwap_diff", 0) or 0),
                    "provider_open_diff": float(indicators.get("provider_open_diff", 0) or 0),
                    "provider_high_diff": float(indicators.get("provider_high_diff", 0) or 0),
                    "provider_low_diff": float(indicators.get("provider_low_diff", 0) or 0),
                    "provider_close_diff": float(indicators.get("provider_close_diff", 0) or 0),
                    "provider_volume_diff": float(indicators.get("provider_volume_diff", 0) or 0),
                    "provider_missing_inputs": list(indicators.get("provider_missing_inputs", []) or []),
                    "provider_supported_inputs": list(indicators.get("provider_supported_inputs", []) or []),
                }
            )
        snapshots.sort(key=lambda item: str(item["last_bar_at"]), reverse=True)
        return snapshots[:8]

    def _emit_open_intent(self, signal: dict[str, float | int | str]) -> TradeIntentEvent:
        symbol = str(signal["ticker"])
        self.pending_open_symbols.add(symbol)
        reference_price = str(signal["price"])
        quote = self.latest_quotes.get(symbol.upper(), {})
        routed_price = _format_limit_price(quote.get("ask")) or _format_limit_price(reference_price) or reference_price
        metadata = {
            "path": str(signal["path"]),
            "score": str(signal["score"]),
            "score_details": str(signal["score_details"]),
            "timeframe_secs": str(self.definition.interval_secs),
            "reference_price": reference_price,
            "entry_stage": str(signal.get("entry_stage", "")),
        }
        metadata.update(order_routing_metadata(price=routed_price, side="buy"))
        return TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=self.definition.code,
                broker_account_name=self.definition.account_name,
                symbol=symbol,
                side="buy",
                quantity=Decimal(str(signal.get("quantity", self.definition.trading_config.default_quantity))),
                intent_type="open",
                reason=f"ENTRY_{signal['path']}",
                metadata=metadata,
            ),
        )

    def _emit_close_intent(self, signal: dict[str, float | int | str]) -> TradeIntentEvent:
        symbol = str(signal["ticker"])
        self.pending_close_symbols.add(symbol)
        position = self.positions.get_position(symbol)
        quantity = Decimal(str(position.quantity if position else self.definition.trading_config.default_quantity))
        reference_price = str(signal.get("price", ""))
        quote = self.latest_quotes.get(symbol.upper(), {})
        routed_price = _format_limit_price(quote.get("bid")) or _format_limit_price(reference_price) or reference_price
        metadata = {
            "tier": str(signal.get("tier", "")),
            "profit_pct": str(signal.get("profit_pct", "")),
            "reference_price": reference_price,
        }
        if routed_price:
            metadata.update(order_routing_metadata(price=routed_price, side="sell"))
        return TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=self.definition.code,
                broker_account_name=self.definition.account_name,
                symbol=symbol,
                side="sell",
                quantity=quantity,
                intent_type="close",
                reason=str(signal["reason"]),
                metadata=metadata,
            ),
        )

    def _emit_scale_intent(self, signal: dict[str, float | int | str]) -> TradeIntentEvent:
        symbol = str(signal["ticker"])
        level = str(signal["level"])
        self.pending_scale_levels.add((symbol, level))
        reference_price = str(signal.get("price", ""))
        quote = self.latest_quotes.get(symbol.upper(), {})
        routed_price = _format_limit_price(quote.get("bid")) or _format_limit_price(reference_price) or reference_price
        metadata = {
            "level": level,
            "sell_pct": str(signal["sell_pct"]),
            "profit_pct": str(signal["profit_pct"]),
            "reference_price": reference_price,
        }
        if routed_price:
            metadata.update(order_routing_metadata(price=routed_price, side="sell"))
        return TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=self.definition.code,
                broker_account_name=self.definition.account_name,
                symbol=symbol,
                side="sell",
                quantity=Decimal(str(signal["sell_qty"])),
                intent_type="scale",
                reason=str(signal["reason"]),
                metadata=metadata,
            ),
        )

    def _is_exit_retry_blocked(self, symbol: str) -> bool:
        blocked_until = self.exit_retry_blocked_until.get(symbol)
        return blocked_until is not None and utcnow() < blocked_until

    def _is_scale_retry_blocked(self, symbol: str, level: str) -> bool:
        blocked_until = self.scale_retry_blocked_until.get((symbol, level))
        return blocked_until is not None and utcnow() < blocked_until

    @staticmethod
    def _is_no_position_reason(reason: str) -> bool:
        return (
            "cannot be sold short" in reason
            or "insufficient qty" in reason
            or "no broker position available to sell" in reason
            or "no strategy position available to sell" in reason
        )

    def _capture_entry_decision(
        self,
        symbol: str,
        indicators: dict[str, float | bool],
    ) -> dict[str, str] | None:
        decision = self.entry_engine.pop_last_decision(symbol)
        if decision is None:
            return None
        return self._record_decision(
            symbol=symbol,
            status=decision.get("status", "info"),
            reason=decision.get("reason", ""),
            indicators=indicators,
            path=decision.get("path", ""),
            score=decision.get("score", ""),
            score_details=decision.get("score_details", ""),
        )

    def _record_decision(
        self,
        *,
        symbol: str,
        status: str,
        reason: str,
        indicators: dict[str, float | bool],
        path: str = "",
        score: str = "",
        score_details: str = "",
    ) -> dict[str, str]:
        entry = self._build_persisted_decision(
            symbol=symbol,
            status=status,
            reason=reason,
            indicators=indicators,
            path=path,
            score=score,
            score_details=score_details,
        )
        self.recent_decisions.insert(0, entry)
        self.recent_decisions = self.recent_decisions[:50]
        return entry

    def _build_persisted_decision(
        self,
        *,
        symbol: str,
        status: str,
        reason: str,
        indicators: dict[str, float | bool],
        path: str = "",
        score: str = "",
        score_details: str = "",
    ) -> dict[str, str]:
        builder = self.builder_manager.get_builder(symbol)
        bar_time = ""
        if builder is not None and builder.bars:
            last_bar = builder.bars[-1]
            bar_time = datetime.fromtimestamp(last_bar.timestamp, UTC).astimezone(EASTERN_TZ).isoformat()
        return {
            "symbol": symbol,
            "status": str(status),
            "reason": str(reason),
            "path": str(path),
            "score": str(score),
            "score_details": str(score_details),
            "price": f'{float(indicators.get("price", 0) or 0):.4f}',
            "last_bar_at": bar_time,
        }

    def _finalize_completed_bar(
        self,
        symbol: str,
        indicators: dict[str, object],
        intents: list[TradeIntentEvent],
        *,
        decision: dict[str, str] | None = None,
    ) -> list[TradeIntentEvent]:
        self._persist_bar_history(symbol=symbol, indicators=indicators, decision=decision)
        return intents

    def _persist_bar_history(
        self,
        *,
        symbol: str,
        indicators: dict[str, object],
        decision: dict[str, str] | None = None,
    ) -> None:
        if self.session_factory is None:
            return

        builder = self.builder_manager.get_builder(symbol)
        if builder is None or not builder.bars:
            return

        last_bar = builder.bars[-1]
        bar_time = datetime.fromtimestamp(last_bar.timestamp, UTC)
        position_state, position_quantity = self._position_snapshot(symbol)
        indicator_payload = self._build_history_indicator_payload(indicators)

        try:
            with self.session_factory() as session:
                record = session.scalar(
                    select(StrategyBarHistory).where(
                        StrategyBarHistory.strategy_code == self.definition.code,
                        StrategyBarHistory.symbol == symbol,
                        StrategyBarHistory.interval_secs == self.definition.interval_secs,
                        StrategyBarHistory.bar_time == bar_time,
                    )
                )
                if record is None:
                    record = StrategyBarHistory(
                        strategy_code=self.definition.code,
                        symbol=symbol,
                        interval_secs=self.definition.interval_secs,
                        bar_time=bar_time,
                        open_price=Decimal(str(last_bar.open)),
                        high_price=Decimal(str(last_bar.high)),
                        low_price=Decimal(str(last_bar.low)),
                        close_price=Decimal(str(last_bar.close)),
                        volume=int(last_bar.volume),
                        trade_count=int(last_bar.trade_count),
                    )
                    session.add(record)

                record.open_price = Decimal(str(last_bar.open))
                record.high_price = Decimal(str(last_bar.high))
                record.low_price = Decimal(str(last_bar.low))
                record.close_price = Decimal(str(last_bar.close))
                record.volume = int(last_bar.volume)
                record.trade_count = int(last_bar.trade_count)
                record.position_state = position_state
                record.position_quantity = position_quantity
                record.decision_status = str((decision or {}).get("status", ""))
                record.decision_reason = str((decision or {}).get("reason", ""))
                record.decision_path = str((decision or {}).get("path", ""))
                record.decision_score = str((decision or {}).get("score", ""))
                record.decision_score_details = str((decision or {}).get("score_details", ""))
                record.indicators_json = indicator_payload
                session.commit()
        except Exception:
            logger.exception(
                "failed to persist strategy bar history for %s %s",
                self.definition.code,
                symbol,
            )

    def _position_snapshot(self, symbol: str) -> tuple[str, int]:
        position = self.positions.get_position(symbol)
        if symbol in self.pending_close_symbols:
            return "pending_close", int(position.quantity if position is not None else 0)
        if any(pending_symbol == symbol for pending_symbol, _level in self.pending_scale_levels):
            return "pending_scale", int(position.quantity if position is not None else 0)
        if symbol in self.pending_open_symbols:
            return "pending_open", int(position.quantity if position is not None else 0)
        if position is not None:
            return "open", int(position.quantity)
        return "flat", 0

    @staticmethod
    def _build_history_indicator_payload(indicators: dict[str, object]) -> dict[str, object]:
        allowed_keys = (
            "price",
            "open",
            "ema9",
            "ema20",
            "macd",
            "signal",
            "histogram",
            "stoch_k",
            "vwap",
            "extended_vwap",
            "decision_vwap",
            "selected_vwap",
            "bar_volume",
            "macd_delta",
            "macd_above_signal",
            "macd_cross_above",
            "macd_increasing",
            "macd_was_below_3bars",
            "macd_delta_accelerating",
            "price_above_vwap",
            "price_above_ema9",
            "price_above_ema20",
            "provider_source",
            "provider_status",
            "provider_last_bar_at",
            "provider_interval_secs",
            "provider_open",
            "provider_high",
            "provider_low",
            "provider_close",
            "provider_volume",
            "provider_macd",
            "provider_signal",
            "provider_histogram",
            "provider_ema9",
            "provider_ema20",
            "provider_stoch_k",
            "provider_stoch_d",
            "provider_vwap",
            "provider_macd_diff",
            "provider_signal_diff",
            "provider_histogram_diff",
            "provider_ema9_diff",
            "provider_ema20_diff",
            "provider_stoch_k_diff",
            "provider_stoch_d_diff",
            "provider_vwap_diff",
            "provider_open_diff",
            "provider_high_diff",
            "provider_low_diff",
            "provider_close_diff",
            "provider_volume_diff",
            "provider_supported_inputs",
            "provider_missing_inputs",
        )
        payload: dict[str, object] = {}
        for key in allowed_keys:
            if key not in indicators:
                continue
            value = indicators[key]
            if isinstance(value, bool):
                payload[key] = value
            elif isinstance(value, (list, tuple)):
                payload[key] = [str(item) for item in value]
            elif value is None:
                payload[key] = None
            else:
                try:
                    payload[key] = float(value)
                except (TypeError, ValueError):
                    payload[key] = str(value)
        return payload

    def _decorate_indicators(
        self,
        symbol: str,
        trading_indicators: dict[str, float | bool],
    ) -> dict[str, object]:
        indicators = dict(trading_indicators)
        if self.definition.interval_secs not in {30, 60}:
            return indicators

        provider_source = (
            str(getattr(self.indicator_overlay_provider, "SOURCE", "") or "")
            if self.indicator_overlay_provider is not None
            else ""
        )
        provider_supported_inputs = list(
            getattr(self.indicator_overlay_provider, "SUPPORTED_INPUTS", ()) or ()
        )
        provider_missing_inputs = list(
            getattr(self.indicator_overlay_provider, "MISSING_INPUTS", ()) or ()
        )
        indicators.update(
            {
                "provider_source": provider_source,
                "provider_status": "disabled",
                "provider_supported_inputs": provider_supported_inputs,
                "provider_missing_inputs": provider_missing_inputs,
            }
        )
        if self.indicator_overlay_provider is None:
            return indicators

        builder = self.builder_manager.get_builder(symbol)
        if builder is None or not builder.bars:
            return indicators

        last_bar = builder.bars[-1]
        bar_time = datetime.fromtimestamp(last_bar.timestamp, UTC)
        if self.definition.interval_secs == 30:
            fetch_overlay = getattr(self.indicator_overlay_provider, "fetch_aggregate_overlay", None)
            if fetch_overlay is None:
                return indicators
            overlay = fetch_overlay(
                symbol,
                bar_time=bar_time,
                interval_secs=self.definition.interval_secs,
            )
        else:
            overlay = self.indicator_overlay_provider.fetch_minute_indicators(
                symbol,
                bar_time=bar_time,
                indicator_config=self.definition.indicator_config,
            )
        indicators.update(overlay)

        for field in ("macd", "signal", "histogram", "ema9", "ema20", "stoch_k", "stoch_d", "vwap"):
            provider_key = f"provider_{field}"
            provider_value = indicators.get(provider_key)
            local_value = trading_indicators.get(field)
            if provider_value is None or local_value is None:
                continue
            try:
                indicators[f"{provider_key}_diff"] = float(local_value) - float(provider_value)
            except (TypeError, ValueError):
                continue

        if self.definition.interval_secs == 30:
            provider_bar_field_map = (
                ("provider_open", last_bar.open),
                ("provider_high", last_bar.high),
                ("provider_low", last_bar.low),
                ("provider_close", last_bar.close),
                ("provider_volume", last_bar.volume),
            )
            for provider_key, local_value in provider_bar_field_map:
                provider_value = indicators.get(provider_key)
                if provider_value is None:
                    continue
                try:
                    indicators[f"{provider_key}_diff"] = float(local_value) - float(provider_value)
                except (TypeError, ValueError):
                    continue
            return indicators

        if str(indicators.get("provider_status", "")) != "ready":
            return indicators

        provider_field_map = (
            ("macd", "provider_macd"),
            ("macd_prev", "provider_macd_prev"),
            ("macd_prev2", "provider_macd_prev2"),
            ("signal", "provider_signal"),
            ("signal_prev", "provider_signal_prev"),
            ("signal_prev2", "provider_signal_prev2"),
            ("histogram", "provider_histogram"),
            ("histogram_prev", "provider_histogram_prev"),
            ("ema9", "provider_ema9"),
            ("ema20", "provider_ema20"),
            ("stoch_k", "provider_stoch_k"),
            ("stoch_k_prev", "provider_stoch_k_prev"),
            ("stoch_k_prev2", "provider_stoch_k_prev2"),
            ("stoch_d", "provider_stoch_d"),
            ("stoch_d_prev", "provider_stoch_d_prev"),
            ("vwap", "provider_vwap"),
        )
        for field, provider_key in provider_field_map:
            provider_value = indicators.get(provider_key)
            if provider_value is not None:
                indicators[field] = provider_value

        macd = float(indicators.get("macd", 0) or 0)
        macd_prev = float(indicators.get("macd_prev", 0) or 0)
        macd_prev2 = float(indicators.get("provider_macd_prev2", indicators.get("macd_prev", 0)) or 0)
        macd_prev3 = float(indicators.get("provider_macd_prev3", indicators.get("provider_macd_prev2", indicators.get("macd_prev", 0))) or 0)
        signal = float(indicators.get("signal", 0) or 0)
        signal_prev = float(indicators.get("signal_prev", 0) or 0)
        signal_prev2 = float(indicators.get("provider_signal_prev2", indicators.get("signal_prev", 0)) or 0)
        signal_prev3 = float(indicators.get("provider_signal_prev3", indicators.get("provider_signal_prev2", indicators.get("signal_prev", 0))) or 0)
        histogram = float(indicators.get("histogram", 0) or 0)
        histogram_prev = float(indicators.get("histogram_prev", 0) or 0)
        stoch_k = float(indicators.get("stoch_k", 0) or 0)
        stoch_k_prev = float(indicators.get("stoch_k_prev", 0) or 0)
        price = float(indicators.get("price", 0) or 0)
        price_prev = float(indicators.get("price_prev", 0) or 0)
        ema9 = float(indicators.get("ema9", 0) or 0)
        ema20 = float(indicators.get("ema20", 0) or 0)
        vwap = float(indicators.get("vwap", 0) or 0)
        vwap_prev = indicators.get("provider_vwap_prev")
        vwap_prev_value = float(vwap_prev or 0) if vwap_prev is not None else vwap

        indicators["macd_above_signal"] = macd > signal
        indicators["macd_cross_above"] = macd > signal and macd_prev <= signal_prev
        indicators["macd_cross_below"] = macd < signal and macd_prev >= signal_prev
        indicators["macd_increasing"] = macd > macd_prev
        indicators["macd_delta"] = macd - macd_prev
        indicators["macd_delta_prev"] = macd_prev - macd_prev2
        indicators["macd_delta_accelerating"] = (macd - macd_prev) > (macd_prev - macd_prev2)
        indicators["histogram_growing"] = histogram > histogram_prev
        indicators["stoch_k_rising"] = stoch_k > stoch_k_prev
        indicators["stoch_k_below_exit"] = stoch_k < self.definition.indicator_config.stoch_exit_level
        indicators["stoch_k_falling"] = stoch_k < stoch_k_prev
        indicators["price_above_vwap"] = price > vwap
        indicators["price_above_ema9"] = price > ema9
        indicators["price_above_ema20"] = price > ema20
        indicators["price_above_both_emas"] = price > ema9 and price > ema20
        indicators["price_cross_above_vwap"] = price > vwap and price_prev <= vwap_prev_value
        indicators["macd_was_below_3bars"] = (
            macd_prev <= signal_prev and macd_prev2 <= signal_prev2 and macd_prev3 <= signal_prev3
        )

        return indicators

    def _prune_runtime_state(self) -> None:
        keep = set(self.watchlist)
        keep.update(self.pending_open_symbols)
        keep.update(self.pending_close_symbols)
        keep.update(symbol for symbol, _level in self.pending_scale_levels)
        keep.update(position["ticker"] for position in self.positions.get_all_positions())
        self.last_indicators = {
            symbol: indicators
            for symbol, indicators in self.last_indicators.items()
            if symbol in keep
        }
        self.latest_quotes = {
            symbol: quote
            for symbol, quote in self.latest_quotes.items()
            if symbol in keep
        }
        self.entry_engine.prune_tickers(keep)
        self.builder_manager.remove_tickers(
            {ticker for ticker in self.builder_manager.get_all_tickers() if ticker not in keep}
        )


StrategyRuntime = StrategyBotRuntime | RunnerStrategyRuntime


class StrategyEngineState:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        alert_config: MomentumAlertConfig | None = None,
        confirmed_config: MomentumConfirmedConfig | None = None,
        indicator_config: IndicatorConfig | None = None,
        base_trading_config: TradingConfig | None = None,
        now_provider: Callable[[], datetime] | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ):
        self.settings = settings or get_settings()
        resolved_now_provider = now_provider or now_eastern
        default_alert_config = alert_config or MomentumAlertConfig(
            min_price=self.settings.market_data_scan_min_price,
            max_price=self.settings.market_data_scan_max_price,
        )
        self.alert_engine = MomentumAlertEngine(
            default_alert_config,
            scan_interval_secs=self.settings.market_data_snapshot_interval_seconds,
            now_provider=now_provider,
        )
        self.confirmed_scanner = MomentumConfirmedScanner(confirmed_config or MomentumConfirmedConfig())
        self.catalyst_engine = self._build_catalyst_engine(now_provider=now_provider)
        if self.catalyst_engine is not None:
            self.confirmed_scanner.set_catalyst_engine(self.catalyst_engine)
        self.five_pillars_config = FivePillarsConfig(
            min_price=self.settings.market_data_scan_min_price,
            max_price=self.settings.market_data_scan_max_price,
        )
        self.top_gainers_tracker = TopGainersTracker(
            TopGainersConfig(
                min_price=self.settings.market_data_scan_min_price,
                max_price=self.settings.market_data_scan_max_price,
            )
        )
        self.reference_data: dict[str, ReferenceData] = {}
        self.current_confirmed: list[dict[str, object]] = []
        self.all_confirmed: list[dict[str, object]] = []
        self.five_pillars: list[dict[str, object]] = []
        self.top_gainers: list[dict[str, object]] = []
        self.top_gainer_changes: list[dict[str, object]] = []
        self.recent_alerts: list[dict[str, object]] = []
        self.alert_warmup: dict[str, object] = self.alert_engine.get_warmup_status()
        self.cycle_count = 0
        self.latest_snapshots: dict[str, MarketSnapshot] = {}
        self._first_seen_by_ticker: dict[str, str] = {}
        self._seeded_confirmed_pending_revalidation = False
        self._pending_recent_alert_replay = False
        self._active_scanner_session_start = current_scanner_session_start_utc(
            self.alert_engine.now_provider()
        )
        self._schwab_stream_bot_codes = self._resolve_schwab_stream_bot_codes()
        self.reclaim_excluded_symbols = set(
            self.settings.strategy_macd_30s_reclaim_excluded_symbol_list
        )
        registrations = strategy_registration_map(self.settings)
        macd_30s_indicator_overlay_provider = None
        macd_1m_indicator_overlay_provider = None
        if (
            self.settings.strategy_macd_1m_taapi_indicator_source_enabled
            and self.settings.taapi_secret
            and self.settings.massive_api_key
        ):
            macd_1m_indicator_overlay_provider = TaapiIndicatorProvider(
                self.settings.taapi_secret,
                provider_secret=self.settings.massive_api_key,
            )
        elif (
            self.settings.strategy_macd_1m_massive_indicator_overlay_enabled
            and self.settings.massive_api_key
        ):
            macd_1m_indicator_overlay_provider = MassiveIndicatorProvider(
                self.settings.massive_api_key
            )

        base_trading = base_trading_config or TradingConfig()
        macd_30s_trading = self._resolve_30s_trading_config(base_trading, variant="regular")
        macd_30s_probe_trading = self._resolve_30s_trading_config(base_trading, variant="probe")
        macd_30s_reclaim_trading = self._resolve_30s_trading_config(base_trading, variant="reclaim")
        macd_30s_retest_trading = self._resolve_30s_trading_config(base_trading, variant="retest")
        default_indicator_config = indicator_config or IndicatorConfig()
        runner_trading = base_trading.make_tos_variant(quantity=100, bar_interval_secs=60)
        use_live_aggregate_bars = (
            self.settings.strategy_macd_30s_live_aggregate_bars_enabled
            or self.settings.market_data_live_aggregate_stream_enabled
        )
        self.bots: dict[str, StrategyRuntime] = {
            "macd_1m": StrategyBotRuntime(
                StrategyDefinition(
                    code="macd_1m",
                    display_name=registrations["macd_1m"].display_name,
                    account_name=registrations["macd_1m"].account_name,
                    interval_secs=60,
                    trading_config=base_trading.make_1m_variant(),
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
                session_factory=session_factory if self.settings.strategy_history_persistence_enabled else None,
                use_live_aggregate_bars=False,
                live_aggregate_fallback_enabled=False,
                indicator_overlay_provider=macd_1m_indicator_overlay_provider,
            ),
            "tos": StrategyBotRuntime(
                StrategyDefinition(
                    code="tos",
                    display_name=registrations["tos"].display_name,
                    account_name=registrations["tos"].account_name,
                    interval_secs=60,
                    trading_config=base_trading.make_tos_variant(
                        quantity=self.settings.strategy_tos_default_quantity
                    ),
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
                session_factory=session_factory if self.settings.strategy_history_persistence_enabled else None,
                use_live_aggregate_bars=False,
                live_aggregate_fallback_enabled=False,
            ),
            "runner": RunnerStrategyRuntime(
                definition_code="runner",
                account_name=registrations["runner"].account_name,
                default_quantity=runner_trading.default_quantity,
                bar_interval_secs=runner_trading.bar_interval_secs,
                now_provider=now_provider,
                source_service=SERVICE_NAME,
            ),
        }
        if self.settings.strategy_macd_30s_enabled and "macd_30s" in registrations:
            self.bots["macd_30s"] = StrategyBotRuntime(
                StrategyDefinition(
                    code="macd_30s",
                    display_name=registrations["macd_30s"].display_name,
                    account_name=registrations["macd_30s"].account_name,
                    interval_secs=30,
                    trading_config=macd_30s_trading,
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
                session_factory=session_factory if self.settings.strategy_history_persistence_enabled else None,
                use_live_aggregate_bars=use_live_aggregate_bars,
                live_aggregate_fallback_enabled=self.settings.strategy_macd_30s_live_aggregate_fallback_enabled,
                live_aggregate_stale_after_seconds=self.settings.strategy_macd_30s_live_aggregate_stale_after_seconds,
                indicator_overlay_provider=macd_30s_indicator_overlay_provider,
                builder_manager=SchwabNativeBarBuilderManager(
                    interval_secs=30,
                    time_provider=lambda: resolved_now_provider().timestamp(),
                ),
                indicator_engine=SchwabNativeIndicatorEngine(default_indicator_config),
                entry_engine=SchwabNativeEntryEngine(
                    macd_30s_trading,
                    name=registrations["macd_30s"].display_name,
                    now_provider=resolved_now_provider,
                ),
            )
        if self.settings.strategy_macd_30s_probe_enabled and "macd_30s_probe" in registrations:
            self.bots["macd_30s_probe"] = StrategyBotRuntime(
                StrategyDefinition(
                    code="macd_30s_probe",
                    display_name=registrations["macd_30s_probe"].display_name,
                    account_name=registrations["macd_30s_probe"].account_name,
                    interval_secs=30,
                    trading_config=macd_30s_probe_trading,
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
                session_factory=session_factory if self.settings.strategy_history_persistence_enabled else None,
                use_live_aggregate_bars=use_live_aggregate_bars,
                live_aggregate_fallback_enabled=self.settings.strategy_macd_30s_live_aggregate_fallback_enabled,
                live_aggregate_stale_after_seconds=self.settings.strategy_macd_30s_live_aggregate_stale_after_seconds,
                indicator_overlay_provider=macd_30s_indicator_overlay_provider,
            )
        if self.settings.strategy_macd_30s_reclaim_enabled and "macd_30s_reclaim" in registrations:
            self.bots["macd_30s_reclaim"] = StrategyBotRuntime(
                StrategyDefinition(
                    code="macd_30s_reclaim",
                    display_name=registrations["macd_30s_reclaim"].display_name,
                    account_name=registrations["macd_30s_reclaim"].account_name,
                    interval_secs=30,
                    trading_config=macd_30s_reclaim_trading,
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
                session_factory=session_factory if self.settings.strategy_history_persistence_enabled else None,
                use_live_aggregate_bars=use_live_aggregate_bars,
                live_aggregate_fallback_enabled=self.settings.strategy_macd_30s_live_aggregate_fallback_enabled,
                live_aggregate_stale_after_seconds=self.settings.strategy_macd_30s_live_aggregate_stale_after_seconds,
                indicator_overlay_provider=macd_30s_indicator_overlay_provider,
            )
        if self.settings.strategy_macd_30s_retest_enabled and "macd_30s_retest" in registrations:
            self.bots["macd_30s_retest"] = StrategyBotRuntime(
                StrategyDefinition(
                    code="macd_30s_retest",
                    display_name=registrations["macd_30s_retest"].display_name,
                    account_name=registrations["macd_30s_retest"].account_name,
                    interval_secs=30,
                    trading_config=macd_30s_retest_trading,
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
                session_factory=session_factory if self.settings.strategy_history_persistence_enabled else None,
                use_live_aggregate_bars=use_live_aggregate_bars,
                live_aggregate_fallback_enabled=self.settings.strategy_macd_30s_live_aggregate_fallback_enabled,
                live_aggregate_stale_after_seconds=self.settings.strategy_macd_30s_live_aggregate_stale_after_seconds,
                indicator_overlay_provider=macd_30s_indicator_overlay_provider,
            )

    def _resolve_30s_trading_config(
        self,
        base_trading: TradingConfig,
        *,
        variant: str,
    ) -> TradingConfig:
        if variant == "regular":
            config = base_trading.make_30s_schwab_native_variant(
                quantity=self.settings.strategy_macd_30s_default_quantity
            )
            raw_overrides = self.settings.strategy_macd_30s_config_overrides_json
            scope = "strategy_macd_30s_config_overrides_json"
        elif variant == "probe":
            config = base_trading.make_30s_pretrigger_variant(quantity=100)
            raw_overrides = self.settings.strategy_macd_30s_probe_config_overrides_json
            scope = "strategy_macd_30s_probe_config_overrides_json"
        elif variant == "reclaim":
            config = base_trading.make_30s_reclaim_variant(quantity=100)
            raw_overrides = self.settings.strategy_macd_30s_reclaim_config_overrides_json
            scope = "strategy_macd_30s_reclaim_config_overrides_json"
        elif variant == "retest":
            config = base_trading.make_30s_retest_variant(quantity=100)
            raw_overrides = self.settings.strategy_macd_30s_retest_config_overrides_json
            scope = "strategy_macd_30s_retest_config_overrides_json"
        else:
            raise ValueError(f"Unsupported 30s variant: {variant}")

        config = self._apply_trading_config_overrides(
            config,
            self.settings.strategy_macd_30s_common_config_overrides_json,
            scope="strategy_macd_30s_common_config_overrides_json",
        )
        return self._apply_trading_config_overrides(
            config,
            raw_overrides,
            scope=scope,
        )

    def _resolve_schwab_stream_bot_codes(self) -> tuple[str, ...]:
        codes: list[str] = []
        for code in ("macd_30s", "tos"):
            if self.settings.provider_for_strategy(code) == "schwab":
                codes.append(code)
        return tuple(codes)

    def _apply_trading_config_overrides(
        self,
        config: TradingConfig,
        raw_overrides: str,
        *,
        scope: str,
    ) -> TradingConfig:
        if not raw_overrides.strip():
            return config
        try:
            overrides = self.settings.parse_strategy_config_overrides(raw_overrides)
        except ValueError as exc:
            logger.warning("Ignoring invalid TradingConfig overrides for %s: %s", scope, exc)
            return config
        if not overrides:
            return config
        valid_fields = set(TradingConfig.__dataclass_fields__)
        unknown_fields = sorted(field for field in overrides if field not in valid_fields)
        if unknown_fields:
            logger.warning("Ignoring unsupported TradingConfig overrides for %s: %s", scope, ", ".join(unknown_fields))
        applied = {field: value for field, value in overrides.items() if field in valid_fields}
        if not applied:
            return config
        fields = dict(config.__dict__)
        fields.update(applied)
        return TradingConfig(**fields)

    def process_snapshot_batch(
        self,
        snapshots: Sequence[MarketSnapshot],
        reference_data: dict[str, ReferenceData],
        *,
        blacklisted_symbols: set[str] | None = None,
    ) -> dict[str, object]:
        self._roll_scanner_session_if_needed()
        self.cycle_count += 1
        blocked = {symbol.upper() for symbol in (blacklisted_symbols or set()) if symbol}
        if blocked:
            self.confirmed_scanner.remove_tickers(blocked)

        filtered_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot.ticker.upper() not in blocked
        ]
        filtered_reference_data = {
            symbol: value
            for symbol, value in reference_data.items()
            if symbol.upper() not in blocked
        }

        self.reference_data.update(filtered_reference_data)
        current_now = self.alert_engine.now_provider()
        self.five_pillars = self._decorate_scanner_rows(
            apply_five_pillars(
                filtered_snapshots,
                self.reference_data,
                self.five_pillars_config,
                now=current_now,
            )
        )
        self.top_gainers, self.top_gainer_changes = self.top_gainers_tracker.update(
            filtered_snapshots,
            self.reference_data,
            now=current_now,
        )
        self.top_gainers = self._decorate_scanner_rows(self.top_gainers)
        self.alert_engine.record_snapshot(filtered_snapshots)
        alerts = self.alert_engine.check_alerts(filtered_snapshots, self.reference_data)
        if self.catalyst_engine is not None:
            catalyst_tickers = {
                str(alert.get("ticker", "")).upper()
                for alert in alerts
                if str(alert.get("ticker", "")).strip()
            }
            catalyst_tickers.update(
                str(stock.get("ticker", "")).upper()
                for stock in self.confirmed_scanner.get_all_confirmed()
                if str(stock.get("ticker", "")).strip()
            )
            if catalyst_tickers:
                self.catalyst_engine.get_catalysts_batch(sorted(catalyst_tickers))
        self._record_recent_alerts(alerts)
        self.alert_warmup = self.alert_engine.get_warmup_status()
        snapshot_lookup = {snapshot.ticker: snapshot for snapshot in filtered_snapshots}
        if self._seeded_confirmed_pending_revalidation:
            self.confirmed_scanner.revalidate_seeded_candidates(snapshot_lookup, self.reference_data)
            self._seeded_confirmed_pending_revalidation = False
        if self._pending_recent_alert_replay and self.recent_alerts:
            self.confirmed_scanner.process_alerts(
                list(self.recent_alerts),
                filtered_reference_data,
                snapshot_lookup,
            )
            self._pending_recent_alert_replay = False
        newly_confirmed = self.confirmed_scanner.process_alerts(
            alerts,
            filtered_reference_data,
            snapshot_lookup,
        )
        self.confirmed_scanner.update_live_prices(snapshot_lookup)
        self.confirmed_scanner.prune_faded_candidates()

        self.all_confirmed = [
            stock
            for stock in self.confirmed_scanner.get_ranked_confirmed(min_score=0)
            if str(stock.get("ticker", "")).upper() not in blocked
        ]
        self.current_confirmed = [
            stock
            for stock in self.confirmed_scanner.get_top_n(
                min_change_pct=0,
            )
            if str(stock.get("ticker", "")).upper() not in blocked
        ]

        watchlist = [str(stock["ticker"]) for stock in self.current_confirmed]
        tracked_snapshot_symbols = {
            str(stock.get("ticker", "")).upper()
            for stock in self.all_confirmed
            if str(stock.get("ticker", "")).strip()
        }
        tracked_snapshot_symbols.update(symbol.upper() for symbol in watchlist)
        self.latest_snapshots = {
            symbol: snapshot_lookup[symbol]
            for symbol in tracked_snapshot_symbols
            if symbol in snapshot_lookup
        }
        for code, bot in self.bots.items():
            bot_watchlist = self._watchlist_for_bot(code, watchlist)
            if code == "runner":
                bot.update_market_snapshots(filtered_snapshots)
                bot.set_watchlist(bot_watchlist)
                bot.update_candidates(self.current_confirmed)
                continue
            if code not in self._schwab_stream_bot_codes:
                bot.update_market_snapshots(filtered_snapshots)
            bot.set_watchlist(bot_watchlist)

        return {
            "alerts": alerts,
            "newly_confirmed": newly_confirmed,
            "all_confirmed": self.all_confirmed,
            "top_confirmed": self.current_confirmed,
            "five_pillars": self.five_pillars,
            "top_gainers": self.top_gainers,
            "recent_alerts": self.recent_alerts,
            "watchlist": watchlist,
            "market_data_symbols": self.market_data_symbols(),
            "schwab_stream_symbols": self.schwab_stream_symbols(),
        }

    def handle_trade_tick(
        self,
        symbol: str,
        price: float,
        size: int,
        timestamp_ns: int | None = None,
        cumulative_volume: int | None = None,
        strategy_codes: Sequence[str] | None = None,
        exclude_codes: Sequence[str] | None = None,
    ) -> list[TradeIntentEvent]:
        intents: list[TradeIntentEvent] = []
        for _code, bot in self._iter_target_bots(
            strategy_codes=strategy_codes,
            exclude_codes=exclude_codes,
        ):
            intents.extend(bot.handle_trade_tick(symbol, price, size, timestamp_ns, cumulative_volume))
        return intents

    def handle_quote_tick(
        self,
        *,
        symbol: str,
        bid_price: float | None,
        ask_price: float | None,
        strategy_codes: Sequence[str] | None = None,
        exclude_codes: Sequence[str] | None = None,
    ) -> None:
        for _code, bot in self._iter_target_bots(
            strategy_codes=strategy_codes,
            exclude_codes=exclude_codes,
        ):
            handle_quote_tick = getattr(bot, "handle_quote_tick", None)
            if handle_quote_tick is None:
                continue
            handle_quote_tick(
                symbol,
                bid_price=bid_price,
                ask_price=ask_price,
            )

    def handle_live_bar(
        self,
        *,
        symbol: str,
        interval_secs: int,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: int,
        timestamp: float,
        trade_count: int = 1,
        strategy_codes: Sequence[str] | None = None,
        exclude_codes: Sequence[str] | None = None,
    ) -> list[TradeIntentEvent]:
        del interval_secs
        intents: list[TradeIntentEvent] = []
        for _code, bot in self._iter_target_bots(
            strategy_codes=strategy_codes,
            exclude_codes=exclude_codes,
        ):
            handle_live_bar = getattr(bot, "handle_live_bar", None)
            if handle_live_bar is None:
                continue
            intents.extend(
                handle_live_bar(
                    symbol=symbol,
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    volume=volume,
                    timestamp=timestamp,
                    trade_count=trade_count,
                )
            )
        return intents

    def flush_completed_bars(self) -> tuple[list[TradeIntentEvent], int]:
        intents: list[TradeIntentEvent] = []
        completed_count = 0
        for bot in self.bots.values():
            flush_completed_bars = getattr(bot, "flush_completed_bars", None)
            if flush_completed_bars is None:
                continue
            bot_intents, bot_completed_count = flush_completed_bars()
            intents.extend(bot_intents)
            completed_count += bot_completed_count
        return intents, completed_count

    def seed_bars(
        self,
        strategy_code: str,
        symbol: str,
        bars: Sequence[dict[str, float | int]],
    ) -> None:
        self.bots[strategy_code].seed_bars(symbol, bars)

    def hydrate_historical_bars(
        self,
        *,
        symbol: str,
        interval_secs: int,
        bars: Sequence[dict[str, float | int]],
        strategy_codes: Sequence[str] | None = None,
        exclude_codes: Sequence[str] | None = None,
    ) -> list[str]:
        hydrated: list[str] = []
        for code, bot in self._iter_target_bots(
            strategy_codes=strategy_codes,
            exclude_codes=exclude_codes,
        ):
            bot_interval = getattr(getattr(bot, "definition", None), "interval_secs", None)
            if bot_interval == interval_secs:
                bot.seed_bars(symbol, bars)
                hydrated.append(code)
                continue

            runner_interval = getattr(getattr(bot, "builder_manager", None), "interval_secs", None)
            if code == "runner" and runner_interval == interval_secs:
                bot.seed_bars(symbol, bars)
                hydrated.append(code)
        return hydrated

    def apply_execution_fill(
        self,
        *,
        client_order_id: str,
        strategy_code: str,
        symbol: str,
        intent_type: str,
        status: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        level: str | None = None,
        path: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.bots[strategy_code].apply_execution_fill(
            client_order_id=client_order_id,
            symbol=symbol,
            intent_type=intent_type,
            status=status,
            side=side,
            quantity=quantity,
            price=price,
            level=level,
            path=path,
            reason=reason,
        )

    def apply_order_status(
        self,
        *,
        strategy_code: str,
        symbol: str,
        intent_type: str,
        status: str,
        level: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.bots[strategy_code].apply_order_status(
            symbol=symbol,
            intent_type=intent_type,
            status=status,
            level=level,
            reason=reason,
        )

    def summary(self) -> dict[str, object]:
        return {
            "all_confirmed": self.all_confirmed,
            "watchlist": [str(stock["ticker"]) for stock in self.current_confirmed],
            "top_confirmed": self.current_confirmed,
            "five_pillars": self.five_pillars,
            "top_gainers": self.top_gainers,
            "recent_alerts": self.recent_alerts,
            "top_gainer_changes": self.top_gainer_changes,
            "alert_warmup": self.alert_warmup,
            "cycle_count": self.cycle_count,
            "bots": {code: bot.summary() for code, bot in self.bots.items()},
        }

    def seed_confirmed_candidates(self, candidates: Sequence[dict[str, object]]) -> None:
        self.confirmed_scanner.seed_confirmed_candidates(candidates)
        self._seeded_confirmed_pending_revalidation = bool(candidates)

    def restore_confirmed_runtime_view(self, visible_confirmed: Sequence[dict[str, object]]) -> None:
        self.current_confirmed = [
            {**dict(item), "ticker": str(item.get("ticker", "")).upper()}
            for item in visible_confirmed
            if str(item.get("ticker", "")).strip()
        ]
        watchlist = [str(stock["ticker"]) for stock in self.current_confirmed]
        for code, bot in self.bots.items():
            bot.set_watchlist(self._watchlist_for_bot(code, watchlist))
            if code == "runner":
                bot.update_candidates(self.current_confirmed)

    def market_data_symbols(self) -> list[str]:
        symbols: set[str] = set()
        for code, bot in self.bots.items():
            if code in self._schwab_stream_bot_codes:
                continue
            symbols.update(bot.active_symbols())
        return sorted(symbols)

    def market_data_intervals(self) -> set[int]:
        intervals: set[int] = set()
        for code, bot in self.bots.items():
            if code in self._schwab_stream_bot_codes:
                continue
            definition = getattr(bot, "definition", None)
            if definition is not None:
                intervals.add(int(definition.interval_secs))
                continue
            runner_interval = getattr(getattr(bot, "builder_manager", None), "interval_secs", None)
            if runner_interval is not None:
                intervals.add(int(runner_interval))
        return intervals

    def market_data_hydration_pairs(self, symbols: Sequence[str] | None = None) -> set[tuple[str, int]]:
        symbol_filter = (
            {str(symbol).upper() for symbol in symbols if str(symbol).strip()}
            if symbols is not None
            else None
        )
        pairs: set[tuple[str, int]] = set()
        for code, bot in self.bots.items():
            if code in self._schwab_stream_bot_codes:
                continue

            definition = getattr(bot, "definition", None)
            interval_secs = int(definition.interval_secs) if definition is not None else None
            if interval_secs is None:
                runner_interval = getattr(getattr(bot, "builder_manager", None), "interval_secs", None)
                if runner_interval is not None:
                    interval_secs = int(runner_interval)
            if interval_secs is None:
                continue

            for symbol in bot.active_symbols():
                normalized_symbol = str(symbol).upper()
                if symbol_filter is not None and normalized_symbol not in symbol_filter:
                    continue
                pairs.add((normalized_symbol, interval_secs))
        return pairs

    def schwab_stream_symbols(self) -> list[str]:
        symbols: set[str] = set()
        for code in self._schwab_stream_bot_codes:
            bot = self.bots.get(code)
            if bot is None:
                continue
            symbols.update(bot.active_symbols())
        return sorted(symbols)

    def schwab_stream_strategy_codes(self) -> tuple[str, ...]:
        return self._schwab_stream_bot_codes

    def _iter_target_bots(
        self,
        *,
        strategy_codes: Sequence[str] | None = None,
        exclude_codes: Sequence[str] | None = None,
    ) -> list[tuple[str, StrategyRuntime]]:
        include = (
            {str(code) for code in strategy_codes if str(code).strip()}
            if strategy_codes is not None
            else None
        )
        exclude = {str(code) for code in (exclude_codes or ()) if str(code).strip()}
        return [
            (code, bot)
            for code, bot in self.bots.items()
            if (include is None or code in include) and code not in exclude
        ]

    def _decorate_scanner_rows(self, rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
        decorated: list[dict[str, object]] = []
        for row in rows:
            ticker = str(row.get("ticker", "")).upper()
            if ticker and ticker not in self._first_seen_by_ticker:
                self._first_seen_by_ticker[ticker] = self.alert_engine.now_provider().strftime("%I:%M:%S %p ET")
            decorated.append(
                {
                    **row,
                    "ticker": ticker,
                    "first_seen": self._first_seen_by_ticker.get(ticker, ""),
                }
            )
        return decorated

    def _record_recent_alerts(self, alerts: Sequence[dict[str, object]]) -> None:
        if not alerts:
            return
        normalized = [
            {
                **alert,
                "ticker": str(alert.get("ticker", "")).upper(),
            }
            for alert in alerts
        ]
        self.recent_alerts.extend(normalized)
        self.recent_alerts = self.recent_alerts[-100:]

    def _watchlist_for_bot(self, code: str, watchlist: Sequence[str]) -> list[str]:
        normalized = [str(symbol).upper() for symbol in watchlist if str(symbol).strip()]
        if code != "macd_30s_reclaim" or not self.reclaim_excluded_symbols:
            return normalized
        return [
            symbol
            for symbol in normalized
            if symbol not in self.reclaim_excluded_symbols
        ]

    def _roll_scanner_session_if_needed(self) -> None:
        current_session_start = current_scanner_session_start_utc(self.alert_engine.now_provider())
        if current_session_start == self._active_scanner_session_start:
            return

        self.confirmed_scanner.reset()
        self.all_confirmed = []
        self.current_confirmed = []
        self.five_pillars = []
        self.top_gainers = []
        self.top_gainer_changes = []
        self.recent_alerts = []
        self.latest_snapshots = {}
        self._first_seen_by_ticker.clear()
        self._seeded_confirmed_pending_revalidation = False
        self._pending_recent_alert_replay = False
        self._active_scanner_session_start = current_session_start

    def _build_catalyst_engine(
        self,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> CatalystEngine | None:
        if not self.settings.news_enabled:
            return None

        api_key, secret_key = self._resolve_news_credentials()
        if not api_key or not secret_key:
            logger.info("Catalyst engine disabled: no Alpaca credentials available for news enrichment")
            return None

        ai_evaluator: CatalystAiEvaluator | None = None
        if self.settings.news_ai_shadow_enabled and self.settings.news_ai_api_key:
            ai_evaluator = CatalystAiEvaluator(
                api_key=self.settings.news_ai_api_key,
                config=CatalystAiConfig(
                    provider=self.settings.news_ai_provider,
                    model=self.settings.news_ai_model,
                    base_url=self.settings.news_ai_base_url,
                    request_timeout_seconds=self.settings.news_ai_request_timeout_seconds,
                    max_articles=self.settings.news_ai_max_articles,
                    max_summary_chars=self.settings.news_ai_max_summary_chars,
                ),
            )

        return CatalystEngine(
            api_key=api_key,
            secret_key=secret_key,
            config=CatalystConfig(
                session_start_hour_et=self.settings.news_session_start_hour_et,
                cache_ttl_minutes=self.settings.news_cache_ttl_minutes,
                request_timeout_seconds=self.settings.news_request_timeout_seconds,
                max_articles_per_symbol=self.settings.news_max_articles_per_symbol,
                batch_size=self.settings.news_batch_size,
                path_a_min_confidence=self.settings.news_path_a_min_confidence,
            ),
            now_provider=now_provider,
            ai_evaluator=ai_evaluator,
            promote_ai_result=self.settings.news_ai_promote_enabled,
        )

    def _resolve_news_credentials(self) -> tuple[str | None, str | None]:
        candidates = (
            (
                self.settings.alpaca_macd_1m_api_key,
                self.settings.alpaca_macd_1m_secret_key,
            ),
            (
                self.settings.alpaca_macd_30s_api_key,
                self.settings.alpaca_macd_30s_secret_key,
            ),
            (
                self.settings.alpaca_tos_runner_api_key,
                self.settings.alpaca_tos_runner_secret_key,
            ),
        )
        for api_key, secret_key in candidates:
            if api_key and secret_key:
                return api_key, secret_key
        return None, None


class StrategyEngineService:
    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ):
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        persistence_enabled = (
            self.settings.dashboard_snapshot_persistence_enabled
            or self.settings.strategy_history_persistence_enabled
        )
        self.session_factory = (
            session_factory
            if session_factory is not None
            else build_session_factory(self.settings)
            if persistence_enabled
            else None
        )
        self.state = StrategyEngineState(self.settings, session_factory=self.session_factory)
        self.logger = configure_logging(SERVICE_NAME, self.settings.log_level)
        self.instance_name = socket.gethostname()
        self._stream_offsets = {
            stream_name(self.settings.redis_stream_prefix, "market-data"): "$",
            stream_name(self.settings.redis_stream_prefix, "order-events"): "$",
            stream_name(self.settings.redis_stream_prefix, "snapshot-batches"): "$",
        }
        self._last_market_data_symbols: set[str] = set()
        self._last_schwab_stream_symbols: set[str] = set()
        self._last_scanner_history_signature: str | None = None
        self._historical_hydration_attempts = 5
        self._historical_hydration_poll_delay_secs = 0.2
        self._runtime_db_reconcile_interval_secs = 5
        self._schwab_trade_queue: asyncio.Queue[TradeTickRecord] = asyncio.Queue()
        self._schwab_quote_queue: asyncio.Queue[QuoteTickRecord] = asyncio.Queue()
        self._schwab_stream_client = self._build_schwab_stream_client()
        self._schwab_tick_archive = self._build_schwab_tick_archive()

    async def run(self) -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        heartbeat_interval_secs = max(1, self.settings.service_heartbeat_interval_seconds)
        stream_block_ms = min(1_000, heartbeat_interval_secs * 1_000)
        last_heartbeat_at = utcnow()

        self.logger.info("%s starting", SERVICE_NAME)
        self._restore_alert_engine_state_from_dashboard_snapshot()
        self._seed_confirmed_candidates_from_dashboard_snapshot()
        self._restore_runtime_state_from_database()
        await self._prefill_alert_history_from_snapshot_batches()
        if self._schwab_stream_client is not None:
            await self._schwab_stream_client.start(
                on_trade=self._enqueue_schwab_trade_tick,
                on_quote=self._enqueue_schwab_quote_tick,
            )
        await self._sync_subscription_targets()
        await self._publish_strategy_state_snapshot()
        await self._publish_heartbeat("starting")
        last_runtime_db_reconcile_at = utcnow()

        while not stop_event.is_set():
            try:
                messages = await self.redis.xread(
                    self._stream_offsets,
                    block=stream_block_ms,
                    count=50,
                )
            except Exception:
                self.logger.exception("redis xread failed")
                await asyncio.sleep(1)
                continue

            if messages:
                for stream, entries in messages:
                    for message_id, fields in entries:
                        self._stream_offsets[stream] = message_id
                        await self._handle_stream_message(stream, fields)

            schwab_intent_count = await self._drain_schwab_stream_queues()
            if schwab_intent_count:
                await self._sync_subscription_targets()
                await self._publish_strategy_state_snapshot()

            bar_close_intents, completed_bar_count = self.state.flush_completed_bars()
            for intent in bar_close_intents:
                await self._publish_intent(intent)
            if bar_close_intents:
                await self._sync_subscription_targets()
            if completed_bar_count:
                await self._publish_strategy_state_snapshot()
            if bar_close_intents:
                self.logger.info(
                    "generated %s intents from %s forced bar closes",
                    len(bar_close_intents),
                    completed_bar_count,
                )

            if (utcnow() - last_runtime_db_reconcile_at).total_seconds() >= self._runtime_db_reconcile_interval_secs:
                runtime_changed = self._reconcile_runtime_state_from_database(log_when_changed=False)
                if runtime_changed:
                    await self._sync_subscription_targets()
                    await self._publish_strategy_state_snapshot()
                last_runtime_db_reconcile_at = utcnow()

            if (utcnow() - last_heartbeat_at).total_seconds() >= heartbeat_interval_secs:
                await self._publish_heartbeat("healthy")
                last_heartbeat_at = utcnow()

        await self._publish_heartbeat("stopping")
        if self._schwab_stream_client is not None:
            await self._schwab_stream_client.stop()
        if self._schwab_tick_archive is not None:
            self._schwab_tick_archive.close()
        await self.redis.aclose()
        self.logger.info("%s stopping", SERVICE_NAME)

    async def _prefill_alert_history_from_snapshot_batches(self) -> None:
        required_cycles = int(self.state.alert_engine.get_warmup_status().get("squeeze_10min_needs", 0) or 0)
        if required_cycles <= 0:
            return

        stream = stream_name(self.settings.redis_stream_prefix, "snapshot-batches")
        try:
            entries = await self.redis.xrevrange(stream, count=required_cycles)
        except Exception:
            self.logger.exception("snapshot batch warmup prefill failed")
            return

        if not entries:
            return

        history_batches: list[list[MarketSnapshot]] = []
        replay_batches: list[tuple[datetime, list[MarketSnapshot], dict[str, ReferenceData]]] = []
        for _message_id, fields in reversed(entries):
            data = fields.get("data")
            if not data:
                continue
            try:
                payload = json.loads(data)
                event = SnapshotBatchEvent.model_validate(payload)
            except Exception:
                self.logger.exception("invalid snapshot batch during warmup prefill")
                continue

            snapshots = [snapshot_from_payload(item) for item in event.payload.snapshots]
            if snapshots:
                history_batches.append(snapshots)
                replay_batches.append(
                    (
                        event.payload.completed_at,
                        snapshots,
                        {
                            item.symbol: ReferenceData(
                                shares_outstanding=item.shares_outstanding,
                                avg_daily_volume=float(item.avg_daily_volume),
                            )
                            for item in event.payload.reference_data
                        },
                    )
                )

        if not history_batches:
            return

        self.state.alert_engine.prefill_history(history_batches)
        self.state.alert_warmup = self.state.alert_engine.get_warmup_status()
        if not self.state.recent_alerts and replay_batches:
            self._rebuild_recent_alert_tape_from_snapshot_batches(replay_batches)
        self.logger.info(
            "prefilled momentum alert history from %s snapshot batches",
            len(history_batches),
        )

    def _rebuild_recent_alert_tape_from_snapshot_batches(
        self,
        replay_batches: Sequence[tuple[datetime, list[MarketSnapshot], dict[str, ReferenceData]]],
    ) -> None:
        replay_time = utcnow().astimezone(EASTERN_TZ)
        replay_engine = MomentumAlertEngine(
            self.state.alert_engine.config,
            scan_interval_secs=self.settings.market_data_snapshot_interval_seconds,
            now_provider=lambda: replay_time,
        )
        rebuilt_alerts: list[dict[str, object]] = []
        rebuilt_first_seen: dict[str, str] = {}

        for completed_at, snapshots, reference_data in replay_batches:
            replay_time = completed_at.astimezone(EASTERN_TZ)
            label = replay_time.strftime("%I:%M:%S %p ET")
            for snapshot in snapshots:
                ticker = str(snapshot.ticker or "").upper()
                if ticker and ticker not in rebuilt_first_seen:
                    rebuilt_first_seen[ticker] = label
            replay_engine.record_snapshot(snapshots)
            alerts = replay_engine.check_alerts(snapshots, reference_data)
            rebuilt_alerts.extend(
                {
                    **alert,
                    "ticker": str(alert.get("ticker", "")).upper(),
                }
                for alert in alerts
            )

        if rebuilt_alerts:
            self.state.recent_alerts = rebuilt_alerts[-100:]
        if rebuilt_first_seen:
            self.state._first_seen_by_ticker.update(rebuilt_first_seen)

    async def _handle_stream_message(self, stream: str, fields: dict[str, str]) -> None:
        del stream
        data = fields.get("data")
        if not data:
            return

        payload = json.loads(data)
        event_type = payload.get("event_type")
        if event_type == "snapshot_batch":
            event = SnapshotBatchEvent.model_validate(payload)
            snapshots = [snapshot_from_payload(item) for item in event.payload.snapshots]
            reference = {
                item.symbol: ReferenceData(
                    shares_outstanding=item.shares_outstanding,
                    avg_daily_volume=float(item.avg_daily_volume),
                )
                for item in event.payload.reference_data
            }
            summary = self.state.process_snapshot_batch(
                snapshots,
                reference,
                blacklisted_symbols=self._load_scanner_blacklist_symbols(),
            )
            await self._sync_subscription_targets(
                market_data_symbols=summary["market_data_symbols"],
                schwab_stream_symbols=summary["schwab_stream_symbols"],
            )
            await self._publish_strategy_state_snapshot()
            self.logger.info(
                "snapshot batch processed | alerts=%s confirmed=%s",
                len(summary["alerts"]),
                len(summary["top_confirmed"]),
            )
            return

        if event_type == "trade_tick":
            event = TradeTickEvent.model_validate(payload)
            intents = self.state.handle_trade_tick(
                symbol=event.payload.symbol,
                price=float(event.payload.price),
                size=event.payload.size,
                timestamp_ns=event.payload.timestamp_ns,
                cumulative_volume=event.payload.cumulative_volume,
                exclude_codes=self.state.schwab_stream_strategy_codes(),
            )
            for intent in intents:
                await self._publish_intent(intent)
            if intents:
                await self._sync_subscription_targets()
                await self._publish_strategy_state_snapshot()
            if intents:
                self.logger.info(
                    "generated %s intents from %s trade tick",
                    len(intents),
                    event.payload.symbol,
                )
            return

        if event_type == "quote_tick":
            event = QuoteTickEvent.model_validate(payload)
            self.state.handle_quote_tick(
                symbol=event.payload.symbol,
                bid_price=float(event.payload.bid_price) if event.payload.bid_price is not None else None,
                ask_price=float(event.payload.ask_price) if event.payload.ask_price is not None else None,
                exclude_codes=self.state.schwab_stream_strategy_codes(),
            )
            return

        if event_type == "live_bar":
            event = LiveBarEvent.model_validate(payload)
            intents = self.state.handle_live_bar(
                symbol=event.payload.symbol,
                interval_secs=int(event.payload.interval_secs),
                open_price=float(event.payload.open),
                high_price=float(event.payload.high),
                low_price=float(event.payload.low),
                close_price=float(event.payload.close),
                volume=int(event.payload.volume),
                timestamp=float(event.payload.timestamp),
                trade_count=int(event.payload.trade_count),
                exclude_codes=self.state.schwab_stream_strategy_codes(),
            )
            for intent in intents:
                await self._publish_intent(intent)
            if intents:
                await self._sync_subscription_targets()
                await self._publish_strategy_state_snapshot()
                self.logger.info(
                    "generated %s intents from %s live bars",
                    len(intents),
                    event.payload.symbol,
                )
            return

        if event_type == "historical_bars":
            event = HistoricalBarsEvent.model_validate(payload)
            bars = [
                {
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume),
                    "timestamp": float(bar.timestamp),
                    "trade_count": int(bar.trade_count),
                }
                for bar in event.payload.bars
            ]
            hydrated = self.state.hydrate_historical_bars(
                symbol=event.payload.symbol,
                interval_secs=event.payload.interval_secs,
                bars=bars,
                exclude_codes=self.state.schwab_stream_strategy_codes(),
            )
            if hydrated:
                self.logger.info(
                    "hydrated %s bars for %s @ %ss into %s",
                    len(bars),
                    event.payload.symbol,
                    event.payload.interval_secs,
                    ",".join(hydrated),
                )
            return

        if event_type == "order_event":
            event = OrderEventEvent.model_validate(payload)
            order = event.payload
            level = order.metadata.get("level")

            self.state.apply_order_status(
                strategy_code=order.strategy_code,
                symbol=order.symbol,
                intent_type=order.intent_type,
                status=order.status,
                level=level,
                reason=order.reason,
            )

            if (
                order.status in {"filled", "partially_filled"}
                and order.fill_price is not None
                and order.filled_quantity > 0
            ):
                self.state.apply_execution_fill(
                    client_order_id=order.client_order_id,
                    strategy_code=order.strategy_code,
                    symbol=order.symbol,
                    intent_type=order.intent_type,
                    status=order.status,
                    side=order.side,
                    quantity=order.filled_quantity,
                    price=order.fill_price,
                    level=level,
                    path=order.metadata.get("path"),
                    reason=order.reason,
                )

            await self._sync_subscription_targets()
            await self._publish_strategy_state_snapshot()

    async def _publish_intent(self, intent: TradeIntentEvent) -> None:
        stream = stream_name(self.settings.redis_stream_prefix, "strategy-intents")
        await self.redis.xadd(
            stream,
            {"data": intent.model_dump_json()},
            maxlen=self.settings.redis_strategy_intent_stream_maxlen,
            approximate=True,
        )

    async def _publish_heartbeat(self, status: str) -> None:
        stream = stream_name(self.settings.redis_stream_prefix, "heartbeats")
        event = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=self.instance_name,
                status=status,
                details={
                    "watchlist_size": str(len(self.state.current_confirmed)),
                    "bot_count": str(len(self.state.bots)),
                    "schwab_stream_symbols": str(len(self.state.schwab_stream_symbols())),
                },
            ),
        )
        await self.redis.xadd(
            stream,
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )

    async def _publish_strategy_state_snapshot(self) -> None:
        stream = stream_name(self.settings.redis_stream_prefix, "strategy-state")
        summary = self.state.summary()
        bots = [
            StrategyBotStatePayload(
                strategy_code=str(bot["strategy"]),
                account_name=str(bot["account_name"]),
                watchlist=[str(symbol) for symbol in bot["watchlist"]],
                positions=list(bot["positions"]),
                pending_open_symbols=[str(symbol) for symbol in bot["pending_open_symbols"]],
                pending_close_symbols=[str(symbol) for symbol in bot["pending_close_symbols"]],
                pending_scale_levels=[str(level) for level in bot["pending_scale_levels"]],
                daily_pnl=float(bot.get("daily_pnl", 0) or 0),
                closed_today=list(bot.get("closed_today", [])),
                recent_decisions=list(bot.get("recent_decisions", [])),
                indicator_snapshots=list(bot.get("indicator_snapshots", [])),
            )
            for bot in summary["bots"].values()
        ]
        event = StrategyStateSnapshotEvent(
            source_service=SERVICE_NAME,
            payload=StrategyStateSnapshotPayload(
                all_confirmed=list(summary["all_confirmed"]),
                watchlist=[str(symbol) for symbol in summary["watchlist"]],
                top_confirmed=list(summary["top_confirmed"]),
                five_pillars=list(summary["five_pillars"]),
                top_gainers=list(summary["top_gainers"]),
                recent_alerts=list(summary["recent_alerts"]),
                top_gainer_changes=list(summary["top_gainer_changes"]),
                alert_warmup=dict(summary["alert_warmup"]),
                cycle_count=int(summary["cycle_count"]),
                bots=bots,
            ),
        )
        await self.redis.xadd(
            stream,
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_strategy_state_stream_maxlen,
            approximate=True,
        )
        self._persist_scanner_snapshots(summary)

    async def _sync_market_data_subscriptions(self, symbols: Sequence[str]) -> None:
        normalized = {symbol.upper() for symbol in symbols if symbol}
        if normalized == self._last_market_data_symbols:
            return

        self._last_market_data_symbols = normalized
        stream = stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions")
        event = MarketDataSubscriptionEvent(
            source_service=SERVICE_NAME,
            payload=MarketDataSubscriptionPayload(
                consumer_name=SERVICE_NAME,
                mode="replace",
                symbols=sorted(normalized),
            ),
        )
        await self.redis.xadd(
            stream,
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_market_data_subscription_stream_maxlen,
            approximate=True,
        )
        await self._hydrate_recent_historical_bars(normalized)

    async def _sync_schwab_stream_subscriptions(self, symbols: Sequence[str]) -> None:
        normalized = {symbol.upper() for symbol in symbols if symbol}
        if normalized == self._last_schwab_stream_symbols:
            return

        self._last_schwab_stream_symbols = normalized
        if self._schwab_tick_archive is not None:
            self._schwab_tick_archive.record_subscription_snapshot(sorted(normalized))
        if self._schwab_stream_client is None:
            return
        await self._schwab_stream_client.sync_subscriptions(sorted(normalized))

    async def _sync_subscription_targets(
        self,
        *,
        market_data_symbols: Sequence[str] | None = None,
        schwab_stream_symbols: Sequence[str] | None = None,
    ) -> None:
        await self._sync_market_data_subscriptions(
            self.state.market_data_symbols() if market_data_symbols is None else market_data_symbols
        )
        await self._sync_schwab_stream_subscriptions(
            self.state.schwab_stream_symbols()
            if schwab_stream_symbols is None
            else schwab_stream_symbols
        )

    async def _hydrate_recent_historical_bars(self, symbols: set[str]) -> None:
        if not symbols:
            return

        pending = self.state.market_data_hydration_pairs(sorted(symbols))
        if not pending:
            return

        stream = stream_name(self.settings.redis_stream_prefix, "market-data")
        hydrated_any = False

        for _attempt in range(self._historical_hydration_attempts):
            try:
                entries = await self.redis.xrevrange(stream, count=500)
            except Exception:
                self.logger.exception("historical bar hydration replay failed")
                return

            for _message_id, fields in entries:
                data = fields.get("data")
                if not data:
                    continue
                try:
                    payload = json.loads(data)
                except Exception:
                    continue
                if payload.get("event_type") != "historical_bars":
                    continue

                event = HistoricalBarsEvent.model_validate(payload)
                pair = (event.payload.symbol.upper(), int(event.payload.interval_secs))
                if pair not in pending:
                    continue

                bars = [
                    {
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": int(bar.volume),
                        "timestamp": float(bar.timestamp),
                        "trade_count": int(bar.trade_count),
                    }
                    for bar in event.payload.bars
                ]
                hydrated = self.state.hydrate_historical_bars(
                    symbol=event.payload.symbol,
                    interval_secs=event.payload.interval_secs,
                    bars=bars,
                )
                if hydrated:
                    hydrated_any = True
                    self.logger.info(
                        "replayed %s historical bars for %s @ %ss into %s",
                        len(bars),
                        event.payload.symbol,
                        event.payload.interval_secs,
                        ",".join(hydrated),
                    )
                pending.discard(pair)

            if not pending:
                break

            await asyncio.sleep(self._historical_hydration_poll_delay_secs)

        if hydrated_any:
            await self._publish_strategy_state_snapshot()

    def _build_schwab_stream_client(self) -> SchwabStreamerClient | None:
        if not self.state.schwab_stream_strategy_codes():
            return None
        return SchwabStreamerClient(self.settings)

    def _build_schwab_tick_archive(self) -> SchwabTickArchive | None:
        if not self.settings.schwab_tick_archive_enabled:
            return None
        return SchwabTickArchive(self.settings.schwab_tick_archive_root)

    def _enqueue_schwab_trade_tick(self, record: TradeTickRecord) -> None:
        self._schwab_trade_queue.put_nowait(record)

    def _enqueue_schwab_quote_tick(self, record: QuoteTickRecord) -> None:
        self._schwab_quote_queue.put_nowait(record)

    async def _drain_schwab_stream_queues(self) -> int:
        intent_count = 0

        while not self._schwab_quote_queue.empty():
            quote = await self._schwab_quote_queue.get()
            if self._schwab_tick_archive is not None:
                self._schwab_tick_archive.record_quote(quote)
            self.state.handle_quote_tick(
                symbol=quote.symbol,
                bid_price=quote.bid_price,
                ask_price=quote.ask_price,
                strategy_codes=self.state.schwab_stream_strategy_codes(),
            )

        while not self._schwab_trade_queue.empty():
            trade = await self._schwab_trade_queue.get()
            if self._schwab_tick_archive is not None:
                self._schwab_tick_archive.record_trade(trade)
            intents = self.state.handle_trade_tick(
                symbol=trade.symbol,
                price=trade.price,
                size=trade.size,
                timestamp_ns=trade.timestamp_ns,
                cumulative_volume=trade.cumulative_volume,
                strategy_codes=self.state.schwab_stream_strategy_codes(),
            )
            for intent in intents:
                await self._publish_intent(intent)
            intent_count += len(intents)
            if intents:
                self.logger.info(
                    "generated %s intents from %s Schwab trade tick",
                    len(intents),
                    trade.symbol,
                )

        return intent_count

    def _persist_scanner_snapshots(self, summary: dict[str, object]) -> None:
        if self.session_factory is None:
            return

        persisted_at = utcnow().isoformat()
        top_confirmed = list(summary.get("top_confirmed", []))
        all_confirmed_candidates = list(self.state.confirmed_scanner.get_all_confirmed())
        if top_confirmed or all_confirmed_candidates:
            payload = {
                "top_confirmed": top_confirmed,
                "all_confirmed_candidates": all_confirmed_candidates,
                "watchlist": list(summary.get("watchlist", [])),
                "cycle_count": int(summary.get("cycle_count", 0) or 0),
                "persisted_at": persisted_at,
            }
            self._replace_dashboard_snapshot("scanner_confirmed_last_nonempty", payload)

        alert_state = self.state.alert_engine.export_state()
        alert_state["cycle_count"] = int(summary.get("cycle_count", 0) or 0)
        alert_state["recent_alerts"] = list(self.state.recent_alerts[-100:])
        alert_state["top_gainer_changes"] = list(self.state.top_gainer_changes[-100:])
        alert_state["first_seen_by_ticker"] = dict(self.state._first_seen_by_ticker)
        self._replace_dashboard_snapshot("scanner_alert_engine_state", alert_state)

        history_payload = self._build_scanner_history_snapshot(summary, persisted_at=persisted_at)
        history_signature = json.dumps(history_payload, sort_keys=True, separators=(",", ":"))
        if history_signature != self._last_scanner_history_signature:
            self._append_dashboard_snapshot(
                "scanner_cycle_history",
                history_payload,
                retention_limit=max(0, int(self.settings.dashboard_scanner_history_retention or 0)),
            )
            self._last_scanner_history_signature = history_signature

    def _restore_alert_engine_state_from_dashboard_snapshot(self) -> None:
        if self.session_factory is None:
            return

        try:
            with self.session_factory() as session:
                snapshot = session.scalar(
                    select(DashboardSnapshot).where(
                        DashboardSnapshot.snapshot_type == "scanner_alert_engine_state"
                    )
                )
        except Exception:
            self.logger.exception("failed to load alert-engine warmup snapshot")
            return

        if snapshot is None or not isinstance(snapshot.payload, dict):
            return

        persisted_at_raw = snapshot.payload.get("persisted_at")
        if not isinstance(persisted_at_raw, str):
            self.logger.info("skipping alert-engine restore: persisted_at missing")
            return

        try:
            persisted_at = datetime.fromisoformat(persisted_at_raw)
        except ValueError:
            self.logger.info("skipping alert-engine restore: invalid persisted_at=%s", persisted_at_raw)
            return

        if persisted_at.tzinfo is None:
            persisted_at = persisted_at.replace(tzinfo=UTC)

        session_start = current_scanner_session_start_utc()
        if persisted_at.astimezone(UTC) < session_start:
            self.logger.info(
                "skipping alert-engine restore from prior session: persisted_at=%s session_start=%s",
                persisted_at.isoformat(),
                session_start.isoformat(),
            )
            return

        if self.state.alert_engine.restore_state(snapshot.payload):
            self.state.alert_warmup = self.state.alert_engine.get_warmup_status()
            restored_alerts = snapshot.payload.get("recent_alerts")
            if isinstance(restored_alerts, list):
                self.state.recent_alerts = [
                    {**item, "ticker": str(item.get("ticker", "")).upper()}
                    for item in restored_alerts[-100:]
                    if isinstance(item, dict)
                ]
                self.state._pending_recent_alert_replay = bool(self.state.recent_alerts)

            restored_changes = snapshot.payload.get("top_gainer_changes")
            if isinstance(restored_changes, list):
                self.state.top_gainer_changes = [
                    {**item, "ticker": str(item.get("ticker", "")).upper()}
                    for item in restored_changes[-100:]
                    if isinstance(item, dict)
                ]

            restored_first_seen = snapshot.payload.get("first_seen_by_ticker")
            if isinstance(restored_first_seen, dict):
                self.state._first_seen_by_ticker = {
                    str(ticker).upper(): str(first_seen)
                    for ticker, first_seen in restored_first_seen.items()
                    if str(ticker).strip() and first_seen is not None
                }
            self.logger.info(
                "restored momentum alert warmup from dashboard snapshot | history_cycles=%s",
                self.state.alert_warmup.get("history_cycles", 0),
            )

    def _seed_confirmed_candidates_from_dashboard_snapshot(self) -> None:
        if self.session_factory is None:
            return

        try:
            with self.session_factory() as session:
                snapshot = session.scalar(
                    select(DashboardSnapshot).where(
                        DashboardSnapshot.snapshot_type == "scanner_confirmed_last_nonempty"
                    )
                )
        except Exception:
            self.logger.exception("failed to load seeded confirmed candidates")
            return

        if snapshot is None or not isinstance(snapshot.payload, dict):
            return

        persisted_at_raw = snapshot.payload.get("persisted_at")
        if not isinstance(persisted_at_raw, str):
            self.logger.info("skipping confirmed-candidate seed: persisted_at missing")
            return

        try:
            persisted_at = datetime.fromisoformat(persisted_at_raw)
        except ValueError:
            self.logger.info("skipping confirmed-candidate seed: invalid persisted_at=%s", persisted_at_raw)
            return

        if persisted_at.tzinfo is None:
            persisted_at = persisted_at.replace(tzinfo=UTC)

        session_start = current_scanner_session_start_utc()
        if persisted_at.astimezone(UTC) < session_start:
            self.logger.info(
                "skipping confirmed-candidate seed from prior session: persisted_at=%s session_start=%s",
                persisted_at.isoformat(),
                session_start.isoformat(),
            )
            return

        seeded_candidates = snapshot.payload.get("all_confirmed_candidates")
        if not isinstance(seeded_candidates, list) or not seeded_candidates:
            seeded_candidates = snapshot.payload.get("top_confirmed", [])
        if not isinstance(seeded_candidates, list) or not seeded_candidates:
            return

        seeded = [dict(item) for item in seeded_candidates if isinstance(item, dict)]
        if not seeded:
            return

        self.state.seed_confirmed_candidates(seeded)
        self.state.all_confirmed = self.state.confirmed_scanner.get_ranked_confirmed(min_score=0)
        visible_confirmed = self.state.confirmed_scanner.get_top_n(min_change_pct=0)
        self.state.restore_confirmed_runtime_view(
            [dict(item) for item in visible_confirmed if isinstance(item, dict)]
        )
        self.logger.info("seeded %s confirmed candidates for fresh restart revalidation", len(seeded))

    def _restore_runtime_state_from_database(self) -> None:
        self._reconcile_runtime_state_from_database(log_when_changed=True)

    def _reconcile_runtime_state_from_database(self, *, log_when_changed: bool) -> bool:
        if self.session_factory is None:
            return False

        strategy_map = strategy_registration_map(self.settings)
        account_names = {registration.account_name for registration in strategy_map.values()}

        try:
            with self.session_factory() as session:
                strategies = {
                    strategy.id: strategy
                    for strategy in session.scalars(
                        select(Strategy).where(Strategy.code.in_(list(strategy_map.keys())))
                    ).all()
                }
                accounts = {
                    account.id: account
                    for account in session.scalars(
                        select(BrokerAccount).where(BrokerAccount.name.in_(list(account_names)))
                    ).all()
                }
                open_virtual_positions = session.scalars(
                    select(VirtualPosition).where(VirtualPosition.quantity > 0)
                ).all()
                open_orders = session.scalars(
                    select(BrokerOrder).where(
                        BrokerOrder.status.in_(("pending", "submitted", "accepted", "partially_filled"))
                    )
                ).all()
                intent_ids = [order.intent_id for order in open_orders if order.intent_id is not None]
                intents = {
                    intent.id: intent
                    for intent in session.scalars(
                        select(TradeIntent).where(TradeIntent.id.in_(intent_ids))
                    ).all()
                } if intent_ids else {}
        except Exception:
            self.logger.exception("failed to reconcile runtime state from database")
            return False

        expected_positions: dict[str, dict[str, tuple[int, float]]] = {
            code: {}
            for code in self.state.bots
        }
        expected_pending_open: dict[str, set[str]] = {
            code: set()
            for code in self.state.bots
        }
        expected_pending_close: dict[str, set[str]] = {
            code: set()
            for code in self.state.bots
        }
        expected_pending_scale: dict[str, set[tuple[str, str]]] = {
            code: set()
            for code in self.state.bots
        }

        for virtual_position in open_virtual_positions:
            strategy = strategies.get(virtual_position.strategy_id)
            account = accounts.get(virtual_position.broker_account_id)
            if strategy is None or account is None:
                continue
            registration = strategy_map.get(strategy.code)
            if registration is None or account.name != registration.account_name:
                continue

            runtime = self.state.bots.get(strategy.code)
            if runtime is None:
                continue

            symbol = str(virtual_position.symbol).upper()
            expected_positions.setdefault(strategy.code, {})[symbol] = (
                int(virtual_position.quantity),
                float(virtual_position.average_price),
            )

        for order in open_orders:
            strategy = strategies.get(order.strategy_id)
            account = accounts.get(order.broker_account_id)
            if strategy is None or account is None:
                continue
            registration = strategy_map.get(strategy.code)
            if registration is None or account.name != registration.account_name:
                continue

            runtime = self.state.bots.get(strategy.code)
            if runtime is None:
                continue

            intent = intents.get(order.intent_id) if order.intent_id is not None else None
            intent_type = str(intent.intent_type if intent is not None else "")
            symbol = str(order.symbol).upper()
            payload = order.payload if isinstance(order.payload, dict) else {}

            if intent_type == "open" and order.side == "buy":
                expected_pending_open.setdefault(strategy.code, set()).add(symbol)
                continue

            if order.side != "sell":
                continue

            if intent_type == "close":
                expected_pending_close.setdefault(strategy.code, set()).add(symbol)
                continue

            if intent_type == "scale":
                level = str(payload.get("level", "") or "")
                if level:
                    expected_pending_scale.setdefault(strategy.code, set()).add((symbol, level))

        restored_positions = 0
        cleared_positions = 0
        synced_pending = 0

        for code, runtime in self.state.bots.items():
            expected_runtime_positions = expected_positions.get(code, {})
            runtime_positions = self._runtime_positions_by_symbol(runtime)

            for symbol, (quantity, average_price) in expected_runtime_positions.items():
                runtime_position = runtime_positions.get(symbol)
                runtime_quantity = int(float(runtime_position.get("quantity", 0) or 0)) if runtime_position else 0
                runtime_average = (
                    float(runtime_position.get("entry_price", 0) or 0)
                    if runtime_position
                    else 0.0
                )
                if runtime_position is not None and runtime_quantity == quantity and abs(runtime_average - average_price) < 0.0001:
                    continue
                self._drop_runtime_position(runtime, symbol)
                self._restore_runtime_position(
                    runtime,
                    symbol=symbol,
                    quantity=quantity,
                    average_price=average_price,
                )
                restored_positions += 1

            for symbol in sorted(set(runtime_positions) - set(expected_runtime_positions)):
                self._drop_runtime_position(runtime, symbol)
                cleared_positions += 1

            current_pending_open = self._runtime_pending_open_symbols(runtime)
            current_pending_close = self._runtime_pending_close_symbols(runtime)
            current_pending_scale = self._runtime_pending_scale_levels(runtime)
            desired_pending_open = expected_pending_open.get(code, set())
            desired_pending_close = expected_pending_close.get(code, set())
            desired_pending_scale = expected_pending_scale.get(code, set())
            if (
                current_pending_open != desired_pending_open
                or current_pending_close != desired_pending_close
                or current_pending_scale != desired_pending_scale
            ):
                self._set_runtime_pending_state(
                    runtime,
                    pending_open=desired_pending_open,
                    pending_close=desired_pending_close,
                    pending_scale=desired_pending_scale,
                )
                synced_pending += 1

        changed = bool(restored_positions or cleared_positions or synced_pending)
        if changed and log_when_changed:
            self.logger.info(
                "reconciled runtime state from database | restored_positions=%s cleared_positions=%s pending_syncs=%s",
                restored_positions,
                cleared_positions,
                synced_pending,
            )
        return changed

    @staticmethod
    def _runtime_positions_by_symbol(runtime: StrategyRuntime) -> dict[str, dict[str, object]]:
        positions = getattr(runtime, "summary", lambda: {"positions": []})().get("positions", [])
        return {
            str(item.get("ticker", "")).upper(): dict(item)
            for item in positions
            if isinstance(item, dict) and str(item.get("ticker", "")).strip()
        }

    @staticmethod
    def _runtime_pending_open_symbols(runtime: StrategyRuntime) -> set[str]:
        value = getattr(runtime, "pending_open_symbols", None)
        if value is None:
            value = getattr(runtime, "_pending_open_symbols", set())
        return {str(symbol).upper() for symbol in value}

    @staticmethod
    def _runtime_pending_close_symbols(runtime: StrategyRuntime) -> set[str]:
        value = getattr(runtime, "pending_close_symbols", None)
        if value is None:
            value = getattr(runtime, "_pending_close_symbols", set())
        return {str(symbol).upper() for symbol in value}

    @staticmethod
    def _runtime_pending_scale_levels(runtime: StrategyRuntime) -> set[tuple[str, str]]:
        value = getattr(runtime, "pending_scale_levels", None)
        if value is None:
            return set()
        normalized: set[tuple[str, str]] = set()
        for item in value:
            if isinstance(item, tuple) and len(item) == 2:
                normalized.add((str(item[0]).upper(), str(item[1]).upper()))
                continue
            text = str(item)
            if ":" in text:
                symbol, level = text.split(":", 1)
                normalized.add((symbol.upper(), level.upper()))
        return normalized

    def _restore_runtime_position(
        self,
        runtime: StrategyRuntime,
        *,
        symbol: str,
        quantity: int,
        average_price: float,
    ) -> None:
        restore_position = getattr(runtime, "restore_position", None)
        if restore_position is None:
            return
        restore_position(
            symbol=symbol,
            quantity=quantity,
            average_price=average_price,
            path="DB_RECONCILE",
        )

    def _drop_runtime_position(self, runtime: StrategyRuntime, symbol: str) -> None:
        normalized = symbol.upper()
        position_tracker = getattr(runtime, "positions", None)
        if position_tracker is not None:
            position_tracker.drop_position(normalized)
        elif hasattr(runtime, "_positions"):
            runtime._positions.pop(normalized, None)  # type: ignore[attr-defined]

        if hasattr(runtime, "pending_close_symbols"):
            runtime.pending_close_symbols.discard(normalized)  # type: ignore[attr-defined]
        if hasattr(runtime, "pending_open_symbols"):
            runtime.pending_open_symbols.discard(normalized)  # type: ignore[attr-defined]
        if hasattr(runtime, "pending_scale_levels"):
            runtime.pending_scale_levels = {  # type: ignore[attr-defined]
                item
                for item in runtime.pending_scale_levels  # type: ignore[attr-defined]
                if item[0] != normalized
            }
        if hasattr(runtime, "_pending_close_symbols"):
            runtime._pending_close_symbols.discard(normalized)  # type: ignore[attr-defined]
        if hasattr(runtime, "_pending_open_symbols"):
            runtime._pending_open_symbols.discard(normalized)  # type: ignore[attr-defined]
        if hasattr(runtime, "_pending_close_reasons"):
            runtime._pending_close_reasons.pop(normalized, None)  # type: ignore[attr-defined]
        if hasattr(runtime, "_close_retry_blocked_until"):
            blocked_until = runtime._close_retry_blocked_until  # type: ignore[attr-defined]
            if blocked_until is not None:
                blocked_until.pop(normalized, None)

        if hasattr(runtime, "entry_engine") and hasattr(runtime, "builder_manager"):
            try:
                bar_index = runtime.builder_manager.get_or_create(normalized).get_bar_count()  # type: ignore[attr-defined]
                runtime.entry_engine.record_exit(normalized, bar_index)  # type: ignore[attr-defined]
            except Exception:
                pass

    @staticmethod
    def _set_runtime_pending_state(
        runtime: StrategyRuntime,
        *,
        pending_open: set[str],
        pending_close: set[str],
        pending_scale: set[tuple[str, str]],
    ) -> None:
        if hasattr(runtime, "pending_open_symbols"):
            runtime.pending_open_symbols = set(pending_open)  # type: ignore[attr-defined]
        if hasattr(runtime, "_pending_open_symbols"):
            runtime._pending_open_symbols = set(pending_open)  # type: ignore[attr-defined]
        if hasattr(runtime, "pending_close_symbols"):
            runtime.pending_close_symbols = set(pending_close)  # type: ignore[attr-defined]
        if hasattr(runtime, "_pending_close_symbols"):
            runtime._pending_close_symbols = set(pending_close)  # type: ignore[attr-defined]
        if hasattr(runtime, "pending_scale_levels"):
            runtime.pending_scale_levels = set(pending_scale)  # type: ignore[attr-defined]

    def _replace_dashboard_snapshot(self, snapshot_type: str, payload: dict[str, object]) -> None:
        if self.session_factory is None:
            return

        safe_payload = json.loads(json.dumps(payload, default=str))
        try:
            with self.session_factory() as session:
                session.execute(
                    delete(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == snapshot_type)
                )
                session.add(
                    DashboardSnapshot(
                        snapshot_type=snapshot_type,
                        payload=safe_payload,
                    )
                )
                session.commit()
        except Exception:
            self.logger.exception("failed to persist dashboard snapshot %s", snapshot_type)

    def _append_dashboard_snapshot(
        self,
        snapshot_type: str,
        payload: dict[str, object],
        *,
        retention_limit: int,
    ) -> None:
        if self.session_factory is None:
            return

        safe_payload = json.loads(json.dumps(payload, default=str))
        try:
            with self.session_factory() as session:
                session.add(
                    DashboardSnapshot(
                        snapshot_type=snapshot_type,
                        payload=safe_payload,
                    )
                )
                session.flush()
                if retention_limit > 0:
                    stale_ids = list(
                        session.scalars(
                            select(DashboardSnapshot.id)
                            .where(DashboardSnapshot.snapshot_type == snapshot_type)
                            .order_by(desc(DashboardSnapshot.created_at), desc(DashboardSnapshot.id))
                            .offset(retention_limit)
                        )
                    )
                    if stale_ids:
                        session.execute(
                            delete(DashboardSnapshot).where(DashboardSnapshot.id.in_(stale_ids))
                        )
                session.commit()
        except Exception:
            self.logger.exception("failed to append dashboard snapshot %s", snapshot_type)

    def _build_scanner_history_snapshot(
        self,
        summary: dict[str, object],
        *,
        persisted_at: str,
    ) -> dict[str, object]:
        def _reduce_confirmed_rows(items: Sequence[dict[str, object]]) -> list[dict[str, object]]:
            return [
                {
                    "ticker": str(item.get("ticker", "")).upper(),
                    "confirmed_at": str(item.get("confirmed_at", "")),
                    "confirmation_path": str(item.get("confirmation_path", "")),
                    "rank_score": float(item.get("rank_score", 0) or 0),
                    "price": float(item.get("price", 0) or 0),
                    "change_pct": float(item.get("change_pct", 0) or 0),
                    "volume": float(item.get("volume", 0) or 0),
                    "rvol": float(item.get("rvol", 0) or 0),
                }
                for item in items
                if isinstance(item, dict) and str(item.get("ticker", "")).strip()
            ]

        def _reduce_scanner_rows(items: Sequence[dict[str, object]]) -> list[dict[str, object]]:
            return [
                {
                    "ticker": str(item.get("ticker", "")).upper(),
                    "price": float(item.get("price", 0) or 0),
                    "change_pct": float(item.get("change_pct", 0) or 0),
                    "volume": float(item.get("volume", 0) or 0),
                    "rvol": float(item.get("rvol", 0) or 0),
                    "shares_outstanding": float(item.get("shares_outstanding", 0) or 0),
                    "data_age_secs": int(item.get("data_age_secs", 0) or 0),
                }
                for item in items
                if isinstance(item, dict) and str(item.get("ticker", "")).strip()
            ]

        all_confirmed = _reduce_confirmed_rows(
            [
                item
                for item in self.state.confirmed_scanner.get_all_confirmed()
                if isinstance(item, dict)
            ]
        )
        top_confirmed = _reduce_confirmed_rows(
            [item for item in summary.get("top_confirmed", []) if isinstance(item, dict)]
        )
        five_pillars = _reduce_scanner_rows(
            [item for item in summary.get("five_pillars", []) if isinstance(item, dict)]
        )
        top_gainers = _reduce_scanner_rows(
            [item for item in summary.get("top_gainers", []) if isinstance(item, dict)]
        )

        return {
            "persisted_at": persisted_at,
            "scanner_session_start_utc": current_scanner_session_start_utc().isoformat(),
            "cycle_count": int(summary.get("cycle_count", 0) or 0),
            "watchlist": [str(symbol).upper() for symbol in summary.get("watchlist", []) if str(symbol).strip()],
            "all_confirmed": all_confirmed,
            "all_confirmed_tickers": [item["ticker"] for item in all_confirmed],
            "top_confirmed": top_confirmed,
            "top_confirmed_tickers": [item["ticker"] for item in top_confirmed],
            "five_pillars": five_pillars,
            "five_pillars_tickers": [item["ticker"] for item in five_pillars],
            "top_gainers": top_gainers,
            "top_gainers_tickers": [item["ticker"] for item in top_gainers],
        }

    def _load_scanner_blacklist_symbols(self) -> set[str]:
        if self.session_factory is None:
            return set()

        try:
            with self.session_factory() as session:
                return {
                    str(entry.symbol).upper()
                    for entry in session.scalars(
                        select(ScannerBlacklistEntry).order_by(ScannerBlacklistEntry.symbol)
                    ).all()
                }
        except Exception:
            self.logger.exception("failed to load scanner blacklist entries")
            return set()


def snapshot_from_payload(payload: MarketSnapshotPayload) -> MarketSnapshot:
    return MarketSnapshot(
        ticker=payload.symbol,
        previous_close=float(payload.previous_close) if payload.previous_close is not None else None,
        day=DaySnapshot(
            close=float(payload.day_close) if payload.day_close is not None else None,
            volume=payload.day_volume,
            high=float(payload.day_high) if payload.day_high is not None else None,
            vwap=float(payload.day_vwap) if payload.day_vwap is not None else None,
        ),
        minute=MinuteSnapshot(
            close=float(payload.minute_close) if payload.minute_close is not None else None,
            accumulated_volume=payload.minute_accumulated_volume,
            high=float(payload.minute_high) if payload.minute_high is not None else None,
            vwap=float(payload.minute_vwap) if payload.minute_vwap is not None else None,
        ),
        last_trade=LastTrade(
            price=float(payload.last_trade_price) if payload.last_trade_price is not None else None,
            timestamp_ns=payload.last_trade_timestamp_ns,
        ),
        last_quote=QuoteSnapshot(
            bid_price=float(payload.bid_price) if payload.bid_price is not None else None,
            ask_price=float(payload.ask_price) if payload.ask_price is not None else None,
            bid_size=payload.bid_size,
            ask_size=payload.ask_size,
        ),
        todays_change_percent=float(payload.todays_change_percent) if payload.todays_change_percent is not None else None,
        updated_ns=payload.updated_ns,
    )
