from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.strategy_core.bar_builder import BarBuilderManager
from project_mai_tai.strategy_core.indicators import ema
from project_mai_tai.strategy_core.models import OHLCVBar
from project_mai_tai.strategy_core.time_utils import now_eastern, session_day_eastern_str

EASTERN_TZ = ZoneInfo("America/New_York")


def _format_limit_price(value: float | str | Decimal | None) -> str | None:
    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except Exception:
        return None


def order_routing_metadata(*, price: str, side: str, now: datetime) -> dict[str, str]:
    current = now.astimezone(EASTERN_TZ)
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


@dataclass(frozen=True)
class RunnerConfig:
    min_score: float = 70.0
    min_change_pct: float = 30.0
    max_change_pct: float = 50.0
    live_change_floor_ratio: float = 0.90
    max_spread_cents: float = 10.0
    entry_start_hour: int = 7
    entry_cutoff_hour: int = 20
    exit_cutoff_hour: int = 20
    min_entry_ema_bars: int = 10
    min_exit_ema_bars: int = 25
    cooldown_seconds: int = 300
    ema_entry_period: int = 9
    ema_exit_low_period: int = 9
    ema_exit_high_period: int = 20
    high_profit_break_pct: float = 50.0
    trail_pct_low: float = 10.0
    trail_pct_mid: float = 15.0
    trail_pct_high: float = 20.0
    volume_fade_ratio: float = 0.50
    volume_fade_trail_tighten_pct: float = 5.0
    minimum_trade_size: int = 100


class RunnerPosition:
    def __init__(
        self,
        ticker: str,
        entry_price: float,
        quantity: int,
        entry_change_pct: float = 0.0,
        entry_time: str = "",
    ):
        self.ticker = ticker
        self.entry_price = entry_price
        self.quantity = quantity
        self.original_quantity = quantity
        self.entry_change_pct = entry_change_pct
        self.entry_time = entry_time

        self.current_price = entry_price
        self.peak_price = entry_price
        self.current_profit_pct = 0.0
        self.peak_profit_pct = 0.0

        self.peak_5min_volume = 0
        self.volume_faded = False

    def update_price(self, price: float) -> None:
        self.current_price = price
        if self.entry_price <= 0:
            return

        self.current_profit_pct = (price - self.entry_price) / self.entry_price * 100
        if price > self.peak_price:
            self.peak_price = price
            self.peak_profit_pct = self.current_profit_pct

    def get_trail_pct(self, config: RunnerConfig) -> float:
        return config.trail_pct_low

    def get_trail_stop_price(self, config: RunnerConfig) -> float:
        return self.peak_price * (1 - self.get_trail_pct(config) / 100)

    def is_trail_breached(self, config: RunnerConfig) -> bool:
        stop_price = self.get_trail_stop_price(config)
        return stop_price > 0 and self.current_price <= stop_price

    def to_dict(self, config: RunnerConfig) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "entry_price": round(self.entry_price, 4),
            "entry_time": self.entry_time,
            "current_price": round(self.current_price, 4),
            "quantity": self.quantity,
            "original_quantity": self.original_quantity,
            "entry_change_pct": round(self.entry_change_pct, 1),
            "current_profit_pct": round(self.current_profit_pct, 2),
            "peak_profit_pct": round(self.peak_profit_pct, 2),
            "trail_pct": round(self.get_trail_pct(config), 2),
            "trail_stop": round(self.get_trail_stop_price(config), 4),
            "peak_5min_volume": self.peak_5min_volume,
            "volume_faded": self.volume_faded,
        }


