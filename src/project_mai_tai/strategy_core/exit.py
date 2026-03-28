from __future__ import annotations

import logging

from project_mai_tai.strategy_core.trading_config import TradingConfig

logger = logging.getLogger(__name__)


class ExitEngine:
    def __init__(self, config: TradingConfig):
        self.config = config

    def check_exit(self, position, indicators: dict[str, float | bool]) -> dict[str, float | int | str] | None:
        if not indicators or not position:
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

        tier = position.tier
        exit_reason = None
        if tier == 1:
            if bool(indicators["stoch_k_below_exit"]) and bool(indicators["stoch_k_falling"]):
                exit_reason = "STOCHK_TIER1"
            elif bool(indicators["macd_cross_below"]):
                exit_reason = "MACD_BEAR_T1"
        elif tier == 2:
            if (
                bool(indicators["stoch_k_below_exit"])
                and bool(indicators["stoch_k_falling"])
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
