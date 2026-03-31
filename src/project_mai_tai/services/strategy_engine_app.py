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
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import DashboardSnapshot, ScannerBlacklistEntry
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    HistoricalBarsEvent,
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    MarketSnapshotPayload,
    OrderEventEvent,
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
from project_mai_tai.strategy_core import (
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
    TopGainersConfig,
    TopGainersTracker,
    TradingConfig,
    apply_five_pillars,
)
from project_mai_tai.strategy_core.time_utils import today_eastern_str
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
    ):
        self.definition = definition
        self.builder_manager = BarBuilderManager(interval_secs=definition.interval_secs)
        self.indicator_engine = IndicatorEngine(definition.indicator_config)
        self.entry_engine = EntryEngine(
            definition.trading_config,
            name=definition.display_name,
            now_provider=now_provider,
        )
        self.exit_engine = ExitEngine(definition.trading_config)
        self.positions = PositionTracker(
            definition.trading_config,
            positions_file=self._positions_file_for_strategy(definition.code),
            closed_file_prefix=self._closed_trade_prefix_for_strategy(definition.code),
        )
        self.positions.load_closed_trades()
        self._active_day = today_eastern_str()
        self.watchlist: set[str] = set()
        self.last_indicators: dict[str, dict[str, float | bool]] = {}
        self.latest_quotes: dict[str, dict[str, float]] = {}
        self.pending_open_symbols: set[str] = set()
        self.pending_close_symbols: set[str] = set()
        self.pending_scale_levels: set[tuple[str, str]] = set()
        self.exit_retry_blocked_until: dict[str, datetime] = {}
        self.scale_retry_blocked_until: dict[tuple[str, str], datetime] = {}
        self._applied_fill_quantity_by_order: dict[str, Decimal] = {}
        self.recent_decisions: list[dict[str, str]] = []

    @staticmethod
    def _positions_file_for_strategy(strategy_code: str) -> str:
        return f"data/cache/positions_{strategy_code}.json"

    @staticmethod
    def _closed_trade_prefix_for_strategy(strategy_code: str) -> str:
        if strategy_code == "macd_30s":
            return "macdbot"
        return strategy_code

    def set_watchlist(self, symbols: Iterable[str]) -> None:
        self.watchlist = set(symbols)

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

    def handle_trade_tick(
        self,
        symbol: str,
        price: float,
        size: int,
        timestamp_ns: int | None = None,
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

        if symbol not in self.watchlist and position is None:
            return intents

        completed_bars = self.builder_manager.on_trade(symbol, price, size, timestamp_ns or 0)
        for _bar in completed_bars:
            intents.extend(self._evaluate_completed_bar(symbol))

        return intents

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
                self.positions.close_position(symbol, fill_price, reason="OMS_FILL")
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
        current_day = today_eastern_str()
        if current_day == self._active_day:
            return
        self.positions.reset()
        self.positions.load_closed_trades()
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
        bars = self.builder_manager.get_bars(symbol)
        if not bars:
            return []

        indicators = self.indicator_engine.calculate(bars)
        if indicators is None:
            return []

        self.last_indicators[symbol] = indicators
        intents: list[TradeIntentEvent] = []

        position = self.positions.get_position(symbol)
        if position is not None:
            position.increment_bars()
            exit_signal = self.exit_engine.check_exit(position, indicators)
            if exit_signal:
                if exit_signal["action"] == "SCALE":
                    level = str(exit_signal["level"])
                    if (symbol, level) not in self.pending_scale_levels and not self._is_scale_retry_blocked(symbol, level):
                        intents.append(self._emit_scale_intent(exit_signal))
                elif symbol not in self.pending_close_symbols and not self._is_exit_retry_blocked(symbol):
                    intents.append(self._emit_close_intent(exit_signal))
            return intents

        if symbol in self.pending_open_symbols:
            return []

        can_open, _reason = self.positions.can_open_position()
        if not can_open:
            self._record_decision(
                symbol=symbol,
                status="blocked",
                reason=str(_reason),
                indicators=indicators,
            )
            return []

        signal = self.entry_engine.check_entry(symbol, indicators, len(bars), self)
        self._capture_entry_decision(symbol, indicators)
        if signal is None:
            return []

        intents.append(self._emit_open_intent(signal))
        return intents

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
                    "last_bar_at": datetime.fromtimestamp(last_bar.timestamp, UTC).isoformat(),
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
        }
        metadata.update(order_routing_metadata(price=routed_price, side="buy"))
        return TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=self.definition.code,
                broker_account_name=self.definition.account_name,
                symbol=symbol,
                side="buy",
                quantity=Decimal(str(self.definition.trading_config.default_quantity)),
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

    def _capture_entry_decision(self, symbol: str, indicators: dict[str, float | bool]) -> None:
        decision = self.entry_engine.pop_last_decision(symbol)
        if decision is None:
            return
        self._record_decision(
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
    ) -> None:
        builder = self.builder_manager.get_builder(symbol)
        bar_time = ""
        if builder is not None and builder.bars:
            last_bar = builder.bars[-1]
            bar_time = datetime.fromtimestamp(last_bar.timestamp, UTC).astimezone().isoformat()
        entry = {
            "symbol": symbol,
            "status": status,
            "reason": reason,
            "path": path,
            "score": score,
            "score_details": score_details,
            "price": f'{float(indicators.get("price", 0) or 0):.4f}',
            "last_bar_at": bar_time,
        }
        self.recent_decisions.insert(0, entry)
        self.recent_decisions = self.recent_decisions[:50]


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
    ):
        self.settings = settings or get_settings()
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
        self.five_pillars: list[dict[str, object]] = []
        self.top_gainers: list[dict[str, object]] = []
        self.top_gainer_changes: list[dict[str, object]] = []
        self.recent_alerts: list[dict[str, object]] = []
        self.alert_warmup: dict[str, object] = self.alert_engine.get_warmup_status()
        self.cycle_count = 0
        self.latest_snapshots: dict[str, MarketSnapshot] = {}
        self._first_seen_by_ticker: dict[str, str] = {}
        self._seeded_confirmed_pending_revalidation = False
        registrations = strategy_registration_map(self.settings)

        base_trading = base_trading_config or TradingConfig()
        default_indicator_config = indicator_config or IndicatorConfig()
        runner_trading = base_trading.make_tos_variant(quantity=100, bar_interval_secs=60)
        self.bots: dict[str, StrategyRuntime] = {
            "macd_30s": StrategyBotRuntime(
                StrategyDefinition(
                    code="macd_30s",
                    display_name=registrations["macd_30s"].display_name,
                    account_name=registrations["macd_30s"].account_name,
                    interval_secs=30,
                    trading_config=base_trading,
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
            ),
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
            ),
            "tos": StrategyBotRuntime(
                StrategyDefinition(
                    code="tos",
                    display_name=registrations["tos"].display_name,
                    account_name=registrations["tos"].account_name,
                    interval_secs=60,
                    trading_config=base_trading.make_tos_variant(),
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
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

    def process_snapshot_batch(
        self,
        snapshots: Sequence[MarketSnapshot],
        reference_data: dict[str, ReferenceData],
        *,
        blacklisted_symbols: set[str] | None = None,
    ) -> dict[str, object]:
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
        self.latest_snapshots.update({snapshot.ticker.upper(): snapshot for snapshot in filtered_snapshots})
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
        newly_confirmed = self.confirmed_scanner.process_alerts(
            alerts,
            filtered_reference_data,
            snapshot_lookup,
        )
        self.confirmed_scanner.update_live_prices(snapshot_lookup)
        self.confirmed_scanner.prune_faded_candidates()

        self.current_confirmed = [
            stock
            for stock in self.confirmed_scanner.get_top_n()
            if str(stock.get("ticker", "")).upper() not in blocked
        ]

        watchlist = [str(stock["ticker"]) for stock in self.current_confirmed]
        for code, bot in self.bots.items():
            if code == "runner":
                bot.update_market_snapshots(filtered_snapshots)
                bot.set_watchlist(watchlist)
                bot.update_candidates(self.current_confirmed)
                continue
            bot.update_market_snapshots(filtered_snapshots)
            bot.set_watchlist(watchlist)

        return {
            "alerts": alerts,
            "newly_confirmed": newly_confirmed,
            "top_confirmed": self.current_confirmed,
            "five_pillars": self.five_pillars,
            "top_gainers": self.top_gainers,
            "recent_alerts": self.recent_alerts,
            "watchlist": watchlist,
            "market_data_symbols": self.market_data_symbols(),
        }

    def handle_trade_tick(
        self,
        symbol: str,
        price: float,
        size: int,
        timestamp_ns: int | None = None,
    ) -> list[TradeIntentEvent]:
        intents: list[TradeIntentEvent] = []
        for bot in self.bots.values():
            intents.extend(bot.handle_trade_tick(symbol, price, size, timestamp_ns))
        return intents

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
    ) -> list[str]:
        hydrated: list[str] = []
        for code, bot in self.bots.items():
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

    def market_data_symbols(self) -> list[str]:
        symbols: set[str] = set()
        for bot in self.bots.values():
            symbols.update(bot.active_symbols())
        return sorted(symbols)

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
        self.session_factory = (
            session_factory
            if session_factory is not None
            else build_session_factory(self.settings)
            if self.settings.dashboard_snapshot_persistence_enabled
            else None
        )
        self.state = StrategyEngineState(self.settings)
        self.logger = configure_logging(SERVICE_NAME, self.settings.log_level)
        self.instance_name = socket.gethostname()
        self._stream_offsets = {
            stream_name(self.settings.redis_stream_prefix, "market-data"): "$",
            stream_name(self.settings.redis_stream_prefix, "order-events"): "$",
            stream_name(self.settings.redis_stream_prefix, "snapshot-batches"): "$",
        }
        self._last_market_data_symbols: set[str] = set()
        self._historical_hydration_attempts = 5
        self._historical_hydration_poll_delay_secs = 0.2

    async def run(self) -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        heartbeat_interval_secs = max(1, self.settings.service_heartbeat_interval_seconds)
        last_heartbeat_at = utcnow()

        self.logger.info("%s starting", SERVICE_NAME)
        self._seed_confirmed_candidates_from_dashboard_snapshot()
        await self._prefill_alert_history_from_snapshot_batches()
        await self._sync_market_data_subscriptions(self.state.market_data_symbols())
        await self._publish_strategy_state_snapshot()
        await self._publish_heartbeat("starting")

        while not stop_event.is_set():
            try:
                messages = await self.redis.xread(
                    self._stream_offsets,
                    block=heartbeat_interval_secs * 1000,
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

            if (utcnow() - last_heartbeat_at).total_seconds() >= heartbeat_interval_secs:
                await self._publish_heartbeat("healthy")
                last_heartbeat_at = utcnow()

        await self._publish_heartbeat("stopping")
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

        if not history_batches:
            return

        self.state.alert_engine.prefill_history(history_batches)
        self.state.alert_warmup = self.state.alert_engine.get_warmup_status()
        self.logger.info(
            "prefilled momentum alert history from %s snapshot batches",
            len(history_batches),
        )

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
            await self._sync_market_data_subscriptions(summary["market_data_symbols"])
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
            )
            for intent in intents:
                await self._publish_intent(intent)
            if intents:
                await self._sync_market_data_subscriptions(self.state.market_data_symbols())
                await self._publish_strategy_state_snapshot()
            if intents:
                self.logger.info(
                    "generated %s intents from %s trade tick",
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
                )

            await self._sync_market_data_subscriptions(self.state.market_data_symbols())
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

    async def _hydrate_recent_historical_bars(self, symbols: set[str]) -> None:
        if not symbols:
            return

        target_intervals = {
            int(bot.definition.interval_secs)
            for bot in self.state.bots.values()
            if hasattr(bot, "definition")
        }
        if not target_intervals:
            return

        pending = {(symbol, interval) for symbol in symbols for interval in target_intervals}
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

    def _persist_scanner_snapshots(self, summary: dict[str, object]) -> None:
        if self.session_factory is None:
            return

        top_confirmed = list(summary.get("top_confirmed", []))
        all_confirmed_candidates = list(self.state.confirmed_scanner.get_all_confirmed())
        if not top_confirmed and not all_confirmed_candidates:
            return

        payload = {
            "top_confirmed": top_confirmed,
            "all_confirmed_candidates": all_confirmed_candidates,
            "watchlist": list(summary.get("watchlist", [])),
            "cycle_count": int(summary.get("cycle_count", 0) or 0),
            "persisted_at": utcnow().isoformat(),
        }
        self._replace_dashboard_snapshot("scanner_confirmed_last_nonempty", payload)

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
        self.logger.info("seeded %s confirmed candidates for fresh restart revalidation", len(seeded))

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
