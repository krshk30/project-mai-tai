from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging

from project_mai_tai.strategy_core.time_utils import now_eastern
from project_mai_tai.strategy_core.trading_config import TradingConfig

logger = logging.getLogger(__name__)


class EntryEngine:
    def __init__(
        self,
        config: TradingConfig,
        name: str = "BOT",
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.name = name
        self.now_provider = now_provider or now_eastern
        self._pending: dict[str, dict[str, int | float | str]] = {}
        self._last_buy_bar: dict[str, int] = {}
        self._last_exit_bar: dict[str, int] = {}

    def check_entry(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, float | int | str] | None:
        if not indicators:
            return None

        gate_result = self._check_hard_gates(ticker, indicators, bar_index, position_tracker)
        if not gate_result["passed"]:
            if ticker in self._pending:
                logger.info("[%s] %s confirmation CANCELLED: %s", self.name, ticker, gate_result["reason"])
                del self._pending[ticker]
            return None

        if ticker in self._pending:
            return self._check_confirmation(ticker, indicators, bar_index)

        path = self._check_paths(ticker, indicators)
        if path is None:
            return None

        if self.config.confirm_bars <= 0:
            self._last_buy_bar[ticker] = bar_index
            score, score_details = self._quality_score(indicators)
            if self.config.min_score <= 0:
                score = 0
                score_details = "no_score"
            logger.info(
                "[%s] BUY %s instant | %s | price=%.4f",
                self.name,
                ticker,
                path,
                float(indicators["price"]),
            )
            return self._build_buy_signal(ticker, path, indicators, score, score_details)

        self._pending[ticker] = {
            "trigger_bar": bar_index,
            "trigger_price": float(indicators["price"]),
            "path": path,
            "bars_waiting": 0,
        }
        logger.info(
            "[%s] %s — %s triggered @ $%.4f | waiting %s bars",
            self.name,
            ticker,
            path,
            float(indicators["price"]),
            self.config.confirm_bars,
        )
        return None

    def record_exit(self, ticker: str, bar_index: int) -> None:
        self._last_exit_bar[ticker] = bar_index

    def cancel_pending(self, ticker: str) -> None:
        self._pending.pop(ticker, None)

    def reset(self) -> None:
        self._pending.clear()
        self._last_exit_bar.clear()

    def _check_hard_gates(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, str | bool]:
        eastern_now = self.now_provider()
        if eastern_now.hour < self.config.trading_start_hour or eastern_now.hour >= self.config.trading_end_hour:
            return {"passed": False, "reason": f"outside trading hours ({eastern_now.hour}:00 ET)"}

        time_str = eastern_now.strftime("%H:%M")
        if self.config.dead_zone_start <= time_str < self.config.dead_zone_end:
            return {"passed": False, "reason": f"in dead zone ({time_str} ET)"}

        if self.config.use_ema_gate and not bool(indicators["price_above_ema20"]):
            return {"passed": False, "reason": "price below EMA20"}

        last_exit = self._last_exit_bar.get(ticker, -999)
        if last_exit >= 0:
            bars_since_exit = bar_index - last_exit
            if bars_since_exit < self.config.cooldown_bars:
                return {
                    "passed": False,
                    "reason": f"cooldown ({bars_since_exit}/{self.config.cooldown_bars} bars)",
                }

        if position_tracker and position_tracker.has_position(ticker):
            return {"passed": False, "reason": "already in position"}

        if self._last_buy_bar.get(ticker, -1) == bar_index:
            return {"passed": False, "reason": "dedup (already fired this bar)"}

        return {"passed": True, "reason": ""}

    def _check_paths(self, ticker: str, indicators: dict[str, float | bool]) -> str | None:
        if bool(indicators["macd_cross_above"]):
            if not self.config.p1_require_below_3bars or bool(indicators.get("macd_was_below_3bars", False)):
                logger.debug("[%s] %s — P1 MACD Cross triggered", self.name, ticker)
                return "P1_MACD_CROSS"

        if (
            bool(indicators["price_cross_above_vwap"])
            and bool(indicators["macd_above_signal"])
            and bool(indicators["macd_increasing"])
        ):
            logger.debug("[%s] %s — P2 VWAP Breakout triggered", self.name, ticker)
            return "P2_VWAP_BREAKOUT"

        min_hist = 0.01 if self.name == "MACD Bot" else 0.001
        if (
            bool(indicators["macd_above_signal"])
            and not bool(indicators["macd_cross_above"])
            and float(indicators["macd_delta"]) >= self.config.surge_rate
            and bool(indicators.get("macd_delta_accelerating", False))
            and float(indicators["histogram"]) >= min_hist
            and bool(indicators.get("price_above_ema9", False))
            and float(indicators["volume"]) >= 5000
        ):
            logger.debug("[%s] %s — P3 MACD Surge triggered", self.name, ticker)
            return "P3_MACD_SURGE"

        return None

    def _check_confirmation(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
    ) -> dict[str, float | int | str] | None:
        pending = self._pending[ticker]
        pending["bars_waiting"] = int(pending["bars_waiting"]) + 1

        if not bool(indicators["macd_above_signal"]):
            del self._pending[ticker]
            return None

        if float(indicators["price"]) < float(pending["trigger_price"]):
            del self._pending[ticker]
            return None

        if int(pending["bars_waiting"]) < self.config.confirm_bars:
            return None

        score, details = self._quality_score(indicators)
        required_score = 5 if pending["path"] == "P3_MACD_SURGE" else self.config.min_score
        if score < required_score:
            del self._pending[ticker]
            return None

        path = str(pending["path"])
        del self._pending[ticker]
        self._last_buy_bar[ticker] = bar_index
        logger.info("[%s] BUY SIGNAL %s | %s | score=%s/6", self.name, ticker, path, score)
        return self._build_buy_signal(ticker, path, indicators, score, details)

    def _quality_score(self, indicators: dict[str, float | bool]) -> tuple[int, str]:
        score = 0
        parts: list[str] = []

        for passed, label in (
            (bool(indicators["histogram_growing"]), "hist"),
            (bool(indicators["stoch_k_rising"]), "stK"),
            (bool(indicators["price_above_vwap"]), "vwap"),
            (float(indicators["volume"]) > self.config.vol_min, "vol"),
            (bool(indicators["macd_increasing"]), "macd"),
            (bool(indicators["price_above_both_emas"]), "emas"),
        ):
            if passed:
                score += 1
                parts.append(f"{label}+")
            else:
                parts.append(f"{label}-")

        return score, " ".join(parts)

    def _build_buy_signal(
        self,
        ticker: str,
        path: str,
        indicators: dict[str, float | bool],
        score: int,
        score_details: str,
    ) -> dict[str, float | int | str]:
        return {
            "action": "BUY",
            "ticker": ticker,
            "path": path,
            "price": float(indicators["price"]),
            "score": score,
            "score_details": score_details,
            "macd": float(indicators["macd"]),
            "signal": float(indicators["signal"]),
            "histogram": float(indicators["histogram"]),
            "stoch_k": float(indicators["stoch_k"]),
            "ema9": float(indicators["ema9"]),
            "ema20": float(indicators["ema20"]),
            "vwap": float(indicators["vwap"]),
            "bar_volume": float(indicators["volume"]),
        }
