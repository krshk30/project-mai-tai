from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import time_ns
from uuid import UUID
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import delete, desc, not_, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    DashboardSnapshot,
    ScannerBlacklistEntry,
    Strategy,
    StrategyBarHistory,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    HistoricalBarsEvent,
    LiveBarEvent,
    ManualStopUpdateEvent,
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
from project_mai_tai.market_data.models import LiveBarRecord, QuoteTickRecord, TradeTickRecord
from project_mai_tai.market_data.massive_indicator_provider import MassiveIndicatorProvider
from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider
from project_mai_tai.market_data.schwab_tick_archive import (
    SchwabTickArchive,
    load_aggregated_trade_bars,
    load_recorded_trades,
    load_recorded_live_bars,
)
from project_mai_tai.market_data.schwab_streamer import SchwabStreamerClient
from project_mai_tai.market_data.taapi_indicator_provider import TaapiIndicatorProvider
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.strategy_core import (
    CatalystAiConfig,
    CatalystAiEvaluator,
    CatalystConfig,
    CatalystEngine,
    DaySnapshot,
    EntryEngine,
    ExitEngine,
    FeedRetentionConfig,
    FeedRetentionMetrics,
    FeedRetentionPolicy,
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
    Polygon30sBarBuilderManager,
    Polygon30sEntryEngine,
    Polygon30sIndicatorEngine,
    QuoteSnapshot,
    ReferenceData,
    RetainedSymbolState,
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
from project_mai_tai.strategy_core.order_routing import (
    _format_limit_price,
    extended_hours_session,
    order_routing_metadata,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "strategy-engine"
EASTERN_TZ = ZoneInfo("America/New_York")
LEGACY_STRATEGY_CODE_ALIASES = {
    "webull_30s": "polygon_30s",
}


def normalize_strategy_code(strategy_code: str | None) -> str:
    normalized = str(strategy_code or "").strip().lower()
    return LEGACY_STRATEGY_CODE_ALIASES.get(normalized, normalized)


def normalize_strategy_code_map(
    values: dict[str, Sequence[object]] | dict[str, set[str]] | None,
) -> dict[str, Sequence[object]] | dict[str, set[str]]:
    normalized: dict[str, Sequence[object]] | dict[str, set[str]] = {}
    for code, items in (values or {}).items():
        normalized[normalize_strategy_code(code)] = items
    return normalized


def strategy_code_candidates(strategy_code: str | None) -> tuple[str, ...]:
    normalized = normalize_strategy_code(strategy_code)
    if normalized == "polygon_30s":
        return ("polygon_30s", "webull_30s")
    if not normalized:
        return tuple()
    return (normalized,)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _panic_limit_price(value: float | str | Decimal | None, buffer_pct: float) -> str | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value))
        if price <= 0:
            return None
        buffered = price * (Decimal("1") - (Decimal(str(buffer_pct)) / Decimal("100")))
        return format(max(buffered, Decimal("0.01")).quantize(Decimal("0.01")), "f")
    except Exception:
        return None


def _coerce_float(*values: object) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def stop_guard_order_routing_metadata(
    *,
    price: str,
    price_source: str,
    now: datetime | None = None,
) -> dict[str, str]:
    metadata = {
        "order_type": "limit",
        "time_in_force": "day",
        "limit_price": price,
        "reference_price": price,
        "price_source": price_source,
    }
    session = extended_hours_session(now)
    if session is None:
        return metadata
    metadata.update(
        {
            "session": session,
            "extended_hours": "true",
        }
    )
    return metadata


def current_scanner_session_start_utc(now: datetime | None = None) -> datetime:
    current = now or utcnow()
    current_et = current.astimezone(EASTERN_TZ)
    session_start_et = current_et.replace(hour=4, minute=0, second=0, microsecond=0)
    if current_et < session_start_et:
        session_start_et -= timedelta(days=1)
    return session_start_et.astimezone(UTC)