class RunnerStrategyRuntime:
    def __init__(
        self,
        *,
        definition_code: str,
        account_name: str,
        default_quantity: int,
        bar_interval_secs: int = 60,
        now_provider: Callable[[], datetime] | None = None,
        config: RunnerConfig | None = None,
        source_service: str = "strategy-engine",
    ) -> None:
        self.definition_code = definition_code
        self.account_name = account_name
        self.default_quantity = default_quantity
        self.now_provider = now_provider or now_eastern
        self.config = config or RunnerConfig()
        self.source_service = source_service

        self.builder_manager = BarBuilderManager(interval_secs=bar_interval_secs)
        self.watchlist: set[str] = set()
        self._candidates: dict[str, dict[str, object]] = {}
        self._latest_quotes: dict[str, dict[str, float]] = {}
        self._cooldown_until: dict[str, datetime] = {}
        self._entered_today: set[str] = set()
        self._positions: dict[str, RunnerPosition] = {}
        self._pending_open_symbols: set[str] = set()
        self._pending_close_symbols: set[str] = set()
        self._pending_close_reasons: dict[str, str] = {}
        self._close_retry_blocked_until: dict[str, datetime] = {}
        self._applied_fill_quantity_by_order: dict[str, Decimal] = {}
        self._daily_pnl = 0.0
        self._closed_today: list[dict[str, object]] = []
        self._active_day = session_day_eastern_str(self.now_provider())

    @property
    def _position(self) -> RunnerPosition | None:
        return next(iter(self._positions.values()), None)

    @_position.setter
    def _position(self, value: RunnerPosition | None) -> None:
        self._positions.clear()
        if value is not None:
            self._positions[value.ticker.upper()] = value

    @property
    def _pending_open_symbol(self) -> str | None:
        return next(iter(sorted(self._pending_open_symbols)), None)

    @_pending_open_symbol.setter
    def _pending_open_symbol(self, value: str | None) -> None:
        self._pending_open_symbols.clear()
        if value:
            self._pending_open_symbols.add(str(value).upper())

    @property
    def _pending_close_symbol(self) -> str | None:
        return next(iter(sorted(self._pending_close_symbols)), None)

    @_pending_close_symbol.setter
    def _pending_close_symbol(self, value: str | None) -> None:
        self._pending_close_symbols.clear()
        if value:
            self._pending_close_symbols.add(str(value).upper())

    def set_watchlist(self, symbols: Iterable[str]) -> None:
        self.watchlist = {symbol.upper() for symbol in symbols if symbol}
        self._prune_runtime_state()

    def update_market_snapshots(self, snapshots: Sequence[object]) -> None:
        for snapshot in snapshots:
            ticker = str(getattr(snapshot, "ticker", "")).upper()
            if not ticker:
                continue
            last_quote = getattr(snapshot, "last_quote", None)
            bid = getattr(last_quote, "bid_price", None) if last_quote is not None else None
            ask = getattr(last_quote, "ask_price", None) if last_quote is not None else None
            quote: dict[str, float] = {}
            if bid is not None and bid > 0:
                quote["bid"] = float(bid)
            if ask is not None and ask > 0:
                quote["ask"] = float(ask)
            if quote:
                self._latest_quotes[ticker] = quote

    def update_candidates(self, candidates: Sequence[dict[str, object]]) -> None:
        self._candidates = {str(candidate.get("ticker", "")).upper(): dict(candidate) for candidate in candidates}
        self._prune_runtime_state()

    def restore_position(
        self,
        *,
        symbol: str,
        quantity: int,
        average_price: float,
        path: str = "",
    ) -> None:
        del path
        normalized = symbol.upper()
        if quantity <= 0 or average_price <= 0:
            return
        self._entered_today.add(normalized)
        self._positions[normalized] = RunnerPosition(
            ticker=normalized,
            entry_price=average_price,
            quantity=quantity,
            entry_change_pct=0.0,
            entry_time=self.now_provider().strftime("%I:%M:%S %p ET"),
        )

    def restore_pending_open(self, symbol: str) -> None:
        if symbol:
            self._pending_open_symbols.add(symbol.upper())

    def restore_pending_close(self, symbol: str) -> None:
        if symbol:
            self._pending_close_symbols.add(symbol.upper())

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

        builder.bars = hydrated[-builder.max_bars :]
        builder._bar_count = len(builder.bars)
        builder._current_bar = None
        builder._current_bar_start = 0.0

    def _prune_runtime_state(self) -> None:
        keep = set(self.watchlist)
        keep.update(self._candidates.keys())
        keep.update(self._positions.keys())
        keep.update(self._pending_open_symbols)
        keep.update(self._pending_close_symbols)

        self._latest_quotes = {
            symbol: quote
            for symbol, quote in self._latest_quotes.items()
            if symbol in keep
        }
        self._cooldown_until = {
            symbol: blocked_until
            for symbol, blocked_until in self._cooldown_until.items()
            if symbol in keep
        }
        self._close_retry_blocked_until = {
            symbol: blocked_until
            for symbol, blocked_until in self._close_retry_blocked_until.items()
            if symbol in keep
        }
        self._pending_close_reasons = {
            symbol: reason
            for symbol, reason in self._pending_close_reasons.items()
            if symbol in keep
        }
        self._pending_open_symbols = {symbol for symbol in self._pending_open_symbols if symbol in keep}
        self._pending_close_symbols = {symbol for symbol in self._pending_close_symbols if symbol in keep}
        self.builder_manager.remove_tickers(
            {ticker for ticker in self.builder_manager.get_all_tickers() if ticker not in keep}
        )

    def handle_trade_tick(
        self,
        symbol: str,
        price: float,
        size: int,
        timestamp_ns: int | None = None,
        cumulative_volume: int | None = None,
    ) -> list[TradeIntentEvent]:
        del cumulative_volume
        self._roll_day_if_needed()
        normalized = symbol.upper()
        intents: list[TradeIntentEvent] = []
        position = self._positions.get(normalized)

        if position is not None:
            position.update_price(price)
            if (
                self._should_force_time_close()
                and normalized not in self._pending_close_symbols
                and not self._is_close_retry_blocked(normalized)
            ):
                intents.append(self._emit_close_intent(symbol=normalized, reason="TIME_CLOSE_6PM"))
                return intents
            if (
                position.is_trail_breached(self.config)
                and normalized not in self._pending_close_symbols
                and not self._is_close_retry_blocked(normalized)
            ):
                trail_pct = round(position.get_trail_pct(self.config), 0)
                intents.append(self._emit_close_intent(symbol=normalized, reason=f"TRAIL_STOP_{trail_pct:.0f}%"))
                return intents

        should_build_bars = normalized in self.watchlist
        should_build_bars = should_build_bars or normalized in self._positions
        should_build_bars = should_build_bars or normalized in self.builder_manager.get_all_tickers()
        if should_build_bars and size >= self.config.minimum_trade_size:
            completed_bars = self.builder_manager.on_trade(normalized, price, size, timestamp_ns or 0)
            for bar in completed_bars:
                intents.extend(self._handle_completed_bar(normalized, bar))

        if normalized in self._positions or normalized in self._pending_open_symbols or normalized in self._pending_close_symbols:
            return intents
        if normalized not in self.watchlist:
            return intents

        candidate = self._candidates.get(normalized)
        if candidate is None:
            return intents

        if not self._entry_window_open():
            return intents
        if not self._is_candidate_eligible(candidate, price):
            return intents

        intents.append(self._emit_open_intent(candidate, price))
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
        del level
        del path

        normalized = symbol.upper()
        incremental_quantity = self._incremental_fill_quantity(client_order_id, quantity)
        if incremental_quantity <= 0:
            return

        filled_qty = int(incremental_quantity)
        fill_price = float(price)

        if intent_type == "open" and side == "buy":
            self._pending_open_symbols.discard(normalized)

            candidate = self._candidates.get(normalized, {})
            entry_change_pct = float(candidate.get("change_pct", 0) or 0)
            self._entered_today.add(normalized)
            position = self._positions.get(normalized)
            if position is None:
                self._positions[normalized] = RunnerPosition(
                    ticker=normalized,
                    entry_price=fill_price,
                    quantity=filled_qty or self.default_quantity,
                    entry_change_pct=entry_change_pct,
                    entry_time=self.now_provider().strftime("%I:%M:%S %p ET"),
                )
            else:
                total_qty = position.quantity + filled_qty
                if total_qty > 0:
                    position.entry_price = (
                        (position.entry_price * position.quantity)
                        + (fill_price * filled_qty)
                    ) / total_qty
                position.quantity = total_qty
                position.update_price(fill_price)
            return

        position = self._positions.get(normalized)
        if intent_type == "close" and side == "sell" and position is not None:
            if status == "filled" or filled_qty >= position.quantity:
                closed = self._close_position(normalized, fill_price, self._pending_close_reasons.get(normalized, "OMS_FILL"))
                self._positions.pop(normalized, None)
                self._pending_close_symbols.discard(normalized)
                self._pending_close_reasons.pop(normalized, None)
                self._cooldown_until[normalized] = self.now_provider() + timedelta(seconds=self.config.cooldown_seconds)
                if closed is not None:
                    self._closed_today.append(closed)
                    self._daily_pnl += float(closed["pnl"])
                return

            position.quantity -= filled_qty
            position.update_price(fill_price)

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
        del level
        normalized = symbol.upper()
        if status not in {"rejected", "cancelled"}:
            return

        if intent_type == "open" and normalized in self._pending_open_symbols:
            self._pending_open_symbols.discard(normalized)
            return

        if intent_type == "close" and normalized in self._pending_close_symbols:
            self._pending_close_symbols.discard(normalized)
            normalized_reason = (reason or "").strip().lower()
            if "rate limit exceeded" in normalized_reason:
                self._close_retry_blocked_until[normalized] = self.now_provider() + timedelta(seconds=5)
            elif (
                "duplicate_exit_in_flight" in normalized_reason
                or "broker quantity already reserved for pending exits" in normalized_reason
            ):
                self._close_retry_blocked_until[normalized] = self.now_provider() + timedelta(seconds=2)
            elif (
                "cannot be sold short" in normalized_reason
                or "insufficient qty" in normalized_reason
                or "no broker position available to sell" in normalized_reason
                or "no strategy position available to sell" in normalized_reason
            ):
                self._positions.pop(normalized, None)
                self._pending_close_reasons.pop(normalized, None)

    def summary(self) -> dict[str, object]:
        self._roll_day_if_needed()
        return {
            "strategy": self.definition_code,
            "account_name": self.account_name,
            "watchlist": sorted(self.watchlist),
            "positions": [position.to_dict(self.config) for position in self._positions.values()],
            "pending_open_symbols": sorted(self._pending_open_symbols),
            "pending_close_symbols": sorted(self._pending_close_symbols),
            "pending_scale_levels": [],
            "entered_today": sorted(self._entered_today),
            "daily_pnl": self._daily_pnl,
            "closed_today": list(self._closed_today),
            "indicator_snapshots": [],
        }

    def _roll_day_if_needed(self) -> None:
        current_day = session_day_eastern_str(self.now_provider())
        if current_day == self._active_day:
            return
        self._daily_pnl = 0.0
        self._closed_today.clear()
        self._entered_today.clear()
        self._active_day = current_day

    def active_symbols(self) -> set[str]:
        symbols = set(self.watchlist)
        symbols.update(self._positions.keys())
        symbols.update(self._pending_open_symbols)
        symbols.update(self._pending_close_symbols)
        return symbols

    def _handle_completed_bar(self, symbol: str, bar: OHLCVBar) -> list[TradeIntentEvent]:
        position = self._positions.get(symbol)
        if position is None:
            return []

        bar_volume = int(bar.volume)
        if bar_volume > position.peak_5min_volume:
            position.peak_5min_volume = bar_volume
        elif (
            position.peak_5min_volume > 0
            and bar_volume < position.peak_5min_volume * self.config.volume_fade_ratio
        ):
            position.volume_faded = True

        if symbol not in self._pending_close_symbols and not self._is_close_retry_blocked(symbol):
            ema_break = self._check_ema_break(symbol)
            if ema_break is not None:
                return [self._emit_close_intent(symbol=symbol, reason=ema_break)]
        return []

    def _entry_window_open(self) -> bool:
        current = self.now_provider()
        return self.config.entry_start_hour <= current.hour < self.config.entry_cutoff_hour

    def _should_force_time_close(self) -> bool:
        current = self.now_provider()
        return current.hour >= self.config.exit_cutoff_hour

    def _is_candidate_eligible(self, candidate: dict[str, object], live_price: float) -> bool:
        symbol = str(candidate.get("ticker", "")).upper()
        score = float(candidate.get("rank_score", 0) or 0)
        if score < self.config.min_score:
            return False

        candidate_change_pct = float(candidate.get("change_pct", 0) or 0)
        if candidate_change_pct > self.config.max_change_pct:
            return False

        if candidate_change_pct < self.config.min_change_pct:
            return False

        cooldown_until = self._cooldown_until.get(symbol)
        if cooldown_until is not None and self.now_provider() < cooldown_until:
            return False

        if symbol in self._entered_today:
            return False

        if not self._confirmed_after_open(candidate):
            return False

        prev_close = float(candidate.get("prev_close", 0) or 0)
        if prev_close <= 0:
            return False

        live_change_pct = (live_price - prev_close) / prev_close * 100
        if live_change_pct < candidate_change_pct * self.config.live_change_floor_ratio:
            return False

        bid = float(candidate.get("bid", 0) or 0)
        ask = float(candidate.get("ask", 0) or 0)
        if bid > 0 and ask > 0 and (ask - bid) * 100 > self.config.max_spread_cents:
            return False

        bars = self.builder_manager.get_bars(symbol)
        if len(bars) >= self.config.min_entry_ema_bars:
            ema9 = ema([float(bar["close"]) for bar in bars], self.config.ema_entry_period)
            if ema9 and live_price < ema9[-1]:
                return False

        return True

    def _confirmed_after_open(self, candidate: dict[str, object]) -> bool:
        raw_time = str(candidate.get("confirmed_at", "")).strip()
        if not raw_time:
            return False

        normalized = raw_time.replace(" ET", "")
        try:
            confirmed_time = datetime.strptime(normalized, "%I:%M:%S %p")
        except ValueError:
            return False
        return confirmed_time.hour >= self.config.entry_start_hour

    def _check_ema_break(self, symbol: str) -> str | None:
        bars = self.builder_manager.get_bars(symbol)
        position = self._positions.get(symbol)
        if len(bars) < self.config.min_exit_ema_bars or position is None:
            return None

        closes = [float(bar["close"]) for bar in bars]
        current_close = closes[-1]
        ema9 = ema(closes, self.config.ema_exit_low_period)
        ema20 = ema(closes, self.config.ema_exit_high_period)
        if not ema9 or not ema20:
            return None

        if position.peak_profit_pct < self.config.high_profit_break_pct:
            if current_close < ema9[-1]:
                return f"EMA9_BREAK(5m) price=${current_close:.2f}<EMA9=${ema9[-1]:.2f}"
            return None

        if current_close < ema20[-1]:
            return f"EMA20_BREAK(5m) price=${current_close:.2f}<EMA20=${ema20[-1]:.2f}"
        return None

    def _emit_open_intent(self, candidate: dict[str, object], live_price: float) -> TradeIntentEvent:
        symbol = str(candidate.get("ticker", "")).upper()
        self._pending_open_symbols.add(symbol)
        reference_price = str(live_price)
        quote = self._latest_quotes.get(symbol, {})
        routed_price = _format_limit_price(quote.get("ask")) or _format_limit_price(reference_price) or reference_price
        metadata = {
            "reference_price": reference_price,
            "rank_score": str(candidate.get("rank_score", "")),
            "change_pct": str(candidate.get("change_pct", "")),
            "confirmation_path": str(candidate.get("confirmation_path", "")),
        }
        metadata.update(order_routing_metadata(price=routed_price, side="buy", now=self.now_provider()))
        return TradeIntentEvent(
            source_service=self.source_service,
            payload=TradeIntentPayload(
                strategy_code=self.definition_code,
                broker_account_name=self.account_name,
                symbol=symbol,
                side="buy",
                quantity=Decimal(str(self.default_quantity)),
                intent_type="open",
                reason="ENTRY_RUNNER_MOMENTUM",
                metadata=metadata,
            ),
        )

    def _emit_close_intent(self, *, symbol: str | None = None, reason: str) -> TradeIntentEvent:
        if symbol is None:
            position = self._position
            symbol = position.ticker if position is not None else None
        if symbol is None:
            raise RuntimeError("runner close intent requested without an open position")
        symbol = symbol.upper()
        position = self._positions.get(symbol)
        if position is None:
            raise RuntimeError("runner close intent requested without an open position")

        self._pending_close_symbols.add(symbol)
        self._pending_close_reasons[symbol] = reason
        reference_price = str(position.current_price)
        quote = self._latest_quotes.get(symbol, {})
        routed_price = _format_limit_price(quote.get("bid")) or _format_limit_price(reference_price) or reference_price
        metadata = {
            "reference_price": reference_price,
            "peak_profit_pct": str(position.peak_profit_pct),
            "trail_pct": str(position.get_trail_pct(self.config)),
        }
        metadata.update(order_routing_metadata(price=routed_price, side="sell", now=self.now_provider()))
        return TradeIntentEvent(
            source_service=self.source_service,
            payload=TradeIntentPayload(
                strategy_code=self.definition_code,
                broker_account_name=self.account_name,
                symbol=symbol,
                side="sell",
                quantity=Decimal(str(position.quantity)),
                intent_type="close",
                reason=reason,
                metadata=metadata,
            ),
        )

    def _is_close_retry_blocked(self, symbol: str | None = None) -> bool:
        if symbol is None:
            pending_symbol = self._pending_close_symbol
            if pending_symbol is not None:
                symbol = pending_symbol
            else:
                position = self._position
                symbol = position.ticker if position is not None else None
        if symbol is None:
            return False
        symbol = symbol.upper()
        blocked_until = self._close_retry_blocked_until.get(symbol)
        return blocked_until is not None and self.now_provider() < blocked_until

    def _close_position(self, symbol: str, exit_price: float, reason: str) -> dict[str, object] | None:
        position = self._positions.get(symbol)
        if position is None:
            return None
        pnl = (exit_price - position.entry_price) * position.quantity
        pnl_pct = (
            ((exit_price - position.entry_price) / position.entry_price) * 100
            if position.entry_price > 0
            else 0.0
        )
        return {
            "ticker": position.ticker,
            "entry_price": round(position.entry_price, 4),
            "exit_price": round(exit_price, 4),
            "quantity": position.quantity,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "entry_time": position.entry_time,
            "exit_time": self.now_provider().strftime("%I:%M:%S %p ET"),
            "peak_profit_pct": round(position.peak_profit_pct, 2),
        }
