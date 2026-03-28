from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.strategy_core.bar_builder import BarBuilderManager
from project_mai_tai.strategy_core.indicators import ema
from project_mai_tai.strategy_core.models import OHLCVBar


@dataclass(frozen=True)
class RunnerConfig:
    min_score: float = 70.0
    min_change_pct: float = 35.0
    min_change_pct_with_news: float = 20.0
    max_change_pct: float = 50.0
    live_change_floor_ratio: float = 0.90
    max_spread_cents: float = 10.0
    entry_start_hour: int = 7
    entry_cutoff_hour: int = 18
    exit_cutoff_hour: int = 18
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
        trail_pct = config.trail_pct_low
        if self.peak_profit_pct >= 100.0:
            trail_pct = config.trail_pct_high
        elif self.peak_profit_pct >= 80.0:
            trail_pct = config.trail_pct_mid

        if self.volume_faded:
            trail_pct -= config.volume_fade_trail_tighten_pct
        return max(trail_pct, 5.0)

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
        now_provider: Callable[[], datetime] | None = None,
        config: RunnerConfig | None = None,
        source_service: str = "strategy-engine",
    ) -> None:
        self.definition_code = definition_code
        self.account_name = account_name
        self.default_quantity = default_quantity
        self.now_provider = now_provider or datetime.now
        self.config = config or RunnerConfig()
        self.source_service = source_service

        self.builder_manager = BarBuilderManager(interval_secs=300)
        self.watchlist: set[str] = set()
        self._candidates: dict[str, dict[str, object]] = {}
        self._cooldown_until: dict[str, datetime] = {}
        self._position: RunnerPosition | None = None
        self._pending_open_symbol: str | None = None
        self._pending_close_symbol: str | None = None
        self._pending_close_reason: str = ""
        self._daily_pnl = 0.0
        self._closed_today: list[dict[str, object]] = []

    def set_watchlist(self, symbols: Iterable[str]) -> None:
        self.watchlist = {symbol.upper() for symbol in symbols if symbol}

    def update_candidates(self, candidates: Sequence[dict[str, object]]) -> None:
        self._candidates = {str(candidate.get("ticker", "")).upper(): dict(candidate) for candidate in candidates}

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

    def handle_trade_tick(
        self,
        symbol: str,
        price: float,
        size: int,
        timestamp_ns: int | None = None,
    ) -> list[TradeIntentEvent]:
        normalized = symbol.upper()
        intents: list[TradeIntentEvent] = []

        if self._position and self._position.ticker == normalized:
            self._position.update_price(price)
            if self._should_force_time_close() and self._pending_close_symbol is None:
                intents.append(self._emit_close_intent(reason="TIME_CLOSE_6PM"))
                return intents
            if self._position.is_trail_breached(self.config) and self._pending_close_symbol is None:
                trail_pct = round(self._position.get_trail_pct(self.config), 0)
                intents.append(self._emit_close_intent(reason=f"TRAIL_STOP_{trail_pct:.0f}%"))
                return intents

        should_build_bars = normalized in self.watchlist
        should_build_bars = should_build_bars or (self._position is not None and self._position.ticker == normalized)
        should_build_bars = should_build_bars or normalized in self.builder_manager.get_all_tickers()
        if should_build_bars and size >= self.config.minimum_trade_size:
            completed_bars = self.builder_manager.on_trade(normalized, price, size, timestamp_ns or 0)
            for bar in completed_bars:
                intents.extend(self._handle_completed_bar(normalized, bar))

        if self._position is not None or self._pending_open_symbol is not None or self._pending_close_symbol is not None:
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
        symbol: str,
        intent_type: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        level: str | None = None,
        path: str | None = None,
    ) -> None:
        del level
        del path

        normalized = symbol.upper()
        filled_qty = int(quantity)
        fill_price = float(price)

        if intent_type == "open" and side == "buy":
            if self._pending_open_symbol == normalized:
                self._pending_open_symbol = None

            candidate = self._candidates.get(normalized, {})
            entry_change_pct = float(candidate.get("change_pct", 0) or 0)
            if self._position is None:
                self._position = RunnerPosition(
                    ticker=normalized,
                    entry_price=fill_price,
                    quantity=filled_qty or self.default_quantity,
                    entry_change_pct=entry_change_pct,
                    entry_time=self.now_provider().strftime("%I:%M:%S %p ET"),
                )
            else:
                self._position.update_price(fill_price)
            return

        if intent_type == "close" and side == "sell" and self._position and self._position.ticker == normalized:
            if filled_qty >= self._position.quantity:
                closed = self._close_position(fill_price, self._pending_close_reason or "OMS_FILL")
                self._position = None
                self._pending_close_symbol = None
                self._pending_close_reason = ""
                self._cooldown_until[normalized] = self.now_provider() + timedelta(seconds=self.config.cooldown_seconds)
                if closed is not None:
                    self._closed_today.append(closed)
                    self._daily_pnl += float(closed["pnl"])
                return

            self._position.quantity -= filled_qty
            self._position.update_price(fill_price)

    def apply_order_status(
        self,
        *,
        symbol: str,
        intent_type: str,
        status: str,
        level: str | None = None,
    ) -> None:
        del level
        normalized = symbol.upper()
        if status not in {"rejected", "cancelled"}:
            return

        if intent_type == "open" and self._pending_open_symbol == normalized:
            self._pending_open_symbol = None
            return

        if intent_type == "close" and self._pending_close_symbol == normalized:
            self._pending_close_symbol = None

    def summary(self) -> dict[str, object]:
        positions = [self._position.to_dict(self.config)] if self._position is not None else []
        return {
            "strategy": self.definition_code,
            "account_name": self.account_name,
            "watchlist": sorted(self.watchlist),
            "positions": positions,
            "pending_open_symbols": [self._pending_open_symbol] if self._pending_open_symbol else [],
            "pending_close_symbols": [self._pending_close_symbol] if self._pending_close_symbol else [],
            "pending_scale_levels": [],
            "daily_pnl": self._daily_pnl,
            "closed_today": list(self._closed_today),
        }

    def active_symbols(self) -> set[str]:
        symbols = set(self.watchlist)
        if self._position is not None:
            symbols.add(self._position.ticker)
        if self._pending_open_symbol:
            symbols.add(self._pending_open_symbol)
        if self._pending_close_symbol:
            symbols.add(self._pending_close_symbol)
        return symbols

    def _handle_completed_bar(self, symbol: str, bar: OHLCVBar) -> list[TradeIntentEvent]:
        if self._position is None or self._position.ticker != symbol:
            return []

        bar_volume = int(bar.volume)
        if bar_volume > self._position.peak_5min_volume:
            self._position.peak_5min_volume = bar_volume
        elif (
            self._position.peak_5min_volume > 0
            and bar_volume < self._position.peak_5min_volume * self.config.volume_fade_ratio
        ):
            self._position.volume_faded = True

        if self._pending_close_symbol is None:
            ema_break = self._check_ema_break(symbol)
            if ema_break is not None:
                return [self._emit_close_intent(reason=ema_break)]
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

        min_change = self.config.min_change_pct
        if str(candidate.get("confirmation_path", "")) == "PATH_A_NEWS":
            min_change = self.config.min_change_pct_with_news
        if candidate_change_pct < min_change:
            return False

        cooldown_until = self._cooldown_until.get(symbol)
        if cooldown_until is not None and self.now_provider() < cooldown_until:
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
        if len(bars) < self.config.min_exit_ema_bars or self._position is None:
            return None

        closes = [float(bar["close"]) for bar in bars]
        current_close = closes[-1]
        ema9 = ema(closes, self.config.ema_exit_low_period)
        ema20 = ema(closes, self.config.ema_exit_high_period)
        if not ema9 or not ema20:
            return None

        if self._position.peak_profit_pct < self.config.high_profit_break_pct:
            if current_close < ema9[-1]:
                return f"EMA9_BREAK(5m) price=${current_close:.2f}<EMA9=${ema9[-1]:.2f}"
            return None

        if current_close < ema20[-1]:
            return f"EMA20_BREAK(5m) price=${current_close:.2f}<EMA20=${ema20[-1]:.2f}"
        return None

    def _emit_open_intent(self, candidate: dict[str, object], live_price: float) -> TradeIntentEvent:
        symbol = str(candidate.get("ticker", "")).upper()
        self._pending_open_symbol = symbol
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
                metadata={
                    "reference_price": str(live_price),
                    "rank_score": str(candidate.get("rank_score", "")),
                    "change_pct": str(candidate.get("change_pct", "")),
                    "confirmation_path": str(candidate.get("confirmation_path", "")),
                },
            ),
        )

    def _emit_close_intent(self, *, reason: str) -> TradeIntentEvent:
        if self._position is None:
            raise RuntimeError("runner close intent requested without an open position")

        self._pending_close_symbol = self._position.ticker
        self._pending_close_reason = reason
        return TradeIntentEvent(
            source_service=self.source_service,
            payload=TradeIntentPayload(
                strategy_code=self.definition_code,
                broker_account_name=self.account_name,
                symbol=self._position.ticker,
                side="sell",
                quantity=Decimal(str(self._position.quantity)),
                intent_type="close",
                reason=reason,
                metadata={
                    "reference_price": str(self._position.current_price),
                    "peak_profit_pct": str(self._position.peak_profit_pct),
                    "trail_pct": str(self._position.get_trail_pct(self.config)),
                },
            ),
        )

    def _close_position(self, exit_price: float, reason: str) -> dict[str, object] | None:
        if self._position is None:
            return None
        pnl = (exit_price - self._position.entry_price) * self._position.quantity
        pnl_pct = (
            ((exit_price - self._position.entry_price) / self._position.entry_price) * 100
            if self._position.entry_price > 0
            else 0.0
        )
        return {
            "ticker": self._position.ticker,
            "entry_price": round(self._position.entry_price, 4),
            "exit_price": round(exit_price, 4),
            "quantity": self._position.quantity,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "entry_time": self._position.entry_time,
            "exit_time": self.now_provider().strftime("%I:%M:%S %p ET"),
            "peak_profit_pct": round(self._position.peak_profit_pct, 2),
        }
