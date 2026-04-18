from __future__ import annotations

import logging

from project_mai_tai.strategy_core.trading_config import TradingConfig

logger = logging.getLogger(__name__)


class ExitEngine:
    def __init__(self, config: TradingConfig):
        self.config = config

    def check_intrabar_exit(self, position) -> dict[str, float | int | str] | None:
        if not position:
            return None

        if position.is_floor_breached():
            return {
                "action": "CLOSE",
                "ticker": position.ticker,
                "reason": "FLOOR_BREACH",
                "price": position.current_price,
                "tier": position.tier,
                "profit_pct": position.current_profit_pct,
            }

        scale = position.get_scale_action(self.config)
        if scale:
            return {
                "action": "SCALE",
                "ticker": position.ticker,
                "reason": f"SCALE_{scale['level']}",
                "level": scale["level"],
                "sell_qty": scale["sell_qty"],
                "sell_pct": scale["sell_pct"],
                "price": position.current_price,
                "profit_pct": position.current_profit_pct,
            }

        return None

    def check_exit(self, position, indicators: dict[str, float | bool]) -> dict[str, float | int | str] | None:
        if not indicators or not position:
            return None

        intrabar_signal = self.check_intrabar_exit(position)
        if intrabar_signal is not None:
            return intrabar_signal

        tier = position.tier
        exit_reason = None
        if tier == 1:
            if self._should_take_stoch_exit(indicators):
                exit_reason = "STOCHK_TIER1"
            elif bool(indicators["macd_cross_below"]):
                exit_reason = "MACD_BEAR_T1"
        elif tier == 2:
            if (
                self._should_take_stoch_exit(indicators)
                and not bool(indicators["price_above_ema9"])
            ):
                exit_reason = "STOCHK_TIER2"
            elif bool(indicators["macd_cross_below"]):
                exit_reason = "MACD_BEAR_T2"
        elif tier == 3:
            if bool(indicators["macd_cross_below"]):
                exit_reason = "MACD_BEAR_T3"

        if exit_reason is None:
            return None

        return {
            "action": "CLOSE",
            "ticker": position.ticker,
            "reason": exit_reason,
            "price": position.current_price,
            "tier": tier,
            "profit_pct": position.current_profit_pct,
        }

    def _should_take_stoch_exit(self, indicators: dict[str, float | bool]) -> bool:
        if not bool(indicators["stoch_k_below_exit"]) or not bool(indicators["stoch_k_falling"]):
            return False
        if not self.config.exit_stoch_health_filter_enabled:
            return True
        return not self._stoch_momentum_healthy(indicators)

    def _stoch_momentum_healthy(self, indicators: dict[str, float | bool]) -> bool:
        stoch_k = float(indicators.get("stoch_k", 0.0) or 0.0)
        stoch_k_prev = float(indicators.get("stoch_k_prev", 0.0) or 0.0)
        stoch_k_prev2 = float(indicators.get("stoch_k_prev2", 0.0) or 0.0)
        stoch_d = float(indicators.get("stoch_d", 0.0) or 0.0)
        slope = stoch_k - stoch_k_prev

        rising_two_bars = stoch_k > stoch_k_prev > stoch_k_prev2
        above_d = stoch_k > stoch_d
        slope_positive = slope >= self.config.exit_stoch_min_slope
        rolling_over_from_overbought = (
            stoch_k_prev >= self.config.exit_stoch_overbought_level and stoch_k < stoch_k_prev
        )

        return rising_two_bars and above_d and slope_positive and not rolling_over_from_overbought

    def check_hard_stop(self, position, current_price: float) -> dict[str, float | int | str] | None:
        stop_price = position.entry_price * (1 - self.config.stop_loss_pct / 100)
        if current_price <= stop_price:
            return {
                "action": "CLOSE",
                "ticker": position.ticker,
                "reason": "HARD_STOP",
                "price": current_price,
                "tier": position.tier,
                "profit_pct": position.current_profit_pct,
            }
        return None