def _datetime_str(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET")


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
        trade_tick_service: str = "LEVELONE_EQUITIES",
        live_aggregate_fallback_enabled: bool = True,
        live_aggregate_bars_are_final: bool = False,
        live_aggregate_stale_after_seconds: int = 3,
        indicator_overlay_provider: MassiveIndicatorProvider | TaapiIndicatorProvider | None = None,
        extended_hours_vwap_provider: Callable[[str, Sequence[float], int], dict[float, float]] | None = None,
        builder_manager: BarBuilderManager | SchwabNativeBarBuilderManager | Polygon30sBarBuilderManager | None = None,
        indicator_engine: IndicatorEngine | SchwabNativeIndicatorEngine | Polygon30sIndicatorEngine | None = None,
        entry_engine: EntryEngine | SchwabNativeEntryEngine | Polygon30sEntryEngine | None = None,
        retention_config: FeedRetentionConfig | None = None,
        persist_offload_enabled: bool = False,
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
        self.prewarm_symbols: set[str] = set()
        self.last_indicators: dict[str, dict[str, object]] = {}
        self.latest_quotes: dict[str, dict[str, float]] = {}
        self._last_quote_received_at: dict[str, datetime] = {}
        self.entry_blocked_symbols: set[str] = set()
        self.lifecycle_policy = FeedRetentionPolicy(retention_config or FeedRetentionConfig())
        self.lifecycle_states: dict[str, RetainedSymbolState] = {}
        self._desired_watchlist_symbols: set[str] = set()
        self.manual_stop_symbols: set[str] = set()
        self.broker_blocked_symbols: set[str] = set()
        self.pending_open_symbols: set[str] = set()
        self.pending_close_symbols: set[str] = set()
        self.pending_scale_levels: set[tuple[str, str]] = set()
        self.exit_retry_blocked_until: dict[str, datetime] = {}
        self.scale_retry_blocked_until: dict[tuple[str, str], datetime] = {}
        self._applied_fill_quantity_by_order: dict[str, Decimal] = {}
        self.recent_decisions: list[dict[str, str]] = []
        self._last_tick_at: dict[str, datetime] = {}
        self.data_halt_symbols: dict[str, str] = {}
        self.data_halt_since: dict[str, datetime] = {}
        self.data_warning_symbols: dict[str, str] = {}
        self.data_warning_since: dict[str, datetime] = {}
        self._gap_recovery_bars_remaining: dict[str, int] = {}
        self._gap_recovery_synthetic_bars: dict[str, int] = {}
        self.session_factory = session_factory
        self.use_live_aggregate_bars = use_live_aggregate_bars
        self.trade_tick_service = str(trade_tick_service or "LEVELONE_EQUITIES").strip().upper() or "LEVELONE_EQUITIES"
        self.live_aggregate_fallback_enabled = live_aggregate_fallback_enabled
        self.live_aggregate_bars_are_final = live_aggregate_bars_are_final
        self.live_aggregate_stale_after_seconds = max(0, int(live_aggregate_stale_after_seconds))
        self.indicator_overlay_provider = indicator_overlay_provider
        self.extended_hours_vwap_provider = extended_hours_vwap_provider
        self._last_live_bar_received_at: dict[str, datetime] = {}
        self._recent_live_bar_delivery: dict[str, dict[float, tuple[datetime | None, datetime | None, str]]] = {}
        self._live_aggregate_skipped_bucket_start: dict[str, float] = {}
        self._live_aggregate_trade_tick_counts: dict[str, dict[float, int]] = {}
        self._history_seed_attempted: set[str] = set()
        # Batched persist offload (flag-gated, default off). When enabled, the
        # two persist methods capture a fully-resolved payload at call time and
        # append it here instead of touching the DB; flush_pending_persists()
        # runs the buffered SELECT+upsert+commit off the event loop.
        self._persist_offload_enabled = bool(persist_offload_enabled)
        self._pending_persist_writes: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def _positions_file_for_strategy(strategy_code: str) -> str:
        return f"data/cache/positions_{strategy_code}.json"

    @staticmethod
    def _closed_trade_prefix_for_strategy(strategy_code: str) -> str:
        if strategy_code == "macd_30s":
            return "macdbot"
        return strategy_code

    def set_watchlist(self, symbols: Iterable[str]) -> None:
        desired_symbols = {
            str(symbol).upper()
            for symbol in symbols
            if str(symbol).strip() and str(symbol).upper() not in self.manual_stop_symbols
        }
        self._desired_watchlist_symbols = set(desired_symbols)
        if not self.lifecycle_policy.config.enabled:
            self.watchlist = set(desired_symbols)
            self.lifecycle_states.clear()
            self.entry_blocked_symbols = set(self.manual_stop_symbols)
            self._prune_runtime_state()
            return
        now = self.now_provider()
        for symbol in desired_symbols:
            state = self.lifecycle_states.get(symbol)
            if state is None:
                self.lifecycle_states[symbol] = self.lifecycle_policy.promote(symbol, now, None)
        self._sync_watchlist_from_lifecycle()
        self._prune_runtime_state()

    def discard_watchlist_symbols(self, symbols: Iterable[str]) -> None:
        discard_symbols = {
            str(symbol).upper()
            for symbol in symbols
            if str(symbol).strip()
        }
        if not discard_symbols:
            return

        self._desired_watchlist_symbols.difference_update(discard_symbols)
        self.prewarm_symbols.difference_update(discard_symbols)
        unprotected_symbols = {
            symbol
            for symbol in discard_symbols
            if not self._symbol_requires_feed(symbol)
        }
        if not self.lifecycle_policy.config.enabled:
            self.watchlist.difference_update(unprotected_symbols)
            self.entry_blocked_symbols = self._blocked_lifecycle_symbols()
            self._prune_runtime_state()
            return

        for symbol in unprotected_symbols:
            self.lifecycle_states.pop(symbol, None)
        self._sync_watchlist_from_lifecycle()
        self._prune_runtime_state()

    def set_prewarm_symbols(self, symbols: Iterable[str]) -> None:
        self.prewarm_symbols = {
            str(symbol).upper()
            for symbol in symbols
            if str(symbol).strip() and str(symbol).upper() not in self.manual_stop_symbols
        }
        self._prune_runtime_state()

    def set_entry_blocked_symbols(self, symbols: Iterable[str]) -> None:
        del symbols
        if not self.lifecycle_policy.config.enabled:
            self.entry_blocked_symbols = set(self.manual_stop_symbols)
            return
        self.entry_blocked_symbols = self._blocked_lifecycle_symbols()

    def set_manual_stop_symbols(self, symbols: Iterable[str]) -> None:
        self.manual_stop_symbols = {
            str(symbol).upper() for symbol in symbols if str(symbol).strip()
        }
        self._desired_watchlist_symbols.difference_update(self.manual_stop_symbols)
        self.prewarm_symbols.difference_update(self.manual_stop_symbols)
        if not self.lifecycle_policy.config.enabled:
            self.watchlist = {
                symbol
                for symbol in self.watchlist
                if symbol not in self.manual_stop_symbols
            }
            self.lifecycle_states.clear()
            self.entry_blocked_symbols = set(self.manual_stop_symbols)
            self._prune_runtime_state()
            return
        for symbol in list(self.lifecycle_states):
            if symbol in self.manual_stop_symbols:
                self.lifecycle_states.pop(symbol, None)
        self._sync_watchlist_from_lifecycle()
        self._prune_runtime_state()

    def set_broker_blocked_symbols(self, symbols) -> None:
        # Mirror of set_manual_stop_symbols for symbols cached as Schwab-
        # ineligible (PR #92 cache). Evicts from _desired_watchlist_symbols,
        # prewarm_symbols, and lifecycle_states so the bot stops emitting
        # OPEN intents. Open positions are preserved by
        # _sync_watchlist_from_lifecycle's positions clause (exits still work).
        self.broker_blocked_symbols = {
            str(symbol).upper() for symbol in symbols if str(symbol).strip()
        }
        self._desired_watchlist_symbols.difference_update(self.broker_blocked_symbols)
        self.prewarm_symbols.difference_update(self.broker_blocked_symbols)
        if not self.lifecycle_policy.config.enabled:
            self.watchlist = {
                symbol
                for symbol in self.watchlist
                if symbol not in self.broker_blocked_symbols
            }
            self.entry_blocked_symbols = self._blocked_lifecycle_symbols()
            self._prune_runtime_state()
            return
        for symbol in list(self.lifecycle_states):
            if symbol in self.broker_blocked_symbols:
                self.lifecycle_states.pop(symbol, None)
        self._sync_watchlist_from_lifecycle()
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
        received_at = self._normalize_now(self.now_provider())
        for snapshot in snapshots:
            if snapshot.last_quote is None:
                continue
            quote: dict[str, float] = {}
            if snapshot.last_quote.bid_price is not None and snapshot.last_quote.bid_price > 0:
                quote["bid"] = float(snapshot.last_quote.bid_price)
            if snapshot.last_quote.ask_price is not None and snapshot.last_quote.ask_price > 0:
                quote["ask"] = float(snapshot.last_quote.ask_price)
            if quote:
                normalized_symbol = snapshot.ticker.upper()
                self.latest_quotes[normalized_symbol] = quote
                self._last_quote_received_at[normalized_symbol] = received_at

    def handle_quote_tick(
        self,
        symbol: str,
        *,
        bid_price: float | None,
        ask_price: float | None,
    ) -> list[TradeIntentEvent]:
        normalized_symbol = str(symbol).upper()
        quote: dict[str, float] = {}
        if bid_price is not None and bid_price > 0:
            quote["bid"] = float(bid_price)
        if ask_price is not None and ask_price > 0:
            quote["ask"] = float(ask_price)
        if quote:
            self.latest_quotes[normalized_symbol] = quote
            self._last_quote_received_at[normalized_symbol] = self._normalize_now(self.now_provider())
        return self._evaluate_position_quote_intents(
            normalized_symbol,
            bid_price=quote.get("bid"),
            ask_price=quote.get("ask"),
        )

    def apply_data_halt(
        self,
        symbol: str,
        *,
        reason: str,
        observed_at: datetime | None = None,
    ) -> None:
        normalized_symbol = str(symbol).upper()
        if not normalized_symbol:
            return
        self.data_halt_symbols[normalized_symbol] = str(reason)
        self.data_halt_since.setdefault(
            normalized_symbol,
            self._normalize_now(observed_at or self.now_provider()),
        )

    def clear_data_halt(self, symbol: str) -> None:
        normalized_symbol = str(symbol).upper()
        self.data_halt_symbols.pop(normalized_symbol, None)
        self.data_halt_since.pop(normalized_symbol, None)

    def apply_data_warning(
        self,
        symbol: str,
        *,
        reason: str,
        observed_at: datetime | None = None,
    ) -> None:
        normalized_symbol = str(symbol).upper()
        if not normalized_symbol:
            return
        self.data_warning_symbols[normalized_symbol] = str(reason)
        self.data_warning_since.setdefault(
            normalized_symbol,
            self._normalize_now(observed_at or self.now_provider()),
        )

    def clear_data_warning(self, symbol: str) -> None:
        normalized_symbol = str(symbol).upper()
        self.data_warning_symbols.pop(normalized_symbol, None)
        self.data_warning_since.pop(normalized_symbol, None)

    def _is_data_halted(self, symbol: str) -> bool:
        return str(symbol).upper() in self.data_halt_symbols

    def _data_halt_reason(self, symbol: str) -> str:
        return self.data_halt_symbols.get(str(symbol).upper(), "Schwab stream data halt active")

    def _has_data_halt_exposure(self, symbol: str) -> bool:
        normalized = str(symbol).upper()
        if self.positions.get_position(normalized) is not None:
            return True
        if normalized in self.pending_open_symbols or normalized in self.pending_close_symbols:
            return True
        return any(pending_symbol == normalized for pending_symbol, _level in self.pending_scale_levels)

    def _is_critical_data_halt_symbol(self, symbol: str) -> bool:
        normalized = str(symbol).upper()
        if self._has_data_halt_exposure(normalized):
            return True
        reason = str(self.data_halt_symbols.get(normalized, "") or "").lower()
        return (
            "trading halted until live schwab ticks recover" in reason
            or "reauthorize schwab tokens before trading" in reason
        )

    def data_health_summary(self) -> dict[str, object]:
        halted_symbols = sorted(self.data_halt_symbols)
        warning_symbols = sorted(
            symbol
            for symbol in self.data_warning_symbols
            if symbol not in self.data_halt_symbols
        )
        halted_exposure_symbols = sorted(
            symbol for symbol in halted_symbols if self._has_data_halt_exposure(symbol)
        )
        critical_halted_symbols = sorted(
            symbol for symbol in halted_symbols if self._is_critical_data_halt_symbol(symbol)
        )
        halted_open_position_symbols = sorted(
            symbol
            for symbol in halted_symbols
            if self.positions.get_position(symbol) is not None
        )
        return {
            "status": (
                "critical"
                if critical_halted_symbols
                else "degraded" if halted_symbols or warning_symbols else "healthy"
            ),
            "halted_symbols": halted_symbols,
            "exposure_halted_symbols": halted_exposure_symbols,
            "critical_halted_symbols": critical_halted_symbols,
            "open_position_halted_symbols": halted_open_position_symbols,
            "warning_symbols": warning_symbols,
            "reasons": dict(sorted(self.data_halt_symbols.items())),
            "warning_reasons": dict(
                sorted(
                    (symbol, reason)
                    for symbol, reason in self.data_warning_symbols.items()
                    if symbol not in self.data_halt_symbols
                )
            ),
            "since": {
                symbol: _datetime_str(observed_at)
                for symbol, observed_at in sorted(self.data_halt_since.items())
            },
            "warning_since": {
                symbol: _datetime_str(observed_at)
                for symbol, observed_at in sorted(self.data_warning_since.items())
                if symbol not in self.data_halt_symbols
            },
        }

    def evaluate_position_price(self, symbol: str, price: float) -> list[TradeIntentEvent]:
        self._roll_day_if_needed()
        return self._evaluate_position_price_intents(symbol, price)

    def emergency_close_for_data_halt(self, symbol: str, price: float | None = None) -> TradeIntentEvent | None:
        self._roll_day_if_needed()
        normalized_symbol = str(symbol).upper()
        position = self.positions.get_position(normalized_symbol)
        if position is None or normalized_symbol in self.pending_close_symbols:
            return None

        close_price = float(price or position.current_price or position.entry_price or 0)
        signal = {
            "ticker": normalized_symbol,
            "action": "SELL",
            "reason": "SCHWAB_DATA_STALE_EMERGENCY_CLOSE",
            "price": close_price,
        }
        try:
            return self._emit_close_intent(signal)
        except RuntimeError as exc:
            self._record_decision(
                symbol=normalized_symbol,
                status="critical",
                reason=f"Schwab data halt; emergency close waiting for sellable quote ({exc})",
                indicators={"price": close_price},
            )
            return None

    def _evaluate_position_price_intents(
        self,
        symbol: str,
        price: float,
        *,
        trigger_source: str = "trade",
    ) -> list[TradeIntentEvent]:
        intents: list[TradeIntentEvent] = []
        position = self.positions.get_position(symbol)
        if position is None or price <= 0:
            return intents

        position.update_price(price)
        hard_stop = self.exit_engine.check_hard_stop(position, price)
        if (
            hard_stop
            and symbol not in self.pending_close_symbols
            and not self._is_exit_retry_blocked(symbol)
        ):
            hard_stop = self._augment_hard_stop_signal(
                hard_stop,
                position=position,
                trigger_price=price,
                trigger_source=trigger_source,
            )
            close_intent = self._safe_emit_close_intent(hard_stop)
            if close_intent is not None:
                intents.append(close_intent)
            return intents

        if symbol in self.pending_close_symbols:
            return intents

        intrabar_exit = self.exit_engine.check_intrabar_exit(position)
        if intrabar_exit is None:
            return intents
        if intrabar_exit["action"] == "SCALE":
            level = str(intrabar_exit["level"])
            if (
                not self._has_pending_scale_for_symbol(symbol)
                and not self._is_scale_retry_blocked(symbol, level)
            ):
                scale_intent = self._safe_emit_scale_intent(intrabar_exit)
                if scale_intent is not None:
                    intents.append(scale_intent)
            return intents
        if not self._is_exit_retry_blocked(symbol):
            close_intent = self._safe_emit_close_intent(intrabar_exit)
            if close_intent is not None:
                intents.append(close_intent)
        return intents

    def _evaluate_position_quote_intents(
        self,
        symbol: str,
        *,
        bid_price: float | None,
        ask_price: float | None,
    ) -> list[TradeIntentEvent]:
        del ask_price
        config = self.definition.trading_config
        if not config.stop_guard_enabled or not config.stop_guard_quote_trigger_enabled:
            return []

        position = self.positions.get_position(symbol)
        if position is None or symbol in self.pending_close_symbols or self._is_exit_retry_blocked(symbol):
            return []

        if bid_price is not None and self._has_fresh_quote(symbol):
            return self._evaluate_position_price_intents(symbol, bid_price, trigger_source="bid")

        last_price = float(position.current_price or 0)
        if last_price > 0:
            return self._evaluate_position_price_intents(symbol, last_price, trigger_source="last")
        return []

    def _augment_hard_stop_signal(
        self,
        signal: dict[str, float | int | str],
        *,
        position: object,
        trigger_price: float,
        trigger_source: str,
    ) -> dict[str, float | int | str]:
        if str(signal.get("reason", "")).upper() != "HARD_STOP":
            return signal
        config = self.definition.trading_config
        if not config.stop_guard_enabled:
            return signal
        stop_price = float(position.entry_price) * (1 - float(config.stop_loss_pct) / 100)
        enriched = dict(signal)
        enriched["stop_guard"] = "true"
        enriched["stop_trigger_source"] = str(trigger_source)
        enriched["stop_trigger_price"] = float(trigger_price)
        enriched["stop_price"] = float(stop_price)
        enriched["panic_buffer_pct"] = float(config.stop_guard_initial_panic_buffer_pct)
        return enriched

    def _has_fresh_quote(self, symbol: str) -> bool:
        received_at = self._last_quote_received_at.get(str(symbol).upper())
        if received_at is None:
            return False
        max_age_ms = max(0, int(self.definition.trading_config.stop_guard_quote_max_age_ms))
        if max_age_ms <= 0:
            return True
        current = self._normalize_now(self.now_provider())
        return (current - received_at).total_seconds() * 1000 <= max_age_ms

    def _entry_freshness_limit_seconds(self) -> float:
        return max(15.0, float(self.definition.interval_secs) * 0.5)

    def _entry_freshness_block_reason(
        self,
        symbol: str,
        *,
        completed_bar: OHLCVBar | None = None,
        tick_timestamp: float | None = None,
        completed_bar_received_at: datetime | None = None,
        completed_bar_recorded_at: datetime | None = None,
        completed_bar_source: str = "live",
    ) -> str | None:
        if not isinstance(self.builder_manager, SchwabNativeBarBuilderManager):
            return None

        now = self._normalize_now(self.now_provider()).astimezone(UTC)
        now_ts = now.timestamp()
        interval = max(1, int(self.definition.interval_secs))
        limit_secs = self._entry_freshness_limit_seconds()
        # Unit tests and historical replay seed bars from old dates while using
        # a fixed current clock. Only treat same-session live lag as a blocker.
        max_live_lag_secs = 18.0 * 60.0 * 60.0

        if completed_bar is not None:
            bar_close_ts = float(completed_bar.timestamp) + float(interval)
            bar_close_at = datetime.fromtimestamp(bar_close_ts, UTC)
            receive_lag_secs: float | None = None
            record_lag_secs: float | None = None
            if completed_bar_received_at is not None:
                receive_lag_secs = (
                    completed_bar_received_at.astimezone(UTC) - bar_close_at
                ).total_seconds()
            if completed_bar_recorded_at is not None:
                record_lag_secs = (
                    completed_bar_recorded_at.astimezone(UTC) - bar_close_at
                ).total_seconds()
            if receive_lag_secs is not None and limit_secs < receive_lag_secs <= max_live_lag_secs:
                return (
                    "Schwab entry freshness guard: "
                    f"completed bar arrived {receive_lag_secs:.1f}s after close "
                    f"(limit {limit_secs:.1f}s); new entries blocked"
                )
            if (
                str(completed_bar_source).strip().lower() == "history"
                and receive_lag_secs is None
            ):
                replay_lag_secs = now_ts - bar_close_ts
                if limit_secs < replay_lag_secs <= max_live_lag_secs:
                    return (
                        "Schwab entry freshness guard: "
                        f"completed live bar missing from Schwab stream; history replay filled after {replay_lag_secs:.1f}s "
                        f"(limit {limit_secs:.1f}s); new entries blocked"
                    )
            if receive_lag_secs is None and record_lag_secs is None:
                close_lag_secs = now_ts - bar_close_ts
                if limit_secs < close_lag_secs <= max_live_lag_secs:
                    return (
                        "Schwab entry freshness guard: "
                        f"completed bar arrived {close_lag_secs:.1f}s after close "
                        f"(limit {limit_secs:.1f}s); new entries blocked"
                    )

        if tick_timestamp is not None:
            tick_lag_secs = now_ts - float(tick_timestamp)
            if limit_secs < tick_lag_secs <= max_live_lag_secs:
                return (
                    "Schwab entry freshness guard: "
                    f"trade tick arrived {tick_lag_secs:.1f}s after exchange timestamp "
                    f"(limit {limit_secs:.1f}s); new entries blocked"
                )

        freshness_issue = self.builder_manager.entry_freshness_issue(symbol)
        if freshness_issue:
            return f"Schwab entry freshness guard: {freshness_issue}; new entries blocked"
        return None

    def _apply_entry_freshness_warning(self, symbol: str, reason: str) -> None:
        self.apply_data_warning(
            symbol,
            reason=reason,
            observed_at=self._normalize_now(self.now_provider()),
        )

    def _clear_entry_freshness_warning(self, symbol: str) -> None:
        normalized = str(symbol).upper()
        reason = self.data_warning_symbols.get(normalized, "")
        if str(reason).startswith("Schwab entry freshness guard:"):
            self.clear_data_warning(normalized)

    def monitor_completed_bar_flow(self, *, provider: str) -> int:
        """Halt new entries when live ticks continue but completed bars stop advancing."""
        provider_label = str(provider or "market data").strip() or "market data"
        market_data_source = provider_label.upper() if len(provider_label) <= 8 else provider_label
        active_symbols = self.active_symbols()
        if not active_symbols:
            return 0

        now = self._normalize_now(self.now_provider()).astimezone(UTC)
        now_ts = now.timestamp()
        interval = max(1, int(self.definition.interval_secs))
        stale_after_secs = max(float(interval * 4), 120.0)
        tick_fresh_secs = max(float(interval * 2), 90.0)
        changed = 0

        for symbol in sorted(active_symbols):
            normalized = str(symbol).upper()
            last_tick_at = self._last_tick_at.get(normalized)
            builder = self.builder_manager.get_builder(normalized)
            bars = getattr(builder, "bars", None) if builder is not None else None
            if last_tick_at is None or builder is None:
                continue

            tick_age_secs = (
                now - self._normalize_now(last_tick_at).astimezone(UTC)
            ).total_seconds()
            if tick_age_secs > tick_fresh_secs:
                continue

            completed_bar_age_secs: float | None = None
            if bars:
                # Only halt when the builder is ACTIVELY trying to close a bar
                # but can't (a real stall — matches the SCHWAB30-STALL WARN
                # heuristic in `schwab_native_30s.check_bar_closes`). If
                # `_current_bar is None` the symbol is just quiet on-exchange:
                # `_last_tick_at` may stay fresh because LEVELONE_EQUITIES
                # keeps emitting events with cum_vol updates from off-exchange
                # prints, but no new bar bucket is being built. For macd_30s
                # (`fill_gap_bars=False`) on thin penny stocks that's normal,
                # not a stall — falsely halting these symbols took 13 of them
                # offline during 2026-05-19 RTH. We only halt when there's a
                # current bar past its force-close threshold, which is the
                # real "bars stuck while ticks arrive" signature.
                current_bar = getattr(builder, "_current_bar", None)
                current_bar_start = float(getattr(builder, "_current_bar_start", 0.0) or 0.0)
                if current_bar is None:
                    reason = self.data_halt_symbols.get(normalized, "")
                    if str(reason).startswith("Completed bar flow stalled:"):
                        self.clear_data_halt(normalized)
                        changed += 1
                    continue
                last_bar = bars[-1]
                last_bar_close_ts = float(last_bar.timestamp) + float(interval)
                completed_bar_age_secs = now_ts - last_bar_close_ts
                current_bar_overdue_secs = now_ts - (current_bar_start + float(interval))
                if current_bar_overdue_secs <= 0:
                    # Current bar is still in-flight within its window. Not a
                    # stall — wait for the next bar's force-close threshold.
                    reason = self.data_halt_symbols.get(normalized, "")
                    if str(reason).startswith("Completed bar flow stalled:"):
                        self.clear_data_halt(normalized)
                        changed += 1
                    continue
                reason = (
                    "Completed bar flow stalled: "
                    f"fresh {market_data_source} ticks are arriving but the last completed "
                    f"{interval}s bar is {completed_bar_age_secs:.1f}s old; "
                    "new entries halted until bar flow recovers"
                )
            else:
                current_bar = getattr(builder, "_current_bar", None)
                current_bar_start = float(getattr(builder, "_current_bar_start", 0.0) or 0.0)
                if current_bar is None or current_bar_start <= 0:
                    continue
                first_bar_due_ts = current_bar_start + float(interval)
                completed_bar_age_secs = now_ts - first_bar_due_ts
                reason = (
                    "Completed bar flow stalled: "
                    f"fresh {market_data_source} ticks are arriving but no completed "
                    f"{interval}s bar has formed for {completed_bar_age_secs:.1f}s; "
                    "new entries halted until bar flow recovers"
                )

            if completed_bar_age_secs <= stale_after_secs:
                reason = self.data_halt_symbols.get(normalized, "")
                if str(reason).startswith("Completed bar flow stalled:"):
                    self.clear_data_halt(normalized)
                    changed += 1
                continue

            if self.data_halt_symbols.get(normalized) != reason:
                self.apply_data_halt(normalized, reason=reason, observed_at=now)
                changed += 1

        return changed

    def seed_bars(self, symbol: str, bars: Sequence[dict[str, float | int]]) -> None:
        normalized_symbol = str(symbol).upper()
        builder = self.builder_manager.get_or_create(symbol)
        builder.reset()
        self._live_aggregate_skipped_bucket_start.pop(normalized_symbol, None)
        self._live_aggregate_trade_tick_counts.pop(normalized_symbol, None)

        sorted_bars = sorted(
            bars,
            key=lambda bar: float(bar["timestamp"]),
        )
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
            for bar in sorted_bars
        ]
        if not hydrated:
            self.last_indicators.pop(normalized_symbol, None)
            return

        builder.bars = hydrated[-builder.max_bars :]
        builder._bar_count = len(builder.bars)
        builder._current_bar = None
        builder._current_bar_start = 0.0
        if hasattr(builder, "_current_bar_last_cum_volume"):
            builder._current_bar_last_cum_volume = None

        self.rebuild_indicator_state(normalized_symbol)

    def rebuild_indicator_state(self, symbol: str) -> bool:
        normalized_symbol = str(symbol).upper()
        builder = self.builder_manager.get_builder(normalized_symbol)
        if builder is None or not builder.bars:
            self.last_indicators.pop(normalized_symbol, None)
            return False

        historical_indicators: list[dict[str, float | bool]] = []
        closed_bars = builder.bars
        for index in range(len(closed_bars)):
            indicators = self.indicator_engine.calculate(closed_bars[: index + 1])
            if indicators is None:
                continue
            historical_indicators.append(indicators)
        self.entry_engine.seed_recent_bars(normalized_symbol, historical_indicators)
        if historical_indicators:
            self.last_indicators[normalized_symbol] = self._decorate_indicators(
                normalized_symbol,
                historical_indicators[-1],
            )
            self._history_seed_attempted.add(normalized_symbol)
            return True
        self.last_indicators.pop(normalized_symbol, None)
        return False

    def _required_history_bars(self) -> int:
        indicator_config = self.definition.indicator_config
        trading_config = self.definition.trading_config
        indicator_min_bars = int(indicator_config.macd_slow + indicator_config.macd_signal)
        strategy_min_bars = int(getattr(trading_config, "schwab_native_warmup_bars_required", 0) or 0)
        return max(indicator_min_bars, strategy_min_bars, 1)

    def required_history_bars(self) -> int:
        return self._required_history_bars()

    def needs_history_seed(self, symbol: str) -> bool:
        normalized_symbol = str(symbol).upper()
        builder = self.builder_manager.get_or_create(normalized_symbol)
        if builder.get_bar_count() >= self._required_history_bars():
            self._history_seed_attempted.add(normalized_symbol)
            return False
        return True

    def _ensure_history_seeded(self, symbol: str) -> None:
        if self.session_factory is None:
            return

        normalized_symbol = str(symbol).upper()
        builder = self.builder_manager.get_or_create(normalized_symbol)
        required_bars = self._required_history_bars()
        if builder.get_bar_count() >= required_bars:
            self._history_seed_attempted.add(normalized_symbol)
            return

        session_start_utc = current_scanner_session_start_utc(self.now_provider())

        try:
            with self.session_factory() as session:
                current_session_records = list(
                    session.scalars(
                        select(StrategyBarHistory)
                        .where(
                            StrategyBarHistory.strategy_code.in_(strategy_code_candidates(self.definition.code)),
                            StrategyBarHistory.symbol == normalized_symbol,
                            StrategyBarHistory.interval_secs == self.definition.interval_secs,
                            StrategyBarHistory.bar_time >= session_start_utc,
                        )
                        .order_by(StrategyBarHistory.bar_time.asc())
                    ).all()
                )

                records = list(current_session_records)
                if len(records) < required_bars:
                    older_records = list(
                        reversed(
                            list(
                                session.scalars(
                                    select(StrategyBarHistory)
                                    .where(
                                        StrategyBarHistory.strategy_code.in_(strategy_code_candidates(self.definition.code)),
                                        StrategyBarHistory.symbol == normalized_symbol,
                                        StrategyBarHistory.interval_secs == self.definition.interval_secs,
                                        StrategyBarHistory.bar_time < session_start_utc,
                                    )
                                    .order_by(StrategyBarHistory.bar_time.desc())
                                    .limit(max(required_bars - len(records), 0))
                                ).all()
                            )
                        )
                    )
                    if older_records:
                        records = older_records + records
        except Exception:
            logger.exception(
                "failed lazy history seed for %s %s",
                self.definition.code,
                normalized_symbol,
            )
            return

        if not records:
            return

        bars = [
            {
                "open": float(record.open_price),
                "high": float(record.high_price),
                "low": float(record.low_price),
                "close": float(record.close_price),
                "volume": int(record.volume),
                "timestamp": float(record.bar_time.timestamp()),
                "trade_count": int(record.trade_count),
            }
            for record in records
        ]
        self.seed_bars(normalized_symbol, bars)
        if (
            self.builder_manager.get_or_create(normalized_symbol).get_bar_count() >= required_bars
            and normalized_symbol in self.last_indicators
        ):
            self._history_seed_attempted.add(normalized_symbol)

    def handle_trade_tick(
        self,
        symbol: str,
        price: float,
        size: int,
        timestamp_ns: int | None = None,
        cumulative_volume: int | None = None,
    ) -> list[TradeIntentEvent]:
        self._roll_day_if_needed()
        normalized_symbol = str(symbol).upper()
        normalized_timestamp_ns = self._normalize_tick_timestamp_ns(timestamp_ns)
        self._last_tick_at[normalized_symbol] = self._normalize_now(self.now_provider())
        intents = self._evaluate_position_price_intents(symbol, price)

        position = self.positions.get_position(symbol)
        prewarm_only = normalized_symbol in self.prewarm_symbols and normalized_symbol not in self.watchlist

        if normalized_symbol not in self.watchlist and position is None and not prewarm_only:
            return intents

        self._ensure_history_seeded(symbol)

        # Count every tick for live-aggregate-final bots (e.g. schwab_1m) so we
        # can stamp the CHART_EQUITY bar's missing trade_count from the parallel
        # TIMESALE/LEVELONE stream. Must run before the live/fallback split so
        # we capture ticks even when _should_fallback_to_trade_ticks routes the
        # tick to the native builder path inside the same bucket.
        if (
            self.use_live_aggregate_bars
            and self.live_aggregate_bars_are_final
            and not prewarm_only
        ):
            self._record_live_aggregate_trade_tick(normalized_symbol, normalized_timestamp_ns)

        if self.use_live_aggregate_bars and not prewarm_only and not self._should_fallback_to_trade_ticks(symbol):
            intents.extend(
                self._evaluate_intrabar_entry_from_trade_tick(
                    symbol,
                    price=price,
                    size=size,
                    timestamp_ns=normalized_timestamp_ns,
                )
            )
            return intents

        completed_bars = self.builder_manager.on_trade(
            symbol,
            price,
            size,
            normalized_timestamp_ns or 0,
            cumulative_volume,
        )
        # When a late-arriving trade tick lands in an already-closed bucket,
        # the SchwabNativeBarBuilder revises the closed bar in-place and stamps
        # _recent_revised_closed_bar. Pull it here and persist so the DB record
        # reflects the corrected volume/OHLC. Mirrors the same hook in
        # handle_live_bar (line ~1006) for the on_bar revision path.
        consume_revised = getattr(self.builder_manager, "consume_recent_revised_closed_bar", None)
        if callable(consume_revised):
            revised_closed_bar = consume_revised(symbol)
            if revised_closed_bar is not None and not prewarm_only:
                self._persist_revised_closed_bar(symbol=symbol, bar=revised_closed_bar)
        synthetic_gap_bars = [
            bar
            for bar in completed_bars
            if int(getattr(bar, "trade_count", 0) or 0) <= 0 and int(getattr(bar, "volume", 0) or 0) <= 0
        ]
        # Arm gap-recovery tracking for synthetic gap bars on tracked symbols
        # (open positions / pending / halt / warning). Don't early-return —
        # PR #196 (2026-05-19) flipped macd_30s fill_gap_bars to True, so
        # synthetic bars now coexist with real bars in the same batch on
        # symbols that have data_warning_symbols entries (e.g. "Schwab
        # symbol quiet" warns on thin penny stocks). Skipping the bar-loop
        # here would drop the real bars on the floor — their volume/OHLC
        # never reach _evaluate_completed_bar → _persist_bar_history, so the
        # decision tape goes stale even though the bot is processing ticks.
        gap_recovery_armed = False
        if synthetic_gap_bars and not prewarm_only:
            if self._should_track_gap_recovery(symbol):
                self._arm_gap_recovery(symbol, synthetic_gap_count=len(synthetic_gap_bars))
                self._finalize_gap_recovery_completed_bar(symbol)
                gap_recovery_armed = True
            else:
                self._clear_gap_recovery(symbol)
        for _bar in completed_bars:
            if prewarm_only:
                self._finalize_prewarm_completed_bar(symbol)
                continue
            is_synthetic = (
                int(getattr(_bar, "trade_count", 0) or 0) <= 0
                and int(getattr(_bar, "volume", 0) or 0) <= 0
            )
            if is_synthetic:
                # When gap-recovery is armed we already emitted the
                # gap-recovery completed-bar finalize above; skip the
                # per-bar quiet finalize so we don't double-stamp the
                # decision tape for the same bucket.
                if gap_recovery_armed:
                    continue
                self._finalize_synthetic_quiet_completed_bar(symbol)
                continue
            intents.extend(self._evaluate_completed_bar(symbol, completed_bar=_bar))
            self._advance_gap_recovery(symbol, _bar)
        if not prewarm_only:
            intents.extend(self._evaluate_intrabar_entry(symbol))

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
        coverage_started_at: float | None = None,
        live_bar_received_at: datetime | None = None,
        live_bar_recorded_at: datetime | None = None,
        live_bar_source: str = "live",
    ) -> list[TradeIntentEvent]:
        self._roll_day_if_needed()
        normalized_symbol = str(symbol).upper()
        self._last_tick_at[normalized_symbol] = self._normalize_now(self.now_provider())
        normalized_source = str(live_bar_source or "live").strip().lower() or "live"
        if normalized_source == "live":
            self._last_live_bar_received_at[normalized_symbol] = self._normalize_now(
                live_bar_received_at or self.now_provider()
            )
        self._remember_live_bar_delivery(
            normalized_symbol,
            timestamp=float(timestamp),
            received_at=live_bar_received_at,
            recorded_at=live_bar_recorded_at,
            source=normalized_source,
        )
        intents: list[TradeIntentEvent] = []

        position = self.positions.get_position(symbol)
        if position is not None:
            position.update_price(close_price)
        prewarm_only = normalized_symbol in self.prewarm_symbols and normalized_symbol not in self.watchlist

        if normalized_symbol not in self.watchlist and position is None and not prewarm_only:
            return intents

        self._ensure_history_seeded(symbol)

        if not self.use_live_aggregate_bars:
            # Keep tick-built runtimes on a single source of truth. Mixing live-bar
            # packets into the same builder drifts persisted bars away from the raw
            # trade-tick reconstruction we use for validation.
            return intents

        if self.live_aggregate_bars_are_final:
            effective_trade_count = self._effective_live_aggregate_trade_count(
                normalized_symbol,
                timestamp=timestamp,
                provided_trade_count=trade_count,
            )
            completed_bars = self.builder_manager.on_final_bar(
                symbol,
                OHLCVBar(
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=volume,
                    timestamp=timestamp,
                    trade_count=effective_trade_count,
                ),
            )
            for _bar in completed_bars:
                if prewarm_only:
                    self._finalize_prewarm_completed_bar(symbol)
                else:
                    intents.extend(
                        self._evaluate_completed_bar(
                            symbol,
                            completed_bar=_bar,
                            completed_bar_received_at=live_bar_received_at,
                            completed_bar_recorded_at=live_bar_recorded_at,
                            completed_bar_source=normalized_source,
                        )
                    )
                    self._advance_gap_recovery(symbol, _bar)
            return intents

        if self._should_skip_partial_live_aggregate_bucket(
            symbol,
            timestamp=timestamp,
            coverage_started_at=coverage_started_at,
        ):
            return intents

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
        revised_closed_bar = self.builder_manager.consume_recent_revised_closed_bar(symbol)
        if revised_closed_bar is not None and not prewarm_only:
            self._persist_revised_closed_bar(symbol=symbol, bar=revised_closed_bar)
        synthetic_gap_bars = [
            bar
            for bar in completed_bars
            if int(getattr(bar, "trade_count", 0) or 0) <= 0 and int(getattr(bar, "volume", 0) or 0) <= 0
        ]
        # See handle_trade_tick for the rationale: PR #196 made synthetic
        # bars common; the early-return here previously dropped real bars
        # from the same batch.
        gap_recovery_armed = False
        if synthetic_gap_bars and not prewarm_only:
            if self._should_track_gap_recovery(symbol):
                self._arm_gap_recovery(symbol, synthetic_gap_count=len(synthetic_gap_bars))
                self._finalize_gap_recovery_completed_bar(symbol)
                gap_recovery_armed = True
            else:
                self._clear_gap_recovery(symbol)
        for _bar in completed_bars:
            if prewarm_only:
                self._finalize_prewarm_completed_bar(symbol)
                continue
            is_synthetic = (
                int(getattr(_bar, "trade_count", 0) or 0) <= 0
                and int(getattr(_bar, "volume", 0) or 0) <= 0
            )
            if is_synthetic:
                if gap_recovery_armed:
                    continue
                self._finalize_synthetic_quiet_completed_bar(symbol)
                continue
            intents.extend(self._evaluate_completed_bar(symbol, completed_bar=_bar))
            self._advance_gap_recovery(symbol, _bar)
        if not prewarm_only:
            intents.extend(self._evaluate_intrabar_entry(symbol))

        return intents

    def _should_skip_partial_live_aggregate_bucket(
        self,
        symbol: str,
        *,
        timestamp: float,
        coverage_started_at: float | None = None,
    ) -> bool:
        if not self.use_live_aggregate_bars or self.live_aggregate_bars_are_final:
            return False

        interval = max(1, int(self.definition.interval_secs))
        if interval <= 1:
            return False

        normalized_symbol = str(symbol).upper()
        bucket_start = (float(timestamp) // interval) * interval
        skipped_bucket_start = self._live_aggregate_skipped_bucket_start.get(normalized_symbol)
        if skipped_bucket_start is not None:
            if bucket_start == skipped_bucket_start:
                return True
            if bucket_start > skipped_bucket_start:
                self._live_aggregate_skipped_bucket_start.pop(normalized_symbol, None)

        builder = self.builder_manager.get_or_create(normalized_symbol)
        current_bar = getattr(builder, "_current_bar", None)
        current_bar_start = float(getattr(builder, "_current_bar_start", 0.0) or 0.0)
        if current_bar is not None and current_bar_start == bucket_start:
            return False

        last_closed_bar = builder.bars[-1] if builder.bars else None
        if last_closed_bar is not None and bucket_start <= float(last_closed_bar.timestamp):
            return False

        coverage_started_at = float(coverage_started_at) if coverage_started_at is not None else None
        if coverage_started_at is not None:
            if coverage_started_at <= bucket_start:
                return False
        elif float(timestamp) <= bucket_start:
            return False

        # Persisted canonical bars should have full live coverage. If a symbol
        # first becomes active mid-bucket, or provider coverage restarts
        # mid-bucket, skip that partial bucket and wait for the next aligned
        # boundary instead of persisting a truncated canonical bar. When
        # coverage metadata is unavailable, fall back to the older
        # first-aggregate timestamp heuristic.
        self._live_aggregate_skipped_bucket_start[normalized_symbol] = bucket_start
        logger.info(
            "skipping partial live aggregate bucket for %s on %s at %.3f (bucket %.3f coverage %.3f)",
            self.definition.code,
            normalized_symbol,
            float(timestamp),
            bucket_start,
            coverage_started_at if coverage_started_at is not None else float(timestamp),
        )
        return True

    def _should_use_live_bar_builder_fallback(self, symbol: str, *, timestamp: float) -> bool:
        if not self.live_aggregate_fallback_enabled:
            return False

        latest_bucket_start = self._latest_builder_bucket_start(symbol)
        if latest_bucket_start is None:
            return True

        incoming_bucket_start = (float(timestamp) // self.definition.interval_secs) * self.definition.interval_secs
        return (incoming_bucket_start - latest_bucket_start) >= self.definition.interval_secs

    def _should_fallback_to_trade_ticks(self, symbol: str) -> bool:
        if not self.live_aggregate_fallback_enabled:
            return False
        last_live_bar_at = self._last_live_bar_received_at.get(symbol)
        if last_live_bar_at is None:
            return True
        now = self._normalize_now(self.now_provider())
        if (now - last_live_bar_at).total_seconds() > self.live_aggregate_stale_after_seconds:
            return True

        latest_bucket_start = self._latest_builder_bucket_start(symbol)
        if latest_bucket_start is None:
            return True

        now_bucket_start = (now.timestamp() // self.definition.interval_secs) * self.definition.interval_secs
        return (now_bucket_start - latest_bucket_start) >= self.definition.interval_secs

    def _latest_builder_bucket_start(self, symbol: str) -> float | None:
        builder = self.builder_manager.get_builder(symbol)
        if builder is None:
            return None

        get_intrabar_bars = getattr(builder, "get_bars_with_current_as_dicts", None)
        if callable(get_intrabar_bars):
            bars = get_intrabar_bars()
        else:
            get_closed_bars = getattr(builder, "get_bars_as_dicts", None)
            bars = get_closed_bars() if callable(get_closed_bars) else []

        if not bars:
            return None

        latest_timestamp = bars[-1].get("timestamp")
        try:
            return float(latest_timestamp)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_now(current: datetime) -> datetime:
        if current.tzinfo is None:
            return current.replace(tzinfo=EASTERN_TZ)
        return current

    def _record_live_aggregate_trade_tick(self, symbol: str, timestamp_ns: int | None) -> None:
        if not timestamp_ns:
            return
        interval = max(1, int(self.definition.interval_secs))
        bucket_start = (float(timestamp_ns) / 1_000_000_000.0 // interval) * interval
        counts = self._live_aggregate_trade_tick_counts.setdefault(symbol, {})
        counts[bucket_start] = counts.get(bucket_start, 0) + 1

    def _effective_live_aggregate_trade_count(
        self,
        symbol: str,
        *,
        timestamp: float,
        provided_trade_count: int,
    ) -> int:
        # Schwab CHART_EQUITY bars carry no per-bar trade count, so the streamer
        # stamps trade_count=1. When the parallel TIMESALE/LEVELONE tick stream
        # has been routed through handle_trade_tick during this bucket we use
        # the accumulated count instead. Falling back to the provided value
        # preserves the synthetic-gap-bar sentinel (trade_count<=0) used by the
        # builder for symbols that have not yet seen any tick traffic.
        interval = max(1, int(self.definition.interval_secs))
        bucket_start = (float(timestamp) // interval) * interval
        counts = self._live_aggregate_trade_tick_counts.get(symbol)
        if not counts:
            return int(provided_trade_count or 0)
        accumulated = int(counts.pop(bucket_start, 0) or 0)
        for stale_bucket in [b for b in counts if b < bucket_start]:
            counts.pop(stale_bucket, None)
        if not counts:
            self._live_aggregate_trade_tick_counts.pop(symbol, None)
        if accumulated > 0:
            return accumulated
        return int(provided_trade_count or 0)

    def flush_completed_bars(self) -> tuple[list[TradeIntentEvent], int]:
        self._roll_day_if_needed()
        if self.use_live_aggregate_bars and self.live_aggregate_bars_are_final:
            return [], 0
        intents: list[TradeIntentEvent] = []
        completed = self.builder_manager.check_all_bar_closes()
        completed_by_symbol: dict[str, list[OHLCVBar]] = {}
        for symbol, bar in completed:
            completed_by_symbol.setdefault(str(symbol).upper(), []).append(bar)

        for normalized_symbol, symbol_bars in completed_by_symbol.items():
            symbol = normalized_symbol
            normalized_symbol = str(symbol).upper()
            self._last_tick_at[normalized_symbol] = self._normalize_now(self.now_provider())
            position = self.positions.get_position(symbol)
            prewarm_only = normalized_symbol in self.prewarm_symbols and normalized_symbol not in self.watchlist
            if normalized_symbol not in self.watchlist and position is None and not prewarm_only:
                continue
            synthetic_gap_bars = [
                bar
                for bar in symbol_bars
                if int(getattr(bar, "trade_count", 0) or 0) <= 0 and int(getattr(bar, "volume", 0) or 0) <= 0
            ]
            # See handle_trade_tick for the rationale: PR #196 made
            # synthetic bars common; the `continue` here previously dropped
            # real bars in the same batch on tracked-recovery symbols
            # (data_warning, etc.) so the decision tape went stale for them.
            gap_recovery_armed = False
            if synthetic_gap_bars and not prewarm_only:
                if self._should_track_gap_recovery(symbol):
                    self._arm_gap_recovery(symbol, synthetic_gap_count=len(synthetic_gap_bars))
                    self._finalize_gap_recovery_completed_bar(symbol)
                    gap_recovery_armed = True
                else:
                    self._clear_gap_recovery(symbol)
            for bar in symbol_bars:
                if prewarm_only:
                    self._finalize_prewarm_completed_bar(symbol)
                    continue
                is_synthetic = (
                    int(getattr(bar, "trade_count", 0) or 0) <= 0
                    and int(getattr(bar, "volume", 0) or 0) <= 0
                )
                if is_synthetic:
                    if gap_recovery_armed:
                        continue
                    self._finalize_synthetic_quiet_completed_bar(symbol)
                    continue
                intents.extend(self._evaluate_completed_bar(symbol, completed_bar=bar))
                self._advance_gap_recovery(symbol, bar)
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
                self.positions.open_position(
                    symbol,
                    fill_price,
                    quantity=qty,
                    path=path or "",
                    scale_profile=self._scale_profile_for_symbol(symbol),
                )
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
                close_reason = (reason or "").strip() or "OMS_FILL"
                self._finalize_flattened_position(symbol, fill_price, reason=close_reason)
                return

            position.scale_pnl += (fill_price - position.entry_price) * qty
            position.quantity -= qty
            return

        if intent_type == "scale" and side == "sell" and level and position is not None:
            self.pending_scale_levels.discard((symbol, level))
            position.apply_scale(level, qty, fill_price)
            if position.quantity <= 0:
                close_reason = (reason or "").strip() or level or "OMS_FILL"
                self._finalize_flattened_position(symbol, fill_price, reason=close_reason)

    def _finalize_flattened_position(self, symbol: str, fill_price: float, *, reason: str) -> None:
        position = self.positions.get_position(symbol)
        entry_path = str(position.entry_path) if position is not None else ""
        self.pending_open_symbols.discard(symbol)
        self.pending_close_symbols.discard(symbol)
        self.pending_scale_levels = {
            (pending_symbol, pending_level)
            for pending_symbol, pending_level in self.pending_scale_levels
            if pending_symbol != symbol
        }
        self.positions.close_position(symbol, fill_price, reason=reason)
        bar_index = self.builder_manager.get_or_create(symbol).get_bar_count()
        self.entry_engine.record_exit(symbol, bar_index)
        record_path_exit = getattr(self.entry_engine, "record_path_exit", None)
        if callable(record_path_exit) and entry_path:
            try:
                record_path_exit(symbol, path=entry_path, reason=reason)
            except Exception:
                logger.exception("failed to record path exit for %s", symbol)

    def _finalize_missing_broker_position(self, symbol: str, *, reason: str) -> None:
        position = self.positions.get_position(symbol)
        if position is None:
            self.pending_open_symbols.discard(symbol)
            self.pending_close_symbols.discard(symbol)
            self.pending_scale_levels = {
                (pending_symbol, pending_level)
                for pending_symbol, pending_level in self.pending_scale_levels
                if pending_symbol != symbol
            }
            self.entry_engine.record_exit(symbol, self.builder_manager.get_or_create(symbol).get_bar_count())
            return

        exit_price = float(position.current_price or 0) or float(position.entry_price or 0)
        if exit_price > 0:
            self._finalize_flattened_position(symbol, exit_price, reason=reason)
            return

        self.positions.drop_position(symbol)
        bar_index = self.builder_manager.get_or_create(symbol).get_bar_count()
        self.entry_engine.record_exit(symbol, bar_index)

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
            if self.definition.code == "polygon_30s" and self._should_apply_polygon_rejected_open_cooldown(
                normalized_reason
            ):
                self.entry_engine.record_rejected_open(
                    symbol,
                    self.builder_manager.get_or_create(symbol).get_bar_count(),
                    cooldown_bars=20,
                )
            self.entry_engine.cancel_pending(symbol)
            return

        if intent_type == "close":
            self.pending_close_symbols.discard(symbol)
            if self._is_no_position_reason(normalized_reason):
                self._finalize_missing_broker_position(symbol, reason="BROKER_FLAT_RECONCILE")
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
                self._finalize_missing_broker_position(symbol, reason="BROKER_FLAT_RECONCILE")
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
            "prewarm_symbols": sorted(self.prewarm_symbols),
            "data_health": self.data_health_summary(),
            "entry_blocked_symbols": sorted(self.entry_blocked_symbols),
            "retention_states": self._lifecycle_state_summary(),
            "manual_stop_symbols": sorted(self.manual_stop_symbols),
            "positions": self.positions.get_all_positions(),
            "pending_open_symbols": sorted(self.pending_open_symbols),
            "pending_close_symbols": sorted(self.pending_close_symbols),
            "pending_scale_levels": sorted(f"{symbol}:{level}" for symbol, level in self.pending_scale_levels),
            "daily_pnl": self.positions.get_daily_pnl(),
            "closed_today": self.positions.get_closed_today(),
            "recent_decisions": self._live_decision_rows(),
            "indicator_snapshots": self._indicator_snapshots(),
            "bar_counts": self._bar_counts(),
            "last_tick_at": self._last_tick_summary(),
        }

    def _roll_day_if_needed(self) -> bool:
        current_day = session_day_eastern_str(self.now_provider())
        if current_day == self._active_day:
            return False
        self.positions.reset()
        self.positions.load_closed_trades()
        self.entry_engine.reset()
        self.last_indicators.clear()
        self.latest_quotes.clear()
        self._last_quote_received_at.clear()
        self.entry_blocked_symbols.clear()
        self.data_halt_symbols.clear()
        self.data_halt_since.clear()
        self.data_warning_symbols.clear()
        self.data_warning_since.clear()
        self._gap_recovery_bars_remaining.clear()
        self._gap_recovery_synthetic_bars.clear()
        self.lifecycle_states.clear()
        self.watchlist.clear()
        self._desired_watchlist_symbols.clear()
        self.prewarm_symbols.clear()
        self.recent_decisions.clear()
        self.builder_manager.reset()
        self._applied_fill_quantity_by_order.clear()
        self._last_live_bar_received_at.clear()
        self._recent_live_bar_delivery.clear()
        self._live_aggregate_skipped_bucket_start.clear()
        self._live_aggregate_trade_tick_counts.clear()
        self._history_seed_attempted.clear()
        previous_day = self._active_day
        self._active_day = current_day
        logger.info(
            "bot day-roll fired | strategy=%s previous_day=%s current_day=%s",
            self.definition.code,
            previous_day,
            current_day,
        )
        return True

    def purge_non_protected_session_state(self) -> set[str]:
        """Session-roll hard-purge with a load-bearing carve-out.

        `_roll_day_if_needed()` clears all lifecycle/watchlist state, but it is a
        no-op when a pre-roll async tick/fill already advanced `_active_day` to
        today (the 04:00 watchlist-staleness race: the roll's clear never runs,
        so the broker-blocked resync's re-promoted stale symbols survive). This
        selectively clears `lifecycle_states` / `_desired_watchlist_symbols` for
        symbols that no longer require a feed, while PRESERVING any symbol with an
        open position or in-flight open/close/scale (`_symbol_requires_feed`) so a
        live position's feed and exit ladder survive the roll. Returns the purged
        symbols. Idempotent / safe to call when there is nothing to purge.
        """
        candidates = set(self.lifecycle_states) | set(self._desired_watchlist_symbols)
        purgeable = {symbol for symbol in candidates if not self._symbol_requires_feed(symbol)}
        if not purgeable:
            return set()
        for symbol in purgeable:
            self.lifecycle_states.pop(symbol, None)
        self._desired_watchlist_symbols.difference_update(purgeable)
        # Rebuild the watchlist from the surviving lifecycle + positions/pending
        # (positions and in-flight orders are re-included here too — belt and
        # suspenders alongside the _symbol_requires_feed carve-out above).
        self._sync_watchlist_from_lifecycle()
        return purgeable

    def has_position(self, ticker: str) -> bool:
        return self.positions.has_position(ticker) or ticker in self.pending_open_symbols

    def active_symbols(self) -> set[str]:
        active = set(self.watchlist)
        active.update(self.pending_open_symbols)
        active.update(self.pending_close_symbols)
        active.update(symbol for symbol, _level in self.pending_scale_levels)
        active.update(position["ticker"] for position in self.positions.get_all_positions())
        return active

    def stream_symbols(self) -> set[str]:
        active = self.active_symbols()
        active.update(self.prewarm_symbols)
        return active

    def _remember_live_bar_delivery(
        self,
        symbol: str,
        *,
        timestamp: float,
        received_at: datetime | None,
        recorded_at: datetime | None,
        source: str,
    ) -> None:
        entries = self._recent_live_bar_delivery.setdefault(symbol, {})
        entries[float(timestamp)] = (
            self._normalize_now(received_at) if received_at is not None else None,
            self._normalize_now(recorded_at) if recorded_at is not None else None,
            str(source or "live").strip().lower() or "live",
        )
        cutoff = float(timestamp) - (float(self.definition.interval_secs) * 12.0)
        stale_keys = [bar_ts for bar_ts in entries if bar_ts < cutoff]
        for stale_key in stale_keys:
            entries.pop(stale_key, None)

    def _evaluate_completed_bar(
        self,
        symbol: str,
        *,
        completed_bar: OHLCVBar | None = None,
        completed_bar_received_at: datetime | None = None,
        completed_bar_recorded_at: datetime | None = None,
        completed_bar_source: str = "live",
    ) -> list[TradeIntentEvent]:
        builder = self.builder_manager.get_builder(symbol)
        if builder is None:
            return []

        bars = builder.get_bars_as_dicts()
        if not bars:
            return []

        local_indicators = self.indicator_engine.calculate(bars)
        if local_indicators is None:
            return self._finalize_warmup_completed_bar(symbol, completed_bar=completed_bar)

        indicators = self._decorate_indicators(symbol, local_indicators)
        self.last_indicators[symbol] = indicators
        metrics = self._build_lifecycle_metrics(symbol, indicators, self.builder_manager)
        self._update_symbol_lifecycle(symbol, metrics=metrics)
        intents: list[TradeIntentEvent] = []

        if not self._should_track_gap_recovery(symbol):
            self._clear_gap_recovery(symbol)

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
                        close_intent = self._safe_emit_close_intent(probe_signal)
                        if close_intent is not None:
                            intents.append(close_intent)
                    return self._finalize_completed_bar(
                        symbol,
                        indicators,
                        intents,
                        decision=decision,
                        completed_bar=completed_bar,
                    )

            exit_signal = self.exit_engine.check_exit(position, indicators)
            if exit_signal:
                if exit_signal["action"] == "SCALE":
                    level = str(exit_signal["level"])
                    if not self._has_pending_scale_for_symbol(symbol) and not self._is_scale_retry_blocked(symbol, level):
                        scale_intent = self._safe_emit_scale_intent(exit_signal)
                        if scale_intent is not None:
                            intents.append(scale_intent)
                elif symbol not in self.pending_close_symbols and not self._is_exit_retry_blocked(symbol):
                    close_intent = self._safe_emit_close_intent(exit_signal)
                    if close_intent is not None:
                        intents.append(close_intent)
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
                    completed_bar=completed_bar,
                )

            if probe_signal is not None and probe_signal.get("action") == "BUY":
                if symbol not in self.pending_open_symbols:
                    open_intent, routing_block_reason = self._try_emit_open_intent(probe_signal)
                    if open_intent is not None:
                        intents.append(open_intent)
                    else:
                        decision = self._build_persisted_decision(
                            symbol=symbol,
                            status="blocked",
                            reason=routing_block_reason or "missing extended-hours ask quote",
                            indicators=indicators,
                        )
                return self._finalize_completed_bar(
                    symbol,
                    indicators,
                    intents,
                    decision=decision,
                    completed_bar=completed_bar,
                )
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
                completed_bar=completed_bar,
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
                completed_bar=completed_bar,
            )

        if self._is_data_halted(symbol):
            decision = self._record_decision(
                symbol=symbol,
                status="critical",
                reason=self._data_halt_reason(symbol),
                indicators=indicators,
            )
            return self._finalize_completed_bar(
                symbol,
                indicators,
                [],
                decision=decision,
                completed_bar=completed_bar,
            )

        can_open, _reason = self.positions.can_open_position(symbol)
        if not can_open:
            decision = self._record_decision(
                symbol=symbol,
                status="blocked",
                reason=str(_reason),
                indicators=indicators,
            )
            return self._finalize_completed_bar(
                symbol,
                indicators,
                [],
                decision=decision,
                completed_bar=completed_bar,
            )

        if self._is_gap_recovery_active(symbol):
            decision = self._record_decision(
                symbol=symbol,
                status="warning",
                reason=self._gap_recovery_reason(symbol),
                indicators=indicators,
            )
            return self._finalize_completed_bar(
                symbol,
                indicators,
                [],
                decision=decision,
                completed_bar=completed_bar,
            )

        freshness_block_reason = self._entry_freshness_block_reason(
            symbol,
            completed_bar=completed_bar,
            completed_bar_received_at=completed_bar_received_at,
            completed_bar_recorded_at=completed_bar_recorded_at,
            completed_bar_source=completed_bar_source,
        )
        if freshness_block_reason:
            self._apply_entry_freshness_warning(symbol, freshness_block_reason)
            decision = self._record_decision(
                symbol=symbol,
                status="blocked",
                reason=freshness_block_reason,
                indicators=indicators,
            )
            return self._finalize_completed_bar(
                symbol,
                indicators,
                [],
                decision=decision,
                completed_bar=completed_bar,
            )
        self._clear_entry_freshness_warning(symbol)

        signal = self.entry_engine.check_entry(symbol, indicators, builder.get_bar_count(), self)
        decision = self._capture_entry_decision(symbol, indicators)
        if self.lifecycle_policy.config.enabled and symbol in self.entry_blocked_symbols:
            if symbol in self.manual_stop_symbols:
                decision = self._record_decision(
                    symbol=symbol,
                    status="blocked",
                    reason="manually stopped by operator",
                    indicators=indicators,
                )
                return self._finalize_completed_bar(
                    symbol,
                    indicators,
                    [],
                    decision=decision,
                    completed_bar=completed_bar,
                )
            if signal is not None and self._reactivate_lifecycle_from_signal(symbol, metrics, signal):
                self.entry_blocked_symbols.discard(symbol)
            else:
                decision = self._record_decision(
                    symbol=symbol,
                    status="blocked",
                    reason="bot lifecycle cooldown active: waiting for P4 or VWAP/EMA20 reclaim",
                    indicators=indicators,
                )
                return self._finalize_completed_bar(
                    symbol,
                    indicators,
                    [],
                    decision=decision,
                    completed_bar=completed_bar,
                )
        if signal is None:
            return self._finalize_completed_bar(
                symbol,
                indicators,
                [],
                decision=decision,
                completed_bar=completed_bar,
            )

        open_intent, routing_block_reason = self._try_emit_open_intent(signal)
        if open_intent is not None:
            intents.append(open_intent)
        else:
            decision = self._build_persisted_decision(
                symbol=symbol,
                status="blocked",
                reason=routing_block_reason or "missing extended-hours ask quote",
                indicators=indicators,
            )
        return self._finalize_completed_bar(
            symbol,
            indicators,
            intents,
            decision=decision,
            completed_bar=completed_bar,
        )

    def _finalize_prewarm_completed_bar(self, symbol: str) -> None:
        del symbol
        return

    def _finalize_warmup_completed_bar(
        self,
        symbol: str,
        *,
        completed_bar: OHLCVBar | None = None,
    ) -> list[TradeIntentEvent]:
        builder = self.builder_manager.get_builder(symbol)
        if builder is None:
            return []
        bar = completed_bar
        if bar is None:
            if not builder.bars:
                return []
            bar = builder.bars[-1]
        required_bars = max(1, self._required_history_bars())
        current_bars = min(max(0, builder.get_bar_count()), required_bars)
        indicators = {
            "price": float(bar.close),
            "open": float(bar.open),
            "bar_timestamp": float(bar.timestamp),
            "bar_volume": int(bar.volume),
        }
        decision = self._record_decision(
            symbol=symbol,
            status="blocked",
            reason=f"warmup ({current_bars}/{required_bars} bars)",
            indicators=indicators,
        )
        self._persist_bar_history(
            symbol=symbol,
            indicators=indicators,
            decision=decision,
            completed_bar=bar,
        )
        return []

    def _finalize_gap_recovery_completed_bar(self, symbol: str) -> None:
        builder = self.builder_manager.get_builder(symbol)
        if builder is None:
            return
        bars = builder.get_bars_as_dicts()
        if not bars:
            return
        local_indicators = self.indicator_engine.calculate(bars)
        if local_indicators is None:
            return
        indicators = self._decorate_indicators(symbol, local_indicators)
        self.last_indicators[symbol] = indicators
        position = self.positions.get_position(symbol)
        decision = self._record_decision(
            symbol=symbol,
            status="warning" if position is not None else "blocked",
            reason=self._gap_recovery_reason(symbol),
            indicators=indicators,
        )
        self._finalize_completed_bar(symbol, indicators, [], decision=decision)

    def _finalize_synthetic_quiet_completed_bar(self, symbol: str) -> None:
        builder = self.builder_manager.get_builder(symbol)
        if builder is None:
            return
        bars = builder.get_bars_as_dicts()
        if not bars:
            return
        local_indicators = self.indicator_engine.calculate(bars)
        if local_indicators is None:
            return
        indicators = self._decorate_indicators(symbol, local_indicators)
        self.last_indicators[symbol] = indicators
        self._finalize_completed_bar(symbol, indicators, [], decision=None)

    def _intrabar_entry_mode_enabled(self) -> bool:
        trading = self.definition.trading_config
        return bool(
            getattr(trading, "entry_intrabar_enabled", False)
            or getattr(trading, "p4_prev_bar_entry_enabled", False)
        )

    def _intrabar_entry_is_p4_only(self) -> bool:
        trading = self.definition.trading_config
        return bool(
            getattr(trading, "p4_prev_bar_entry_enabled", False)
            and not getattr(trading, "entry_intrabar_enabled", False)
        )

    def _evaluate_intrabar_entry(self, symbol: str) -> list[TradeIntentEvent]:
        if not self._intrabar_entry_mode_enabled():
            return []

        position = self.positions.get_position(symbol)
        if position is not None or symbol in self.pending_open_symbols:
            return []
        builder = self.builder_manager.get_builder(symbol)
        if builder is None:
            return []

        get_intrabar_bars = getattr(builder, "get_bars_with_current_as_dicts", None)
        if get_intrabar_bars is None:
            return []

        bars = get_intrabar_bars()
        if not bars:
            return []

        closed_bar_count = builder.get_bar_count()
        get_closed_bars = getattr(builder, "get_bars_as_dicts", None)
        closed_bars = get_closed_bars() if callable(get_closed_bars) else []
        if len(bars) <= len(closed_bars):
            return []

        local_indicators = self.indicator_engine.calculate(bars)
        if local_indicators is None:
            return []

        indicators = self._decorate_indicators(symbol, local_indicators)
        metrics = self._build_lifecycle_metrics(symbol, indicators, self.builder_manager)
        freshness_block_reason = self._entry_freshness_block_reason(symbol)
        if freshness_block_reason:
            self._apply_entry_freshness_warning(symbol, freshness_block_reason)
            self._record_decision(
                symbol=symbol,
                status="blocked",
                reason=freshness_block_reason,
                indicators=indicators,
            )
            return []
        self._clear_entry_freshness_warning(symbol)
        if self._is_data_halted(symbol):
            self._record_decision(
                symbol=symbol,
                status="critical",
                reason=self._data_halt_reason(symbol),
                indicators=indicators,
            )
            return []
        can_open, _reason = self.positions.can_open_position(symbol)
        if not can_open:
            self.entry_engine.pop_last_decision(symbol)
            return []

        signal = self.entry_engine.check_entry(symbol, indicators, closed_bar_count + 1, self)
        if signal is None:
            self.entry_engine.pop_last_decision(symbol)
            return []
        if self._intrabar_entry_is_p4_only() and str(signal.get("path", "")).upper() != "P4_BURST":
            self.entry_engine.pop_last_decision(symbol)
            return []

        self._capture_entry_decision(symbol, indicators)
        if symbol in self.manual_stop_symbols:
            self._record_decision(
                symbol=symbol,
                status="blocked",
                reason="manually stopped by operator",
                indicators=indicators,
            )
            return []
        if symbol in self.broker_blocked_symbols:
            self._record_decision(
                symbol=symbol,
                status="blocked",
                reason="schwab_ineligible_cached",
                indicators=indicators,
            )
            return []
        if symbol in self.entry_blocked_symbols and not self._reactivate_lifecycle_from_signal(symbol, metrics, signal):
            return []
        open_intent, _routing_block_reason = self._try_emit_open_intent(signal)
        return [open_intent] if open_intent is not None else []

    def _evaluate_intrabar_entry_from_trade_tick(
        self,
        symbol: str,
        *,
        price: float,
        size: int,
        timestamp_ns: int | None = None,
    ) -> list[TradeIntentEvent]:
        if not self._intrabar_entry_mode_enabled():
            return []

        position = self.positions.get_position(symbol)
        if position is not None or symbol in self.pending_open_symbols:
            return []

        builder = self.builder_manager.get_builder(symbol)
        if builder is None:
            return []

        bars = builder.get_bars_with_current_as_dicts()
        if not bars:
            return []

        tick_ts = self._resolve_tick_timestamp(timestamp_ns)
        bucket_start = (tick_ts // self.definition.interval_secs) * self.definition.interval_secs
        adjusted_bars = [dict(bar) for bar in bars]
        current_bar = adjusted_bars[-1]
        current_ts = float(current_bar.get("timestamp", 0) or 0)
        tick_size = max(0, int(size))

        if current_ts <= 0 or bucket_start > current_ts:
            last_close = float(current_bar.get("close", price) or price)
            adjusted_bars.append(
                {
                    "open": last_close,
                    "high": max(last_close, price),
                    "low": min(last_close, price),
                    "close": price,
                    "volume": tick_size,
                    "timestamp": float(bucket_start),
                    "trade_count": 1,
                }
            )
        elif bucket_start == current_ts:
            current_bar["high"] = max(float(current_bar.get("high", price) or price), price)
            current_bar["low"] = min(float(current_bar.get("low", price) or price), price)
            current_bar["close"] = price
            current_bar["volume"] = max(0, int(current_bar.get("volume", 0) or 0)) + tick_size
            current_bar["trade_count"] = max(0, int(current_bar.get("trade_count", 0) or 0)) + 1
        else:
            return []

        local_indicators = self.indicator_engine.calculate(adjusted_bars)
        if local_indicators is None:
            return []

        indicators = self._decorate_indicators(symbol, local_indicators)
        metrics = self._build_lifecycle_metrics(symbol, indicators, self.builder_manager)
        freshness_block_reason = self._entry_freshness_block_reason(
            symbol,
            tick_timestamp=tick_ts,
        )
        if freshness_block_reason:
            self._apply_entry_freshness_warning(symbol, freshness_block_reason)
            self._record_decision(
                symbol=symbol,
                status="blocked",
                reason=freshness_block_reason,
                indicators=indicators,
            )
            return []
        self._clear_entry_freshness_warning(symbol)
        if self._is_data_halted(symbol):
            self._record_decision(
                symbol=symbol,
                status="critical",
                reason=self._data_halt_reason(symbol),
                indicators=indicators,
            )
            return []
        can_open, _reason = self.positions.can_open_position(symbol)
        if not can_open:
            self.entry_engine.pop_last_decision(symbol)
            return []

        closed_bar_count = builder.get_bar_count()
        signal = self.entry_engine.check_entry(symbol, indicators, closed_bar_count + 1, self)
        if signal is None:
            self.entry_engine.pop_last_decision(symbol)
            return []
        if self._intrabar_entry_is_p4_only() and str(signal.get("path", "")).upper() != "P4_BURST":
            self.entry_engine.pop_last_decision(symbol)
            return []

        self._capture_entry_decision(symbol, indicators)
        if symbol in self.manual_stop_symbols:
            self._record_decision(
                symbol=symbol,
                status="blocked",
                reason="manually stopped by operator",
                indicators=indicators,
            )
            return []
        if symbol in self.broker_blocked_symbols:
            self._record_decision(
                symbol=symbol,
                status="blocked",
                reason="schwab_ineligible_cached",
                indicators=indicators,
            )
            return []
        if symbol in self.entry_blocked_symbols and not self._reactivate_lifecycle_from_signal(symbol, metrics, signal):
            return []
        open_intent, _routing_block_reason = self._try_emit_open_intent(signal)
        return [open_intent] if open_intent is not None else []

    def _resolve_tick_timestamp(self, timestamp_ns: int | None) -> float:
        normalized_timestamp_ns = self._normalize_tick_timestamp_ns(timestamp_ns)
        if normalized_timestamp_ns:
            return normalized_timestamp_ns / 1_000_000_000
        return self.now_provider().timestamp()

    def _normalize_tick_timestamp_ns(self, timestamp_ns: int | None) -> int | None:
        if not timestamp_ns:
            return None
        value = int(timestamp_ns)
        if value >= 1_000_000_000_000_000_000:
            return value
        if value >= 1_000_000_000_000_000:
            return value * 1_000
        if value >= 1_000_000_000_000:
            return value * 1_000_000
        if value >= 1_000_000_000:
            return value * 1_000_000_000
        return None

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

    def _scale_profile_for_symbol(self, symbol: str) -> str:
        if not self.lifecycle_policy.config.degraded_enabled:
            return "NORMAL"
        state = self.lifecycle_states.get(str(symbol).upper())
        if state is not None and state.degraded_mode:
            return "DEGRADED"
        return "NORMAL"

    def _emit_open_intent(self, signal: dict[str, float | int | str]) -> TradeIntentEvent:
        symbol = str(signal["ticker"])
        self.pending_open_symbols.add(symbol)
        reference_price = str(signal["price"])
        routed_price, routing_block_reason, routed_price_source = self._resolve_routed_price(
            symbol=symbol,
            side="buy",
            reference_price=reference_price,
            intent_label="entry",
        )
        if routed_price is None:
            self.pending_open_symbols.discard(symbol)
            raise RuntimeError(routing_block_reason or f"missing ask quote for extended-hours entry: {symbol}")
        breakdown_veto_reason = self._p4_entry_breakdown_veto_reason(
            signal=signal,
            routed_price=routed_price,
            routed_price_source=routed_price_source,
        )
        if breakdown_veto_reason is not None:
            self.pending_open_symbols.discard(symbol)
            raise RuntimeError(breakdown_veto_reason)
        metadata = {
            "path": str(signal["path"]),
            "score": str(signal["score"]),
            "score_details": str(signal["score_details"]),
            "timeframe_secs": str(self.definition.interval_secs),
            "reference_price": reference_price,
            "entry_stage": str(signal.get("entry_stage", "")),
        }
        if self.definition.trading_config.stop_guard_enabled:
            metadata.update(
                {
                    "stop_guard_enabled": "true",
                    "stop_loss_pct": str(self.definition.trading_config.stop_loss_pct),
                    "stop_guard_quote_max_age_ms": str(self.definition.trading_config.stop_guard_quote_max_age_ms),
                    "stop_guard_initial_panic_buffer_pct": str(
                        self.definition.trading_config.stop_guard_initial_panic_buffer_pct
                    ),
                }
            )
        metadata.update(order_routing_metadata(price=routed_price, side="buy", now=self.now_provider()))
        if routed_price_source:
            metadata["price_source"] = routed_price_source
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

    def _p4_entry_breakdown_veto_reason(
        self,
        *,
        signal: dict[str, float | int | str],
        routed_price: str | None,
        routed_price_source: str | None,
    ) -> str | None:
        if str(signal.get("path", "")).upper() != "P4_BURST":
            return None
        max_breakdown_pct = getattr(self.definition.trading_config, "p4_entry_max_breakdown_pct", None)
        if max_breakdown_pct is None:
            return None
        reference_value = _coerce_float(signal.get("price"))
        routed_value = _coerce_float(routed_price)
        if reference_value is None or routed_value is None or reference_value <= 0:
            return None
        min_allowed_price = reference_value * (1.0 - (float(max_breakdown_pct) / 100.0))
        if routed_value >= min_allowed_price:
            return None
        breakdown_pct = ((reference_value - routed_value) / reference_value) * 100.0
        source = routed_price_source or "reference"
        return (
            "P4 follow-through veto "
            f"({source} {routed_value:.4f} is {breakdown_pct:.2f}% below signal close "
            f"{reference_value:.4f}; max {float(max_breakdown_pct):.2f}%)"
        )

    def _try_emit_open_intent(
        self,
        signal: dict[str, float | int | str],
    ) -> tuple[TradeIntentEvent | None, str | None]:
        try:
            return self._emit_open_intent(signal), None
        except RuntimeError as exc:
            return None, str(exc)

    def _emit_close_intent(self, signal: dict[str, float | int | str]) -> TradeIntentEvent:
        symbol = str(signal["ticker"])
        self.pending_close_symbols.add(symbol)
        position = self.positions.get_position(symbol)
        quantity = Decimal(str(position.quantity if position else self.definition.trading_config.default_quantity))
        reference_price = str(signal.get("price", ""))
        routed_price, routing_block_reason, routed_price_source = self._resolve_routed_price(
            symbol=symbol,
            side="sell",
            reference_price=reference_price,
            intent_label="exit",
            signal=signal,
        )
        if routed_price is None:
            self.pending_close_symbols.discard(symbol)
            raise RuntimeError(routing_block_reason or f"missing bid quote for extended-hours exit: {symbol}")
        metadata = {
            "tier": str(signal.get("tier", "")),
            "profit_pct": str(signal.get("profit_pct", "")),
            "reference_price": reference_price,
        }
        is_stop_guard = str(signal.get("stop_guard", "")).lower() == "true"
        if is_stop_guard:
            metadata.update(
                {
                    "stop_guard": "true",
                    "stop_trigger_source": str(signal.get("stop_trigger_source", "")),
                    "stop_trigger_price": str(signal.get("stop_trigger_price", "")),
                    "stop_price": str(signal.get("stop_price", "")),
                    "panic_buffer_pct": str(signal.get("panic_buffer_pct", "")),
                }
            )
        if routed_price:
            if is_stop_guard:
                metadata.update(
                    stop_guard_order_routing_metadata(
                        price=routed_price,
                        price_source=routed_price_source or "reference",
                        now=self.now_provider(),
                    )
                )
            else:
                metadata.update(order_routing_metadata(price=routed_price, side="sell", now=self.now_provider()))
        if routed_price_source:
            metadata["price_source"] = routed_price_source
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

    def _safe_emit_close_intent(self, signal: dict[str, float | int | str]) -> TradeIntentEvent | None:
        try:
            return self._emit_close_intent(signal)
        except RuntimeError:
            return None

    def _emit_scale_intent(self, signal: dict[str, float | int | str]) -> TradeIntentEvent:
        symbol = str(signal["ticker"])
        level = str(signal["level"])
        self.pending_scale_levels.add((symbol, level))
        reference_price = str(signal.get("price", ""))
        routed_price, routing_block_reason, routed_price_source = self._resolve_routed_price(
            symbol=symbol,
            side="sell",
            reference_price=reference_price,
            intent_label="scale",
        )
        if routed_price is None:
            self.pending_scale_levels.discard((symbol, level))
            raise RuntimeError(routing_block_reason or f"missing bid quote for extended-hours scale: {symbol}")
        metadata = {
            "level": level,
            "sell_pct": str(signal["sell_pct"]),
            "profit_pct": str(signal["profit_pct"]),
            "reference_price": reference_price,
        }
        if routed_price:
            metadata.update(order_routing_metadata(price=routed_price, side="sell", now=self.now_provider()))
        if routed_price_source:
            metadata["price_source"] = routed_price_source
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

    def _safe_emit_scale_intent(self, signal: dict[str, float | int | str]) -> TradeIntentEvent | None:
        try:
            return self._emit_scale_intent(signal)
        except RuntimeError:
            return None

    def _resolve_routed_price(
        self,
        *,
        symbol: str,
        side: str,
        reference_price: str,
        intent_label: str,
        signal: dict[str, float | int | str] | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        quote = self.latest_quotes.get(symbol.upper(), {})
        quote_field = "ask" if side == "buy" else "bid"
        quote_price = _format_limit_price(quote.get(quote_field))
        signal = signal or {}
        is_stop_guard = (
            side == "sell"
            and str(signal.get("reason", "")).upper() == "HARD_STOP"
            and str(signal.get("stop_guard", "")).lower() == "true"
        )
        if is_stop_guard:
            panic_buffer_pct = float(signal.get("panic_buffer_pct", 0) or 0)
            bid_price = quote.get("bid")
            if bid_price is not None and self._has_fresh_quote(symbol):
                routed_price = _panic_limit_price(bid_price, panic_buffer_pct)
                if routed_price is not None:
                    return routed_price, None, "bid"
            routed_price = _panic_limit_price(reference_price, panic_buffer_pct)
            if routed_price is not None:
                stop_source = str(signal.get("stop_trigger_source", "")).lower() or "reference"
                return routed_price, None, "last" if stop_source == "last" else stop_source
            return None, f"missing {quote_field} quote for extended-hours {intent_label}", None
        session = extended_hours_session(self.now_provider())
        if session is None:
            routed_price = quote_price or _format_limit_price(reference_price) or reference_price
            return routed_price, None, quote_field if quote_price else "reference"
        if quote_price:
            return quote_price, None, quote_field
        if side == "sell":
            routed_price = _format_limit_price(reference_price) or reference_price
            if routed_price:
                return routed_price, None, "reference"
        return None, f"missing {quote_field} quote for extended-hours {intent_label}", None

    def _is_exit_retry_blocked(self, symbol: str) -> bool:
        blocked_until = self.exit_retry_blocked_until.get(symbol)
        return blocked_until is not None and utcnow() < blocked_until

    def _is_scale_retry_blocked(self, symbol: str, level: str) -> bool:
        blocked_until = self.scale_retry_blocked_until.get((symbol, level))
        return blocked_until is not None and utcnow() < blocked_until

    @staticmethod
    def _should_apply_polygon_rejected_open_cooldown(reason: str) -> bool:
        if not reason:
            return True
        infrastructure_reasons = (
            "missing webull app key/app secret",
            "broker auth is not configured yet",
            "missing webull account id",
            "official order submission is not implemented yet",
        )
        return not any(marker in reason for marker in infrastructure_reasons)

    def _gap_recovery_bars_required(self) -> int:
        interval = max(1, int(self.definition.interval_secs))
        return max(2, int((90 + interval - 1) // interval))

    def _trading_window_open(self) -> bool:
        current = self.now_provider()
        config = self.definition.trading_config
        if current.hour < config.trading_start_hour or current.hour >= config.trading_end_hour:
            return False
        time_str = current.strftime("%H:%M")
        if config.dead_zone_start <= time_str < config.dead_zone_end:
            return False
        return True

    def _should_track_gap_recovery(self, symbol: str) -> bool:
        normalized = str(symbol).upper()
        if self.positions.get_position(normalized) is not None:
            return True
        if normalized in self.pending_open_symbols or normalized in self.pending_close_symbols:
            return True
        if any(pending_symbol == normalized for pending_symbol, _level in self.pending_scale_levels):
            return True
        if self._is_data_halted(normalized):
            return True
        if normalized in self.data_warning_symbols:
            return True
        return False

    def _clear_gap_recovery(self, symbol: str) -> None:
        normalized = str(symbol).upper()
        self._gap_recovery_bars_remaining.pop(normalized, None)
        self._gap_recovery_synthetic_bars.pop(normalized, None)

    def _arm_gap_recovery(self, symbol: str, *, synthetic_gap_count: int) -> None:
        normalized = str(symbol).upper()
        if not normalized or synthetic_gap_count <= 0:
            return
        self._gap_recovery_bars_remaining[normalized] = max(
            self._gap_recovery_bars_remaining.get(normalized, 0),
            self._gap_recovery_bars_required(),
        )
        self._gap_recovery_synthetic_bars[normalized] = max(
            self._gap_recovery_synthetic_bars.get(normalized, 0),
            int(synthetic_gap_count),
        )

    def _advance_gap_recovery(self, symbol: str, completed_bar: OHLCVBar) -> None:
        normalized = str(symbol).upper()
        remaining = self._gap_recovery_bars_remaining.get(normalized)
        if remaining is None:
            return
        is_real_bar = int(getattr(completed_bar, "trade_count", 0) or 0) > 0 or int(
            getattr(completed_bar, "volume", 0) or 0
        ) > 0
        if not is_real_bar:
            return
        remaining -= 1
        if remaining <= 0:
            self._gap_recovery_bars_remaining.pop(normalized, None)
            self._gap_recovery_synthetic_bars.pop(normalized, None)
            return
        self._gap_recovery_bars_remaining[normalized] = remaining

    def _is_gap_recovery_active(self, symbol: str) -> bool:
        return self._gap_recovery_bars_remaining.get(str(symbol).upper(), 0) > 0

    def _gap_recovery_reason(self, symbol: str) -> str:
        normalized = str(symbol).upper()
        remaining = self._gap_recovery_bars_remaining.get(normalized, 0)
        synthetic = self._gap_recovery_synthetic_bars.get(normalized, 0)
        interval = max(1, int(self.definition.interval_secs))
        return (
            f"live feed gap recovery active: skipped {synthetic} synthetic {interval}s bar(s); "
            f"waiting for {remaining} real completed bar(s) before trusting new entries"
        )

    def _has_pending_scale_for_symbol(self, symbol: str) -> bool:
        normalized = symbol.upper()
        return any(pending_symbol == normalized for pending_symbol, _level in self.pending_scale_levels)

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

    def _live_decision_rows(self) -> list[dict[str, str]]:
        live_symbols = self.active_symbols()
        if not live_symbols:
            return []
        return [
            self._decision_display_row(item)
            for item in self.recent_decisions
            if str(item.get("symbol", "")).upper() in live_symbols
        ]

    @staticmethod
    def _decision_display_row(item: dict[str, str]) -> dict[str, str]:
        row = dict(item)
        status = str(row.get("status", "")).lower()
        reason = str(row.get("reason", "")).lower()
        if status == "idle" and reason == "no entry path matched":
            row["status"] = "evaluated"
            row["reason"] = "entry evaluated; no setup matched this bar"
        return row

    def _bar_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for symbol in self.builder_manager.get_all_tickers():
            builder = self.builder_manager.get_builder(symbol)
            if builder is None:
                continue
            if hasattr(builder, "bars"):
                counts[str(symbol).upper()] = len(getattr(builder, "bars"))
            else:
                counts[str(symbol).upper()] = len(builder.get_bars_as_dicts())
        return counts

    def _last_tick_summary(self) -> dict[str, str]:
        return {
            str(symbol).upper(): _datetime_str(observed_at)
            for symbol, observed_at in sorted(self._last_tick_at.items())
        }

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
        bar_timestamp = indicators.get("bar_timestamp")
        if bar_timestamp is not None:
            try:
                bar_time = datetime.fromtimestamp(float(bar_timestamp), UTC).astimezone(EASTERN_TZ).isoformat()
            except (TypeError, ValueError, OSError):
                bar_time = ""
        elif builder is not None and builder.bars:
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
        completed_bar: OHLCVBar | None = None,
    ) -> list[TradeIntentEvent]:
        if decision is None:
            position_state, _position_quantity = self._position_snapshot(symbol)
            if position_state == "open":
                decision = self._record_decision(
                    symbol=symbol,
                    status="position_open",
                    reason="position open",
                    indicators=indicators,
                )
            elif position_state == "pending_open":
                decision = self._record_decision(
                    symbol=symbol,
                    status="pending_open",
                    reason="awaiting open fill",
                    indicators=indicators,
                )
            elif position_state == "pending_close":
                decision = self._record_decision(
                    symbol=symbol,
                    status="pending_close",
                    reason="awaiting close fill",
                    indicators=indicators,
                )
            elif position_state == "pending_scale":
                decision = self._record_decision(
                    symbol=symbol,
                    status="pending_scale",
                    reason="awaiting scale fill",
                    indicators=indicators,
                )
            else:
                decision = self._record_decision(
                    symbol=symbol,
                    status="idle",
                    reason="no entry path matched",
                    indicators=indicators,
                )
        self._persist_bar_history(
            symbol=symbol,
            indicators=indicators,
            decision=decision,
            completed_bar=completed_bar,
        )
        return intents

    def _persist_bar_history(
        self,
        *,
        symbol: str,
        indicators: dict[str, object],
        decision: dict[str, str] | None = None,
        completed_bar: OHLCVBar | None = None,
    ) -> None:
        if self.session_factory is None:
            return

        builder = self.builder_manager.get_builder(symbol)
        if builder is None:
            return

        last_bar = completed_bar
        if last_bar is None:
            if not builder.bars:
                return
            last_bar = builder.bars[-1]
        # Skip persisting placeholder bars. vol=0 + tc=0 means either a
        # CHART_EQUITY quiet-minute report or a bar-builder force-close when
        # no trades arrived during the bar window (e.g., mid-bar symbol
        # promotion with no subsequent fills). OHLC on such bars is stale
        # (carried from prior close), so writing them pollutes
        # strategy_bar_history with idle rows whose price values don't reflect
        # the actual market state. Real trades arriving later go through
        # _persist_revised_closed_bar, which creates the row at revision time.
        if int(last_bar.volume) == 0 and int(last_bar.trade_count) == 0:
            return

        bar_time = datetime.fromtimestamp(last_bar.timestamp, UTC)
        position_state, position_quantity = self._position_snapshot(symbol)
        indicator_payload = self._build_history_indicator_payload(indicators)

        # Capture-at-enqueue: snapshot every value the DB write needs so the
        # worker thread never reads mutable runtime/builder state later.
        payload: dict[str, object] = {
            "symbol": symbol,
            "strategy_code": self.definition.code,
            "interval_secs": self.definition.interval_secs,
            "bar_time": bar_time,
            "open": Decimal(str(last_bar.open)),
            "high": Decimal(str(last_bar.high)),
            "low": Decimal(str(last_bar.low)),
            "close": Decimal(str(last_bar.close)),
            "volume": int(last_bar.volume),
            "trade_count": int(last_bar.trade_count),
            "position_state": position_state,
            "position_quantity": position_quantity,
            "decision_status": str((decision or {}).get("status", "")),
            "decision_reason": str((decision or {}).get("reason", "")),
            "decision_path": str((decision or {}).get("path", "")),
            "decision_score": str((decision or {}).get("score", "")),
            "decision_score_details": str((decision or {}).get("score_details", "")),
            "indicators_json": indicator_payload,
        }

        if not self._persist_offload_enabled:
            try:
                with self.session_factory() as session:
                    self._write_bar_history_record(payload, session)
                    session.commit()
            except Exception:
                logger.exception(
                    "failed to persist strategy bar history for %s %s",
                    self.definition.code,
                    symbol,
                )
            return

        self._pending_persist_writes.append(("bar", payload))

    def _write_bar_history_record(self, payload: dict[str, object], session: Session) -> None:
        """Run the bar-history SELECT + upsert against ``session`` (no commit).

        Logic moved verbatim from the inline ``_persist_bar_history`` path; the
        caller owns the commit so the same code serves both the synchronous
        inline path and the off-loop batched flush.
        """
        symbol = payload["symbol"]
        bar_time = payload["bar_time"]
        record = session.scalar(
            select(StrategyBarHistory).where(
                StrategyBarHistory.strategy_code.in_(strategy_code_candidates(self.definition.code)),
                StrategyBarHistory.symbol == symbol,
                StrategyBarHistory.interval_secs == payload["interval_secs"],
                StrategyBarHistory.bar_time == bar_time,
            )
        )
        if record is None:
            record = StrategyBarHistory(
                strategy_code=payload["strategy_code"],
                symbol=symbol,
                interval_secs=payload["interval_secs"],
                bar_time=bar_time,
                open_price=payload["open"],
                high_price=payload["high"],
                low_price=payload["low"],
                close_price=payload["close"],
                volume=payload["volume"],
                trade_count=payload["trade_count"],
            )
            session.add(record)

        record.open_price = payload["open"]
        record.high_price = payload["high"]
        record.low_price = payload["low"]
        record.close_price = payload["close"]
        record.volume = payload["volume"]
        record.trade_count = payload["trade_count"]
        record.position_state = payload["position_state"]
        record.position_quantity = payload["position_quantity"]
        record.decision_status = payload["decision_status"]
        record.decision_reason = payload["decision_reason"]
        record.decision_path = payload["decision_path"]
        record.decision_score = payload["decision_score"]
        record.decision_score_details = payload["decision_score_details"]
        record.indicators_json = payload["indicators_json"]

    def _persist_revised_closed_bar(
        self,
        *,
        symbol: str,
        bar: OHLCVBar,
    ) -> None:
        if self.session_factory is None:
            return

        bar_time = datetime.fromtimestamp(float(bar.timestamp), UTC)
        # Capture-at-enqueue: snapshot the bar values and the position state now
        # (position_state/quantity is only used on the INSERT branch, but it
        # reads mutable runtime state so must be captured at call time rather
        # than at off-loop write time).
        position_state, position_quantity = self._position_snapshot(symbol)
        payload: dict[str, object] = {
            "symbol": symbol,
            "strategy_code": self.definition.code,
            "interval_secs": self.definition.interval_secs,
            "bar_time": bar_time,
            "open": Decimal(str(bar.open)),
            "high": Decimal(str(bar.high)),
            "low": Decimal(str(bar.low)),
            "close": Decimal(str(bar.close)),
            "volume": int(bar.volume),
            "trade_count": int(bar.trade_count),
            "position_state": position_state,
            "position_quantity": position_quantity,
        }

        if not self._persist_offload_enabled:
            try:
                with self.session_factory() as session:
                    self._write_revised_bar_record(payload, session)
                    session.commit()
                    self._log_revise_persist(payload)
            except Exception:
                logger.exception(
                    "failed to persist revised strategy bar history for %s %s",
                    self.definition.code,
                    symbol,
                )
            return

        self._pending_persist_writes.append(("revise", payload))

    def _write_revised_bar_record(self, payload: dict[str, object], session: Session) -> None:
        """Run the revised-bar SELECT + upsert against ``session`` (no commit).

        Logic moved verbatim from the inline ``_persist_revised_closed_bar``
        path, preserving the insert/update decision-field rules. The caller owns
        the commit and the ``[STRATEGY-REVISE-PERSIST]`` logging. ``record_action``
        and ``vol_before`` are stashed back into ``payload`` so the logging
        helper can report them after the commit succeeds.
        """
        symbol = payload["symbol"]
        bar_time = payload["bar_time"]
        record = session.scalar(
            select(StrategyBarHistory).where(
                StrategyBarHistory.strategy_code.in_(strategy_code_candidates(self.definition.code)),
                StrategyBarHistory.symbol == symbol,
                StrategyBarHistory.interval_secs == payload["interval_secs"],
                StrategyBarHistory.bar_time == bar_time,
            )
        )
        record_action = "update"
        vol_before = int(record.volume) if record is not None else None
        if record is None:
            record_action = "insert"
            # 2026-05-18 fix: this insert happens when the live
            # _persist_completed_bar never ran for this bar (e.g.,
            # restart during the close window, or the bar lived in
            # the builder's history but the engine missed
            # _evaluate_completed_bar). Stamp a sentinel decision so
            # the decision tape clearly shows "this row exists
            # because a late tick filled in the OHLCV -- no entry
            # decision was ever made" instead of an empty row.
            record = StrategyBarHistory(
                strategy_code=payload["strategy_code"],
                symbol=symbol,
                interval_secs=payload["interval_secs"],
                bar_time=bar_time,
                position_state=payload["position_state"],
                position_quantity=payload["position_quantity"],
                decision_status="late_revision",
                decision_reason=(
                    "Bar persisted via late-trade revision path; "
                    "no entry decision was recorded at the bar's "
                    "original close (likely restart or missed close event)"
                ),
            )
            session.add(record)

        record.open_price = payload["open"]
        record.high_price = payload["high"]
        record.low_price = payload["low"]
        record.close_price = payload["close"]
        record.volume = payload["volume"]
        record.trade_count = payload["trade_count"]
        # NB: decision_status / decision_reason / decision_path /
        # decision_score are intentionally NOT overwritten on
        # update. If the row already exists, it had a real entry
        # decision recorded at the bar's original close; preserve
        # it. The revision only updates OHLCV / volume / trade_count.
        payload["record_action"] = record_action
        payload["vol_before"] = vol_before

    def _log_revise_persist(self, payload: dict[str, object]) -> None:
        bar_time = payload["bar_time"]
        record_action = payload.get("record_action", "update")
        vol_before = payload.get("vol_before")
        persist_lag_secs = (datetime.now(UTC) - bar_time).total_seconds()
        logger.info(
            "[STRATEGY-REVISE-PERSIST] strategy=%s symbol=%s bar_ts=%s action=%s vol_before=%s vol_after=%d trade_count=%d persist_lag_secs=%.1f",
            self.definition.code,
            payload["symbol"],
            bar_time.isoformat(),
            record_action,
            vol_before,
            int(payload["volume"]),
            int(payload["trade_count"]),
            persist_lag_secs,
        )
        if persist_lag_secs > 120.0:
            # Issue #145: bar updated more than 2 minutes after it was
            # supposed to close. MOBX 2026-05-14 showed updated_at
            # ~10 minutes after bar_time when the stall happened.
            logger.warning(
                "[STRATEGY-REVISE-PERSIST-LAG] strategy=%s symbol=%s bar_ts=%s persist_lag_secs=%.1f action=%s vol_after=%d "
                "— bar revised long after its scheduled close, likely a check_bar_closes stall",
                self.definition.code,
                payload["symbol"],
                bar_time.isoformat(),
                persist_lag_secs,
                record_action,
                int(payload["volume"]),
            )

    def flush_pending_persists(self) -> None:
        """Flush buffered persist writes off the event loop (one session).

        Runs the buffered SELECT+upsert+commit for each enqueued write IN ORDER
        under a single session so a revise sees the base row it depends on. Per
        item is isolated by try/except + per-item commit so one bad row never
        loses the others — matching the per-call swallow behaviour of the inline
        path. Intended to be invoked via ``asyncio.to_thread`` so the whole
        method (including DB I/O) runs on a worker thread, not the event loop.
        """
        if not self._pending_persist_writes:
            return
        if self.session_factory is None:
            self._pending_persist_writes = []
            return
        # Snapshot + clear first so writes enqueued during the flush survive to
        # the next flush instead of being dropped.
        writes = self._pending_persist_writes
        self._pending_persist_writes = []
        with self.session_factory() as session:
            for kind, payload in writes:
                try:
                    if kind == "bar":
                        self._write_bar_history_record(payload, session)
                        session.commit()
                    else:
                        self._write_revised_bar_record(payload, session)
                        session.commit()
                        self._log_revise_persist(payload)
                except Exception:
                    logger.exception(
                        "failed to flush buffered %s persist for %s %s",
                        kind,
                        self.definition.code,
                        payload.get("symbol"),
                    )
                    session.rollback()
                    continue

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

        builder = self.builder_manager.get_builder(symbol)
        bar_dicts = builder.get_bars_as_dicts() if builder is not None and builder.bars else []
        last_bar = builder.bars[-1] if builder is not None and builder.bars else None

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
            self._apply_extended_hours_vwap_override(symbol, indicators, bar_dicts=bar_dicts)
            return indicators

        if builder is not None and builder.bars and last_bar is not None:
            bar_time = datetime.fromtimestamp(last_bar.timestamp, UTC)
            if self.definition.interval_secs == 30:
                fetch_overlay = getattr(self.indicator_overlay_provider, "fetch_aggregate_overlay", None)
                if fetch_overlay is not None:
                    overlay = fetch_overlay(
                        symbol,
                        bar_time=bar_time,
                        interval_secs=self.definition.interval_secs,
                    )
                    indicators.update(overlay)
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
            elif str(indicators.get("provider_status", "")) == "ready":
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

        self._apply_extended_hours_vwap_override(symbol, indicators, bar_dicts=bar_dicts)
        return indicators

    def _apply_extended_hours_vwap_override(
        self,
        symbol: str,
        indicators: dict[str, object],
        *,
        bar_dicts: Sequence[dict[str, float | int]],
    ) -> None:
        base_vwap = float(indicators.get("vwap", 0) or 0)
        indicators.setdefault("extended_vwap", base_vwap)
        indicators.setdefault("decision_vwap", base_vwap)
        indicators.setdefault("selected_vwap", base_vwap)

        if not bar_dicts or self.extended_hours_vwap_provider is None:
            return
        if bool(indicators.get("in_regular_session", False)):
            return

        try:
            current_timestamp = float(bar_dicts[-1].get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            return
        if current_timestamp <= 0:
            return

        timestamps = [current_timestamp]
        previous_timestamp = current_timestamp
        if len(bar_dicts) > 1:
            try:
                previous_timestamp = float(bar_dicts[-2].get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                previous_timestamp = current_timestamp
            if previous_timestamp > 0:
                timestamps.insert(0, previous_timestamp)

        series = self.extended_hours_vwap_provider(symbol, timestamps, int(self.definition.interval_secs))
        current_vwap = float(series.get(current_timestamp, 0) or 0)
        if current_vwap <= 0:
            return

        previous_vwap = float(series.get(previous_timestamp, current_vwap) or current_vwap)
        current_price = float(indicators.get("price", bar_dicts[-1].get("close", 0)) or 0)
        if len(bar_dicts) > 1:
            previous_price = float(indicators.get("price_prev", bar_dicts[-2].get("close", current_price)) or current_price)
        else:
            previous_price = current_price

        indicators["extended_vwap"] = current_vwap
        indicators["vwap"] = current_vwap
        indicators["decision_vwap"] = current_vwap
        indicators["selected_vwap"] = current_vwap
        indicators["price_above_vwap"] = current_price > current_vwap
        indicators["price_above_extended_vwap"] = current_price > current_vwap
        cross_above = current_price > current_vwap and previous_price <= previous_vwap
        indicators["price_cross_above_vwap"] = cross_above
        indicators["price_cross_above_extended_vwap"] = cross_above
        indicators["vwap_dist_pct"] = (
            ((current_price - current_vwap) / current_vwap) * 100 if current_vwap > 0 else 999.0
        )

    def _prune_runtime_state(self) -> None:
        keep = set(self.watchlist)
        keep.update(self.pending_open_symbols)
        keep.update(self.pending_close_symbols)
        keep.update(symbol for symbol, _level in self.pending_scale_levels)
        keep.update(position["ticker"] for position in self.positions.get_all_positions())
        keep.update(self.prewarm_symbols)
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
        self._last_quote_received_at = {
            symbol: received_at
            for symbol, received_at in self._last_quote_received_at.items()
            if symbol in keep
        }
        self._gap_recovery_bars_remaining = {
            symbol: remaining
            for symbol, remaining in self._gap_recovery_bars_remaining.items()
            if symbol in keep
        }
        self._gap_recovery_synthetic_bars = {
            symbol: synthetic
            for symbol, synthetic in self._gap_recovery_synthetic_bars.items()
            if symbol in keep
        }
        self._live_aggregate_skipped_bucket_start = {
            symbol: bucket_start
            for symbol, bucket_start in self._live_aggregate_skipped_bucket_start.items()
            if symbol in keep
        }
        self._recent_live_bar_delivery = {
            symbol: entries
            for symbol, entries in self._recent_live_bar_delivery.items()
            if symbol in keep
        }
        self.entry_engine.prune_tickers(keep)
        self.builder_manager.remove_tickers(
            {ticker for ticker in self.builder_manager.get_all_tickers() if ticker not in keep}
        )

    def _sync_watchlist_from_lifecycle(self) -> None:
        watchlist = set(self._desired_watchlist_symbols)
        watchlist.update(
            symbol
            for symbol, state in self.lifecycle_states.items()
            if state.keeps_feed()
        )
        watchlist.update(self.pending_open_symbols)
        watchlist.update(self.pending_close_symbols)
        watchlist.update(symbol for symbol, _level in self.pending_scale_levels)
        watchlist.update(position["ticker"] for position in self.positions.get_all_positions())
        self.watchlist = watchlist
        self.entry_blocked_symbols = self._blocked_lifecycle_symbols()

    def _blocked_lifecycle_symbols(self) -> set[str]:
        blocked = {
            symbol
            for symbol, state in self.lifecycle_states.items()
            if state.blocks_entries()
        }
        blocked.update(self.manual_stop_symbols)
        blocked.update(self.broker_blocked_symbols)
        return blocked

    def _symbol_requires_feed(self, symbol: str) -> bool:
        normalized = str(symbol).upper()
        if normalized in self.pending_open_symbols or normalized in self.pending_close_symbols:
            return True
        if any(pending_symbol == normalized for pending_symbol, _level in self.pending_scale_levels):
            return True
        return self.positions.has_position(normalized)

    def _build_lifecycle_metrics(
        self,
        symbol: str,
        indicators: dict[str, float | bool],
        builder: BarBuilderManager | SchwabNativeBarBuilderManager | Polygon30sBarBuilderManager,
    ) -> FeedRetentionMetrics | None:
        runtime_builder = builder.get_builder(symbol)
        if runtime_builder is None:
            return None
        bars = runtime_builder.get_bars_as_dicts()
        if not bars:
            return None

        def _ema_series(closes: list[float], period: int) -> list[float]:
            if not closes:
                return []
            alpha = 2.0 / (period + 1.0)
            ema_values = [closes[0]]
            for close in closes[1:]:
                ema_values.append((close * alpha) + (ema_values[-1] * (1.0 - alpha)))
            return ema_values

        def _strictly_trending(values: list[float], *, rising: bool) -> bool:
            if len(values) < 4:
                return False
            window = values[-4:]
            comparisons = zip(window, window[1:], strict=False)
            if rising:
                return all(prev < current for prev, current in comparisons)
            return all(prev > current for prev, current in comparisons)

        def _strict_structure(values: list[float], *, rising: bool) -> bool:
            if len(values) < 4:
                return False
            window = values[-4:]
            comparisons = zip(window, window[1:], strict=False)
            if rising:
                return all(prev < current for prev, current in comparisons)
            return all(prev > current for prev, current in comparisons)

        bar_window = max(1, int(300 / max(1, self.definition.interval_secs)))
        recent_bars = bars[-bar_window:]
        lows = [float(bar.get("low", 0) or 0) for bar in recent_bars if float(bar.get("low", 0) or 0) > 0]
        highs = [float(bar.get("high", 0) or 0) for bar in recent_bars]
        closes = [float(bar.get("close", 0) or 0) for bar in bars if float(bar.get("close", 0) or 0) > 0]
        ema9_series = _ema_series(closes[-20:], 9)
        recent_5_bars = bars[-5:]
        recent_20_bars = bars[-20:]
        avg_bar_volume_5 = None
        avg_bar_volume_20 = None
        if recent_5_bars:
            avg_bar_volume_5 = float(
                sum(float(bar.get("volume", 0) or 0) for bar in recent_5_bars) / len(recent_5_bars)
            )
        if recent_20_bars:
            avg_bar_volume_20 = float(
                sum(float(bar.get("volume", 0) or 0) for bar in recent_20_bars) / len(recent_20_bars)
            )
        last_bar = bars[-1]
        recent_highs = [float(bar.get("high", 0) or 0) for bar in bars[-4:]]
        recent_closes = [float(bar.get("close", 0) or 0) for bar in bars[-4:]]
        lower_highs_or_closes = _strict_structure(recent_highs, rising=False) or _strict_structure(
            recent_closes,
            rising=False,
        )
        higher_highs_or_closes = _strict_structure(recent_highs, rising=True) or _strict_structure(
            recent_closes,
            rising=True,
        )
        rolling_range_pct = None
        if lows and highs:
            floor = min(lows)
            ceiling = max(highs)
            if floor > 0 and ceiling >= floor:
                rolling_range_pct = ((ceiling - floor) / floor) * 100.0
        return FeedRetentionMetrics(
            price=_coerce_float(indicators.get("price")),
            ema9=_coerce_float(indicators.get("ema9")),
            vwap=_coerce_float(indicators.get("selected_vwap"), indicators.get("vwap")),
            ema20=_coerce_float(indicators.get("ema20")),
            rolling_5m_volume=float(sum(float(bar.get("volume", 0) or 0) for bar in recent_bars)),
            rolling_5m_range_pct=rolling_range_pct,
            avg_bar_volume_5=avg_bar_volume_5,
            avg_bar_volume_20=avg_bar_volume_20,
            latest_bar_volume=float(last_bar.get("volume", 0) or 0),
            latest_bar_red=float(last_bar.get("close", 0) or 0) < float(last_bar.get("open", 0) or 0),
            ema9_falling=_strictly_trending(ema9_series, rising=False),
            ema9_rising=_strictly_trending(ema9_series, rising=True),
            lower_highs_or_closes=lower_highs_or_closes,
            higher_highs_or_closes=higher_highs_or_closes,
            total_bars=len(bars),
            bar_timestamp=float(bars[-1].get("timestamp", 0) or 0),
        )

    def _update_symbol_lifecycle(
        self,
        symbol: str,
        *,
        metrics: FeedRetentionMetrics | None,
    ) -> None:
        state = self.lifecycle_states.get(symbol)
        if state is None:
            return
        previous_state = state.state
        next_state = self.lifecycle_policy.evaluate(
            state,
            symbol=symbol,
            now=self.now_provider(),
            is_confirmed=False,
            metrics=metrics,
        )
        if next_state is None:
            return
        if next_state.state == "dropped" and (
            self._symbol_requires_feed(symbol) or symbol in self._desired_watchlist_symbols
        ):
            next_state.state = previous_state
        self.lifecycle_states[symbol] = next_state
        self._sync_watchlist_from_lifecycle()

    def _reactivate_lifecycle_from_signal(
        self,
        symbol: str,
        metrics: FeedRetentionMetrics | None,
        signal: dict[str, float | int | str] | None = None,
    ) -> bool:
        state = self.lifecycle_states.get(symbol)
        if state is None or state.state not in {"cooldown", "resume_probe"}:
            return state is not None and state.state == "active"
        if not self._signal_can_reactivate_lifecycle(signal, metrics):
            return False
        now = self.now_provider()
        self.lifecycle_policy._transition(state, "active", now)
        state.last_activity_at = now
        state.cooldown_started_at = None
        state.above_structure_bars = 0
        state.below_structure_bars = 0
        self.lifecycle_policy._refresh_reference_volume(state, metrics)
        self._sync_watchlist_from_lifecycle()
        return True

    def _signal_can_reactivate_lifecycle(
        self,
        signal: dict[str, float | int | str] | None,
        metrics: FeedRetentionMetrics | None,
    ) -> bool:
        path = str((signal or {}).get("path", "")).upper()
        if path.startswith("P4"):
            return True
        if metrics is None:
            return False
        return self.lifecycle_policy._is_above_structure(metrics)

    def _lifecycle_state_summary(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for symbol, state in sorted(self.lifecycle_states.items()):
            rows.append(
                {
                    "ticker": symbol,
                    "state": state.state,
                    "blocks_entries": state.blocks_entries(),
                    "keeps_feed": state.keeps_feed(),
                    "promoted_at": state.promoted_at.isoformat(),
                    "last_confirmed_at": state.last_confirmed_at.isoformat(),
                    "state_changed_at": state.state_changed_at.isoformat(),
                    "cooldown_started_at": state.cooldown_started_at.isoformat() if state.cooldown_started_at else "",
                    "below_structure_bars": state.below_structure_bars,
                    "above_structure_bars": state.above_structure_bars,
                    "active_reference_5m_volume": state.active_reference_5m_volume,
                    "degraded_mode": state.degraded_mode,
                    "degraded_since": state.degraded_since.isoformat() if state.degraded_since else "",
                    "degraded_score": state.degraded_score,
                    "recovery_score": state.recovery_score,
                    "degraded_enter_streak_bars": state.degraded_enter_streak_bars,
                    "degraded_exit_streak_bars": state.degraded_exit_streak_bars,
                }
            )
        return rows

    def refresh_lifecycle(self) -> None:
        for symbol in list(self.lifecycle_states):
            indicators = self.last_indicators.get(symbol)
            metrics = None
            if indicators:
                metrics = self._build_lifecycle_metrics(symbol, indicators, self.builder_manager)
            self._update_symbol_lifecycle(symbol, metrics=metrics)


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
        self._schwab_stream_bot_codes = self._resolve_schwab_stream_bot_codes()
        self._schwab_native_history_bot_codes = self._resolve_schwab_native_history_bot_codes()
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
        self.retained_watchlist: list[str] = []
        self.market_data_archive_symbols: list[str] = []
        self._market_data_archive_added_at: dict[str, datetime] = {}
        self.market_data_archive_ttl = timedelta(
            minutes=max(1, int(self.settings.market_data_archive_retention_minutes))
        )
        self.market_data_archive_max_symbols = max(
            0, int(self.settings.market_data_archive_retention_max_symbols)
        )
        self.schwab_prewarm_symbols: list[str] = []
        self._schwab_prewarm_added_at: dict[str, datetime] = {}
        self.schwab_prewarm_max_symbols = 12
        self.schwab_prewarm_ttl = timedelta(
            seconds=max(1.0, float(self.settings.schwab_prewarm_symbol_ttl_seconds))
        )
        self._broker_blocked_symbols_by_strategy: dict[str, set[str]] = {}
        self.five_pillars: list[dict[str, object]] = []
        self.top_gainers: list[dict[str, object]] = []
        self.top_gainer_changes: list[dict[str, object]] = []
        self.recent_alerts: list[dict[str, object]] = []
        self.today_alerts: list[dict[str, object]] = []
        self.alert_warmup: dict[str, object] = self.alert_engine.get_warmup_status()
        self.cycle_count = 0
        self.latest_snapshots: dict[str, MarketSnapshot] = {}
        self._first_seen_by_ticker: dict[str, str] = {}
        self._seeded_confirmed_pending_revalidation = False
        self._pending_recent_alert_replay = False
        self._active_scanner_session_start = current_scanner_session_start_utc(
            self.alert_engine.now_provider()
        )
        self.feed_retention_policy = FeedRetentionPolicy(
            FeedRetentionConfig(
                enabled=self.settings.scanner_feed_retention_enabled,
                structure_bars=max(1, int(self.settings.scanner_feed_retention_structure_bars)),
                no_activity_minutes=max(1, int(self.settings.scanner_feed_retention_no_activity_minutes)),
                cooldown_volume_ratio=max(0.0, float(self.settings.scanner_feed_retention_cooldown_volume_ratio)),
                cooldown_max_5m_range_pct=max(0.0, float(self.settings.scanner_feed_retention_cooldown_max_5m_range_pct)),
                resume_hold_bars=max(1, int(self.settings.scanner_feed_retention_resume_hold_bars)),
                resume_min_5m_range_pct=max(0.0, float(self.settings.scanner_feed_retention_resume_min_5m_range_pct)),
                resume_min_5m_volume_ratio=max(0.0, float(self.settings.scanner_feed_retention_resume_min_5m_volume_ratio)),
                resume_min_5m_volume_abs=max(0.0, float(self.settings.scanner_feed_retention_resume_min_5m_volume_abs)),
                drop_cooldown_minutes=max(1, int(self.settings.scanner_feed_retention_drop_cooldown_minutes)),
                drop_max_5m_range_pct=max(0.0, float(self.settings.scanner_feed_retention_drop_max_5m_range_pct)),
                drop_max_5m_volume_abs=max(0.0, float(self.settings.scanner_feed_retention_drop_max_5m_volume_abs)),
            )
        )
        logger.info(
            "feed retention config | enabled=%s structure_bars=%s no_activity_min=%s cooldown_volume_ratio=%.2f cooldown_max_5m_range_pct=%.2f resume_hold_bars=%s resume_min_5m_range_pct=%.2f resume_min_5m_volume_ratio=%.2f resume_min_5m_volume_abs=%.0f drop_cooldown_min=%s drop_max_5m_range_pct=%.2f drop_max_5m_volume_abs=%.0f degraded_enabled=%s",
            self.feed_retention_policy.config.enabled,
            self.feed_retention_policy.config.structure_bars,
            self.feed_retention_policy.config.no_activity_minutes,
            self.feed_retention_policy.config.cooldown_volume_ratio,
            self.feed_retention_policy.config.cooldown_max_5m_range_pct,
            self.feed_retention_policy.config.resume_hold_bars,
            self.feed_retention_policy.config.resume_min_5m_range_pct,
            self.feed_retention_policy.config.resume_min_5m_volume_ratio,
            self.feed_retention_policy.config.resume_min_5m_volume_abs,
            self.feed_retention_policy.config.drop_cooldown_minutes,
            self.feed_retention_policy.config.drop_max_5m_range_pct,
            self.feed_retention_policy.config.drop_max_5m_volume_abs,
            self.feed_retention_policy.config.degraded_enabled,
        )
        self.feed_retention_states: dict[str, RetainedSymbolState] = {}
        self.global_manual_stop_symbols: set[str] = set()
        self.manual_stop_symbols_by_strategy: dict[str, set[str]] = {}
        self.bot_handoff_symbols_by_strategy: dict[str, set[str]] = {}
        self.bot_handoff_history_by_strategy: dict[str, set[str]] = {}
        self.session_handoff_active = False
        self._schwab_stream_bot_codes = self._resolve_schwab_stream_bot_codes()
        self._schwab_native_history_bot_codes = self._resolve_schwab_native_history_bot_codes()
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
        polygon_30s_trading = self._resolve_30s_trading_config(base_trading, variant="polygon")
        macd_30s_probe_trading = self._resolve_30s_trading_config(base_trading, variant="probe")
        macd_30s_reclaim_trading = self._resolve_30s_trading_config(base_trading, variant="reclaim")
        macd_30s_retest_trading = self._resolve_30s_trading_config(base_trading, variant="retest")
        macd_30s_retention = self._build_bot_feed_retention_config(interval_secs=30)
        one_minute_retention = self._build_bot_feed_retention_config(interval_secs=60)
        default_indicator_config = indicator_config or IndicatorConfig()
        runner_trading = base_trading.make_tos_variant(quantity=100, bar_interval_secs=60)
        schwab_1m_trading = self._resolve_1m_trading_config(base_trading, variant="schwab")
        use_live_aggregate_bars = (
            self.settings.strategy_macd_30s_live_aggregate_bars_enabled
            or self.settings.market_data_live_aggregate_stream_enabled
        )
        polygon_use_live_aggregate_bars = self.settings.strategy_polygon_30s_runtime_uses_live_aggregate_bars
        if polygon_use_live_aggregate_bars:
            logger.warning(
                "polygon_30s runtime resolved to live-aggregate-bar mode "
                "(MAI_TAI_STRATEGY_POLYGON_30S_LIVE_AGGREGATE_BARS_ENABLED=true). "
                "Since PR #122 disabled Massive A.* subscriptions at the market-data "
                "gateway, aggregate bars no longer flow and polygon_30s will silently "
                "stop persisting bars. Set "
                "MAI_TAI_STRATEGY_POLYGON_30S_LIVE_AGGREGATE_BARS_ENABLED=false "
                "(default since PR #124) to use the tick-built path."
            )
        self.bots: dict[str, StrategyRuntime] = {}
        if self.settings.strategy_macd_1m_enabled and "macd_1m" in registrations:
            self.bots["macd_1m"] = StrategyBotRuntime(
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
                retention_config=one_minute_retention,
                persist_offload_enabled=self.settings.strategy_persist_offload_enabled,
            )
        if self.settings.strategy_schwab_1m_enabled and "schwab_1m" in registrations:
            self.bots["schwab_1m"] = StrategyBotRuntime(
                StrategyDefinition(
                    code="schwab_1m",
                    display_name=registrations["schwab_1m"].display_name,
                    account_name=registrations["schwab_1m"].account_name,
                    interval_secs=60,
                    trading_config=schwab_1m_trading,
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
                session_factory=session_factory if self.settings.strategy_history_persistence_enabled else None,
                use_live_aggregate_bars=True,
                live_aggregate_fallback_enabled=True,
                live_aggregate_bars_are_final=True,
                extended_hours_vwap_provider=self._load_schwab_trade_extended_vwap_series,
                builder_manager=SchwabNativeBarBuilderManager(
                    interval_secs=60,
                    time_provider=lambda: resolved_now_provider().timestamp(),
                ),
                indicator_engine=SchwabNativeIndicatorEngine(default_indicator_config),
                entry_engine=SchwabNativeEntryEngine(
                    schwab_1m_trading,
                    name=registrations["schwab_1m"].display_name,
                    now_provider=resolved_now_provider,
                ),
                retention_config=one_minute_retention,
                persist_offload_enabled=self.settings.strategy_persist_offload_enabled,
            )
        if self.settings.strategy_tos_enabled and "tos" in registrations:
            self.bots["tos"] = StrategyBotRuntime(
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
                retention_config=one_minute_retention,
                persist_offload_enabled=self.settings.strategy_persist_offload_enabled,
            )
        if self.settings.strategy_runner_enabled and "runner" in registrations:
            self.bots["runner"] = RunnerStrategyRuntime(
                definition_code="runner",
                account_name=registrations["runner"].account_name,
                default_quantity=runner_trading.default_quantity,
                bar_interval_secs=runner_trading.bar_interval_secs,
                now_provider=now_provider,
                source_service=SERVICE_NAME,
            )
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
                trade_tick_service=self.settings.strategy_macd_30s_trade_stream_service,
                live_aggregate_fallback_enabled=self.settings.strategy_macd_30s_live_aggregate_fallback_enabled,
                live_aggregate_stale_after_seconds=self.settings.strategy_macd_30s_live_aggregate_stale_after_seconds,
                indicator_overlay_provider=macd_30s_indicator_overlay_provider,
                extended_hours_vwap_provider=self._load_schwab_trade_extended_vwap_series,
                builder_manager=SchwabNativeBarBuilderManager(
                    interval_secs=30,
                    time_provider=lambda: resolved_now_provider().timestamp(),
                    close_grace_seconds=self.settings.strategy_macd_30s_tick_bar_close_grace_seconds,
                    # Aligned with schwab_1m (default=True) on 2026-05-19.
                    # Previously False (since e7ffa07 2026-05-07, bundled with
                    # the schwab_1m live-aggregate-final work; schwab_1m was
                    # later reverted to default True, macd_30s was left at False).
                    # The False setting interacted catastrophically with Schwab
                    # LEVELONE_EQUITIES on thin penny stocks during 2026-05-19
                    # RTH: off-exchange prints grow TOTAL_VOLUME without
                    # advancing LAST_TRADE_TIME, so every event bucketed to
                    # bars[-1].timestamp and routed through
                    # _revise_last_closed_bar_from_trade. Each revise = one
                    # STRATEGY-REVISE-PERSIST DB write; the persist rate
                    # exceeded the 1Hz main loop's drain budget and persist_lag
                    # grew exponentially (33s → 1850s = 31min over 24 minutes
                    # of bars on AMST). The decision tape rendered
                    # half-an-hour-old data while the bot was nominally
                    # "running". With fill_gap_bars=True, bars[-1] advances
                    # via synthetic zero-volume bars every interval; late
                    # LEVELONE events with old LAST_TRADE_TIME now bucket
                    # < bars[-1].timestamp and get ignored as stale (debug
                    # log only, no persist). The revise-storm + DB-write
                    # feedback loop breaks.
                ),
                indicator_engine=SchwabNativeIndicatorEngine(default_indicator_config),
                entry_engine=SchwabNativeEntryEngine(
                    macd_30s_trading,
                    name=registrations["macd_30s"].display_name,
                    now_provider=resolved_now_provider,
                ),
                retention_config=macd_30s_retention,
                persist_offload_enabled=self.settings.strategy_persist_offload_enabled,
            )
        if self.settings.strategy_polygon_30s_enabled and "polygon_30s" in registrations:
            self.bots["polygon_30s"] = StrategyBotRuntime(
                StrategyDefinition(
                    code="polygon_30s",
                    display_name=registrations["polygon_30s"].display_name,
                    account_name=registrations["polygon_30s"].account_name,
                    interval_secs=30,
                    trading_config=polygon_30s_trading,
                    indicator_config=default_indicator_config,
                ),
                now_provider=now_provider,
                session_factory=session_factory if self.settings.strategy_history_persistence_enabled else None,
                use_live_aggregate_bars=polygon_use_live_aggregate_bars,
                trade_tick_service=self.settings.strategy_polygon_30s_trade_stream_service,
                live_aggregate_fallback_enabled=self.settings.strategy_polygon_30s_runtime_live_aggregate_fallback_enabled,
                live_aggregate_stale_after_seconds=self.settings.strategy_polygon_30s_live_aggregate_stale_after_seconds,
                live_aggregate_bars_are_final=False,
                builder_manager=Polygon30sBarBuilderManager(
                    interval_secs=30,
                    time_provider=lambda: resolved_now_provider().timestamp(),
                    close_grace_seconds=self.settings.strategy_polygon_30s_tick_bar_close_grace_seconds,
                    fill_gap_bars=polygon_use_live_aggregate_bars,
                ),
                indicator_engine=Polygon30sIndicatorEngine(default_indicator_config),
                entry_engine=Polygon30sEntryEngine(
                    polygon_30s_trading,
                    name=registrations["polygon_30s"].display_name,
                    now_provider=resolved_now_provider,
                ),
                retention_config=macd_30s_retention,
                persist_offload_enabled=self.settings.strategy_persist_offload_enabled,
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
                trade_tick_service=self.settings.strategy_macd_30s_trade_stream_service,
                live_aggregate_fallback_enabled=self.settings.strategy_macd_30s_live_aggregate_fallback_enabled,
                live_aggregate_stale_after_seconds=self.settings.strategy_macd_30s_live_aggregate_stale_after_seconds,
                indicator_overlay_provider=macd_30s_indicator_overlay_provider,
                retention_config=macd_30s_retention,
                persist_offload_enabled=self.settings.strategy_persist_offload_enabled,
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
                trade_tick_service=self.settings.strategy_macd_30s_trade_stream_service,
                live_aggregate_fallback_enabled=self.settings.strategy_macd_30s_live_aggregate_fallback_enabled,
                live_aggregate_stale_after_seconds=self.settings.strategy_macd_30s_live_aggregate_stale_after_seconds,
                indicator_overlay_provider=macd_30s_indicator_overlay_provider,
                retention_config=macd_30s_retention,
                persist_offload_enabled=self.settings.strategy_persist_offload_enabled,
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
                trade_tick_service=self.settings.strategy_macd_30s_trade_stream_service,
                live_aggregate_fallback_enabled=self.settings.strategy_macd_30s_live_aggregate_fallback_enabled,
                live_aggregate_stale_after_seconds=self.settings.strategy_macd_30s_live_aggregate_stale_after_seconds,
                indicator_overlay_provider=macd_30s_indicator_overlay_provider,
                retention_config=macd_30s_retention,
                persist_offload_enabled=self.settings.strategy_persist_offload_enabled,
            )
        self._ensure_bot_handoff_state()

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
        elif variant == "polygon":
            config = base_trading.make_30s_polygon_variant(
                quantity=self.settings.strategy_polygon_30s_default_quantity
            )
            raw_overrides = self.settings.strategy_polygon_30s_config_overrides_json
            scope = "strategy_polygon_30s_config_overrides_json"
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

    def _resolve_1m_trading_config(
        self,
        base_trading: TradingConfig,
        *,
        variant: str,
    ) -> TradingConfig:
        if variant == "schwab":
            config = base_trading.make_1m_schwab_native_variant(
                quantity=self.settings.strategy_schwab_1m_default_quantity
            )
            return self._apply_trading_config_overrides(
                config,
                self.settings.strategy_schwab_1m_config_overrides_json,
                scope="strategy_schwab_1m_config_overrides_json",
            )
        raise ValueError(f"Unsupported 1m variant: {variant}")

    def _build_bot_feed_retention_config(self, *, interval_secs: int) -> FeedRetentionConfig:
        base_interval_secs = 30

        def scale_bar_count(bars: int) -> int:
            base_seconds = max(1, int(bars)) * base_interval_secs
            return max(1, round(base_seconds / max(1, interval_secs)))

        return FeedRetentionConfig(
            enabled=self.settings.scanner_feed_retention_enabled,
            structure_bars=scale_bar_count(self.settings.scanner_feed_retention_structure_bars),
            no_activity_minutes=max(1, int(self.settings.scanner_feed_retention_no_activity_minutes)),
            cooldown_volume_ratio=max(0.0, float(self.settings.scanner_feed_retention_cooldown_volume_ratio)),
            cooldown_max_5m_range_pct=max(0.0, float(self.settings.scanner_feed_retention_cooldown_max_5m_range_pct)),
            resume_hold_bars=scale_bar_count(self.settings.scanner_feed_retention_resume_hold_bars),
            resume_min_5m_range_pct=max(0.0, float(self.settings.scanner_feed_retention_resume_min_5m_range_pct)),
            resume_min_5m_volume_ratio=max(0.0, float(self.settings.scanner_feed_retention_resume_min_5m_volume_ratio)),
            resume_min_5m_volume_abs=max(0.0, float(self.settings.scanner_feed_retention_resume_min_5m_volume_abs)),
            drop_cooldown_minutes=max(1, int(self.settings.scanner_feed_retention_drop_cooldown_minutes)),
            drop_max_5m_range_pct=max(0.0, float(self.settings.scanner_feed_retention_drop_max_5m_range_pct)),
            drop_max_5m_volume_abs=max(0.0, float(self.settings.scanner_feed_retention_drop_max_5m_volume_abs)),
        )

    def _resolve_schwab_stream_bot_codes(self) -> tuple[str, ...]:
        codes: list[str] = []
        enabled_by_code = {
            "macd_30s": self.settings.strategy_macd_30s_enabled,
            "polygon_30s": self.settings.strategy_polygon_30s_enabled,
            "schwab_1m": self.settings.strategy_schwab_1m_enabled,
            "tos": self.settings.strategy_tos_enabled,
        }
        for code, enabled in enabled_by_code.items():
            if enabled and self.settings.market_data_provider_for_strategy(code) == "schwab":
                codes.append(code)
        return tuple(codes)

    def _resolve_schwab_native_history_bot_codes(self) -> tuple[str, ...]:
        codes: list[str] = []
        if self.settings.strategy_schwab_1m_enabled:
            codes.append("schwab_1m")
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
        blocked.update(self.global_manual_stop_symbols)
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
        self._add_schwab_prewarm_symbols(alert.get("ticker", "") for alert in alerts)
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
        self._record_bot_handoff_symbols(newly_confirmed)
        self.confirmed_scanner.update_live_prices(snapshot_lookup)
        faded_confirmed_symbols = self.confirmed_scanner.prune_faded_candidates() or []
        if faded_confirmed_symbols:
            self._discard_bot_handoff_symbols(faded_confirmed_symbols)
            self._purge_faded_symbols_from_bot_watchlists(faded_confirmed_symbols)

        self.all_confirmed = [
            stock
            for stock in self.confirmed_scanner.get_all_confirmed()
            if str(stock.get("ticker", "")).upper() not in blocked
        ]
        self._record_bot_handoff_symbols(self.all_confirmed)
        self.current_confirmed = self._ranked_scanner_confirmed_view(limit=5)
        self._add_market_data_archive_symbols(
            stock.get("ticker", "") for stock in self.all_confirmed
        )
        if self.settings.scanner_feed_retention_enabled:
            self._update_retained_watchlist(snapshot_lookup)
            retained_symbols = set(self.feed_retention_states)
            confirmed_handoff_symbols = {
                str(stock.get("ticker", "")).upper()
                for stock in self.all_confirmed
                if str(stock.get("ticker", "")).strip()
            }
            dropped_handoff_symbols = {
                symbol
                for symbols in self.bot_handoff_symbols_by_strategy.values()
                for symbol in symbols
                if symbol not in retained_symbols and symbol not in confirmed_handoff_symbols
            }
            if dropped_handoff_symbols:
                self._discard_bot_handoff_symbols(dropped_handoff_symbols)
        tracked_snapshot_symbols = {
            str(stock.get("ticker", "")).upper()
            for stock in self.all_confirmed
            if str(stock.get("ticker", "")).strip()
        }
        for code, bot in self.bots.items():
            bot_watchlist = self._watchlist_for_bot(code, self._bot_handoff_symbols_for_bot(code))
            if code == "runner":
                blocked_candidates = self._broker_blocked_symbols_for_bot(code)
                runner_candidates = [
                    candidate
                    for candidate in self.current_confirmed
                    if str(candidate.get("ticker", "")).upper() not in blocked_candidates
                ]
                bot.update_market_snapshots(filtered_snapshots)
                bot.set_watchlist(bot_watchlist)
                bot.update_candidates(runner_candidates)
                continue
            if hasattr(bot, "set_manual_stop_symbols"):
                bot.set_manual_stop_symbols(self._manual_stop_symbols_for_bot(code))
            bot.set_watchlist(bot_watchlist)
            if hasattr(bot, "set_entry_blocked_symbols"):
                bot.set_entry_blocked_symbols(())
            refresh_lifecycle = getattr(bot, "refresh_lifecycle", None)
            if refresh_lifecycle is not None:
                refresh_lifecycle()
            if code not in self._schwab_stream_bot_codes:
                bot.update_market_snapshots(filtered_snapshots)

        watchlist = sorted(
            {
                symbol.upper()
                for bot in self.bots.values()
                for symbol in bot.active_symbols()
            }
        )
        tracked_snapshot_symbols.update(symbol.upper() for symbol in watchlist)
        self.latest_snapshots = {
            symbol: snapshot_lookup[symbol]
            for symbol in tracked_snapshot_symbols
            if symbol in snapshot_lookup
        }
        self.retained_watchlist = list(watchlist)
        self.feed_retention_states = self._aggregate_bot_retention_states()

        return {
            "alerts": alerts,
            "newly_confirmed": newly_confirmed,
            "all_confirmed": self.all_confirmed,
            "top_confirmed": self.current_confirmed,
            "five_pillars": self.five_pillars,
            "top_gainers": self.top_gainers,
            "recent_alerts": self.recent_alerts,
            "today_alerts": self.today_alerts,
            "watchlist": watchlist,
            "retention_states": self.retention_summary(),
            "market_data_symbols": self.market_data_symbols(),
            "schwab_stream_symbols": self.schwab_stream_symbols(),
            "schwab_prewarm_symbols": list(self.schwab_prewarm_symbols),
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
    ) -> list[TradeIntentEvent]:
        intents: list[TradeIntentEvent] = []
        for _code, bot in self._iter_target_bots(
            strategy_codes=strategy_codes,
            exclude_codes=exclude_codes,
        ):
            handle_quote_tick = getattr(bot, "handle_quote_tick", None)
            if handle_quote_tick is None:
                continue
            bot_intents = handle_quote_tick(
                symbol,
                bid_price=bid_price,
                ask_price=ask_price,
            )
            if bot_intents:
                intents.extend(bot_intents)
        return intents

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
        coverage_started_at: float | None = None,
        live_bar_received_at: datetime | None = None,
        live_bar_recorded_at: datetime | None = None,
        live_bar_source: str = "live",
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
                    coverage_started_at=coverage_started_at,
                    live_bar_received_at=live_bar_received_at,
                    live_bar_recorded_at=live_bar_recorded_at,
                    live_bar_source=live_bar_source,
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

    def flush_pending_persists(self) -> None:
        """Flush each bot's buffered persist writes (batched-offload path).

        No-op for bots that don't buffer (e.g. RunnerStrategyRuntime). Designed
        to run via ``asyncio.to_thread`` so the bots' DB I/O stays off the event
        loop.
        """
        for bot in self.bots.values():
            flush_pending_persists = getattr(bot, "flush_pending_persists", None)
            if flush_pending_persists is None:
                continue
            flush_pending_persists()

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
        bot = self.bots.get(strategy_code)
        if bot is None:
            logger.warning(
                "ignoring execution fill for unknown strategy_code=%s symbol=%s intent_type=%s",
                strategy_code,
                symbol,
                intent_type,
            )
            return
        bot.apply_execution_fill(
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
        bot = self.bots.get(strategy_code)
        if bot is None:
            logger.warning(
                "ignoring order status for unknown strategy_code=%s symbol=%s intent_type=%s status=%s",
                strategy_code,
                symbol,
                intent_type,
                status,
            )
            return
        bot.apply_order_status(
            symbol=symbol,
            intent_type=intent_type,
            status=status,
            level=level,
            reason=reason,
        )

    def summary(self) -> dict[str, object]:
        return {
            "all_confirmed": self.all_confirmed,
            "watchlist": list(self.retained_watchlist),
            "top_confirmed": self.current_confirmed,
            "global_manual_stop_symbols": sorted(self.global_manual_stop_symbols),
            "bot_handoff_symbols_by_strategy": {
                code: sorted(symbols)
                for code, symbols in self.bot_handoff_symbols_by_strategy.items()
            },
            "bot_handoff_history_by_strategy": {
                code: sorted(symbols)
                for code, symbols in self.bot_handoff_history_by_strategy.items()
            },
            "five_pillars": self.five_pillars,
            "top_gainers": self.top_gainers,
            "recent_alerts": self.recent_alerts,
            "today_alerts": self.today_alerts,
            "top_gainer_changes": self.top_gainer_changes,
            "alert_warmup": self.alert_warmup,
            "cycle_count": self.cycle_count,
            "retention_states": self.retention_summary(),
            "schwab_prewarm_symbols": list(self.schwab_prewarm_symbols),
            "bots": {code: bot.summary() for code, bot in self.bots.items()},
        }

    def monitor_completed_bar_flow(
        self,
        *,
        provider_for_strategy: Callable[[str], str] | None = None,
        market_data_provider_for_strategy: Callable[[str], str] | None = None,
    ) -> int:
        changed = 0
        provider_resolver = market_data_provider_for_strategy or provider_for_strategy
        for code, bot in self.bots.items():
            if not isinstance(bot, StrategyBotRuntime):
                continue
            provider = provider_resolver(code) if provider_resolver is not None else "market data"
            changed += bot.monitor_completed_bar_flow(provider=provider)
        return changed

    def seed_confirmed_candidates(self, candidates: Sequence[dict[str, object]]) -> None:
        self.confirmed_scanner.seed_confirmed_candidates(candidates)
        self._seeded_confirmed_pending_revalidation = bool(candidates)

    def restore_confirmed_runtime_view(
        self,
        visible_confirmed: Sequence[dict[str, object]],
        *,
        all_confirmed: Sequence[dict[str, object]] | None = None,
        bot_handoff_symbols_by_strategy: dict[str, Sequence[object]] | None = None,
        bot_handoff_history_by_strategy: dict[str, Sequence[object]] | None = None,
    ) -> None:
        self.current_confirmed = [
            {**dict(item), "ticker": str(item.get("ticker", "")).upper()}
            for item in visible_confirmed
            if str(item.get("ticker", "")).strip()
            and str(item.get("ticker", "")).upper() not in self.global_manual_stop_symbols
        ]
        all_confirmed_rows = all_confirmed if all_confirmed is not None else self.current_confirmed
        self.all_confirmed = [
            {**dict(item), "ticker": str(item.get("ticker", "")).upper()}
            for item in all_confirmed_rows
            if str(item.get("ticker", "")).strip()
            and str(item.get("ticker", "")).upper() not in self.global_manual_stop_symbols
        ]
        if bot_handoff_symbols_by_strategy is not None or bot_handoff_history_by_strategy is not None:
            self._restore_bot_handoff_state(
                active_by_strategy=bot_handoff_symbols_by_strategy,
                history_by_strategy=bot_handoff_history_by_strategy,
            )
        else:
            self._seed_bot_handoff_state(self.all_confirmed or self.current_confirmed)
        self._resync_bot_watchlists_from_current_confirmed()
        if self.all_confirmed or self.current_confirmed:
            self.session_handoff_active = True

    def _ranked_scanner_confirmed_view(self, *, limit: int = 5) -> list[dict[str, object]]:
        ranked_confirmed = [
            dict(item)
            for item in self.confirmed_scanner.get_ranked_confirmed(
                min_change_pct=0,
                min_score=0,
            )
            if str(item.get("ticker", "")).strip()
            and str(item.get("ticker", "")).upper() not in self.global_manual_stop_symbols
        ]
        return ranked_confirmed[:limit]

    def apply_global_manual_stop_symbols(self, symbols: Iterable[str] | None) -> None:
        normalized_symbols = {
            str(symbol).upper() for symbol in (symbols or []) if str(symbol).strip()
        }
        stopped_now = normalized_symbols - self.global_manual_stop_symbols
        self.global_manual_stop_symbols = normalized_symbols
        if stopped_now:
            self._discard_bot_handoff_symbols(stopped_now)
        for code, bot in self.bots.items():
            set_manual_stops = getattr(bot, "set_manual_stop_symbols", None)
            if set_manual_stops is not None:
                set_manual_stops(self._manual_stop_symbols_for_bot(code))
        self._sync_schwab_prewarm_symbols()
        self._resync_bot_watchlists_from_current_confirmed()

    def apply_manual_stop_symbols(self, symbols_by_strategy: dict[str, set[str]] | None) -> None:
        normalized: dict[str, set[str]] = {}
        for code, symbols in (symbols_by_strategy or {}).items():
            normalized[normalize_strategy_code(code)] = {
                str(symbol).upper() for symbol in symbols if str(symbol).strip()
            }
        self.manual_stop_symbols_by_strategy = normalized
        for code, bot in self.bots.items():
            set_manual_stops = getattr(bot, "set_manual_stop_symbols", None)
            if set_manual_stops is not None:
                set_manual_stops(self._manual_stop_symbols_for_bot(code))

    def apply_manual_stop_update(
        self,
        *,
        scope: str,
        action: str,
        symbol: str,
        strategy_code: str | None = None,
    ) -> None:
        normalized_symbol = str(symbol).upper()
        if not normalized_symbol:
            return
        if scope == "global":
            if action == "stop":
                self.global_manual_stop_symbols.add(normalized_symbol)
                self._discard_bot_handoff_symbols([normalized_symbol])
            else:
                self.global_manual_stop_symbols.discard(normalized_symbol)
                self._restore_bot_handoff_symbols([normalized_symbol])
            self._sync_schwab_prewarm_symbols()
            self._resync_bot_watchlists_from_current_confirmed()
            return

        code = normalize_strategy_code(strategy_code)
        if not code:
            return
        current = set(self.manual_stop_symbols_by_strategy.get(code, set()))
        if action == "stop":
            current.add(normalized_symbol)
        else:
            current.discard(normalized_symbol)
        self.manual_stop_symbols_by_strategy[code] = current
        self._sync_schwab_prewarm_symbols()
        self._resync_bot_watchlists_from_current_confirmed(strategy_codes=[code])

    def _manual_stop_symbols_for_bot(self, strategy_code: str) -> set[str]:
        return set(self.global_manual_stop_symbols) | set(
            self.manual_stop_symbols_by_strategy.get(normalize_strategy_code(strategy_code), set())
        )

    def _resync_bot_watchlists_from_current_confirmed(
        self,
        *,
        strategy_codes: Sequence[str] | None = None,
    ) -> None:
        for code, bot in self._iter_target_bots(strategy_codes=strategy_codes):
            if hasattr(bot, "set_manual_stop_symbols"):
                bot.set_manual_stop_symbols(self._manual_stop_symbols_for_bot(code))
            bot.set_watchlist(self._watchlist_for_bot(code, self._bot_handoff_symbols_for_bot(code)))
            if hasattr(bot, "set_entry_blocked_symbols"):
                bot.set_entry_blocked_symbols(())
            if code == "runner":
                blocked_candidates = self._broker_blocked_symbols_for_bot(code)
                bot.update_candidates(
                    [
                        candidate
                        for candidate in self.current_confirmed
                        if str(candidate.get("ticker", "")).upper() not in blocked_candidates
                    ]
                )
            else:
                refresh_lifecycle = getattr(bot, "refresh_lifecycle", None)
                if refresh_lifecycle is not None:
                    refresh_lifecycle()
        self.retained_watchlist = sorted(
            {
                symbol.upper()
                for bot in self.bots.values()
                for symbol in bot.active_symbols()
            }
        )
        self.feed_retention_states = self._aggregate_bot_retention_states()

    def _ensure_bot_handoff_state(self) -> None:
        for code in self.bots:
            self.bot_handoff_symbols_by_strategy.setdefault(code, set())
            self.bot_handoff_history_by_strategy.setdefault(code, set())

    def _load_schwab_trade_extended_vwap_series(
        self,
        symbol: str,
        bar_timestamps: Sequence[float],
        interval_secs: int,
    ) -> dict[float, float]:
        if not self.settings.schwab_tick_archive_enabled or not bar_timestamps:
            return {}

        normalized_symbol = str(symbol).upper().strip()
        if not normalized_symbol:
            return {}

        clean_timestamps: list[float] = []
        for value in bar_timestamps:
            try:
                timestamp = float(value)
            except (TypeError, ValueError):
                continue
            if timestamp > 0:
                clean_timestamps.append(timestamp)
        if not clean_timestamps:
            return {}

        latest_timestamp = max(clean_timestamps)
        session_day = datetime.fromtimestamp(latest_timestamp, UTC).astimezone(EASTERN_TZ).strftime("%Y-%m-%d")
        session_date = datetime.strptime(session_day, "%Y-%m-%d").replace(tzinfo=EASTERN_TZ)
        session_start_ns = int(
            session_date.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(UTC).timestamp()
            * 1_000_000_000
        )
        session_end_ns = int(
            session_date.replace(hour=20, minute=0, second=0, microsecond=0).astimezone(UTC).timestamp()
            * 1_000_000_000
        )
        fetch_end_ns = min(
            session_end_ns,
            int((latest_timestamp + max(1, int(interval_secs))) * 1_000_000_000),
        )
        if fetch_end_ns <= session_start_ns:
            return {}

        trades = load_recorded_trades(
            self.settings.schwab_tick_archive_root,
            symbol=normalized_symbol,
            day=session_day,
            start_at_ns=session_start_ns,
            end_at_ns=fetch_end_ns,
        )
        if not trades:
            return {}

        ordered_targets = sorted(set(clean_timestamps))
        ordered_trades = sorted(trades, key=lambda record: int(record.timestamp_ns or 0))
        cumulative_price_volume = 0.0
        cumulative_volume = 0.0
        trade_index = 0
        interval_ns = max(1, int(interval_secs)) * 1_000_000_000
        result: dict[float, float] = {}

        for bar_timestamp in ordered_targets:
            bar_end_ns = int(bar_timestamp * 1_000_000_000) + interval_ns
            while trade_index < len(ordered_trades):
                trade = ordered_trades[trade_index]
                event_ns = int(trade.timestamp_ns or 0)
                if event_ns >= bar_end_ns:
                    break
                cumulative_price_volume += float(trade.price) * int(trade.size)
                cumulative_volume += int(trade.size)
                trade_index += 1
            if cumulative_volume > 0:
                result[bar_timestamp] = cumulative_price_volume / cumulative_volume

        return result

    @staticmethod
    def _normalize_symbol_items(items: Iterable[object] | None) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in items or ():
            if isinstance(item, dict):
                symbol = str(item.get("ticker", "")).upper()
            else:
                symbol = str(item).upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
        return normalized

    def _seed_bot_handoff_state(
        self,
        items: Sequence[object],
        *,
        strategy_codes: Sequence[str] | None = None,
    ) -> None:
        symbols = self._normalize_symbol_items(items)
        self._ensure_bot_handoff_state()
        target_codes = [code for code, _ in self._iter_target_bots(strategy_codes=strategy_codes)]
        target_code_set = set(target_codes)
        for code in self.bots:
            if code in target_code_set:
                seeded = set(symbols)
                self.bot_handoff_symbols_by_strategy[code] = set(seeded)
                self.bot_handoff_history_by_strategy[code] = set(seeded)
            else:
                self.bot_handoff_symbols_by_strategy.setdefault(code, set())
                self.bot_handoff_history_by_strategy.setdefault(code, set())

    def _record_bot_handoff_symbols(
        self,
        items: Sequence[object],
        *,
        strategy_codes: Sequence[str] | None = None,
        replace_active: bool = False,
    ) -> None:
        symbols = self._normalize_symbol_items(items)
        if not symbols and not replace_active:
            return
        self._ensure_bot_handoff_state()
        self.session_handoff_active = True
        for code, _ in self._iter_target_bots(strategy_codes=strategy_codes):
            if replace_active:
                self.bot_handoff_symbols_by_strategy[code] = set(symbols)
            else:
                self.bot_handoff_symbols_by_strategy.setdefault(code, set()).update(symbols)
            self.bot_handoff_history_by_strategy.setdefault(code, set()).update(symbols)

    def _discard_bot_handoff_symbols(
        self,
        items: Iterable[object],
        *,
        strategy_codes: Sequence[str] | None = None,
    ) -> None:
        symbols = set(self._normalize_symbol_items(items))
        if not symbols:
            return
        self._ensure_bot_handoff_state()
        for code, _ in self._iter_target_bots(strategy_codes=strategy_codes):
            self.bot_handoff_symbols_by_strategy.setdefault(code, set()).difference_update(symbols)

    def _purge_faded_symbols_from_bot_watchlists(
        self,
        items: Iterable[object],
        *,
        strategy_codes: Sequence[str] | None = None,
    ) -> None:
        symbols = set(self._normalize_symbol_items(items))
        if not symbols:
            return
        for _code, bot in self._iter_target_bots(strategy_codes=strategy_codes):
            discard_watchlist_symbols = getattr(bot, "discard_watchlist_symbols", None)
            if callable(discard_watchlist_symbols):
                discard_watchlist_symbols(symbols)
        protected_symbols = {
            symbol
            for symbol in symbols
            if any(
                isinstance(bot, StrategyBotRuntime) and bot._symbol_requires_feed(symbol)
                for _code, bot in self._iter_target_bots(strategy_codes=strategy_codes)
            )
        }
        removable_symbols = symbols - protected_symbols
        if not removable_symbols:
            return
        self.market_data_archive_symbols = [
            symbol for symbol in self.market_data_archive_symbols if symbol not in removable_symbols
        ]
        for symbol in removable_symbols:
            self._market_data_archive_added_at.pop(symbol, None)
            self.feed_retention_states.pop(symbol, None)
        self.schwab_prewarm_symbols = [
            symbol for symbol in self.schwab_prewarm_symbols if symbol not in removable_symbols
        ]
        for symbol in removable_symbols:
            self._schwab_prewarm_added_at.pop(symbol, None)
        self._sync_schwab_prewarm_symbols()

    def _restore_bot_handoff_symbols(
        self,
        items: Iterable[object],
        *,
        strategy_codes: Sequence[str] | None = None,
    ) -> None:
        symbols = set(self._normalize_symbol_items(items))
        if not symbols:
            return
        self._ensure_bot_handoff_state()
        for code, _ in self._iter_target_bots(strategy_codes=strategy_codes):
            history = self.bot_handoff_history_by_strategy.setdefault(code, set())
            self.bot_handoff_symbols_by_strategy.setdefault(code, set()).update(history & symbols)

    def _restore_bot_handoff_state(
        self,
        *,
        active_by_strategy: dict[str, Sequence[object]] | None = None,
        history_by_strategy: dict[str, Sequence[object]] | None = None,
    ) -> None:
        self._ensure_bot_handoff_state()
        restored_active: dict[str, set[str]] = {}
        restored_history: dict[str, set[str]] = {}
        active_map = normalize_strategy_code_map(active_by_strategy)
        history_map = normalize_strategy_code_map(history_by_strategy)
        fallback_symbols = set(self._normalize_symbol_items(self._confirmed_handoff_candidates()))
        if not fallback_symbols:
            for items in active_map.values():
                fallback_symbols.update(self._normalize_symbol_items(items))
            for items in history_map.values():
                fallback_symbols.update(self._normalize_symbol_items(items))
        for code in self.bots:
            active_symbols = set(self._normalize_symbol_items(active_map.get(code, ())))
            history_symbols = set(self._normalize_symbol_items(history_map.get(code, ())))
            if code not in active_map and code not in history_map:
                active_symbols = set(fallback_symbols)
                history_symbols = set(fallback_symbols)
            elif code in {"macd_30s", "polygon_30s"} and not active_symbols and not history_symbols and fallback_symbols:
                active_symbols = set(fallback_symbols)
                history_symbols = set(fallback_symbols)
            if not history_symbols:
                history_symbols = set(active_symbols)
            restored_active[code] = active_symbols
            restored_history[code] = history_symbols | active_symbols
        self.bot_handoff_symbols_by_strategy = restored_active
        self.bot_handoff_history_by_strategy = restored_history

    def _bot_handoff_symbols_for_bot(self, code: str) -> list[str]:
        self._ensure_bot_handoff_state()
        return sorted(self.bot_handoff_symbols_by_strategy.get(code, set()))

    def _confirmed_handoff_candidates(self) -> list[dict[str, object]]:
        if self.all_confirmed:
            return list(self.all_confirmed)
        return [
            stock
            for stock in self.current_confirmed
            if str(stock.get("ticker", "")).upper() not in self.global_manual_stop_symbols
        ]

    def market_data_symbols(self) -> list[str]:
        self._sync_market_data_archive_symbols()
        symbols: set[str] = set()
        for code, bot in self.bots.items():
            if code in self._schwab_stream_bot_codes:
                continue
            symbols.update(bot.active_symbols())
        symbols.update(self.market_data_archive_symbols)
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
            if code in self._schwab_native_history_bot_codes:
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

    def schwab_native_history_targets(
        self,
        symbols: Sequence[str] | None = None,
    ) -> list[tuple[str, str, int]]:
        symbol_filter = (
            {str(symbol).upper() for symbol in symbols if str(symbol).strip()}
            if symbols is not None
            else None
        )
        targets: list[tuple[str, str, int]] = []
        for code in self._schwab_native_history_bot_codes:
            bot = self.bots.get(code)
            if not isinstance(bot, StrategyBotRuntime):
                continue
            interval_secs = int(bot.definition.interval_secs)
            for symbol in bot.active_symbols():
                normalized_symbol = str(symbol).upper()
                if symbol_filter is not None and normalized_symbol not in symbol_filter:
                    continue
                targets.append((code, normalized_symbol, interval_secs))
        return targets

    def schwab_stream_symbols(self) -> list[str]:
        if not self._schwab_stream_bot_codes:
            return []
        self._sync_schwab_prewarm_symbols()
        symbols: set[str] = set()
        for code in self._schwab_stream_bot_codes:
            bot = self.bots.get(code)
            if bot is None:
                continue
            stream_symbols = getattr(bot, "stream_symbols", None)
            if callable(stream_symbols):
                symbols.update(stream_symbols())
            else:
                symbols.update(bot.active_symbols())
        symbols.update(self.schwab_prewarm_symbols)
        blocked = set(self.global_manual_stop_symbols)
        for manual_symbols in self.manual_stop_symbols_by_strategy.values():
            blocked.update(manual_symbols)
        symbols.difference_update(blocked)
        return sorted(symbols)

    def schwab_live_bar_symbols(self, *, interval_secs: int) -> list[str]:
        symbols: set[str] = set()
        for code in self._schwab_stream_bot_codes:
            bot = self.bots.get(code)
            if not isinstance(bot, StrategyBotRuntime):
                continue
            if int(bot.definition.interval_secs) != int(interval_secs):
                continue
            if not bot.use_live_aggregate_bars:
                continue
            stream_symbols = getattr(bot, "stream_symbols", None)
            if callable(stream_symbols):
                symbols.update(stream_symbols())
            else:
                symbols.update(bot.active_symbols())
        blocked = set(self.global_manual_stop_symbols)
        for manual_symbols in self.manual_stop_symbols_by_strategy.values():
            blocked.update(manual_symbols)
        symbols.difference_update(blocked)
        return sorted(symbols)

    def schwab_timesale_symbols(self) -> list[str]:
        symbols: set[str] = set()
        for code in self._schwab_stream_bot_codes:
            bot = self.bots.get(code)
            if not isinstance(bot, StrategyBotRuntime):
                continue
            if getattr(bot, "trade_tick_service", "LEVELONE_EQUITIES") != "TIMESALE_EQUITY":
                continue
            stream_symbols = getattr(bot, "stream_symbols", None)
            if callable(stream_symbols):
                symbols.update(stream_symbols())
            else:
                symbols.update(bot.active_symbols())
        blocked = set(self.global_manual_stop_symbols)
        for manual_symbols in self.manual_stop_symbols_by_strategy.values():
            blocked.update(manual_symbols)
        symbols.difference_update(blocked)
        return sorted(symbols)

    def schwab_active_symbols(self) -> list[str]:
        if not self._schwab_stream_bot_codes:
            return []
        symbols: set[str] = set()
        for code in self._schwab_stream_bot_codes:
            bot = self.bots.get(code)
            if bot is None:
                continue
            symbols.update(bot.active_symbols())
        blocked = set(self.global_manual_stop_symbols)
        for manual_symbols in self.manual_stop_symbols_by_strategy.values():
            blocked.update(manual_symbols)
        symbols.difference_update(blocked)
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
            {normalize_strategy_code(code) for code in strategy_codes if str(code).strip()}
            if strategy_codes is not None
            else None
        )
        exclude = {normalize_strategy_code(code) for code in (exclude_codes or ()) if str(code).strip()}
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
        self.today_alerts.extend(normalized)
        self.today_alerts = self.today_alerts[-5000:]

    def _add_market_data_archive_symbols(self, symbols: Iterable[object]) -> None:
        if (
            not self.settings.market_data_archive_retention_enabled
            or self.market_data_archive_max_symbols <= 0
        ):
            self.market_data_archive_symbols = []
            self._market_data_archive_added_at = {}
            return

        observed_at = utcnow()
        existing = set(self.market_data_archive_symbols)
        for symbol in symbols:
            normalized = str(symbol).upper().strip()
            if not normalized:
                continue
            if normalized in existing:
                self._market_data_archive_added_at[normalized] = observed_at
                self.market_data_archive_symbols = [
                    item for item in self.market_data_archive_symbols if item != normalized
                ]
                self.market_data_archive_symbols.append(normalized)
                continue
            self.market_data_archive_symbols.append(normalized)
            existing.add(normalized)
            self._market_data_archive_added_at[normalized] = observed_at

        if len(self.market_data_archive_symbols) > self.market_data_archive_max_symbols:
            self.market_data_archive_symbols = self.market_data_archive_symbols[
                -self.market_data_archive_max_symbols :
            ]
        keep = set(self.market_data_archive_symbols)
        self._market_data_archive_added_at = {
            symbol: seen_at
            for symbol, seen_at in self._market_data_archive_added_at.items()
            if symbol in keep
        }
        self._sync_market_data_archive_symbols()

    def _sync_market_data_archive_symbols(self) -> None:
        if (
            not self.settings.market_data_archive_retention_enabled
            or self.market_data_archive_max_symbols <= 0
        ):
            self.market_data_archive_symbols = []
            self._market_data_archive_added_at = {}
            return

        observed_at = utcnow()
        clean = [
            symbol
            for symbol in self.market_data_archive_symbols
            if (
                observed_at - self._market_data_archive_added_at.get(symbol, observed_at)
            ) < self.market_data_archive_ttl
        ]
        if clean != self.market_data_archive_symbols:
            self.market_data_archive_symbols = clean
        keep = set(self.market_data_archive_symbols)
        self._market_data_archive_added_at = {
            symbol: seen_at
            for symbol, seen_at in self._market_data_archive_added_at.items()
            if symbol in keep
        }

    def _clear_market_data_archive_symbols(self) -> None:
        self.market_data_archive_symbols = []
        self._market_data_archive_added_at = {}

    def _add_schwab_prewarm_symbols(self, symbols: Iterable[object]) -> None:
        if not self._schwab_stream_bot_codes:
            return
        observed_at = utcnow()
        blocked = set(self.global_manual_stop_symbols)
        for manual_symbols in self.manual_stop_symbols_by_strategy.values():
            blocked.update(manual_symbols)
        existing = set(self.schwab_prewarm_symbols)
        for symbol in symbols:
            normalized = str(symbol).upper().strip()
            if not normalized or normalized in blocked:
                continue
            if normalized in existing:
                self._schwab_prewarm_added_at[normalized] = observed_at
                self.schwab_prewarm_symbols = [
                    item for item in self.schwab_prewarm_symbols if item != normalized
                ]
                self.schwab_prewarm_symbols.append(normalized)
                continue
            self.schwab_prewarm_symbols.append(normalized)
            existing.add(normalized)
            self._schwab_prewarm_added_at[normalized] = observed_at

        if len(self.schwab_prewarm_symbols) > self.schwab_prewarm_max_symbols:
            self.schwab_prewarm_symbols = self.schwab_prewarm_symbols[-self.schwab_prewarm_max_symbols :]
        keep = set(self.schwab_prewarm_symbols)
        self._schwab_prewarm_added_at = {
            symbol: seen_at
            for symbol, seen_at in self._schwab_prewarm_added_at.items()
            if symbol in keep
        }
        self._sync_schwab_prewarm_symbols()

    def _sync_schwab_prewarm_symbols(self) -> None:
        observed_at = utcnow()
        blocked = set(self.global_manual_stop_symbols)
        for manual_symbols in self.manual_stop_symbols_by_strategy.values():
            blocked.update(manual_symbols)
        clean = [
            symbol
            for symbol in self.schwab_prewarm_symbols
            if (
                symbol not in blocked
                and (
                    observed_at - self._schwab_prewarm_added_at.get(symbol, observed_at)
                ) < self.schwab_prewarm_ttl
            )
        ]
        if clean != self.schwab_prewarm_symbols:
            self.schwab_prewarm_symbols = clean
        keep = set(self.schwab_prewarm_symbols)
        self._schwab_prewarm_added_at = {
            symbol: seen_at
            for symbol, seen_at in self._schwab_prewarm_added_at.items()
            if symbol in keep
        }
        prewarm_set = set(self.schwab_prewarm_symbols)
        for code in self._schwab_stream_bot_codes:
            bot = self.bots.get(code)
            set_prewarm_symbols = getattr(bot, "set_prewarm_symbols", None)
            if set_prewarm_symbols is not None:
                set_prewarm_symbols(prewarm_set)

    def _watchlist_for_bot(self, code: str, watchlist: Sequence[object]) -> list[str]:
        normalized: list[str] = []
        for item in watchlist:
            if isinstance(item, dict):
                symbol = str(item.get("ticker", "")).upper()
            else:
                symbol = str(item).upper()
            if symbol:
                normalized.append(symbol)
        blocked = self._manual_stop_symbols_for_bot(code)
        if blocked:
            normalized = [symbol for symbol in normalized if symbol not in blocked]
        broker_blocked = self._broker_blocked_symbols_for_bot(code)
        if broker_blocked:
            normalized = [symbol for symbol in normalized if symbol not in broker_blocked]
        if code == "macd_30s_reclaim" and self.reclaim_excluded_symbols:
            normalized = [
                symbol
                for symbol in normalized
                if symbol not in self.reclaim_excluded_symbols
            ]
        seen: set[str] = set()
        deduped: list[str] = []
        for symbol in normalized:
            if symbol in seen:
                continue
            seen.add(symbol)
            deduped.append(symbol)
        return deduped

    def _roll_scanner_session_if_needed(self) -> bool:
        current_session_start = current_scanner_session_start_utc(self.alert_engine.now_provider())
        if current_session_start == self._active_scanner_session_start:
            return False

        logger.info(
            "scanner session-roll fired | previous_session=%s new_session=%s",
            self._active_scanner_session_start.isoformat(),
            current_session_start.isoformat(),
        )
        self.confirmed_scanner.reset()
        self.alert_engine.reset()
        self.alert_warmup = self.alert_engine.get_warmup_status()
        self.top_gainers_tracker.reset()
        self.all_confirmed = []
        self.current_confirmed = []
        self.retained_watchlist = []
        self._clear_market_data_archive_symbols()
        self.schwab_prewarm_symbols = []
        self._schwab_prewarm_added_at.clear()
        self._broker_blocked_symbols_by_strategy = {}
        self.bot_handoff_symbols_by_strategy = {code: set() for code in self.bots}
        self.bot_handoff_history_by_strategy = {code: set() for code in self.bots}
        self.session_handoff_active = False
        self.five_pillars = []
        self.top_gainers = []
        self.top_gainer_changes = []
        self.recent_alerts = []
        self.today_alerts = []
        self.latest_snapshots = {}
        self._first_seen_by_ticker.clear()
        self.feed_retention_states.clear()
        self._seeded_confirmed_pending_revalidation = False
        self._pending_recent_alert_replay = False
        self.apply_global_manual_stop_symbols(set())
        self.apply_manual_stop_symbols({})
        self._sync_schwab_prewarm_symbols()
        for bot in self.bots.values():
            roll_day = getattr(bot, "_roll_day_if_needed", None)
            if roll_day is not None:
                roll_day()
        # Hard-purge non-protected lifecycle/watchlist symbols at the session roll.
        # `_roll_day_if_needed` above is a NO-OP whenever a pre-roll async tick/fill
        # already advanced the bot's `_active_day` to today (the 04:00 staleness
        # race), so this is what actually evicts yesterday's stale symbols — while
        # preserving open positions / pending exits. Runs on the scanner-session
        # guard (advanced only here), so it fires reliably exactly once per roll.
        for bot in self.bots.values():
            purge = getattr(bot, "purge_non_protected_session_state", None)
            if purge is not None:
                purge()
        self._resync_bot_watchlists_from_current_confirmed()
        self._active_scanner_session_start = current_session_start
        return True

    def set_broker_blocked_symbols_by_strategy(
        self,
        blocked_symbols_by_strategy: dict[str, Iterable[str]] | None,
    ) -> None:
        normalized: dict[str, set[str]] = {}
        for code in self.bots:
            symbols = blocked_symbols_by_strategy.get(code, ()) if blocked_symbols_by_strategy else ()
            normalized[code] = {
                str(symbol).upper()
                for symbol in symbols
                if str(symbol).strip()
            }
        self._broker_blocked_symbols_by_strategy = normalized
        for code, bot in self.bots.items():
            set_broker_blocked = getattr(bot, "set_broker_blocked_symbols", None)
            if set_broker_blocked is not None:
                set_broker_blocked(normalized.get(code, set()))
        self._resync_bot_watchlists_from_current_confirmed()

    def _broker_blocked_symbols_for_bot(self, code: str) -> set[str]:
        return set(self._broker_blocked_symbols_by_strategy.get(code, set()))

    def retention_summary(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for symbol, state in sorted(self.feed_retention_states.items()):
            rows.append(
                {
                    "ticker": symbol,
                    "state": state.state,
                    "blocks_entries": state.blocks_entries(),
                    "keeps_feed": state.keeps_feed(),
                    "promoted_at": state.promoted_at.isoformat(),
                    "last_confirmed_at": state.last_confirmed_at.isoformat(),
                    "state_changed_at": state.state_changed_at.isoformat(),
                    "cooldown_started_at": state.cooldown_started_at.isoformat() if state.cooldown_started_at is not None else "",
                    "below_structure_bars": state.below_structure_bars,
                    "above_structure_bars": state.above_structure_bars,
                    "active_reference_5m_volume": state.active_reference_5m_volume,
                    "degraded_mode": state.degraded_mode,
                    "degraded_since": state.degraded_since.isoformat() if state.degraded_since else "",
                    "degraded_score": state.degraded_score,
                    "recovery_score": state.recovery_score,
                    "degraded_enter_streak_bars": state.degraded_enter_streak_bars,
                    "degraded_exit_streak_bars": state.degraded_exit_streak_bars,
                }
            )
        return rows

    def _aggregate_bot_retention_states(self) -> dict[str, RetainedSymbolState]:
        priority = {"active": 0, "resume_probe": 1, "cooldown": 2, "dropped": 3}
        aggregated: dict[str, RetainedSymbolState] = {}
        for runtime in self.bots.values():
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            for symbol, state in runtime.lifecycle_states.items():
                existing = aggregated.get(symbol)
                if existing is None or priority.get(state.state, -1) >= priority.get(existing.state, -1):
                    aggregated[symbol] = state
        return aggregated

    def _retained_watchlist_symbols(self) -> list[str]:
        retained = [
            symbol
            for symbol, state in self.feed_retention_states.items()
            if state.keeps_feed()
        ]
        current_order = [str(stock["ticker"]).upper() for stock in self.current_confirmed]
        extras = sorted(symbol for symbol in retained if symbol not in current_order)
        self.retained_watchlist = current_order + extras
        return list(self.retained_watchlist)

    def _entry_blocked_symbols(self) -> list[str]:
        return sorted(
            symbol
            for symbol, state in self.feed_retention_states.items()
            if state.blocks_entries()
        )

    def _update_retained_watchlist(self, snapshot_lookup: dict[str, MarketSnapshot]) -> list[str]:
        if not self.settings.scanner_feed_retention_enabled:
            self.feed_retention_states = {
                str(stock["ticker"]).upper(): self.feed_retention_policy.promote(
                    str(stock["ticker"]).upper(),
                    self.alert_engine.now_provider(),
                    None,
                )
                for stock in self.current_confirmed
            }
            return self._retained_watchlist_symbols()

        current_now = self.alert_engine.now_provider()
        confirmed_symbols = {
            str(stock["ticker"]).upper()
            for stock in self.current_confirmed
            if str(stock.get("ticker", "")).strip()
        }
        candidate_symbols = confirmed_symbols | set(self.feed_retention_states)
        next_states: dict[str, RetainedSymbolState] = {}
        for symbol in sorted(candidate_symbols):
            metrics = self._retention_metrics_for_symbol(symbol, snapshot_lookup)
            next_state = self.feed_retention_policy.evaluate(
                self.feed_retention_states.get(symbol),
                symbol=symbol,
                now=current_now,
                is_confirmed=symbol in confirmed_symbols,
                metrics=metrics,
            )
            if next_state is None:
                continue
            if next_state.state == "dropped" and symbol not in confirmed_symbols:
                continue
            next_states[symbol] = next_state
        self.feed_retention_states = next_states
        return self._retained_watchlist_symbols()

    def _retention_metrics_for_symbol(
        self,
        symbol: str,
        snapshot_lookup: dict[str, MarketSnapshot],
    ) -> FeedRetentionMetrics | None:
        runtime = self._retention_runtime()
        normalized_symbol = symbol.upper()
        indicators: dict[str, object] = {}
        rolling_volume: float | None = None
        rolling_range_pct: float | None = None
        bar_timestamp: float | None = None

        if runtime is not None:
            indicators = dict(runtime.last_indicators.get(normalized_symbol, {}))
            builder = runtime.builder_manager.get_builder(normalized_symbol)
            if builder is not None:
                bars = builder.get_bars_as_dicts()
                if bars:
                    bar_window = max(1, int(300 / max(1, runtime.definition.interval_secs)))
                    recent_bars = bars[-bar_window:]
                    rolling_volume = float(sum(float(bar.get("volume", 0) or 0) for bar in recent_bars))
                    lows = [float(bar.get("low", 0) or 0) for bar in recent_bars if float(bar.get("low", 0) or 0) > 0]
                    highs = [float(bar.get("high", 0) or 0) for bar in recent_bars]
                    if lows and highs:
                        floor = min(lows)
                        if floor > 0:
                            rolling_range_pct = ((max(highs) - floor) / floor) * 100.0
                    bar_timestamp = float(recent_bars[-1].get("timestamp", 0) or 0)
                    if "price" not in indicators and recent_bars:
                        indicators["price"] = float(recent_bars[-1].get("close", 0) or 0)

        current_snapshot = snapshot_lookup.get(normalized_symbol)
        snapshot = current_snapshot or self.latest_snapshots.get(normalized_symbol)
        snapshot_price = float(snapshot.last_trade.price) if snapshot and snapshot.last_trade and snapshot.last_trade.price is not None else None
        snapshot_vwap = None
        if snapshot and snapshot.minute and snapshot.minute.vwap is not None:
            snapshot_vwap = float(snapshot.minute.vwap)
        elif snapshot and snapshot.day and snapshot.day.vwap is not None:
            snapshot_vwap = float(snapshot.day.vwap)
        if rolling_volume is None and snapshot is not None:
            if current_snapshot is not None:
                snapshot_volume = None
                if current_snapshot.minute and current_snapshot.minute.accumulated_volume is not None:
                    snapshot_volume = float(current_snapshot.minute.accumulated_volume)
                elif current_snapshot.day and current_snapshot.day.volume is not None:
                    snapshot_volume = float(current_snapshot.day.volume)
                if snapshot_volume is not None:
                    rolling_volume = snapshot_volume
            else:
                rolling_volume = 0.0
                if rolling_range_pct is None:
                    rolling_range_pct = 0.0

        price = _coerce_float(
            indicators.get("price"),
            snapshot_price,
        )
        vwap = _coerce_float(
            indicators.get("selected_vwap"),
            indicators.get("decision_vwap"),
            indicators.get("vwap"),
            snapshot_vwap,
        )
        ema20 = _coerce_float(indicators.get("ema20"))
        if price is None:
            return None
        return FeedRetentionMetrics(
            price=price,
            vwap=vwap,
            ema20=ema20,
            rolling_5m_volume=rolling_volume,
            rolling_5m_range_pct=rolling_range_pct,
            bar_timestamp=bar_timestamp,
        )

    def _retention_runtime(self) -> StrategyBotRuntime | None:
        runtime = self.bots.get("macd_30s")
        if isinstance(runtime, StrategyBotRuntime):
            return runtime
        runtime = self.bots.get("tos")
        if isinstance(runtime, StrategyBotRuntime):
            return runtime
        return None

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
    # Class-level default so instances built via __new__ (some unit tests) read the
    # off/byte-identical snapshot-persist path; __init__ overrides from settings.
    _snapshot_throttle_secs: float = 0.0

    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        session_factory: sessionmaker[Session] | None = None,
        now_provider: Callable[[], datetime] | None = None,
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
        self.state = StrategyEngineState(
            self.settings,
            now_provider=now_provider,
            session_factory=self.session_factory,
        )
        self.logger = configure_logging(SERVICE_NAME, self.settings.log_level)
        self.instance_name = socket.gethostname()
        # Stamped on every heartbeat so the dashboard can suppress STALE states
        # during the first few minutes after a strategy restart, when the
        # in-memory decision tape is empty until the first bar evaluates.
        self._started_at = utcnow()
        self._market_data_stream = stream_name(self.settings.redis_stream_prefix, "market-data")
        self._priority_streams = [
            stream_name(self.settings.redis_stream_prefix, "order-events"),
            stream_name(self.settings.redis_stream_prefix, "snapshot-batches"),
            stream_name(self.settings.redis_stream_prefix, "runtime-controls"),
        ]
        self._stream_offsets = {
            self._market_data_stream: "$",
            **{stream: "$" for stream in self._priority_streams},
        }
        self._last_market_data_symbols: set[str] = set()
        self._last_schwab_stream_symbols: set[str] = set()
        self._last_schwab_chart_symbols: set[str] = set()
        self._last_schwab_timesale_symbols: set[str] = set()
        self._last_scanner_history_signature: str | None = None
        self._historical_hydration_attempts = 5
        self._historical_hydration_poll_delay_secs = 0.2
        # Track in-flight background hydration so duplicate subscription-sync
        # passes don't fan out N concurrent fetches for the same symbols.
        # Key is "generic:<SYM>" or "schwab:<SYM>". Tasks keep strong refs in
        # _hydration_tasks so they aren't garbage-collected mid-flight.
        self._pending_hydration_keys: set[str] = set()
        self._hydration_tasks: set[asyncio.Task[None]] = set()
        self._runtime_db_reconcile_interval_secs = 5
        self._schwab_stream_drain_max_events = 100
        self._schwab_trade_queue: asyncio.Queue[tuple[TradeTickRecord, int]] = asyncio.Queue()
        self._schwab_quote_queue: asyncio.Queue[tuple[QuoteTickRecord, int]] = asyncio.Queue()
        self._schwab_bar_queue: asyncio.Queue[tuple[LiveBarRecord, int]] = asyncio.Queue()
        self._schwab_stream_client = self._build_schwab_stream_client()
        self._schwab_quote_poll_adapter = (
            self._schwab_stream_client.auth_adapter
            if self._schwab_stream_client is not None
            else SchwabBrokerAdapter(self.settings)
        )
        self._massive_snapshot_provider = (
            MassiveSnapshotProvider(self.settings.massive_api_key)
            if self.settings.massive_api_key
            else None
        )
        self._schwab_tick_archive = self._build_schwab_tick_archive()
        self._schwab_symbol_last_stream_trade_at: dict[str, datetime] = {}
        self._schwab_symbol_last_stream_quote_at: dict[str, datetime] = {}
        self._schwab_symbol_last_stream_bar_at: dict[str, datetime] = {}
        self._schwab_symbol_last_advancing_trade_at: dict[str, datetime] = {}
        self._schwab_symbol_last_advancing_bar_at: dict[str, datetime] = {}
        self._schwab_symbol_last_trade_event_ts: dict[str, float] = {}
        self._schwab_symbol_last_bar_event_ts: dict[str, float] = {}
        self._schwab_symbol_last_resubscribe_at: dict[str, datetime] = {}
        self._schwab_symbol_last_quote_poll_at: dict[str, datetime] = {}
        self._schwab_symbol_active_first_seen_at: dict[str, datetime] = {}
        self._schwab_stale_symbols: set[str] = set()
        self._schwab_warning_symbols: set[str] = set()
        self._schwab_stream_disconnected_since: datetime | None = None
        self._schwab_1m_last_history_refresh_at: dict[str, datetime] = {}
        self._schwab_1m_history_replay_authority_until_timestamp: dict[str, float] = {}
        self._schwab_1m_last_enqueued_live_bar_timestamp: dict[str, float] = {}
        self._schwab_1m_last_enqueued_live_bar_received_at: dict[str, datetime] = {}
        self._schwab_1m_history_refresh_interval_secs = 15
        self._schwab_cluster_reconnect_threshold = 4
        self._schwab_cluster_reconnect_cooldown_secs = 30.0
        # Only count a symbol toward the stale-cluster reconnect if it has been
        # actively trading within this window — a quiet symbol legitimately has no
        # recent completed CHART bar (no trade -> no bar) and must not look "lagging".
        self._schwab_cluster_reconnect_activity_window_secs = 120.0
        # Delivery slack: the just-closed minute's completed bar may still be in
        # flight, so only count a symbol as lagging if it trails the expected minute
        # by more than this margin (avoids the post-minute-boundary false positive).
        self._schwab_cluster_reconnect_slack_secs = 60.0
        # Chronic-lag exclusion: a symbol trailing the expected minute by MORE than
        # this is Schwab CHART delivering structurally slow (e.g., thin penny-stock
        # pre-market — PROVEN 2026-05-28 07:00 ET burst on ASTC/ATPC/FGL/MASK/SUUN
        # at 15-19 min behind trades). A streamer recycle cannot catch up to that;
        # the existing history-replay path handles it. Excluding these symbols from
        # the lagging count keeps the cluster-reconnect targeted at "slightly behind,
        # kick the socket" rather than churning on structural Schwab slowness.
        self._schwab_cluster_reconnect_chronic_lag_threshold_secs = 300.0
        self._schwab_last_forced_reconnect_at: datetime | None = None
        self._last_generic_bot_activity_snapshot_at: datetime | None = None
        self._generic_bot_activity_snapshot_interval_secs = 5
        self._subscription_sync_timeout_secs = 3.0
        # --- SPOF Workstream A: main-loop resilience state ---
        # (see docs/strategy-engine-main-loop-resilience-design.md)
        self._main_loop_step_consecutive_failures: dict[str, int] = {}
        self._main_loop_step_failure_totals: dict[str, int] = {}
        self._main_loop_exception_total = 0
        self._main_loop_last_error = ""
        self._main_loop_last_error_at: datetime | None = None
        # healthy | recovering | degraded-persistent — dedicated heartbeat field,
        # NOT folded into the top-level status Literal (keeps severity legible).
        self._main_loop_health = "healthy"
        self._main_loop_error_backoff_secs = max(
            0.0,
            float(getattr(self.settings, "strategy_main_loop_error_backoff_seconds", 1.0) or 1.0),
        )
        self._main_loop_persistent_failure_threshold = max(
            1,
            int(getattr(self.settings, "strategy_main_loop_persistent_failure_threshold", 3) or 3),
        )
        self._main_loop_step_timeout_secs = max(
            1.0,
            float(getattr(self.settings, "strategy_main_loop_step_timeout_seconds", 30.0) or 30.0),
        )
        # Snapshot-persist debounce (#350). 0.0 => off => persist every call
        # (byte-identical). When > 0, _replace_dashboard_snapshot coalesces per
        # snapshot_type; _flush_pending_snapshots() drains the trailing snapshot
        # each loop iteration and is force-flushed on shutdown / day-roll.
        self._snapshot_throttle_secs = max(
            0.0, float(getattr(self.settings, "snapshot_persist_throttle_secs", 0.0) or 0.0)
        )
        self._snapshot_last_persist_at: dict[str, datetime] = {}
        self._snapshot_pending: dict[str, dict[str, object]] = {}
        self._main_loop_fault_injection_remaining = max(
            0,
            int(getattr(self.settings, "strategy_main_loop_fault_injection_count", 0) or 0),
        )

    async def _initialize_stream_offsets(self) -> None:
        for stream in list(self._stream_offsets):
            try:
                latest = await self.redis.xrevrange(stream, count=1)
            except Exception:
                self.logger.exception("failed to initialize stream offset for %s", stream)
                self._stream_offsets[stream] = "0-0"
                continue
            self._stream_offsets[stream] = latest[0][0] if latest else "0-0"

    # Per-xread max events. Producer rate in volatile RTH windows with 30+
    # active symbols can exceed 200 events/sec; at count=50 the consumer was
    # rate-bound and polygon_30s persistence fell 100-700s behind real time
    # on 2026-05-18 (pre-fix).
    _MARKET_DATA_XREAD_COUNT = 500

    # Hard cap on events drained from market-data per main-loop iteration.
    # 5000 gives ~10x headroom over the typical 500-event xread while
    # bounding loop iteration time so flush_completed_bars and the heartbeat
    # path still run at reasonable cadence. At 65% strategy CPU with
    # count=500 alone, polygon was barely outpacing the producer and lag
    # stayed at 10+ min throughout RTH on 2026-05-18. The drain loop below
    # keeps reading until the stream is empty or this budget is exhausted.
    _MARKET_DATA_DRAIN_BUDGET = 5000

    async def _read_stream_group(
        self,
        streams: Sequence[str],
        *,
        block_ms: int | None,
    ) -> int:
        """Read up to _MARKET_DATA_XREAD_COUNT events. Returns count read."""
        offsets = {
            stream: self._stream_offsets[stream]
            for stream in streams
            if stream in self._stream_offsets
        }
        if not offsets:
            return 0
        try:
            read_kwargs: dict[str, object] = {
                "count": self._MARKET_DATA_XREAD_COUNT,
            }
            if block_ms is not None:
                read_kwargs["block"] = block_ms
            messages = await self.redis.xread(
                offsets,
                **read_kwargs,
            )
        except Exception:
            self.logger.exception("redis xread failed for streams: %s", ",".join(offsets))
            await asyncio.sleep(1)
            return 0

        if not messages:
            return 0

        processed = 0
        for stream, entries in messages:
            for message_id, fields in entries:
                self._stream_offsets[stream] = message_id
                await self._handle_stream_message(stream, fields)
                processed += 1
        return processed

    async def _drain_market_data_stream(self, *, first_block_ms: int) -> int:
        """Drain market-data aggressively. Returns total events processed.

        First xread waits up to first_block_ms for events; subsequent
        passes omit BLOCK (non-blocking) and stop the moment the stream
        is empty. Bounded by _MARKET_DATA_DRAIN_BUDGET so a single hot
        symbol cannot starve the rest of the main loop. If Schwab trade/live
        bar work is already queued, yield back after the current xread batch so
        canonical Schwab bars do not wait behind thousands of Redis messages.
        """
        events_processed = 0
        block_ms: int | None = first_block_ms
        while events_processed < self._MARKET_DATA_DRAIN_BUDGET:
            count = await self._read_stream_group(
                [self._market_data_stream],
                block_ms=block_ms,
            )
            block_ms = None  # subsequent passes must omit Redis BLOCK
            if count == 0:
                break
            events_processed += count
            if self._schwab_has_pending_priority_stream_events():
                break
        return events_processed

    def _schwab_has_pending_priority_stream_events(self) -> bool:
        return (
            not self._schwab_bar_queue.empty()
            or not self._schwab_trade_queue.empty()
        )

    async def run(self) -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        heartbeat_interval_secs = max(1, self.settings.service_heartbeat_interval_seconds)
        stream_block_ms = min(1_000, heartbeat_interval_secs * 1_000)
        last_heartbeat_at = utcnow()

        self.logger.info("%s starting", SERVICE_NAME)
        self.logger.info(
            "strategy bot config | schwab_30s=%s polygon_30s=%s schwab_1m=%s reclaim=%s macd_1m=%s tos=%s runner=%s qty=%s bots=%s",
            self.settings.strategy_macd_30s_enabled,
            self.settings.strategy_polygon_30s_enabled,
            self.settings.strategy_schwab_1m_enabled,
            self.settings.strategy_macd_30s_reclaim_enabled,
            self.settings.strategy_macd_1m_enabled,
            self.settings.strategy_tos_enabled,
            self.settings.strategy_runner_enabled,
            self.settings.strategy_macd_30s_default_quantity,
            sorted(self.state.bots.keys()),
        )
        init_completed = await self._run_init_phase(stop_event)
        if not init_completed or stop_event.is_set():
            await self._shutdown_cleanup()
            return
        last_runtime_db_reconcile_at = utcnow()

        while not stop_event.is_set():
            try:
                (
                    last_runtime_db_reconcile_at,
                    last_heartbeat_at,
                ) = await self._run_main_loop_iteration(
                    stream_block_ms=stream_block_ms,
                    heartbeat_interval_secs=heartbeat_interval_secs,
                    last_runtime_db_reconcile_at=last_runtime_db_reconcile_at,
                    last_heartbeat_at=last_heartbeat_at,
                )
            except asyncio.CancelledError:
                # Shutdown path — MUST propagate so run() can stop cleanly.
                raise
            except Exception:
                # Layer 2 backstop (SPOF Workstream A): ANY unanticipated
                # exception is contained here so one bad iteration can never end
                # the loop (= the 2026-06-03/06-07 zombie). The failure is logged
                # loudly and surfaced via the heartbeat, never swallowed silently.
                self._record_main_loop_step_failure("main_loop_body")
                self.logger.exception(
                    "[MAIN-LOOP-RECOVERED] main-loop iteration raised; loop continues "
                    "(main_loop_health=%s, consecutive=%s)",
                    self._main_loop_health,
                    self._main_loop_step_consecutive_failures.get("main_loop_body", 0),
                )
                # Keep the service observably alive AND observably degraded.
                try:
                    await self._publish_heartbeat("healthy")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.debug(
                        "post-exception heartbeat also failed", exc_info=True
                    )
                if self._main_loop_error_backoff_secs > 0:
                    await asyncio.sleep(self._main_loop_error_backoff_secs)

        await self._shutdown_cleanup()

    async def _flush_pending_persists(self) -> None:
        """Flush buffered bar-persist writes off the event loop (gated, no-op off).

        Runs the per-bot SELECT+upsert+commit on a worker thread via
        ``asyncio.to_thread`` so the event loop never blocks on DB I/O. MUST be
        invoked before publishing intents so a persisted bar is committed before
        its intent reaches OMS (OMS Tier-3 revalidation reads strategy_bar_history).
        When ``strategy_persist_offload_enabled`` is False the buffers are always
        empty (the persist methods write inline), so this is a cheap no-op and is
        only invoked when the flag is on.
        """
        if not self.settings.strategy_persist_offload_enabled:
            return
        await asyncio.to_thread(self.state.flush_pending_persists)

    async def _run_main_loop_iteration(
        self,
        *,
        stream_block_ms: int,
        heartbeat_interval_secs: int,
        last_runtime_db_reconcile_at: datetime,
        last_heartbeat_at: datetime,
    ) -> tuple[datetime, datetime]:
        """One iteration of the strategy-engine main loop.

        Extracted from ``run()`` so the Layer-2 backstop wraps a single call site
        and the resilience behaviour is unit-testable. Schwab-touching steps that
        historically escaped uncaught (the 2026-06-03 dead-token and 2026-06-07
        streamer-RuntimeError zombies) are wrapped per-step (Layer 1) so a
        contained failure still lets the rest of the iteration — crucially the
        heartbeat and the non-Schwab bots' market-data drain — run.
        """
        # Trailing-edge drain of any debounced snapshot whose window has elapsed
        # (#350). No-op when throttling is off or nothing is pending. Runs every
        # iteration (~<=1s cadence) so a coalesced snapshot is never stale long.
        self._flush_pending_snapshots()
        priority_count = await self._read_stream_group(self._priority_streams, block_ms=1)
        # Drain market-data aggressively per main-loop iteration. Bounded
        # by _MARKET_DATA_DRAIN_BUDGET so flush_completed_bars and the
        # heartbeat path still run at reasonable cadence. When the
        # priority stream was busy, don't block waiting on market-data;
        # otherwise wait up to stream_block_ms on the first xread so
        # we don't spin when there is nothing to do.
        await self._drain_market_data_stream(
            first_block_ms=1 if priority_count else stream_block_ms,
        )

        # Layer 1: a raising/malformed Schwab stream-queue item must not kill the
        # loop (the 2026-06-07 streamer-RuntimeError class).
        schwab_intent_count, schwab_event_count = await self._bounded_loop_step(
            "schwab_stream_drain",
            self._drain_schwab_stream_queues(),
            default=(0, 0),
        )
        bar_close_intents, completed_bar_count = self.state.flush_completed_bars()
        # Flush buffered bar persists BEFORE publishing so each bar is committed
        # before its intent reaches OMS (gated; no-op when offload disabled).
        await self._flush_pending_persists()
        for intent in bar_close_intents:
            await self._publish_intent(intent)
        if bar_close_intents:
            self.logger.info(
                "generated %s intents from %s forced bar closes",
                len(bar_close_intents),
                completed_bar_count,
            )

        schwab_fallback_intent_count = await self._bounded_loop_step(
            "schwab_symbol_health",
            self._monitor_schwab_symbol_health(),
            default=0,
        )
        bar_flow_change_count = self.state.monitor_completed_bar_flow(
            market_data_provider_for_strategy=self.settings.market_data_provider_for_strategy,
        )
        # Real-fix loop (2026-05-18): the bar-flow-stalled detector
        # halts entries when CHART_EQUITY drops behind. Instead of
        # waiting up to 15s per symbol for the next throttled REST
        # refresh, we immediately fetch REST for every schwab_1m
        # symbol the detector just halted. After the fetch, re-run the
        # detector so halts that recovered get cleared in the same
        # iteration. End-to-end recovery becomes ~1-3 seconds (one
        # REST roundtrip) instead of multi-minute halts.
        recovery_bar_count = 0
        if bar_flow_change_count > 0:
            stalled = self._symbols_halted_by_bar_flow_stall("schwab_1m")
            if stalled:
                # Layer 1: this Schwab REST call is a PROVEN 2026-06-03 escape
                # (dead token -> _load_schwab_history_bars -> RuntimeError).
                recovery_bar_count = await self._bounded_loop_step(
                    "schwab_1m_immediate_refresh",
                    self._immediate_schwab_1m_history_refresh(symbols=stalled),
                    default=0,
                    timeout=self._main_loop_step_timeout_secs,
                )
                if recovery_bar_count > 0:
                    # Recovery delivered bars; re-run the detector so
                    # symbols that caught up have their halts cleared
                    # without waiting for the next loop iteration.
                    bar_flow_change_count += self.state.monitor_completed_bar_flow(
                        market_data_provider_for_strategy=self.settings.market_data_provider_for_strategy,
                    )
        # Layer 1: the other PROVEN 2026-06-03 escape (dead-token RuntimeError
        # out of _load_schwab_history_bars). Unconditional every iteration pre-fix.
        schwab_1m_history_intent_count, schwab_1m_history_bar_count = await self._bounded_loop_step(
            "schwab_1m_stale_refresh",
            self._refresh_stale_schwab_1m_history(),
            default=(0, 0),
            timeout=self._main_loop_step_timeout_secs,
        )
        if (
            bar_close_intents
            or completed_bar_count
            or schwab_event_count
            or schwab_intent_count
            or schwab_fallback_intent_count
            or bar_flow_change_count
            or schwab_1m_history_intent_count
            or schwab_1m_history_bar_count
        ):
            await self._sync_subscription_targets()
            await self._publish_strategy_state_snapshot()
            self._sync_runtime_data_health_incidents()

        if (utcnow() - last_runtime_db_reconcile_at).total_seconds() >= self._runtime_db_reconcile_interval_secs:
            runtime_changed = self._reconcile_runtime_state_from_database(log_when_changed=False)
            if runtime_changed:
                await self._sync_subscription_targets()
                await self._publish_strategy_state_snapshot()
            last_runtime_db_reconcile_at = utcnow()

        if (utcnow() - last_heartbeat_at).total_seconds() >= heartbeat_interval_secs:
            if self.state._roll_scanner_session_if_needed():
                self.logger.info(
                    "rolled scanner/runtime session at %s",
                    self.state._active_scanner_session_start.isoformat(),
                )
                await self._sync_subscription_targets()
                await self._publish_strategy_state_snapshot()
                # Force-flush so the freshly-rolled (often empty) snapshot persists
                # NOW rather than waiting out the debounce window (#350).
                self._flush_pending_snapshots(force=True)
            self._sync_runtime_data_health_incidents()
            await self._publish_heartbeat("healthy")
            last_heartbeat_at = utcnow()

        # Safety net: flush anything enqueued during this iteration (e.g. via a
        # publish-site whose intent list was empty, so no per-site flush ran) so
        # nothing survives the iteration unflushed. Gated; no-op when off.
        await self._flush_pending_persists()

        # A full clean iteration clears the outer-backstop failure streak.
        self._record_main_loop_step_success("main_loop_body")
        return last_runtime_db_reconcile_at, last_heartbeat_at

    async def _bounded_loop_step(
        self,
        label: str,
        awaitable: Coroutine[object, object, object],
        *,
        default: object,
        timeout: float | None = None,
    ) -> object:
        """Layer-1 guard: run one Schwab-touching loop step, contain any failure.

        Returns the step result on success (clearing the step's failure streak);
        returns ``default`` on a contained ``Exception``/timeout (recording the
        failure for escalation). ``CancelledError`` is re-raised so shutdown still
        propagates.
        """
        try:
            if timeout is not None:
                result = await asyncio.wait_for(awaitable, timeout=timeout)
            else:
                result = await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_main_loop_step_failure(label)
            self.logger.exception(
                "[MAIN-LOOP-STEP-FAILED] step=%s contained; iteration continues "
                "(main_loop_health=%s, consecutive=%s)",
                label,
                self._main_loop_health,
                self._main_loop_step_consecutive_failures.get(label, 0),
            )
            return default
        self._record_main_loop_step_success(label)
        return result

    def _record_main_loop_step_failure(self, label: str) -> None:
        self._main_loop_step_consecutive_failures[label] = (
            self._main_loop_step_consecutive_failures.get(label, 0) + 1
        )
        self._main_loop_step_failure_totals[label] = (
            self._main_loop_step_failure_totals.get(label, 0) + 1
        )
        self._main_loop_exception_total += 1
        self._main_loop_last_error = label
        self._main_loop_last_error_at = utcnow()
        self._refresh_main_loop_health()

    def _record_main_loop_step_success(self, label: str) -> None:
        if self._main_loop_step_consecutive_failures.get(label):
            self._main_loop_step_consecutive_failures[label] = 0
            self._refresh_main_loop_health()

    def _refresh_main_loop_health(self) -> None:
        worst = max(self._main_loop_step_consecutive_failures.values(), default=0)
        if worst >= self._main_loop_persistent_failure_threshold:
            new_health = "degraded-persistent"
        elif worst >= 1:
            new_health = "recovering"
        else:
            new_health = "healthy"
        if (
            new_health == "degraded-persistent"
            and self._main_loop_health != "degraded-persistent"
        ):
            failing = sorted(
                label
                for label, count in self._main_loop_step_consecutive_failures.items()
                if count >= self._main_loop_persistent_failure_threshold
            )
            # Loud, distinct, transition-edge signature. The CONTINUOUS signal is
            # the main_loop_health heartbeat field (Workstream B dashboard banner).
            self.logger.error(
                "[MAIN-LOOP-DEGRADED-PERSISTENT] strategy-engine main loop has had "
                "%s+ consecutive failures on step(s) %s; the loop is ALIVE and "
                "heartbeating but the Schwab path is failing — INVESTIGATE "
                "(most likely a dead Schwab token; re-auth + restart strategy/oms)",
                self._main_loop_persistent_failure_threshold,
                ",".join(failing) or "?",
            )
        self._main_loop_health = new_health

    def _main_loop_health_heartbeat_details(self) -> dict[str, str]:
        failing = sorted(
            label
            for label, count in self._main_loop_step_consecutive_failures.items()
            if count > 0
        )
        last_error_age = ""
        if self._main_loop_last_error_at is not None:
            last_error_age = str(
                int((utcnow() - self._main_loop_last_error_at).total_seconds())
            )
        return {
            "main_loop_health": self._main_loop_health,
            "main_loop_exceptions_total": str(self._main_loop_exception_total),
            "main_loop_failing_steps": ",".join(failing),
            "main_loop_last_error_age_secs": last_error_age,
        }

    async def _run_init_phase(self, stop_event: asyncio.Event) -> bool:
        """Execute the startup sequence, racing it against ``stop_event``.

        Returns ``True`` when init finishes normally. Returns ``False`` if
        SIGTERM (or SIGINT) arrives mid-init — the caller is then expected to
        skip the main loop and go straight to bounded cleanup. Without this
        race, a back-to-back restart hits the unbounded ``_restore_runtime_*``
        + ``_prefill_alert_history_*`` + Schwab streamer connect chain (~20-40s
        on a real VPS) and gets SIGKILLed at ``TimeoutStopSec=30``.
        """
        if stop_event.is_set():
            return False

        async def _do_init() -> None:
            await self._initialize_stream_offsets()
            await asyncio.to_thread(self._restore_alert_engine_state_from_dashboard_snapshot)
            await asyncio.to_thread(self._seed_confirmed_candidates_from_dashboard_snapshot)
            await asyncio.to_thread(self._restore_runtime_state_from_database)
            await asyncio.to_thread(self._purge_stale_manual_stop_snapshots)
            await asyncio.to_thread(self._preload_manual_stop_state)
            await self._prefill_alert_history_from_snapshot_batches()
            if self._schwab_stream_client is not None:
                await self._schwab_stream_client.start(
                    on_trade=self._enqueue_schwab_trade_tick,
                    on_quote=self._enqueue_schwab_quote_tick,
                    on_bar=self._enqueue_schwab_live_bar,
                )
            await self._sync_subscription_targets()
            await self._publish_strategy_state_snapshot()
            self._sync_runtime_data_health_incidents()
            await self._publish_heartbeat("starting")

        init_task = asyncio.create_task(_do_init())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                [init_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not stop_task.done():
                stop_task.cancel()
                try:
                    await stop_task
                except (asyncio.CancelledError, Exception):
                    pass

        if init_task in done:
            exc = init_task.exception()
            if exc is not None:
                raise exc
            return True

        self.logger.warning(
            "strategy-engine SIGTERM during init; cancelling in-flight init and shutting down"
        )
        init_task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(init_task, return_exceptions=True),
                timeout=3.0,
            )
        except (TimeoutError, Exception):
            self.logger.debug("init task did not exit cleanly during SIGTERM cancel", exc_info=True)
        return False

    async def _shutdown_cleanup(self) -> None:
        """Bounded teardown — every step capped by ``asyncio.wait_for``.

        Worst-case total ~17s, well under systemd's ``TimeoutStopSec=30``.
        """
        try:
            await asyncio.wait_for(self._publish_heartbeat("stopping"), timeout=2.0)
        except TimeoutError:
            self.logger.warning("publish_heartbeat('stopping') timed out; proceeding with shutdown")
        except Exception:
            self.logger.debug("publish_heartbeat('stopping') raised", exc_info=True)
        # Flush any buffered bar persists before teardown so nothing enqueued
        # during the final iteration is lost (gated; no-op when offload off).
        if self.settings.strategy_persist_offload_enabled:
            try:
                self.state.flush_pending_persists()
            except Exception:
                self.logger.debug("flush_pending_persists during shutdown raised", exc_info=True)
        # Force-persist the latest debounced dashboard/scanner snapshot so the
        # dashboard isn't left on a stale row after shutdown (#350; no-op off).
        try:
            self._flush_pending_snapshots(force=True)
        except Exception:
            self.logger.debug("flush_pending_snapshots during shutdown raised", exc_info=True)
        if self._schwab_stream_client is not None:
            try:
                await asyncio.wait_for(self._schwab_stream_client.stop(), timeout=10.0)
            except TimeoutError:
                self.logger.warning("Schwab streamer stop timed out; proceeding with shutdown")
            except Exception:
                self.logger.debug("Schwab streamer stop raised", exc_info=True)
        if self._schwab_tick_archive is not None:
            try:
                self._schwab_tick_archive.close()
            except Exception:
                self.logger.debug("tick archive close raised", exc_info=True)
        try:
            await asyncio.wait_for(self.redis.aclose(), timeout=5.0)
        except TimeoutError:
            self.logger.warning("redis aclose timed out; proceeding with shutdown")
        except Exception:
            self.logger.debug("redis aclose raised", exc_info=True)
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
            self._preload_manual_stop_state()
            # Roll the scanner session (and hard-purge stale watchlist state) BEFORE
            # the broker-blocked resync. Otherwise the resync re-promotes yesterday's
            # handoff symbols into bot lifecycle ~ms ahead of the in-batch roll (the
            # 04:00 staleness race). Rolling first means the resync reads a clean,
            # post-roll handoff. No-op on non-boundary batches (scanner-session guard).
            self.state._roll_scanner_session_if_needed()
            self.state.set_broker_blocked_symbols_by_strategy(
                self._load_schwab_ineligible_symbols_by_strategy()
            )
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

        if event_type == "manual_stop_update":
            event = ManualStopUpdateEvent.model_validate(payload)
            self.state.apply_manual_stop_update(
                scope=event.payload.scope,
                action=event.payload.action,
                symbol=event.payload.symbol,
                strategy_code=event.payload.strategy_code,
            )
            await self._publish_strategy_state_snapshot()
            self.logger.info("manual stop update applied to live runtime")
            return

        if event_type == "trade_tick":
            event = TradeTickEvent.model_validate(payload)
            strategy_codes = self._generic_market_data_strategy_codes(event.payload.symbol)
            intents = self.state.handle_trade_tick(
                symbol=event.payload.symbol,
                price=float(event.payload.price),
                size=event.payload.size,
                timestamp_ns=event.payload.timestamp_ns,
                cumulative_volume=event.payload.cumulative_volume,
                strategy_codes=strategy_codes,
            )
            await self._flush_pending_persists()
            for intent in intents:
                await self._publish_intent(intent)
            if intents:
                await self._sync_subscription_targets()
                await self._publish_strategy_state_snapshot()
            elif strategy_codes:
                await self._publish_strategy_state_snapshot_for_generic_bot_activity()
            if intents:
                self.logger.info(
                    "generated %s intents from %s trade tick",
                    len(intents),
                    event.payload.symbol,
                )
            return

        if event_type == "quote_tick":
            event = QuoteTickEvent.model_validate(payload)
            intents = self.state.handle_quote_tick(
                symbol=event.payload.symbol,
                bid_price=float(event.payload.bid_price) if event.payload.bid_price is not None else None,
                ask_price=float(event.payload.ask_price) if event.payload.ask_price is not None else None,
                strategy_codes=self._generic_market_data_strategy_codes(event.payload.symbol),
            )
            for intent in intents:
                await self._publish_intent(intent)
            if intents:
                await self._sync_subscription_targets()
                await self._publish_strategy_state_snapshot()
            return

        if event_type == "live_bar":
            event = LiveBarEvent.model_validate(payload)
            strategy_codes = self._generic_market_data_strategy_codes(event.payload.symbol)
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
                coverage_started_at=(
                    float(event.payload.coverage_started_at)
                    if event.payload.coverage_started_at is not None
                    else None
                ),
                strategy_codes=strategy_codes,
            )
            await self._flush_pending_persists()
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
            elif strategy_codes:
                await self._publish_strategy_state_snapshot_for_generic_bot_activity()
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
                strategy_codes=self._generic_market_data_strategy_codes(event.payload.symbol),
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
        symbol = str(intent.payload.symbol).strip().upper()
        if symbol and symbol in self.settings.protected_symbol_set:
            self.logger.warning(
                "blocked intent for protected symbol %s (strategy=%s intent_type=%s side=%s qty=%s); "
                "symbol is in MAI_TAI_PROTECTED_SYMBOLS exception list",
                symbol,
                intent.payload.strategy_code,
                intent.payload.intent_type,
                intent.payload.side,
                intent.payload.quantity,
            )
            return
        stream = stream_name(self.settings.redis_stream_prefix, "strategy-intents")
        await self.redis.xadd(
            stream,
            {"data": intent.model_dump_json()},
            maxlen=self.settings.redis_strategy_intent_stream_maxlen,
            approximate=True,
        )

    def _strategy_health_status(self, status: str) -> str:
        if self._schwab_stale_symbols:
            return "degraded"
        for runtime in self.state.bots.values():
            if isinstance(runtime, StrategyBotRuntime) and runtime.data_halt_symbols:
                return "degraded"
        return status

    async def _publish_heartbeat(self, status: str) -> None:
        effective_status = self._strategy_health_status(status)
        stream_client = self._schwab_stream_client
        stream = stream_name(self.settings.redis_stream_prefix, "heartbeats")
        event = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=self.instance_name,
                status=effective_status,
                details={
                    "watchlist_size": str(len(self.state.retained_watchlist)),
                    "bot_count": str(len(self.state.bots)),
                    "schwab_stream_symbols": str(len(self.state.schwab_stream_symbols())),
                    "schwab_stale_symbols": ",".join(sorted(self._schwab_stale_symbols)),
                    "schwab_generic_fallback_active": str(
                        self._should_use_generic_market_data_fallback_for_schwab()
                    ).lower(),
                    "schwab_stream_connected": str(
                        bool(stream_client is not None and getattr(stream_client, "connected", False))
                    ).lower(),
                    "schwab_stream_last_error": str(getattr(stream_client, "last_error", "") or ""),
                    "engine_started_at": self._started_at.isoformat(),
                    # SPOF Workstream A: dedicated main-loop health field (NOT
                    # folded into the top-level status Literal). degraded-persistent
                    # = the loop is alive + heartbeating but a step keeps failing.
                    **self._main_loop_health_heartbeat_details(),
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
                prewarm_symbols=[str(symbol) for symbol in bot.get("prewarm_symbols", [])],
                data_health=dict(bot.get("data_health", {}) or {}),
                retention_states=list(bot.get("retention_states", [])),
                positions=list(bot["positions"]),
                pending_open_symbols=[str(symbol) for symbol in bot["pending_open_symbols"]],
                pending_close_symbols=[str(symbol) for symbol in bot["pending_close_symbols"]],
                pending_scale_levels=[str(level) for level in bot["pending_scale_levels"]],
                daily_pnl=float(bot.get("daily_pnl", 0) or 0),
                closed_today=list(bot.get("closed_today", [])),
                recent_decisions=list(bot.get("recent_decisions", [])),
                indicator_snapshots=list(bot.get("indicator_snapshots", [])),
                bar_counts={
                    str(symbol).upper(): int(count or 0)
                    for symbol, count in dict(bot.get("bar_counts", {}) or {}).items()
                },
                last_tick_at={
                    str(symbol).upper(): str(observed_at or "")
                    for symbol, observed_at in dict(bot.get("last_tick_at", {}) or {}).items()
                },
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
                schwab_prewarm_symbols=[str(symbol) for symbol in summary.get("schwab_prewarm_symbols", [])],
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

    async def _publish_strategy_state_snapshot_for_generic_bot_activity(self) -> None:
        now = utcnow()
        last = self._last_generic_bot_activity_snapshot_at
        if (
            last is not None
            and (now - last).total_seconds() < self._generic_bot_activity_snapshot_interval_secs
        ):
            return
        await self._publish_strategy_state_snapshot()
        self._last_generic_bot_activity_snapshot_at = now

    def _sync_runtime_data_health_incidents(self) -> None:
        if self.session_factory is None:
            return

        active: dict[str, dict[str, object]] = {}
        for code, runtime in self.state.bots.items():
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            health = runtime.data_health_summary()
            reasons = dict(health.get("reasons", {}) or {})
            for symbol in list(health.get("halted_symbols", []) or []):
                normalized = str(symbol).upper()
                if not normalized:
                    continue
                reason = str(reasons.get(normalized) or reasons.get(symbol) or "")
                if not reason.startswith("Completed bar flow stalled:"):
                    continue
                title = f"{code} completed-bar flow stalled for {normalized}"
                severity = (
                    "critical"
                    if normalized
                    in {
                        str(symbol).upper()
                        for symbol in list(health.get("exposure_halted_symbols", []) or [])
                    }
                    else "warning"
                )
                active[title] = {
                    "source": "runtime_data_health",
                    "strategy_code": code,
                    "symbol": normalized,
                    "reason": reason,
                    "account_name": runtime.definition.account_name,
                    "data_health_status": health.get("status", ""),
                    "severity": severity,
                }

        try:
            with self.session_factory() as session:
                open_incidents = session.scalars(
                    select(SystemIncident).where(
                        SystemIncident.service_name == SERVICE_NAME,
                        SystemIncident.status != "closed",
                    )
                ).all()
                tracked_open = {
                    incident.title: incident
                    for incident in open_incidents
                    if isinstance(incident.payload, dict)
                    and incident.payload.get("source") == "runtime_data_health"
                }

                for title, payload in active.items():
                    severity = str(payload.get("severity", "warning") or "warning")
                    incident = tracked_open.get(title)
                    if incident is None:
                        session.add(
                            SystemIncident(
                                service_name=SERVICE_NAME,
                                severity=severity,
                                title=title,
                                status="open",
                                payload=payload,
                                opened_at=utcnow(),
                            )
                        )
                        continue
                    incident.severity = severity
                    incident.payload = payload

                for title, incident in tracked_open.items():
                    if title not in active:
                        incident.status = "closed"
                        incident.closed_at = utcnow()

                session.commit()
        except Exception:
            self.logger.exception("failed syncing runtime data-health incidents")

    def _spawn_background_hydration(
        self,
        *,
        kind: str,
        symbols: set[str],
        coro_factory,
    ) -> None:
        """Run a hydration coroutine off the main subscription-sync path.

        Scanner symbol-promotion was calling `_sync_market_data_subscriptions`
        which `await`ed `_hydrate_recent_historical_bars` inline. On 2026-05-18
        07:07:48 ET, SBFM got promoted and the synchronous replay of 1924
        historical bars blocked the event loop for 47s. During that gap the
        GOVX 07:07:30 macd_30s bar never closed on-schedule, so the
        P1_CROSS signal that should have produced an intent ~07:08:07 ET
        didn't fire until 07:08:40 — a 33s entry delay, 2.45 -> 2.44 fill
        and a hard-stop loss at 2.4002.

        Detaching hydration keeps the event loop unblocked. New symbols
        get live ticks immediately; their indicators backfill once
        hydration completes (bot evaluator gates entries on warmup, so
        no false signals during the window).
        """
        fresh = {sym for sym in symbols if f"{kind}:{sym}" not in self._pending_hydration_keys}
        if not fresh:
            return
        keys = {f"{kind}:{sym}" for sym in fresh}
        self._pending_hydration_keys.update(keys)

        async def _run() -> None:
            try:
                await coro_factory(fresh)
            except Exception:
                self.logger.exception(
                    "background hydration failed | kind=%s symbols=%s",
                    kind,
                    sorted(fresh),
                )
            finally:
                self._pending_hydration_keys.difference_update(keys)

        sample = ",".join(sorted(fresh))[:60]
        task = asyncio.create_task(_run(), name=f"hydrate-{kind}-{sample}")
        self._hydration_tasks.add(task)
        task.add_done_callback(self._hydration_tasks.discard)

    async def _wait_for_pending_hydration(self) -> None:
        """Await all in-flight background hydration tasks.

        Used by tests and by clean-shutdown paths. Hot paths should
        never call this — it would re-introduce the blocking behavior
        the spawn-as-background change exists to remove.
        """
        while self._hydration_tasks:
            tasks = list(self._hydration_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)

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
        self._spawn_background_hydration(
            kind="generic",
            symbols=normalized,
            coro_factory=self._hydrate_recent_historical_bars,
        )

    async def _sync_schwab_stream_subscriptions(self, symbols: Sequence[str]) -> None:
        normalized = {symbol.upper() for symbol in symbols if symbol}
        chart_symbols = set(self.state.schwab_live_bar_symbols(interval_secs=60))
        timesale_symbols = set(self.state.schwab_timesale_symbols())
        if (
            normalized == self._last_schwab_stream_symbols
            and chart_symbols == self._last_schwab_chart_symbols
            and timesale_symbols == self._last_schwab_timesale_symbols
        ):
            return

        self._last_schwab_stream_symbols = normalized
        self._last_schwab_chart_symbols = chart_symbols
        self._last_schwab_timesale_symbols = timesale_symbols
        if self._schwab_tick_archive is not None:
            self._schwab_tick_archive.record_subscription_snapshot(sorted(normalized))
        if self._schwab_stream_client is None:
            return
        try:
            await self._schwab_stream_client.sync_subscriptions(
                sorted(normalized),
                chart_symbols=sorted(chart_symbols),
                timesale_symbols=sorted(timesale_symbols),
            )
        except TypeError:
            await self._schwab_stream_client.sync_subscriptions(sorted(normalized))

    async def _sync_subscription_targets(
        self,
        *,
        market_data_symbols: Sequence[str] | None = None,
        schwab_stream_symbols: Sequence[str] | None = None,
    ) -> None:
        effective_market_data_symbols = (
            self.state.market_data_symbols() if market_data_symbols is None else list(market_data_symbols)
        )
        effective_schwab_symbols = (
            self.state.schwab_stream_symbols() if schwab_stream_symbols is None else list(schwab_stream_symbols)
        )
        if self._should_use_generic_market_data_fallback_for_schwab():
            fallback_schwab_symbols = self.state.schwab_active_symbols()
            effective_market_data_symbols = sorted(
                {str(symbol).upper() for symbol in effective_market_data_symbols}
                | {str(symbol).upper() for symbol in fallback_schwab_symbols}
            )
        await self._bounded_subscription_sync_step(
            "market-data subscriptions",
            self._sync_market_data_subscriptions(
                effective_market_data_symbols
            ),
        )
        await self._bounded_subscription_sync_step(
            "schwab stream subscriptions",
            self._sync_schwab_stream_subscriptions(
                effective_schwab_symbols
            ),
        )
        self._spawn_background_hydration(
            kind="schwab",
            symbols={str(sym).upper() for sym in effective_schwab_symbols},
            coro_factory=self._hydrate_recent_schwab_historical_bars,
        )

    async def _bounded_subscription_sync_step(
        self,
        label: str,
        awaitable: Coroutine[object, object, object],
    ) -> None:
        try:
            await asyncio.wait_for(
                awaitable,
                timeout=self._subscription_sync_timeout_secs,
            )
        except TimeoutError:
            self.logger.warning(
                "timed out syncing %s after %.1fs; continuing with strategy-state publication",
                label,
                self._subscription_sync_timeout_secs,
            )
        except Exception:
            self.logger.exception(
                "failed syncing %s; continuing with strategy-state publication",
                label,
            )

    def _should_use_generic_market_data_fallback_for_schwab(self) -> bool:
        if not self.state.schwab_stream_strategy_codes():
            return False
        client = self._schwab_stream_client
        if client is None:
            return False
        return not getattr(client, "connected", False)

    def _generic_market_data_strategy_codes(self, symbol: str) -> tuple[str, ...]:
        del symbol
        schwab_codes = set(self.state.schwab_stream_strategy_codes())
        selected: list[str] = []
        for code in self.state.bots:
            if code in schwab_codes:
                continue
            selected.append(code)
        return tuple(selected)

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
                    persisted = self._persist_generic_provider_history_bars(
                        symbol=event.payload.symbol,
                        interval_secs=int(event.payload.interval_secs),
                        bars=bars,
                        strategy_codes=hydrated,
                    )
                    hydrated_any = True
                    self.logger.info(
                        "replayed %s historical bars for %s @ %ss into %s%s",
                        len(bars),
                        event.payload.symbol,
                        event.payload.interval_secs,
                        ",".join(hydrated),
                        (
                            f" | persisted={persisted}"
                            if persisted > 0
                            else ""
                        ),
                    )
                pending.discard(pair)

            if not pending:
                break

            await asyncio.sleep(self._historical_hydration_poll_delay_secs)

        if hydrated_any:
            await self._publish_strategy_state_snapshot()
        if pending:
            direct_hydrated = await self._hydrate_generic_history_from_provider(pending)
            if direct_hydrated:
                hydrated_any = True
                pending = {
                    pair
                    for pair in pending
                    if not self._generic_history_seed_ready(*pair)
                }
                await self._publish_strategy_state_snapshot()
        if pending:
            for symbol, interval_secs in sorted(pending):
                self.logger.info(
                    "no historical bars available for %s @ %ss during hydration replay",
                    symbol,
                    interval_secs,
                )

    def _generic_history_seed_ready(self, symbol: str, interval_secs: int) -> bool:
        normalized_symbol = str(symbol).upper()
        for code, bot in self.state.bots.items():
            if code in self.state._schwab_native_history_bot_codes:
                continue
            if normalized_symbol not in bot.active_symbols():
                continue
            definition = getattr(bot, "definition", None)
            bot_interval = int(definition.interval_secs) if definition is not None else None
            if bot_interval != int(interval_secs):
                continue
            if normalized_symbol not in bot.last_indicators:
                bot.rebuild_indicator_state(normalized_symbol)
            if (
                bot.builder_manager.get_or_create(normalized_symbol).get_bar_count() >= bot.required_history_bars()
                and normalized_symbol in bot.last_indicators
            ):
                return True
        return False

    def _generic_history_required_bars(self, symbol: str, interval_secs: int) -> int:
        normalized_symbol = str(symbol).upper()
        required = 0
        for code, bot in self.state.bots.items():
            if code in self.state._schwab_native_history_bot_codes:
                continue
            if normalized_symbol not in bot.active_symbols():
                continue
            definition = getattr(bot, "definition", None)
            bot_interval = int(definition.interval_secs) if definition is not None else None
            if bot_interval != int(interval_secs):
                continue
            required = max(required, bot.required_history_bars())
        return required

    async def _load_generic_market_history_bars(
        self,
        *,
        symbol: str,
        interval_secs: int,
        required_bars: int,
    ) -> list[dict[str, float | int]]:
        if self._massive_snapshot_provider is None:
            return []

        lookback_calendar_days = max(3, min(10, (max(1, required_bars) // 60) + 2))
        limit = max(required_bars * 4, required_bars, 120)
        try:
            records = await asyncio.wait_for(
                asyncio.to_thread(
                    self._massive_snapshot_provider.fetch_historical_bars,
                    symbol,
                    interval_secs=int(interval_secs),
                    lookback_calendar_days=lookback_calendar_days,
                    limit=limit,
                ),
                timeout=15,
            )
        except asyncio.TimeoutError:
            self.logger.warning(
                "generic provider historical fetch timed out for %s @ %ss",
                symbol,
                interval_secs,
            )
            return []
        except Exception:
            self.logger.exception(
                "generic provider historical fetch failed for %s @ %ss",
                symbol,
                interval_secs,
            )
            return []

        return [
            {
                "open": float(record.open),
                "high": float(record.high),
                "low": float(record.low),
                "close": float(record.close),
                "volume": int(record.volume),
                "timestamp": float(record.timestamp),
                "trade_count": int(record.trade_count),
            }
            for record in records
        ]

    async def _hydrate_generic_history_from_provider(
        self,
        pending: set[tuple[str, int]],
    ) -> bool:
        if not pending or self._massive_snapshot_provider is None:
            return False

        hydrated_any = False
        for symbol, interval_secs in sorted(pending):
            if self._generic_history_seed_ready(symbol, interval_secs):
                continue
            required_bars = self._generic_history_required_bars(symbol, interval_secs)
            if required_bars <= 0:
                continue
            bars = await self._load_generic_market_history_bars(
                symbol=symbol,
                interval_secs=interval_secs,
                required_bars=required_bars,
            )
            if not bars:
                continue
            hydrated = self.state.hydrate_historical_bars(
                symbol=symbol,
                interval_secs=interval_secs,
                bars=bars,
            )
            if hydrated:
                persisted = self._persist_generic_provider_history_bars(
                    symbol=symbol,
                    interval_secs=interval_secs,
                    bars=bars,
                    strategy_codes=hydrated,
                )
                hydrated_any = True
                self.logger.info(
                    "fetched %s direct provider history bars for %s @ %ss into %s%s",
                    len(bars),
                    symbol,
                    interval_secs,
                    ",".join(hydrated),
                    (
                        f" | persisted={persisted}"
                        if persisted > 0
                        else ""
                    ),
                )
        return hydrated_any

    def _persist_generic_provider_history_bars(
        self,
        *,
        symbol: str,
        interval_secs: int,
        bars: Sequence[dict[str, float | int]],
        strategy_codes: Sequence[str],
    ) -> int:
        if self.session_factory is None or not bars or not strategy_codes:
            return 0

        normalized_symbol = str(symbol).upper()
        valid_codes: list[str] = []
        for code in strategy_codes:
            runtime = self.state.bots.get(code)
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            if int(runtime.definition.interval_secs) != int(interval_secs):
                continue
            if code in self.state._schwab_native_history_bot_codes:
                continue
            if not runtime.use_live_aggregate_bars:
                continue
            valid_codes.append(code)
        if not valid_codes:
            return 0

        overlap_seconds = max(1, int(interval_secs)) * 4
        persisted_count = 0
        try:
            with self.session_factory() as session:
                latest_by_code: dict[str, datetime | None] = {}
                for code in valid_codes:
                    latest_by_code[code] = session.scalar(
                        select(StrategyBarHistory.bar_time)
                        .where(
                            StrategyBarHistory.strategy_code.in_(strategy_code_candidates(code)),
                            StrategyBarHistory.symbol == normalized_symbol,
                            StrategyBarHistory.interval_secs == int(interval_secs),
                        )
                        .order_by(StrategyBarHistory.bar_time.desc())
                        .limit(1)
                    )

                for code in valid_codes:
                    latest_bar_time = latest_by_code.get(code)
                    replay_after_ts: float | None = None
                    if latest_bar_time is not None:
                        latest_dt = (
                            latest_bar_time.replace(tzinfo=UTC)
                            if latest_bar_time.tzinfo is None
                            else latest_bar_time.astimezone(UTC)
                        )
                        replay_after_ts = latest_dt.timestamp() - overlap_seconds

                    for bar in bars:
                        timestamp = _coerce_float(bar.get("timestamp"))
                        if timestamp is None or timestamp <= 0:
                            continue
                        if replay_after_ts is not None and timestamp < replay_after_ts:
                            continue

                        bar_time = datetime.fromtimestamp(timestamp, UTC)
                        record = session.scalar(
                            select(StrategyBarHistory).where(
                                StrategyBarHistory.strategy_code.in_(strategy_code_candidates(code)),
                                StrategyBarHistory.symbol == normalized_symbol,
                                StrategyBarHistory.interval_secs == int(interval_secs),
                                StrategyBarHistory.bar_time == bar_time,
                            )
                        )
                        if record is None:
                            record = StrategyBarHistory(
                                strategy_code=code,
                                symbol=normalized_symbol,
                                interval_secs=int(interval_secs),
                                bar_time=bar_time,
                                position_state="flat",
                                position_quantity=0,
                            )
                            session.add(record)

                        record.open_price = Decimal(str(bar["open"]))
                        record.high_price = Decimal(str(bar["high"]))
                        record.low_price = Decimal(str(bar["low"]))
                        record.close_price = Decimal(str(bar["close"]))
                        record.volume = int(bar["volume"])
                        record.trade_count = int(bar.get("trade_count", 0) or 0)
                        persisted_count += 1

                if persisted_count > 0:
                    session.commit()
        except Exception:
            self.logger.exception(
                "failed persisting generic provider history bars for %s @ %ss into %s",
                normalized_symbol,
                interval_secs,
                ",".join(valid_codes),
            )
            return 0

        return persisted_count

    async def _hydrate_recent_schwab_historical_bars(self, symbols: set[str]) -> None:
        if not symbols:
            return

        targets = self.state.schwab_native_history_targets(sorted(symbols))
        if not targets:
            return

        hydrated_any = False
        for code, symbol, interval_secs in targets:
            runtime = self.state.bots.get(code)
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            if not runtime.needs_history_seed(symbol):
                continue

            bars = await self._load_schwab_history_bars(
                symbol=symbol,
                interval_secs=interval_secs,
                required_bars=runtime.required_history_bars(),
            )
            if not bars:
                self.logger.info(
                    "no Schwab-native historical bars available for %s @ %ss during bootstrap",
                    symbol,
                    interval_secs,
                )
                continue

            hydrated = self.state.hydrate_historical_bars(
                symbol=symbol,
                interval_secs=interval_secs,
                bars=bars,
                strategy_codes=[code],
            )
            if hydrated:
                hydrated_any = True
                self.logger.info(
                    "bootstrapped %s Schwab historical bars for %s @ %ss into %s",
                    len(bars),
                    symbol,
                    interval_secs,
                    ",".join(hydrated),
                )

        if hydrated_any:
            await self._publish_strategy_state_snapshot()

    @staticmethod
    def _latest_expected_completed_bar_timestamp(
        *,
        now: datetime,
        interval_secs: int,
    ) -> float | None:
        interval = max(1, int(interval_secs))
        current_bucket_start = int(now.timestamp() // interval) * interval
        latest_completed_bucket_start = float(current_bucket_start - interval)
        session_start = current_scanner_session_start_utc(now).timestamp()
        if latest_completed_bucket_start < session_start:
            return None
        return latest_completed_bucket_start

    @staticmethod
    def _latest_runtime_completed_bar_timestamp(
        runtime: StrategyBotRuntime,
        symbol: str,
    ) -> float | None:
        builder = runtime.builder_manager.get_builder(symbol)
        if builder is None:
            return None
        try:
            bars = builder.get_bars_as_dicts()
        except Exception:
            return None
        if not bars:
            return None
        latest = bars[-1]
        if not isinstance(latest, dict):
            return _coerce_float(getattr(latest, "timestamp", None))
        return _coerce_float(latest.get("timestamp"))

    def _symbols_halted_by_bar_flow_stall(self, bot_code: str) -> list[str]:
        """Return symbols currently halted by the bar-flow-stalled detector.

        Used by the on-stall recovery path to know which symbols need an
        immediate REST refresh (bypassing the 15s throttle).
        """
        runtime = self.state.bots.get(bot_code)
        if not isinstance(runtime, StrategyBotRuntime):
            return []
        halt_reasons = getattr(runtime, "data_halt_symbols", None) or {}
        return sorted(
            symbol
            for symbol, reason in halt_reasons.items()
            if str(reason).startswith("Completed bar flow stalled:")
        )

    def _remember_schwab_1m_history_replay_authority(
        self,
        symbol: str,
        *,
        latest_bar_timestamp: float,
    ) -> None:
        normalized = str(symbol).upper()
        current = self._schwab_1m_history_replay_authority_until_timestamp.get(normalized)
        if current is None or latest_bar_timestamp > current:
            self._schwab_1m_history_replay_authority_until_timestamp[normalized] = latest_bar_timestamp

    def _release_schwab_1m_history_replay_authority(
        self,
        symbol: str,
        *,
        live_bar_timestamp: float,
    ) -> None:
        normalized = str(symbol).upper()
        current = self._schwab_1m_history_replay_authority_until_timestamp.get(normalized)
        if current is not None and live_bar_timestamp > current:
            self._schwab_1m_history_replay_authority_until_timestamp.pop(normalized, None)

    def _remember_schwab_1m_enqueued_live_bar(
        self,
        symbol: str,
        *,
        live_bar_timestamp: float,
        received_at_ns: int,
    ) -> None:
        normalized = str(symbol).upper()
        current = self._schwab_1m_last_enqueued_live_bar_timestamp.get(normalized)
        if current is None or live_bar_timestamp >= current:
            self._schwab_1m_last_enqueued_live_bar_timestamp[normalized] = float(live_bar_timestamp)
            self._schwab_1m_last_enqueued_live_bar_received_at[normalized] = datetime.fromtimestamp(
                received_at_ns / 1_000_000_000,
                UTC,
            )

    def _schwab_1m_live_bar_already_received(
        self,
        symbol: str,
        *,
        minimum_timestamp: float,
    ) -> bool:
        latest_timestamp = self._schwab_1m_last_enqueued_live_bar_timestamp.get(str(symbol).upper())
        return latest_timestamp is not None and latest_timestamp >= float(minimum_timestamp)

    @staticmethod
    def _schwab_1m_history_replay_grace_seconds(*, interval_secs: int) -> float:
        interval = max(1, int(interval_secs))
        return max(15.0, float(interval) * 0.5)

    @classmethod
    def _schwab_1m_history_replay_deadline_timestamp(
        cls,
        *,
        expected_latest_completed: float,
        interval_secs: int,
    ) -> float:
        return (
            float(expected_latest_completed)
            + float(max(1, int(interval_secs)))
            + cls._schwab_1m_history_replay_grace_seconds(interval_secs=interval_secs)
        )

    async def _maybe_force_schwab_stream_reconnect_for_stale_1m_cluster(
        self,
        *,
        runtime: StrategyBotRuntime,
        now: datetime,
        expected_latest_completed: float,
    ) -> None:
        client = self._schwab_stream_client
        if client is None or not getattr(client, "connected", False):
            return
        force_reconnect = getattr(client, "force_reconnect", None)
        if not callable(force_reconnect):
            return
        last_reconnect_at = self._schwab_last_forced_reconnect_at
        if (
            last_reconnect_at is not None
            and (now - last_reconnect_at).total_seconds()
            < float(self._schwab_cluster_reconnect_cooldown_secs)
        ):
            return

        # A symbol is "lagging" only if it is ACTIVELY TRADING yet its completed
        # CHART bars are not keeping up. A quiet symbol produces no bar simply
        # because it had no trades, so counting it as lagging turned this recovery
        # path into a self-inflicted ~57/hr reconnect storm on thin penny-stock
        # universes (the 2026-05-27 residual flap; same false-positive class as the
        # PR #194 bar-flow detector and the PR #228 chart deadline).
        # `_schwab_last_bar_driver_activity_at` advances only on genuinely fresh
        # trade/bar events (not stale-duplicate or quote-only packets — see its
        # docstring), and the caller already uses this exact 120s activity window.
        activity_cutoff = now - timedelta(
            seconds=float(self._schwab_cluster_reconnect_activity_window_secs)
        )
        lag_deadline = expected_latest_completed - float(
            self._schwab_cluster_reconnect_slack_secs
        )
        chronic_lag_cutoff = expected_latest_completed - float(
            self._schwab_cluster_reconnect_chronic_lag_threshold_secs
        )
        lagging_symbols: list[tuple[str, float | None]] = []
        for symbol in sorted(runtime.active_symbols()):
            normalized = str(symbol).upper()
            last_activity = self._schwab_last_bar_driver_activity_at(normalized)
            if last_activity is None or last_activity < activity_cutoff:
                # Not actively trading -> missing bars is expected, not a stall.
                continue
            latest_runtime_completed = self._latest_runtime_completed_bar_timestamp(runtime, normalized)
            if (
                latest_runtime_completed is None
                or latest_runtime_completed < lag_deadline
            ):
                if (
                    latest_runtime_completed is not None
                    and latest_runtime_completed < chronic_lag_cutoff
                ):
                    # Chronic Schwab CHART slowness — reconnect cannot catch this up.
                    continue
                lagging_symbols.append((normalized, latest_runtime_completed))

        if len(lagging_symbols) < int(self._schwab_cluster_reconnect_threshold):
            return

        self._schwab_last_forced_reconnect_at = now
        try:
            await force_reconnect()
        except Exception:
            self.logger.exception("failed forcing Schwab streamer reconnect for stale 1m cluster")
            return

        lagging_summary = ",".join(
            f"{symbol}:{_datetime_str(datetime.fromtimestamp(ts, UTC)) if ts is not None else 'none'}"
            for symbol, ts in lagging_symbols[:12]
        )
        self.logger.warning(
            "forced full Schwab streamer reconnect for stale schwab_1m cluster "
            "(lagging_symbols=%s sample=%s expected_completed=%s)",
            len(lagging_symbols),
            lagging_summary,
            _datetime_str(datetime.fromtimestamp(expected_latest_completed, UTC)),
        )

    async def _immediate_schwab_1m_history_refresh(self, *, symbols: Sequence[str]) -> int:
        """Force-refresh schwab_1m history for specific symbols bypassing the
        per-symbol throttle. Called when the bar-flow stall detector trips,
        so recovery is immediate instead of waiting up to 15s/symbol.

        Returns count of fresh bars replayed across all symbols.
        """
        runtime = self.state.bots.get("schwab_1m")
        if not isinstance(runtime, StrategyBotRuntime):
            return 0
        if not runtime.use_live_aggregate_bars or not runtime.live_aggregate_bars_are_final:
            return 0
        if not symbols:
            return 0

        now = utcnow()
        expected_latest_completed = self._latest_expected_completed_bar_timestamp(
            now=now,
            interval_secs=60,
        )
        if expected_latest_completed is None:
            return 0
        session_start_timestamp = current_scanner_session_start_utc(now).timestamp()
        required_bars = max(2, runtime.required_history_bars())
        total_fresh_bars = 0
        replay_deadline_ts = self._schwab_1m_history_replay_deadline_timestamp(
            expected_latest_completed=expected_latest_completed,
            interval_secs=60,
        )

        for normalized in sorted({str(symbol).upper() for symbol in symbols}):
            latest_runtime_completed = self._latest_runtime_completed_bar_timestamp(
                runtime, normalized
            )
            if (
                latest_runtime_completed is not None
                and latest_runtime_completed >= expected_latest_completed
            ):
                continue
            if now.timestamp() < replay_deadline_ts:
                continue
            if self._schwab_1m_live_bar_already_received(
                normalized,
                minimum_timestamp=expected_latest_completed,
            ):
                continue

            self._schwab_1m_last_history_refresh_at[normalized] = now
            bars = await self._load_schwab_history_bars(
                symbol=normalized,
                interval_secs=60,
                required_bars=required_bars,
            )
            if not bars:
                continue

            fresh_completed_bars = [
                bar
                for bar in bars
                if (
                    (timestamp := _coerce_float(bar.get("timestamp"))) is not None
                    and timestamp >= session_start_timestamp
                    and timestamp <= expected_latest_completed
                    and (
                        latest_runtime_completed is None
                        or timestamp > latest_runtime_completed
                    )
                )
            ]
            if not fresh_completed_bars:
                continue

            for bar in fresh_completed_bars:
                intents = self.state.handle_live_bar(
                    symbol=normalized,
                    interval_secs=60,
                    open_price=float(bar["open"]),
                    high_price=float(bar["high"]),
                    low_price=float(bar["low"]),
                    close_price=float(bar["close"]),
                    volume=int(bar["volume"]),
                    timestamp=float(bar["timestamp"]),
                    trade_count=int(bar.get("trade_count", 1) or 1),
                    live_bar_source="history",
                    strategy_codes=("schwab_1m",),
                )
                await self._flush_pending_persists()
                for intent in intents:
                    await self._publish_intent(intent)
            self._remember_schwab_1m_history_replay_authority(
                normalized,
                latest_bar_timestamp=float(fresh_completed_bars[-1]["timestamp"]),
            )
            total_fresh_bars += len(fresh_completed_bars)
            latest_bar_at = datetime.fromtimestamp(
                float(fresh_completed_bars[-1]["timestamp"]),
                UTC,
            ).astimezone(EASTERN_TZ)
            self.logger.info(
                "[ON-STALL-RECOVERY] replayed %s fresh Schwab 1m history bars for %s "
                "through %s (triggered by bar-flow-stalled detector)",
                len(fresh_completed_bars),
                normalized,
                latest_bar_at.isoformat(),
            )
        return total_fresh_bars

    async def _refresh_stale_schwab_1m_history(self) -> tuple[int, int]:
        if self._main_loop_fault_injection_remaining > 0:
            # SPOF Workstream A controlled survival test (default OFF). Simulates
            # the proven 2026-06-03 dead-token escape so an operator can verify the
            # loop survives + escalates in a safe window. Self-clears after N.
            self._main_loop_fault_injection_remaining -= 1
            raise RuntimeError(
                "[FAULT-INJECTION] simulated Schwab history-refresh failure "
                "(MAI_TAI_STRATEGY_MAIN_LOOP_FAULT_INJECTION_COUNT) — "
                "SPOF Workstream A controlled survival test"
            )
        runtime = self.state.bots.get("schwab_1m")
        if not isinstance(runtime, StrategyBotRuntime):
            return 0, 0
        if not runtime.use_live_aggregate_bars or not runtime.live_aggregate_bars_are_final:
            return 0, 0

        now = utcnow()
        expected_latest_completed = self._latest_expected_completed_bar_timestamp(
            now=now,
            interval_secs=60,
        )
        if expected_latest_completed is None:
            return 0, 0
        session_start_timestamp = current_scanner_session_start_utc(now).timestamp()

        intent_count = 0
        refreshed_bar_count = 0
        refresh_interval = max(1, int(self._schwab_1m_history_refresh_interval_secs))
        required_bars = max(2, runtime.required_history_bars())
        recent_activity_after = now - timedelta(seconds=120)
        replay_deadline_ts = self._schwab_1m_history_replay_deadline_timestamp(
            expected_latest_completed=expected_latest_completed,
            interval_secs=60,
        )

        await self._maybe_force_schwab_stream_reconnect_for_stale_1m_cluster(
            runtime=runtime,
            now=now,
            expected_latest_completed=expected_latest_completed,
        )

        for symbol in sorted(runtime.active_symbols()):
            normalized = str(symbol).upper()
            last_refresh_at = self._schwab_1m_last_history_refresh_at.get(normalized)
            if (
                last_refresh_at is not None
                and (now - last_refresh_at).total_seconds() < refresh_interval
            ):
                continue
            last_bar_driver_activity = self._schwab_last_bar_driver_activity_at(normalized)
            if (
                last_bar_driver_activity is None
                or last_bar_driver_activity < recent_activity_after
            ):
                continue

            latest_runtime_completed = self._latest_runtime_completed_bar_timestamp(runtime, normalized)
            if (
                latest_runtime_completed is not None
                and latest_runtime_completed >= expected_latest_completed
            ):
                continue
            if now.timestamp() < replay_deadline_ts:
                continue
            if self._schwab_1m_live_bar_already_received(
                normalized,
                minimum_timestamp=expected_latest_completed,
            ):
                continue

            self._schwab_1m_last_history_refresh_at[normalized] = now
            bars = await self._load_schwab_history_bars(
                symbol=normalized,
                interval_secs=60,
                required_bars=required_bars,
            )
            if not bars:
                continue

            fresh_completed_bars = [
                bar
                for bar in bars
                if (
                    (timestamp := _coerce_float(bar.get("timestamp"))) is not None
                    and timestamp >= session_start_timestamp
                    and timestamp <= expected_latest_completed
                    and (
                        latest_runtime_completed is None
                        or timestamp > latest_runtime_completed
                    )
                )
            ]
            if not fresh_completed_bars:
                continue

            for bar in fresh_completed_bars:
                intents = self.state.handle_live_bar(
                    symbol=normalized,
                    interval_secs=60,
                    open_price=float(bar["open"]),
                    high_price=float(bar["high"]),
                    low_price=float(bar["low"]),
                    close_price=float(bar["close"]),
                    volume=int(bar["volume"]),
                    timestamp=float(bar["timestamp"]),
                    trade_count=int(bar.get("trade_count", 1) or 1),
                    live_bar_source="history",
                    strategy_codes=("schwab_1m",),
                )
                await self._flush_pending_persists()
                for intent in intents:
                    await self._publish_intent(intent)
                intent_count += len(intents)
            self._remember_schwab_1m_history_replay_authority(
                normalized,
                latest_bar_timestamp=float(fresh_completed_bars[-1]["timestamp"]),
            )
            refreshed_bar_count += len(fresh_completed_bars)
            latest_bar_at = datetime.fromtimestamp(
                float(fresh_completed_bars[-1]["timestamp"]),
                UTC,
            ).astimezone(EASTERN_TZ)
            self.logger.info(
                "replayed %s fresh Schwab 1m history bars for %s through %s",
                len(fresh_completed_bars),
                normalized,
                latest_bar_at.isoformat(),
            )

        return intent_count, refreshed_bar_count

    async def _load_schwab_history_bars(
        self,
        *,
        symbol: str,
        interval_secs: int,
        required_bars: int,
    ) -> list[dict[str, float | int]]:
        end_at = utcnow()
        session_start = current_scanner_session_start_utc(end_at)
        interval_minutes = max(1, interval_secs // 60)
        limit = max(required_bars * 4, required_bars)
        bars = await self._schwab_quote_poll_adapter.fetch_historical_bars(
            symbol,
            interval_minutes=interval_minutes,
            start_at=session_start,
            end_at=end_at,
            need_extended_hours_data=True,
        )
        bars = self._drop_placeholder_bars(bars)
        if len(bars) >= required_bars:
            return bars[-limit:]

        if int(interval_secs) == 60:
            lookback_days = max(3, min(10, (required_bars // 60) + 2))
            broader_start = session_start - timedelta(days=lookback_days)
            broader_bars = await self._schwab_quote_poll_adapter.fetch_historical_bars(
                symbol,
                interval_minutes=interval_minutes,
                start_at=broader_start,
                end_at=end_at,
                need_extended_hours_data=True,
            )
            broader_bars = self._drop_placeholder_bars(broader_bars)
            if len(broader_bars) > len(bars):
                bars = broader_bars

        if len(bars) < required_bars:
            persisted_bars = self._load_persisted_schwab_1m_history_bars(
                symbol=symbol,
                limit=limit,
            )
            if persisted_bars:
                bars = self._merge_historical_bar_payloads(persisted_bars, bars)

        if bars:
            if len(bars) >= required_bars:
                return bars[-limit:]

        if self._schwab_tick_archive is None:
            if bars and len(bars) < required_bars:
                self.logger.warning(
                    "short Schwab 1m history for %s: %s bars available, need %s (no archive fallback)",
                    symbol,
                    len(bars),
                    required_bars,
                )
            return bars[-limit:] if bars else []

        if len(bars) < required_bars:
            archived_bars = self._load_recent_archived_schwab_history_bars(
                symbol=symbol,
                interval_secs=interval_secs,
                required_bars=required_bars,
                end_at=end_at,
            )
            if archived_bars:
                bars = self._merge_historical_bar_payloads(archived_bars, bars)
                if len(bars) >= required_bars:
                    return bars[-limit:]

        if int(interval_secs) == 60:
            archive_live_bars = load_recorded_live_bars(
                self.settings.schwab_tick_archive_root,
                symbol=symbol,
                day=end_at.astimezone(EASTERN_TZ).strftime("%Y-%m-%d"),
                interval_secs=interval_secs,
                start_at=session_start.timestamp(),
                end_at=end_at.timestamp(),
            )
            if archive_live_bars:
                return [bar.__dict__ for bar in archive_live_bars[-limit:]]

        archive_bars = load_aggregated_trade_bars(
            self.settings.schwab_tick_archive_root,
            symbol=symbol,
            day=end_at.astimezone(EASTERN_TZ).strftime("%Y-%m-%d"),
            interval_secs=interval_secs,
            start_at_ns=int(session_start.timestamp() * 1_000_000_000),
            end_at_ns=int(end_at.timestamp() * 1_000_000_000),
        )
        merged = self._merge_historical_bar_payloads(
            [bar.__dict__ for bar in archive_bars[-limit:]],
            bars,
        )
        if len(merged) < required_bars:
            self.logger.warning(
                "short Schwab 1m history for %s after all fallbacks: %s bars available, need %s",
                symbol,
                len(merged),
                required_bars,
            )
        return merged[-limit:]

    @staticmethod
    def _drop_placeholder_bars(
        bars: Sequence[dict[str, float | int]],
    ) -> list[dict[str, float | int]]:
        # Schwab's pricehistory API returns a minute bar for every minute in the
        # requested range, including minutes with no trades. Those bars carry
        # volume=0, trade_count=0, and a flat OHLC equal to the symbol's prior
        # close. Hydrating them as-is pollutes persisted history and can drive
        # zero-volume bars into handle_live_bar during refresh.
        return [
            bar
            for bar in bars
            if int(bar.get("volume", 0) or 0) > 0
            or int(bar.get("trade_count", 0) or 0) > 0
        ]

    @staticmethod
    def _merge_historical_bar_payloads(
        *sources: Sequence[dict[str, float | int]],
    ) -> list[dict[str, float | int]]:
        merged: dict[float, dict[str, float | int]] = {}
        for source in sources:
            for bar in source:
                timestamp = _coerce_float(bar.get("timestamp"))
                if timestamp is None or timestamp <= 0:
                    continue
                merged[timestamp] = {
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": int(bar["volume"]),
                    "timestamp": float(timestamp),
                    "trade_count": int(bar.get("trade_count", 1) or 1),
                }
        return [merged[key] for key in sorted(merged)]

    def _load_persisted_schwab_1m_history_bars(
        self,
        *,
        symbol: str,
        limit: int,
    ) -> list[dict[str, float | int]]:
        if self.session_factory is None:
            return []
        normalized_symbol = str(symbol).upper()
        try:
            with self.session_factory() as session:
                records = list(
                    session.scalars(
                        select(StrategyBarHistory)
                        .where(
                            StrategyBarHistory.strategy_code == "schwab_1m",
                            StrategyBarHistory.symbol == normalized_symbol,
                            StrategyBarHistory.interval_secs == 60,
                        )
                        .order_by(StrategyBarHistory.bar_time.desc())
                        .limit(max(1, int(limit)))
                    ).all()
                )
        except Exception:
            self.logger.exception("failed loading persisted Schwab 1m history for %s", normalized_symbol)
            return []

        records.reverse()
        return [
            {
                "open": float(record.open_price),
                "high": float(record.high_price),
                "low": float(record.low_price),
                "close": float(record.close_price),
                "volume": int(record.volume),
                "timestamp": record.bar_time.replace(tzinfo=UTC).timestamp()
                if record.bar_time.tzinfo is None
                else record.bar_time.astimezone(UTC).timestamp(),
                "trade_count": int(record.trade_count or 0),
            }
            for record in records
        ]

    def _load_recent_archived_schwab_history_bars(
        self,
        *,
        symbol: str,
        interval_secs: int,
        required_bars: int,
        end_at: datetime,
    ) -> list[dict[str, float | int]]:
        root_path = self.settings.schwab_tick_archive_root
        limit = max(required_bars * 4, required_bars)
        lookback_days = max(3, min(10, (required_bars // 60) + 2))
        merged: list[dict[str, float | int]] = []
        normalized_symbol = str(symbol).upper()
        for offset in range(lookback_days, -1, -1):
            day_dt = end_at.astimezone(EASTERN_TZ) - timedelta(days=offset)
            day = day_dt.strftime("%Y-%m-%d")
            if int(interval_secs) == 60:
                live_bars = load_recorded_live_bars(
                    root_path,
                    symbol=normalized_symbol,
                    day=day,
                    interval_secs=interval_secs,
                )
                if live_bars:
                    merged.extend([bar.__dict__ for bar in live_bars])
                    continue
            archive_bars = load_aggregated_trade_bars(
                root_path,
                symbol=normalized_symbol,
                day=day,
                interval_secs=interval_secs,
            )
            if archive_bars:
                merged.extend([bar.__dict__ for bar in archive_bars])
        if not merged:
            return []
        return self._merge_historical_bar_payloads(merged)[-limit:]

    def _build_schwab_stream_client(self) -> SchwabStreamerClient | None:
        if not self.state.schwab_stream_strategy_codes():
            return None
        return SchwabStreamerClient(self.settings)

    def _build_schwab_tick_archive(self) -> SchwabTickArchive | None:
        if not self.settings.schwab_tick_archive_enabled:
            return None
        return SchwabTickArchive(self.settings.schwab_tick_archive_root)

    def _load_schwab_trade_extended_vwap_series(
        self,
        symbol: str,
        bar_timestamps: Sequence[float],
        interval_secs: int,
    ) -> dict[float, float]:
        if self._schwab_tick_archive is None or not bar_timestamps:
            return {}

        normalized_symbol = str(symbol).upper().strip()
        if not normalized_symbol:
            return {}

        clean_timestamps: list[float] = []
        for value in bar_timestamps:
            try:
                timestamp = float(value)
            except (TypeError, ValueError):
                continue
            if timestamp > 0:
                clean_timestamps.append(timestamp)
        if not clean_timestamps:
            return {}

        latest_timestamp = max(clean_timestamps)
        session_day = datetime.fromtimestamp(latest_timestamp, UTC).astimezone(EASTERN_TZ).strftime("%Y-%m-%d")
        session_date = datetime.strptime(session_day, "%Y-%m-%d").replace(tzinfo=EASTERN_TZ)
        session_start_ns = int(
            session_date.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(UTC).timestamp()
            * 1_000_000_000
        )
        session_end_ns = int(
            session_date.replace(hour=20, minute=0, second=0, microsecond=0).astimezone(UTC).timestamp()
            * 1_000_000_000
        )
        fetch_end_ns = min(
            session_end_ns,
            int((latest_timestamp + max(1, int(interval_secs))) * 1_000_000_000),
        )
        if fetch_end_ns <= session_start_ns:
            return {}

        trades = load_recorded_trades(
            self.settings.schwab_tick_archive_root,
            symbol=normalized_symbol,
            day=session_day,
            start_at_ns=session_start_ns,
            end_at_ns=fetch_end_ns,
        )
        if not trades:
            return {}

        ordered_targets = sorted(set(clean_timestamps))
        ordered_trades = sorted(trades, key=lambda record: int(record.timestamp_ns or 0))
        cumulative_price_volume = 0.0
        cumulative_volume = 0.0
        trade_index = 0
        interval_ns = max(1, int(interval_secs)) * 1_000_000_000
        result: dict[float, float] = {}

        for bar_timestamp in ordered_targets:
            bar_end_ns = int(bar_timestamp * 1_000_000_000) + interval_ns
            while trade_index < len(ordered_trades):
                trade = ordered_trades[trade_index]
                event_ns = int(trade.timestamp_ns or 0)
                if event_ns >= bar_end_ns:
                    break
                cumulative_price_volume += float(trade.price) * int(trade.size)
                cumulative_volume += int(trade.size)
                trade_index += 1
            if cumulative_volume > 0:
                result[bar_timestamp] = cumulative_price_volume / cumulative_volume

        return result

    def _enqueue_schwab_trade_tick(self, record: TradeTickRecord) -> None:
        self._schwab_trade_queue.put_nowait((record, time_ns()))

    def _enqueue_schwab_quote_tick(self, record: QuoteTickRecord) -> None:
        if not self._should_keep_schwab_quote_tick(record.symbol):
            return
        self._schwab_quote_queue.put_nowait((record, time_ns()))

    def _enqueue_schwab_live_bar(self, record: LiveBarRecord) -> None:
        if not self._should_keep_schwab_live_bar(record.symbol, interval_secs=record.interval_secs):
            return
        received_at_ns = time_ns()
        if int(record.interval_secs) == 60:
            self._remember_schwab_1m_enqueued_live_bar(
                record.symbol,
                live_bar_timestamp=float(record.timestamp),
                received_at_ns=received_at_ns,
            )
        self._schwab_bar_queue.put_nowait((record, received_at_ns))

    def _should_keep_schwab_quote_tick(self, symbol: str) -> bool:
        normalized = str(symbol).upper()
        for code in self.state.schwab_stream_strategy_codes():
            runtime = self.state.bots.get(code)
            active_symbols = getattr(runtime, "active_symbols", None)
            if callable(active_symbols) and normalized in active_symbols():
                return True
        return False

    def _should_keep_schwab_live_bar(self, symbol: str, *, interval_secs: int) -> bool:
        normalized = str(symbol).upper()
        for code in self._schwab_live_bar_strategy_codes(interval_secs):
            runtime = self.state.bots.get(code)
            stream_symbols = getattr(runtime, "stream_symbols", None)
            if callable(stream_symbols) and normalized in stream_symbols():
                return True
        return False

    def _schwab_live_bar_strategy_codes(self, interval_secs: int) -> tuple[str, ...]:
        if int(interval_secs) != 60:
            return ()
        runtime = self.state.bots.get("schwab_1m")
        if not isinstance(runtime, StrategyBotRuntime):
            return ()
        if not runtime.use_live_aggregate_bars:
            return ()
        return ("schwab_1m",)

    async def _drain_schwab_stream_queues(self) -> tuple[int, int]:
        intent_count = 0
        event_count = 0
        max_events = max(1, int(self._schwab_stream_drain_max_events))

        async def _drain_one_quote() -> bool:
            nonlocal event_count
            if self._schwab_quote_queue.empty():
                return False
            quote, received_at_ns = await self._schwab_quote_queue.get()
            event_count += 1
            self._record_schwab_stream_activity(quote.symbol, activity_kind="quote")
            if self._schwab_tick_archive is not None:
                self._schwab_tick_archive.record_quote(
                    quote,
                    received_at_ns=received_at_ns,
                    recorded_at_ns=time_ns(),
                )
            intents = self.state.handle_quote_tick(
                symbol=quote.symbol,
                bid_price=quote.bid_price,
                ask_price=quote.ask_price,
                strategy_codes=self.state.schwab_stream_strategy_codes(),
            )
            for intent in intents:
                await self._publish_intent(intent)
            if intents:
                await self._sync_subscription_targets()
                await self._publish_strategy_state_snapshot()
            return True

        async def _drain_one_bar() -> bool:
            nonlocal intent_count, event_count
            if self._schwab_bar_queue.empty():
                return False
            bar, received_at_ns = await self._schwab_bar_queue.get()
            event_count += 1
            self._record_schwab_stream_activity(
                bar.symbol,
                activity_kind="bar",
                event_timestamp=float(bar.timestamp),
            )
            recorded_at_ns = time_ns()
            if self._schwab_tick_archive is not None:
                self._schwab_tick_archive.record_live_bar(
                    bar,
                    received_at_ns=received_at_ns,
                    recorded_at_ns=recorded_at_ns,
                )
            if int(bar.interval_secs) == 60:
                replay_authority = self._schwab_1m_history_replay_authority_until_timestamp.get(
                    str(bar.symbol).upper()
                )
                if replay_authority is not None and float(bar.timestamp) <= replay_authority:
                    self.logger.info(
                        "ignoring delayed Schwab live bar for %s at %s because fresh history already replayed through %s",
                        str(bar.symbol).upper(),
                        _datetime_str(datetime.fromtimestamp(float(bar.timestamp), UTC)),
                        _datetime_str(datetime.fromtimestamp(float(replay_authority), UTC)),
                    )
                    return True
                self._release_schwab_1m_history_replay_authority(
                    bar.symbol,
                    live_bar_timestamp=float(bar.timestamp),
                )
            strategy_codes = self._schwab_live_bar_strategy_codes(bar.interval_secs)
            intents = self.state.handle_live_bar(
                symbol=bar.symbol,
                interval_secs=bar.interval_secs,
                open_price=bar.open,
                high_price=bar.high,
                low_price=bar.low,
                close_price=bar.close,
                volume=bar.volume,
                timestamp=bar.timestamp,
                trade_count=bar.trade_count,
                coverage_started_at=(
                    float(bar.coverage_started_at)
                    if bar.coverage_started_at is not None
                    else None
                ),
                live_bar_received_at=datetime.fromtimestamp(received_at_ns / 1_000_000_000, UTC),
                live_bar_recorded_at=datetime.fromtimestamp(recorded_at_ns / 1_000_000_000, UTC),
                live_bar_source="live",
                strategy_codes=strategy_codes,
            )
            await self._flush_pending_persists()
            for intent in intents:
                await self._publish_intent(intent)
            intent_count += len(intents)
            if intents:
                self.logger.info(
                    "generated %s intents from %s Schwab live bar",
                    len(intents),
                    bar.symbol,
                )
            elif strategy_codes:
                await self._publish_strategy_state_snapshot_for_generic_bot_activity()
            return True

        async def _drain_one_trade() -> bool:
            nonlocal intent_count, event_count
            if self._schwab_trade_queue.empty():
                return False
            trade, received_at_ns = await self._schwab_trade_queue.get()
            event_count += 1
            trade_event_timestamp = (
                float(trade.timestamp_ns) / 1_000_000_000
                if int(trade.timestamp_ns or 0) > 0
                else None
            )
            self._record_schwab_stream_activity(
                trade.symbol,
                activity_kind="trade",
                event_timestamp=trade_event_timestamp,
            )
            if self._schwab_tick_archive is not None:
                self._schwab_tick_archive.record_trade(
                    trade,
                    received_at_ns=received_at_ns,
                    recorded_at_ns=time_ns(),
                )
            intents = self.state.handle_trade_tick(
                symbol=trade.symbol,
                price=trade.price,
                size=trade.size,
                timestamp_ns=trade.timestamp_ns,
                cumulative_volume=trade.cumulative_volume,
                strategy_codes=self.state.schwab_stream_strategy_codes(),
            )
            await self._flush_pending_persists()
            for intent in intents:
                await self._publish_intent(intent)
            intent_count += len(intents)
            if intents:
                self.logger.info(
                    "generated %s intents from %s Schwab trade tick",
                    len(intents),
                    trade.symbol,
                )
            return True

        while event_count < max_events:
            progressed = False
            # Final completed 1m bars are the canonical source for schwab_1m.
            # Service them ahead of quote churn so the runtime does not keep
            # replaying REST history while fresh CHART_EQUITY bars sit queued.
            progressed = await _drain_one_bar() or progressed
            if event_count >= max_events:
                break
            # Timesale ticks remain next-highest priority because they build the
            # tick-native Schwab runtimes. Quotes still make progress each pass,
            # but they no longer starve completed bars or trades.
            progressed = await _drain_one_trade() or progressed
            if event_count >= max_events:
                break
            progressed = await _drain_one_quote() or progressed
            if not progressed:
                break

        return intent_count, event_count

    def _record_schwab_stream_activity(
        self,
        symbol: str,
        *,
        activity_kind: str,
        event_timestamp: float | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        normalized = str(symbol).upper()
        observed_at = observed_at or utcnow()
        if activity_kind == "trade":
            self._schwab_symbol_last_stream_trade_at[normalized] = observed_at
            if event_timestamp is None:
                self._schwab_symbol_last_advancing_trade_at[normalized] = observed_at
            else:
                prior_event_ts = self._schwab_symbol_last_trade_event_ts.get(normalized)
                if prior_event_ts is None or float(event_timestamp) > prior_event_ts:
                    self._schwab_symbol_last_trade_event_ts[normalized] = float(event_timestamp)
                    self._schwab_symbol_last_advancing_trade_at[normalized] = observed_at
        elif activity_kind == "bar":
            self._schwab_symbol_last_stream_bar_at[normalized] = observed_at
            if event_timestamp is None:
                self._schwab_symbol_last_advancing_bar_at[normalized] = observed_at
            else:
                prior_event_ts = self._schwab_symbol_last_bar_event_ts.get(normalized)
                if prior_event_ts is None or float(event_timestamp) > prior_event_ts:
                    self._schwab_symbol_last_bar_event_ts[normalized] = float(event_timestamp)
                    self._schwab_symbol_last_advancing_bar_at[normalized] = observed_at
        else:
            self._schwab_symbol_last_stream_quote_at[normalized] = observed_at
        if normalized in self._schwab_stale_symbols or normalized in self._schwab_warning_symbols:
            self._schwab_stale_symbols.discard(normalized)
            self._schwab_warning_symbols.discard(normalized)
            self.logger.info(
                "Schwab stream recovered for %s via live %s update",
                normalized,
                activity_kind,
            )
        self._clear_schwab_runtime_data_halt(normalized)
        self._clear_schwab_runtime_data_warning(normalized)

    def _schwab_active_strategy_codes_by_symbol(self) -> dict[str, tuple[str, ...]]:
        symbol_codes: dict[str, set[str]] = {}
        for code in self.state.schwab_stream_strategy_codes():
            runtime = self.state.bots.get(code)
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            for symbol in runtime.active_symbols():
                normalized = str(symbol).upper()
                if normalized:
                    symbol_codes.setdefault(normalized, set()).add(code)
        return {
            symbol: tuple(sorted(codes))
            for symbol, codes in symbol_codes.items()
        }

    def _schwab_open_position_strategy_codes_by_symbol(self) -> dict[str, tuple[str, ...]]:
        symbol_codes: dict[str, set[str]] = {}
        for code in self.state.schwab_stream_strategy_codes():
            runtime = self.state.bots.get(code)
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            for item in runtime.positions.get_all_positions():
                symbol = str(item.get("ticker", "")).upper()
                quantity = float(item.get("quantity", 0) or 0)
                if not symbol or quantity <= 0:
                    continue
                symbol_codes.setdefault(symbol, set()).add(code)
        return {
            symbol: tuple(sorted(codes))
            for symbol, codes in symbol_codes.items()
        }

    def _apply_schwab_runtime_data_halt(
        self,
        symbol: str,
        codes: Sequence[str],
        *,
        reason: str,
        observed_at: datetime,
    ) -> None:
        for code in codes:
            runtime = self.state.bots.get(code)
            if isinstance(runtime, StrategyBotRuntime):
                runtime.apply_data_halt(symbol, reason=reason, observed_at=observed_at)

    def _apply_schwab_runtime_data_warning(
        self,
        symbol: str,
        codes: Sequence[str],
        *,
        reason: str,
        observed_at: datetime,
    ) -> None:
        for code in codes:
            runtime = self.state.bots.get(code)
            if isinstance(runtime, StrategyBotRuntime):
                runtime.apply_data_warning(symbol, reason=reason, observed_at=observed_at)

    def _clear_schwab_runtime_data_halt(self, symbol: str) -> None:
        normalized = str(symbol).upper()
        for code in self.state.schwab_stream_strategy_codes():
            runtime = self.state.bots.get(code)
            if isinstance(runtime, StrategyBotRuntime):
                runtime.clear_data_halt(normalized)

    def _clear_schwab_runtime_data_warning(self, symbol: str) -> None:
        normalized = str(symbol).upper()
        for code in self.state.schwab_stream_strategy_codes():
            runtime = self.state.bots.get(code)
            if isinstance(runtime, StrategyBotRuntime):
                runtime.clear_data_warning(normalized)

    def _clear_all_schwab_runtime_data_halts(self) -> None:
        for code in self.state.schwab_stream_strategy_codes():
            runtime = self.state.bots.get(code)
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            for symbol in list(runtime.data_halt_symbols):
                runtime.clear_data_halt(symbol)
            for symbol in list(runtime.data_warning_symbols):
                runtime.clear_data_warning(symbol)

    def _clear_inactive_schwab_runtime_data_halts(self, active_symbols: set[str]) -> None:
        normalized_active = {str(symbol).upper() for symbol in active_symbols if str(symbol).strip()}
        for code in self.state.schwab_stream_strategy_codes():
            runtime = self.state.bots.get(code)
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            for symbol in list(runtime.data_halt_symbols):
                if symbol not in normalized_active:
                    runtime.clear_data_halt(symbol)
            for symbol in list(runtime.data_warning_symbols):
                if symbol not in normalized_active:
                    runtime.clear_data_warning(symbol)
        self._schwab_symbol_last_stream_trade_at = {
            symbol: observed_at
            for symbol, observed_at in self._schwab_symbol_last_stream_trade_at.items()
            if symbol in normalized_active
        }
        self._schwab_symbol_last_stream_bar_at = {
            symbol: observed_at
            for symbol, observed_at in self._schwab_symbol_last_stream_bar_at.items()
            if symbol in normalized_active
        }
        self._schwab_symbol_last_advancing_trade_at = {
            symbol: observed_at
            for symbol, observed_at in self._schwab_symbol_last_advancing_trade_at.items()
            if symbol in normalized_active
        }
        self._schwab_symbol_last_advancing_bar_at = {
            symbol: observed_at
            for symbol, observed_at in self._schwab_symbol_last_advancing_bar_at.items()
            if symbol in normalized_active
        }
        self._schwab_symbol_last_stream_quote_at = {
            symbol: observed_at
            for symbol, observed_at in self._schwab_symbol_last_stream_quote_at.items()
            if symbol in normalized_active
        }
        self._schwab_symbol_last_trade_event_ts = {
            symbol: event_ts
            for symbol, event_ts in self._schwab_symbol_last_trade_event_ts.items()
            if symbol in normalized_active
        }
        self._schwab_symbol_last_bar_event_ts = {
            symbol: event_ts
            for symbol, event_ts in self._schwab_symbol_last_bar_event_ts.items()
            if symbol in normalized_active
        }
        self._schwab_symbol_last_resubscribe_at = {
            symbol: observed_at
            for symbol, observed_at in self._schwab_symbol_last_resubscribe_at.items()
            if symbol in normalized_active
        }
        self._schwab_symbol_last_quote_poll_at = {
            symbol: observed_at
            for symbol, observed_at in self._schwab_symbol_last_quote_poll_at.items()
            if symbol in normalized_active
        }
        self._schwab_1m_history_replay_authority_until_timestamp = {
            symbol: replay_until
            for symbol, replay_until in self._schwab_1m_history_replay_authority_until_timestamp.items()
            if symbol in normalized_active
        }
        self._schwab_stale_symbols.intersection_update(normalized_active)
        self._schwab_warning_symbols.intersection_update(normalized_active)

    def _schwab_last_stream_update_at(self, symbol: str) -> datetime | None:
        normalized = str(symbol).upper()
        candidates = [
            self._schwab_symbol_last_stream_trade_at.get(normalized),
            self._schwab_symbol_last_stream_quote_at.get(normalized),
        ]
        present = [candidate for candidate in candidates if candidate is not None]
        if not present:
            return None
        return max(present)

    def _schwab_last_bar_driver_activity_at(self, symbol: str) -> datetime | None:
        """Return the most recent Schwab activity that can advance completed bars.

        Fresh quotes alone do not imply `schwab_1m` should produce a new
        completed bar. After-hours names can keep publishing quote updates long
        after their last trade, and treating those quote-only symbols as
        "missing bars" creates false history replays and reconnect storms.
        """
        normalized = str(symbol).upper()
        candidates = [
            self._schwab_symbol_last_advancing_trade_at.get(normalized)
            or self._schwab_symbol_last_stream_trade_at.get(normalized),
            self._schwab_symbol_last_advancing_bar_at.get(normalized)
            or self._schwab_symbol_last_stream_bar_at.get(normalized),
        ]
        present = [candidate for candidate in candidates if candidate is not None]
        if not present:
            return None
        return max(present)

    def _is_schwab_symbol_stale(self, symbol: str, now: datetime) -> bool:
        last_update = self._schwab_last_stream_update_at(symbol)
        if last_update is None:
            return True
        return (
            now - last_update
        ).total_seconds() >= float(self.settings.schwab_stream_symbol_stale_after_seconds)

    def _is_schwab_stream_disconnected(self) -> bool:
        client = self._schwab_stream_client
        return client is not None and not getattr(client, "connected", False)

    def _schwab_stream_disconnect_has_exceeded_grace(
        self,
        now: datetime,
        *,
        has_open_position: bool = False,
    ) -> bool:
        if not self._is_schwab_stream_disconnected():
            self._schwab_stream_disconnected_since = None
            return False
        if self._schwab_stream_disconnected_since is None:
            self._schwab_stream_disconnected_since = now
            return False
        stale_after = self._schwab_data_halt_stale_after_seconds(
            has_open_position=has_open_position
        )
        return (now - self._schwab_stream_disconnected_since).total_seconds() >= stale_after

    def _schwab_data_halt_stale_after_seconds(self, *, has_open_position: bool) -> float:
        base_stale_after = max(30.0, float(self.settings.schwab_stream_symbol_stale_after_seconds))
        if has_open_position:
            return base_stale_after
        return max(
            base_stale_after,
            float(self.settings.schwab_stream_symbol_stale_after_seconds_without_position),
        )

    def _schwab_rest_quote_age_seconds(
        self, quote: dict[str, object], now: datetime
    ) -> float | None:
        candidates: list[float] = []
        for key in ("trade_time_ms", "quote_time_ms"):
            raw = quote.get(key)
            if not isinstance(raw, (int, float)) or raw <= 0:
                continue
            ts_seconds = float(raw) / 1000.0
            age = now.timestamp() - ts_seconds
            if age >= -5.0:
                candidates.append(age)
        if not candidates:
            return None
        return min(candidates)

    def _schwab_symbol_resubscribe_interval_seconds(self, *, has_open_position: bool) -> float:
        base_interval = max(
            1.0,
            float(self.settings.schwab_stream_symbol_resubscribe_interval_seconds),
        )
        if has_open_position:
            return base_interval
        return max(
            base_interval,
            min(
                60.0,
                self._schwab_data_halt_stale_after_seconds(has_open_position=False) / 2.0,
            ),
        )

    def _schwab_symbol_no_first_tick_grace_seconds(
        self,
        *,
        strategy_codes: Iterable[str],
        has_open_position: bool,
    ) -> float:
        base_stale_after = self._schwab_data_halt_stale_after_seconds(
            has_open_position=has_open_position
        )
        if has_open_position:
            return base_stale_after
        max_interval_secs = 30
        for code in strategy_codes:
            runtime = self.state.bots.get(code)
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            try:
                max_interval_secs = max(max_interval_secs, int(runtime.definition.interval_secs or 30))
            except Exception:
                continue
        return max(base_stale_after, float(max_interval_secs) * 5.0)

    def _is_schwab_symbol_data_halt_stale(
        self,
        symbol: str,
        now: datetime,
        *,
        strategy_codes: Iterable[str],
        has_open_position: bool,
    ) -> bool:
        last_update = self._schwab_last_stream_update_at(symbol)
        stale_after = self._schwab_data_halt_stale_after_seconds(
            has_open_position=has_open_position
        )
        if last_update is None:
            if not has_open_position:
                return False
            first_seen = self._schwab_symbol_active_first_seen_at.get(str(symbol).upper(), now)
            no_first_tick_grace = self._schwab_symbol_no_first_tick_grace_seconds(
                strategy_codes=strategy_codes,
                has_open_position=has_open_position,
            )
            return (now - first_seen).total_seconds() >= no_first_tick_grace
        return (now - last_update).total_seconds() >= stale_after

    def _schwab_symbol_should_enforce_data_halt(
        self,
        *,
        strategy_codes: Iterable[str],
        now: datetime,
        has_open_position: bool,
    ) -> bool:
        if has_open_position:
            return True
        current_et = now.astimezone(EASTERN_TZ)
        for code in strategy_codes:
            runtime = self.state.bots.get(code)
            if not isinstance(runtime, StrategyBotRuntime):
                continue
            config = runtime.definition.trading_config
            if config.trading_start_hour <= current_et.hour < config.trading_end_hour:
                return True
        return False

    async def _monitor_schwab_symbol_health(self) -> int:
        active_symbols = self._schwab_active_strategy_codes_by_symbol()
        open_symbols = self._schwab_open_position_strategy_codes_by_symbol()
        if not active_symbols:
            if self._schwab_stale_symbols:
                self._schwab_stale_symbols.clear()
            if self._schwab_warning_symbols:
                self._schwab_warning_symbols.clear()
            self._schwab_symbol_active_first_seen_at.clear()
            self._clear_inactive_schwab_runtime_data_halts(set())
            return 0

        now = utcnow()
        active_set = set(active_symbols)
        for symbol in active_set:
            self._schwab_symbol_active_first_seen_at.setdefault(symbol, now)
        self._schwab_symbol_active_first_seen_at = {
            symbol: first_seen
            for symbol, first_seen in self._schwab_symbol_active_first_seen_at.items()
            if symbol in active_set
        }
        self._clear_inactive_schwab_runtime_data_halts(active_set | set(open_symbols))
        stream_disconnected = self._schwab_stream_disconnect_has_exceeded_grace(
            now,
            has_open_position=bool(open_symbols),
        )
        halt_reason = (
            self._schwab_stream_failure_reason()
            or "Schwab stream stale/disconnected; trading halted until live Schwab ticks recover"
        )
        warning_reason = (
            "Schwab symbol is quiet on a flat positionless name; synthetic 30s bars can continue, "
            "but live Schwab ticks are temporarily sparse."
        )
        open_symbol_set = set(open_symbols)
        auth_failure = bool(self._schwab_stream_failure_reason())
        stale_symbols: dict[str, tuple[str, ...]] = {}
        warning_symbols: dict[str, tuple[str, ...]] = {}
        for symbol, codes in active_symbols.items():
            has_open_position = symbol in open_symbol_set
            if auth_failure:
                stale_symbols[symbol] = codes
                continue
            if not self._schwab_symbol_should_enforce_data_halt(
                strategy_codes=codes,
                now=now,
                has_open_position=has_open_position,
            ):
                continue
            if stream_disconnected or self._is_schwab_symbol_data_halt_stale(
                symbol,
                now,
                strategy_codes=codes,
                has_open_position=has_open_position,
            ):
                if has_open_position or stream_disconnected:
                    stale_symbols[symbol] = codes
                else:
                    warning_symbols[symbol] = codes
        stale_set_before = set(self._schwab_stale_symbols)
        warning_set_before = set(self._schwab_warning_symbols)
        healthy_symbols = set(active_symbols) - set(stale_symbols) - set(warning_symbols)
        for symbol in healthy_symbols:
            if symbol in self._schwab_stale_symbols:
                self._schwab_stale_symbols.discard(symbol)
            self._clear_schwab_runtime_data_halt(symbol)
            self._clear_schwab_runtime_data_warning(symbol)

        for symbol, codes in stale_symbols.items():
            self._clear_schwab_runtime_data_warning(symbol)
            if symbol in self._schwab_stale_symbols:
                self._apply_schwab_runtime_data_halt(
                    symbol,
                    codes,
                    reason=halt_reason,
                    observed_at=now,
                )
                continue
            last_trade_at = self._schwab_symbol_last_stream_trade_at.get(symbol)
            last_quote_at = self._schwab_symbol_last_stream_quote_at.get(symbol)
            self.logger.warning(
                "Schwab stream stale for %s on %s | last_trade_at=%s last_quote_at=%s",
                symbol,
                ",".join(codes),
                last_trade_at.isoformat() if last_trade_at is not None else "never",
                last_quote_at.isoformat() if last_quote_at is not None else "never",
            )
            self._apply_schwab_runtime_data_halt(
                symbol,
                codes,
                reason=halt_reason,
                observed_at=now,
            )
        for symbol, codes in warning_symbols.items():
            self._clear_schwab_runtime_data_halt(symbol)
            if symbol not in warning_set_before:
                last_trade_at = self._schwab_symbol_last_stream_trade_at.get(symbol)
                last_quote_at = self._schwab_symbol_last_stream_quote_at.get(symbol)
                self.logger.warning(
                    "Schwab symbol quiet for %s on %s | last_trade_at=%s last_quote_at=%s",
                    symbol,
                    ",".join(codes),
                    last_trade_at.isoformat() if last_trade_at is not None else "never",
                    last_quote_at.isoformat() if last_quote_at is not None else "never",
                )
            self._apply_schwab_runtime_data_warning(
                symbol,
                codes,
                reason=warning_reason,
                observed_at=now,
            )
        self._schwab_stale_symbols = set(stale_symbols)
        self._schwab_warning_symbols = set(warning_symbols)
        state_changed = (
            stale_set_before != self._schwab_stale_symbols
            or warning_set_before != self._schwab_warning_symbols
        )
        if not stale_symbols and not warning_symbols:
            return 1 if state_changed else 0

        if self._schwab_stream_client is not None:
            should_resubscribe = any(
                (
                    now - self._schwab_symbol_last_resubscribe_at.get(symbol, datetime.min.replace(tzinfo=UTC))
                ).total_seconds()
                >= self._schwab_symbol_resubscribe_interval_seconds(
                    has_open_position=symbol in open_symbol_set
                )
                for symbol in (set(stale_symbols) | set(warning_symbols))
            )
            if should_resubscribe and not auth_failure:
                try:
                    await self._schwab_stream_client.force_resubscribe()
                    for symbol in set(stale_symbols) | set(warning_symbols):
                        self._schwab_symbol_last_resubscribe_at[symbol] = now
                    self.logger.warning(
                        "forced Schwab stream resubscribe for stale Schwab symbols: %s",
                        ",".join(sorted(set(stale_symbols) | set(warning_symbols))),
                    )
                except Exception:
                    self.logger.exception("failed forcing Schwab stream resubscribe")

        stale_open_symbols = {
            symbol: codes
            for symbol, codes in open_symbols.items()
            if symbol in stale_symbols
        }
        if not stale_open_symbols:
            return 1 if state_changed else 0

        poll_interval = max(
            0.5,
            float(self.settings.schwab_stream_symbol_quote_poll_interval_seconds),
        )
        poll_symbols = [
            symbol
            for symbol in sorted(stale_open_symbols)
            if (
                now - self._schwab_symbol_last_quote_poll_at.get(symbol, datetime.min.replace(tzinfo=UTC))
            ).total_seconds()
            >= poll_interval
        ]
        if not poll_symbols:
            return 1 if state_changed else 0

        fetch_quotes = getattr(self._schwab_quote_poll_adapter, "fetch_quotes", None)
        if not callable(fetch_quotes):
            self.logger.error(
                "Schwab quote poll adapter %s does not support fetch_quotes; entries halted but emergency close cannot route",
                type(self._schwab_quote_poll_adapter).__name__,
            )
            return 1 if state_changed else 0

        quotes = await fetch_quotes(poll_symbols)
        intent_count = 0
        rescue_enabled = bool(
            getattr(self.settings, "schwab_emergency_close_rest_rescue_enabled", True)
        )
        rescue_window = self._schwab_data_halt_stale_after_seconds(has_open_position=True)
        for symbol in poll_symbols:
            self._schwab_symbol_last_quote_poll_at[symbol] = now
            quote = quotes.get(symbol)
            if quote is None:
                self.logger.warning(
                    "no Schwab REST quote available for stale open-position symbol %s",
                    symbol,
                )
                continue

            if rescue_enabled:
                rest_age = self._schwab_rest_quote_age_seconds(quote, now)
                if rest_age is not None and rest_age < rescue_window:
                    polled_fresh_at = now - timedelta(seconds=max(0.0, rest_age))
                    existing_quote_at = self._schwab_symbol_last_stream_quote_at.get(symbol)
                    if existing_quote_at is None or polled_fresh_at > existing_quote_at:
                        self._schwab_symbol_last_stream_quote_at[symbol] = polled_fresh_at
                    self._clear_schwab_runtime_data_halt(symbol)
                    self._schwab_stale_symbols.discard(symbol)
                    self.logger.warning(
                        "Schwab stream lagged for %s but REST poll fresh "
                        "(rest_quote_age=%.1fs < %.1fs); deferring emergency close",
                        symbol,
                        rest_age,
                        rescue_window,
                    )
                    continue

            bid_price = quote.get("bid_price")
            ask_price = quote.get("ask_price")
            intents = self.state.handle_quote_tick(
                symbol=symbol,
                bid_price=bid_price,
                ask_price=ask_price,
                strategy_codes=self.state.schwab_stream_strategy_codes(),
            )
            for intent in intents:
                await self._publish_intent(intent)
            if intents:
                await self._sync_subscription_targets()
                await self._publish_strategy_state_snapshot()

            executable_price = bid_price if bid_price is not None and bid_price > 0 else None
            if executable_price is None:
                continue

            for code in stale_open_symbols.get(symbol, ()):
                runtime = self.state.bots.get(code)
                if not isinstance(runtime, StrategyBotRuntime):
                    continue
                intent = runtime.emergency_close_for_data_halt(symbol, executable_price)
                if intent is not None:
                    await self._publish_intent(intent)
                    intent_count += 1

            if intent_count:
                self.logger.warning(
                    "generated %s emergency close intents from polled Schwab quote for %s",
                    intent_count,
                    symbol,
                )

        return intent_count + (1 if state_changed else 0)

    def _persist_scanner_snapshots(self, summary: dict[str, object]) -> None:
        if self.session_factory is None:
            return

        persisted_at = utcnow().isoformat()
        scanner_session_start = current_scanner_session_start_utc(utcnow()).isoformat()
        top_confirmed = list(summary.get("top_confirmed", []))
        watchlist = list(summary.get("watchlist", []))
        all_confirmed_candidates = list(self.state.confirmed_scanner.get_all_confirmed())
        bot_handoff_symbols_by_strategy = {
            code: sorted(symbols)
            for code, symbols in self.state.bot_handoff_symbols_by_strategy.items()
            if symbols
        }
        bot_handoff_history_by_strategy = {
            code: sorted(symbols)
            for code, symbols in self.state.bot_handoff_history_by_strategy.items()
            if symbols
        }
        if top_confirmed or all_confirmed_candidates or bot_handoff_symbols_by_strategy:
            payload = {
                "top_confirmed": top_confirmed,
                "all_confirmed_candidates": all_confirmed_candidates,
                "watchlist": watchlist,
                "bot_handoff_symbols_by_strategy": bot_handoff_symbols_by_strategy,
                "bot_handoff_history_by_strategy": bot_handoff_history_by_strategy,
                "cycle_count": int(summary.get("cycle_count", 0) or 0),
                "persisted_at": persisted_at,
                "scanner_session_start_utc": scanner_session_start,
            }
            self._replace_dashboard_snapshot("scanner_confirmed_last_nonempty", payload)
        elif not watchlist:
            # Clear cross-session scanner carryover when the fresh session is
            # genuinely empty. Without this, control-plane can restore the prior
            # session's last non-empty scanner rows after the live runtime has
            # already rolled to an empty watchlist.
            self._replace_dashboard_snapshot(
                "scanner_confirmed_last_nonempty",
                {
                    "top_confirmed": [],
                    "all_confirmed_candidates": [],
                    "watchlist": [],
                    "bot_handoff_symbols_by_strategy": {},
                    "bot_handoff_history_by_strategy": {},
                    "cycle_count": int(summary.get("cycle_count", 0) or 0),
                    "persisted_at": persisted_at,
                    "scanner_session_start_utc": scanner_session_start,
                },
            )

        alert_state = self.state.alert_engine.export_state()
        alert_state["cycle_count"] = int(summary.get("cycle_count", 0) or 0)
        alert_state["scanner_session_start_utc"] = scanner_session_start
        alert_state["recent_alerts"] = list(self.state.recent_alerts[-100:])
        alert_state["today_alerts"] = list(self.state.today_alerts[-5000:])
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

        session_start = current_scanner_session_start_utc(utcnow())
        persisted_session_start_raw = snapshot.payload.get("scanner_session_start_utc")
        if not isinstance(persisted_session_start_raw, str):
            self.logger.info("skipping alert-engine restore: scanner session marker missing")
            return
        try:
            persisted_session_start = datetime.fromisoformat(persisted_session_start_raw)
        except ValueError:
            self.logger.info(
                "skipping alert-engine restore: invalid scanner_session_start_utc=%s",
                persisted_session_start_raw,
            )
            return
        if persisted_session_start.tzinfo is None:
            persisted_session_start = persisted_session_start.replace(tzinfo=UTC)
        if persisted_session_start.astimezone(UTC) != session_start:
            self.logger.info(
                "skipping alert-engine restore from mismatched scanner session: persisted_session=%s session_start=%s",
                persisted_session_start.isoformat(),
                session_start.isoformat(),
            )
            return
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

            restored_today_alerts = snapshot.payload.get("today_alerts")
            if isinstance(restored_today_alerts, list):
                self.state.today_alerts = [
                    {**item, "ticker": str(item.get("ticker", "")).upper()}
                    for item in restored_today_alerts[-5000:]
                    if isinstance(item, dict)
                ]

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

        session_start = current_scanner_session_start_utc(utcnow())
        persisted_session_start_raw = snapshot.payload.get("scanner_session_start_utc")
        if not isinstance(persisted_session_start_raw, str):
            self.logger.info("skipping confirmed-candidate seed: scanner session marker missing")
            return
        try:
            persisted_session_start = datetime.fromisoformat(persisted_session_start_raw)
        except ValueError:
            self.logger.info(
                "skipping confirmed-candidate seed: invalid scanner_session_start_utc=%s",
                persisted_session_start_raw,
            )
            return
        if persisted_session_start.tzinfo is None:
            persisted_session_start = persisted_session_start.replace(tzinfo=UTC)
        if persisted_session_start.astimezone(UTC) != session_start:
            self.logger.info(
                "skipping confirmed-candidate seed from mismatched scanner session: persisted_session=%s session_start=%s",
                persisted_session_start.isoformat(),
                session_start.isoformat(),
            )
            return
        if persisted_at.astimezone(UTC) < session_start:
            self.logger.info(
                "skipping confirmed-candidate seed from prior session: persisted_at=%s session_start=%s",
                persisted_at.isoformat(),
                session_start.isoformat(),
            )
            return

        # Skip restore if the persisted snapshot is older than the staleness cap.
        # Evening restarts (e.g. 20:43 ET after 2026-05-18 RTH close) used to
        # restore yesterday's full confirmed + handoff because they were still
        # technically inside the same 4 AM ET → 4 AM ET session window. The
        # next 4 AM ET roll then failed to fully clean up because bot
        # `_desired_watchlist_symbols` wasn't cleared by `_roll_day_if_needed`,
        # leaving yesterday's 39 symbols stuck on bot watchlists into the next
        # session. Cap the restore window so post-active-session restarts
        # start fresh.
        now = utcnow()
        max_age_seconds = float(
            self.settings.strategy_seeded_snapshot_max_age_seconds
        )
        if max_age_seconds > 0:
            persisted_age_seconds = (now - persisted_at.astimezone(UTC)).total_seconds()
            if persisted_age_seconds > max_age_seconds:
                self.logger.info(
                    "skipping confirmed-candidate seed: snapshot stale | persisted_at=%s age_seconds=%.0f max_age_seconds=%.0f",
                    persisted_at.isoformat(),
                    persisted_age_seconds,
                    max_age_seconds,
                )
                return

        seeded_candidates = snapshot.payload.get("all_confirmed_candidates")
        if not isinstance(seeded_candidates, list) or not seeded_candidates:
            seeded_candidates = snapshot.payload.get("top_confirmed", [])
        if not isinstance(seeded_candidates, list) or not seeded_candidates:
            self._restore_watchlist_from_scanner_cycle_history()
            return

        seeded = [dict(item) for item in seeded_candidates if isinstance(item, dict)]
        if not seeded:
            self._restore_watchlist_from_scanner_cycle_history()
            return

        self.state.seed_confirmed_candidates(seeded)
        self.state.all_confirmed = self.state.confirmed_scanner.get_all_confirmed()
        visible_confirmed = self.state._ranked_scanner_confirmed_view(limit=5)
        if not visible_confirmed:
            visible_confirmed = list(self.state.all_confirmed)
        self.state.restore_confirmed_runtime_view(
            [dict(item) for item in visible_confirmed if isinstance(item, dict)],
            all_confirmed=[dict(item) for item in self.state.all_confirmed if isinstance(item, dict)],
            bot_handoff_symbols_by_strategy=(
                snapshot.payload.get("bot_handoff_symbols_by_strategy")
                if isinstance(snapshot.payload.get("bot_handoff_symbols_by_strategy"), dict)
                else None
            ),
            bot_handoff_history_by_strategy=(
                snapshot.payload.get("bot_handoff_history_by_strategy")
                if isinstance(snapshot.payload.get("bot_handoff_history_by_strategy"), dict)
                else None
            ),
        )
        self.logger.info("seeded %s confirmed candidates for fresh restart revalidation", len(seeded))

    def _restore_watchlist_from_scanner_cycle_history(self) -> None:
        if self.session_factory is None:
            return

        try:
            with self.session_factory() as session:
                snapshot = session.scalar(
                    select(DashboardSnapshot)
                    .where(DashboardSnapshot.snapshot_type == "scanner_cycle_history")
                    .order_by(desc(DashboardSnapshot.created_at), desc(DashboardSnapshot.id))
                )
        except Exception:
            self.logger.exception("failed to load scanner cycle history watchlist fallback")
            return

        if snapshot is None or not isinstance(snapshot.payload, dict):
            return

        payload = snapshot.payload
        persisted_at_raw = payload.get("persisted_at")
        if not isinstance(persisted_at_raw, str):
            return

        try:
            persisted_at = datetime.fromisoformat(persisted_at_raw)
        except ValueError:
            return

        if persisted_at.tzinfo is None:
            persisted_at = persisted_at.replace(tzinfo=UTC)

        session_start = current_scanner_session_start_utc(utcnow())
        persisted_session_start_raw = payload.get("scanner_session_start_utc")
        if not isinstance(persisted_session_start_raw, str):
            return
        try:
            persisted_session_start = datetime.fromisoformat(persisted_session_start_raw)
        except ValueError:
            return
        if persisted_session_start.tzinfo is None:
            persisted_session_start = persisted_session_start.replace(tzinfo=UTC)
        if persisted_session_start.astimezone(UTC) != session_start:
            return
        if persisted_at.astimezone(UTC) < session_start:
            return

        watchlist = payload.get("watchlist")
        active_handoff = (
            payload.get("bot_handoff_symbols_by_strategy")
            if isinstance(payload.get("bot_handoff_symbols_by_strategy"), dict)
            else None
        )
        history_handoff = (
            payload.get("bot_handoff_history_by_strategy")
            if isinstance(payload.get("bot_handoff_history_by_strategy"), dict)
            else None
        )
        if not bool(payload.get("session_handoff_active", False)):
            return
        if not isinstance(watchlist, list) or not watchlist:
            if not active_handoff:
                return
            watchlist = sorted(
                {
                    str(symbol).upper()
                    for symbols in active_handoff.values()
                    if isinstance(symbols, list)
                    for symbol in symbols
                    if str(symbol).strip()
                }
            )
        if not watchlist:
            return

        visible_confirmed = [
            {"ticker": str(symbol).upper()}
            for symbol in watchlist
            if str(symbol).strip()
        ]
        if not visible_confirmed:
            return

        self.state.restore_confirmed_runtime_view(
            visible_confirmed,
            bot_handoff_symbols_by_strategy=active_handoff,
            bot_handoff_history_by_strategy=history_handoff,
        )
        self.state.session_handoff_active = True
        self.logger.info(
            "restored %s symbols from scanner cycle-history watchlist fallback",
            len(visible_confirmed),
        )

    def _restore_runtime_state_from_database(self) -> None:
        self._reconcile_runtime_state_from_database(log_when_changed=True)
        self._restore_runtime_bar_history_from_database()

    def _restore_runtime_bar_history_from_database(self) -> None:
        if self.session_factory is None:
            return

        restored_pairs = 0
        session_start_utc = self._current_strategy_session_start_utc()

        try:
            with self.session_factory() as session:
                for code, runtime in self.state.bots.items():
                    if not isinstance(runtime, StrategyBotRuntime):
                        continue

                    symbols = sorted(runtime.active_symbols())
                    if not symbols:
                        continue

                    for symbol in symbols:
                        bars = self._load_runtime_restore_bars(
                            session=session,
                            code=code,
                            runtime=runtime,
                            symbol=symbol,
                            session_start_utc=session_start_utc,
                        )
                        if not bars:
                            continue

                        runtime.seed_bars(symbol, bars)
                        restored_pairs += 1
        except Exception:
            self.logger.exception("failed to restore runtime bar history from database")
            return

        if restored_pairs:
            self.logger.info(
                "restored runtime bar history from database | symbol_pairs=%s",
                restored_pairs,
            )

    def _load_runtime_restore_bars(
        self,
        *,
        session: Session,
        code: str,
        runtime: StrategyBotRuntime,
        symbol: str,
        session_start_utc: datetime,
    ) -> list[dict[str, float | int]]:
        history_limit = self._runtime_bar_history_restore_limit(runtime)
        if code == "schwab_1m" and int(runtime.definition.interval_secs) == 60:
            return self._load_schwab_1m_runtime_restore_bars(
                symbol=symbol,
                history_limit=history_limit,
                session=session,
                session_start_utc=session_start_utc,
            )

        query = (
            select(StrategyBarHistory)
            .where(
                StrategyBarHistory.strategy_code.in_(strategy_code_candidates(code)),
                StrategyBarHistory.symbol == symbol,
                StrategyBarHistory.interval_secs == runtime.definition.interval_secs,
                StrategyBarHistory.bar_time >= session_start_utc,
            )
            .order_by(StrategyBarHistory.bar_time.asc())
        )
        if history_limit is not None:
            query = query.limit(history_limit)
        records = list(session.scalars(query).all())
        return self._strategy_bar_history_records_to_payloads(records)

    def _load_schwab_1m_runtime_restore_bars(
        self,
        *,
        symbol: str,
        history_limit: int | None,
        session: Session,
        session_start_utc: datetime,
    ) -> list[dict[str, float | int]]:
        required_bars = max(1, int(history_limit or 1))
        restore_limit = max(required_bars * 4, required_bars)

        persisted_records = list(
            session.scalars(
                select(StrategyBarHistory)
                .where(
                    StrategyBarHistory.strategy_code == "schwab_1m",
                    StrategyBarHistory.symbol == symbol,
                    StrategyBarHistory.interval_secs == 60,
                )
                .order_by(StrategyBarHistory.bar_time.desc())
                .limit(restore_limit)
            ).all()
        )
        persisted_records.reverse()
        persisted_bars = self._strategy_bar_history_records_to_payloads(persisted_records)

        archived_bars: list[dict[str, float | int]] = []
        if self._schwab_tick_archive is not None:
            archived_bars = self._load_recent_archived_schwab_history_bars(
                symbol=symbol,
                interval_secs=60,
                required_bars=required_bars,
                end_at=self.state.alert_engine.now_provider(),
            )

        if archived_bars:
            merged = self._merge_historical_bar_payloads(persisted_bars, archived_bars)
            current_session_bars = [
                bar
                for bar in merged
                if float(bar["timestamp"]) >= session_start_utc.timestamp()
            ]
            if current_session_bars:
                return merged[-restore_limit:]

        if persisted_bars:
            return persisted_bars[-restore_limit:]
        return []

    @staticmethod
    def _strategy_bar_history_records_to_payloads(
        records: Sequence[StrategyBarHistory],
    ) -> list[dict[str, float | int]]:
        return [
            {
                "open": float(record.open_price),
                "high": float(record.high_price),
                "low": float(record.low_price),
                "close": float(record.close_price),
                "volume": int(record.volume),
                "timestamp": float(record.bar_time.timestamp()),
                "trade_count": int(record.trade_count),
            }
            for record in records
        ]

    def _runtime_bar_history_restore_limit(self, runtime: StrategyBotRuntime) -> int | None:
        trading_config = runtime.definition.trading_config
        indicator_config = runtime.definition.indicator_config
        if runtime.definition.code in {"macd_30s", "polygon_30s"} and runtime.definition.interval_secs == 30:
            return None
        indicator_min_bars = int(indicator_config.macd_slow + indicator_config.macd_signal)
        strategy_min_bars = int(getattr(trading_config, "schwab_native_warmup_bars_required", 0) or 0)
        return max(indicator_min_bars, strategy_min_bars, 1)

    def _current_strategy_session_start_utc(self) -> datetime:
        return current_scanner_session_start_utc(self.state.alert_engine.now_provider())

    def _snapshot_matches_current_strategy_session(
        self,
        snapshot: DashboardSnapshot | None,
        *,
        require_session_marker: bool = False,
    ) -> bool:
        session_start = self._current_strategy_session_start_utc()
        if snapshot is None or snapshot.created_at is None:
            return False
        payload = snapshot.payload if isinstance(snapshot.payload, dict) else {}
        marker_raw = payload.get("scanner_session_start_utc")
        if isinstance(marker_raw, str) and marker_raw.strip():
            try:
                marker_dt = datetime.fromisoformat(marker_raw)
            except ValueError:
                return False
            if marker_dt.tzinfo is None:
                marker_dt = marker_dt.replace(tzinfo=UTC)
            return marker_dt.astimezone(UTC) == session_start
        if require_session_marker:
            return False
        return snapshot.created_at.astimezone(UTC) >= session_start

    def _purge_stale_manual_stop_snapshots(self) -> None:
        if self.session_factory is None:
            return

        stale_snapshot_ids: list[object] = []
        try:
            with self.session_factory() as session:
                for snapshot_type in ("bot_manual_stop_symbols", "global_manual_stop_symbols"):
                    snapshot = session.scalar(
                        select(DashboardSnapshot)
                        .where(DashboardSnapshot.snapshot_type == snapshot_type)
                        .order_by(desc(DashboardSnapshot.created_at))
                    )
                    if self._snapshot_matches_current_strategy_session(
                        snapshot,
                        require_session_marker=True,
                    ):
                        continue
                    if snapshot is not None:
                        stale_snapshot_ids.append(snapshot.id)
                if stale_snapshot_ids:
                    session.execute(
                        delete(DashboardSnapshot).where(DashboardSnapshot.id.in_(stale_snapshot_ids))
                    )
                    session.commit()
        except Exception:
            self.logger.exception("failed purging stale manual stop snapshots")

    def _schwab_stream_failure_reason(self) -> str:
        errors = [
            str(getattr(self._schwab_stream_client, "last_error", "") or "").lower(),
            str(getattr(self._schwab_quote_poll_adapter, "last_error", "") or "").lower(),
        ]
        for last_error in errors:
            if "refresh_token_authentication_error" in last_error or "unsupported_token_type" in last_error:
                return "Schwab OAuth refresh failed on the VPS; reauthorize Schwab tokens before trading"
            if "failed refreshing schwab token" in last_error:
                return "Schwab OAuth refresh failed on the VPS; reauthorize Schwab tokens before trading"
        return ""

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

        expected_positions: dict[str, dict[str, tuple[int, float, str]]] = {
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

        position_symbols = {str(position.symbol).upper() for position in open_virtual_positions}
        strategy_ids = {position.strategy_id for position in open_virtual_positions}
        account_ids = {position.broker_account_id for position in open_virtual_positions}
        latest_open_paths: dict[tuple[UUID, UUID, str], str] = {}
        if position_symbols and strategy_ids and account_ids:
            open_intents = session.scalars(
                select(TradeIntent)
                .where(
                    TradeIntent.intent_type == "open",
                    TradeIntent.side == "buy",
                    TradeIntent.strategy_id.in_(list(strategy_ids)),
                    TradeIntent.broker_account_id.in_(list(account_ids)),
                    TradeIntent.symbol.in_(list(position_symbols)),
                    not_(TradeIntent.status.in_(("rejected", "cancelled"))),
                )
                .order_by(TradeIntent.created_at.asc())
            ).all()
            for intent in open_intents:
                payload = intent.payload if isinstance(intent.payload, dict) else {}
                metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), dict) else {}
                path = str(
                    metadata.get("path")
                    or metadata.get("confirmation_path")
                    or metadata.get("decision_path")
                    or ""
                ).strip()
                if not path and str(intent.reason or "").startswith("ENTRY_"):
                    path = str(intent.reason).removeprefix("ENTRY_").strip()
                latest_open_paths[(intent.strategy_id, intent.broker_account_id, str(intent.symbol).upper())] = path

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
                latest_open_paths.get((strategy.id, account.id, symbol), ""),
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
            metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), dict) else {}
            is_native_stop_guard = (
                str(payload.get("native_stop_guard", "")).strip().lower() == "true"
                or str(metadata.get("native_stop_guard", "")).strip().lower() == "true"
            )

            if intent_type == "open" and order.side == "buy":
                expected_pending_open.setdefault(strategy.code, set()).add(symbol)
                continue

            if order.side != "sell":
                continue

            if intent_type == "close":
                if is_native_stop_guard:
                    continue
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

            for symbol, (quantity, average_price, path) in expected_runtime_positions.items():
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
                    path=path,
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
        path: str = "",
    ) -> None:
        restore_position = getattr(runtime, "restore_position", None)
        if restore_position is None:
            return
        restore_position(
            symbol=symbol,
            quantity=quantity,
            average_price=average_price,
            path=path or "DB_RECONCILE",
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
        """Persist the latest snapshot for ``snapshot_type`` (replace prior row).

        With ``snapshot_persist_throttle_secs <= 0`` this persists immediately —
        byte-identical to the original per-call behaviour. With it > 0 the DB
        encode+commit is debounced per snapshot_type to at most one per window;
        the LATEST payload is always kept as pending (trailing-edge) and drained
        by ``_flush_pending_snapshots`` (each loop iteration, and force-flushed on
        shutdown / day-roll), so the dashboard never sticks on a stale snapshot.
        Live Redis state publishing is unaffected — only this Postgres persist.
        """
        if self.session_factory is None:
            return
        if self._snapshot_throttle_secs <= 0:
            self._do_replace_dashboard_snapshot(snapshot_type, payload)
            return
        now = utcnow()
        last = self._snapshot_last_persist_at.get(snapshot_type)
        if last is None or (now - last).total_seconds() >= self._snapshot_throttle_secs:
            self._snapshot_last_persist_at[snapshot_type] = now
            self._snapshot_pending.pop(snapshot_type, None)
            self._do_replace_dashboard_snapshot(snapshot_type, payload)
        else:
            # Within the window: coalesce — keep ONLY the latest (trailing-edge).
            self._snapshot_pending[snapshot_type] = payload

    def _flush_pending_snapshots(self, *, force: bool = False) -> None:
        """Drain coalesced snapshots. Each loop iteration: persist any pending whose
        throttle window has elapsed (trailing-edge, so a stalled dashboard catches up
        within ~1 iteration once messages stop). ``force=True`` (shutdown / day-roll)
        persists everything pending regardless of window — the latest is never lost."""
        if not getattr(self, "_snapshot_pending", None):
            return
        now = utcnow()
        for snapshot_type in list(self._snapshot_pending.keys()):
            last = self._snapshot_last_persist_at.get(snapshot_type)
            due = force or last is None or (now - last).total_seconds() >= self._snapshot_throttle_secs
            if not due:
                continue
            payload = self._snapshot_pending.pop(snapshot_type, None)
            if payload is None:
                continue
            self._snapshot_last_persist_at[snapshot_type] = now
            self._do_replace_dashboard_snapshot(snapshot_type, payload)

    def _do_replace_dashboard_snapshot(self, snapshot_type: str, payload: dict[str, object]) -> None:
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
            "scanner_session_start_utc": current_scanner_session_start_utc(utcnow()).isoformat(),
            "cycle_count": int(summary.get("cycle_count", 0) or 0),
            "watchlist": [str(symbol).upper() for symbol in summary.get("watchlist", []) if str(symbol).strip()],
            "bot_handoff_symbols_by_strategy": {
                code: sorted(symbols)
                for code, symbols in self.state.bot_handoff_symbols_by_strategy.items()
                if symbols
            },
            "bot_handoff_history_by_strategy": {
                code: sorted(symbols)
                for code, symbols in self.state.bot_handoff_history_by_strategy.items()
                if symbols
            },
            "session_handoff_active": bool(self.state.session_handoff_active),
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

    def _load_schwab_ineligible_symbols_by_strategy(self) -> dict[str, set[str]]:
        if self.session_factory is None:
            return {}

        schwab_accounts_by_code = {
            code: bot.definition.account_name
            for code, bot in self.state.bots.items()
            if self.settings.provider_for_account(bot.definition.account_name) == "schwab"
        }
        if not schwab_accounts_by_code:
            return {}

        try:
            with self.session_factory() as session:
                accounts = session.scalars(
                    select(BrokerAccount).where(BrokerAccount.name.in_(list(schwab_accounts_by_code.values())))
                ).all()
                account_ids_by_name = {account.name: account.id for account in accounts}
                blocked_by_account = OmsStore().list_schwab_ineligible_symbols_by_account(
                    session,
                    broker_account_ids=list(account_ids_by_name.values()),
                    session_date=session_day_eastern_str(self.state.alert_engine.now_provider()),
                )
        except Exception:
            self.logger.exception("failed to load Schwab ineligible symbols")
            return {}

        blocked_by_strategy: dict[str, set[str]] = {}
        for code, account_name in schwab_accounts_by_code.items():
            account_id = account_ids_by_name.get(account_name)
            if account_id is None:
                continue
            blocked_by_strategy[code] = set(blocked_by_account.get(account_id, set()))
        return blocked_by_strategy

    def _load_manual_stop_symbols(self) -> dict[str, set[str]]:
        if self.session_factory is None:
            return {}

        try:
            with self.session_factory() as session:
                snapshot = session.scalar(
                    select(DashboardSnapshot)
                    .where(DashboardSnapshot.snapshot_type == "bot_manual_stop_symbols")
                    .order_by(desc(DashboardSnapshot.created_at))
                )
        except Exception:
            self.logger.exception("failed to load manual bot stop symbols")
            return {}

        if snapshot is None or not isinstance(snapshot.payload, dict):
            return {}
        if not self._snapshot_matches_current_strategy_session(
            snapshot,
            require_session_marker=True,
        ):
            return {}

        bots_payload = snapshot.payload.get("bots", {})
        if not isinstance(bots_payload, dict):
            return {}

        normalized: dict[str, set[str]] = {}
        for code, symbols in bots_payload.items():
            if not isinstance(symbols, list):
                continue
            normalized[str(code)] = {
                str(symbol).upper() for symbol in symbols if str(symbol).strip()
            }
        return normalized

    def _load_global_manual_stop_symbols(self) -> set[str]:
        if self.session_factory is None:
            return set()

        try:
            with self.session_factory() as session:
                snapshot = session.scalar(
                    select(DashboardSnapshot)
                    .where(DashboardSnapshot.snapshot_type == "global_manual_stop_symbols")
                    .order_by(desc(DashboardSnapshot.created_at))
                )
        except Exception:
            self.logger.exception("failed to load global manual stop symbols")
            return set()

        if snapshot is None or not isinstance(snapshot.payload, dict):
            return set()
        if not self._snapshot_matches_current_strategy_session(
            snapshot,
            require_session_marker=True,
        ):
            return set()

        payload_symbols = snapshot.payload.get("symbols", [])
        if not isinstance(payload_symbols, list):
            return set()
        return {
            str(symbol).upper() for symbol in payload_symbols if str(symbol).strip()
        }

    def _preload_manual_stop_state(self) -> None:
        self.state.apply_global_manual_stop_symbols(self._load_global_manual_stop_symbols())
        self.state.apply_manual_stop_symbols(self._load_manual_stop_symbols())


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
