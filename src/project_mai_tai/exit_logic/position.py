from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.exit_logic.config import TradingConfig

_EASTERN_TZ = ZoneInfo("America/New_York")


def _now_eastern_str() -> str:
    """Local replica of strategy_core.time_utils.now_eastern_str (identical output)
    so exit_logic stays a PURE LEAF — importing it must never pull in strategy_core,
    which would create a circular import (strategy_core/__init__ → position_tracker →
    exit_logic.position). Enforced by the leaf guard in test_exit_logic_parity.py.
    """
    return datetime.now(_EASTERN_TZ).strftime("%I:%M:%S %p ET")


class Position:
    def __init__(
        self,
        ticker: str,
        entry_price: float,
        quantity: int,
        entry_time: str = "",
        path: str = "",
        scale_profile: str = "NORMAL",
        floor_lock_at_1pct_peak_pct: float = 0.0,
        floor_lock_at_2pct_peak_pct: float = 0.5,
        floor_lock_at_3pct_peak_pct: float = 1.5,
        floor_trail_buffer_over_4pct_pct: float = 1.5,
    ):
        self.ticker = ticker
        self.entry_price = entry_price
        self.quantity = quantity
        self.original_quantity = quantity
        self.entry_time = entry_time or _now_eastern_str()
        self.entry_path = path
        self.scale_profile = str(scale_profile or "NORMAL").upper()

        self.peak_profit_pct = 0.0
        self.current_profit_pct = 0.0
        self.current_price = entry_price

        self.floor_lock_at_1pct_peak_pct = floor_lock_at_1pct_peak_pct
        self.floor_lock_at_2pct_peak_pct = floor_lock_at_2pct_peak_pct
        self.floor_lock_at_3pct_peak_pct = floor_lock_at_3pct_peak_pct
        self.floor_trail_buffer_over_4pct_pct = floor_trail_buffer_over_4pct_pct

        self.tier = 1
        self.floor_pct = -999.0
        self.floor_price = 0.0

        self.scales_done: list[str] = []
        self.scale_pnl = 0.0

        self.last_exit_bar = -999
        self.bars_since_entry = 0

    def update_price(self, price: float) -> None:
        self.current_price = price
        if self.entry_price <= 0:
            return

        self.current_profit_pct = (price - self.entry_price) / self.entry_price * 100
        if self.current_profit_pct > self.peak_profit_pct:
            self.peak_profit_pct = self.current_profit_pct

        if self.peak_profit_pct >= 3.0:
            self.tier = 3
        elif self.peak_profit_pct >= 1.0:
            self.tier = max(self.tier, 2)

        new_floor_pct = self._calculate_floor_pct()
        if new_floor_pct > self.floor_pct:
            self.floor_pct = new_floor_pct
            self.floor_price = self.entry_price * (1 + self.floor_pct / 100)

    def increment_bars(self, count: int = 1) -> None:
        self.bars_since_entry += count

    def is_floor_breached(self) -> bool:
        if self.floor_price <= 0:
            return False
        return self.current_price <= self.floor_price

    def get_scale_action(self, config: TradingConfig) -> dict[str, float | int | str] | None:
        profit = self.current_profit_pct
        qty = self.quantity
        if qty <= 0:
            return None

        if self.scale_profile == "DEGRADED":
            if profit >= config.scale_degraded1_pct and "PCT1" not in self.scales_done:
                sell_qty = max(1, int(qty * config.scale_degraded1_sell_pct / 100))
                return {"level": "PCT1", "sell_pct": config.scale_degraded1_sell_pct, "sell_qty": sell_qty}

            if profit >= config.scale_degraded2_pct and "PCT2" not in self.scales_done:
                sell_qty = max(1, int(qty * config.scale_degraded2_sell_pct / 100))
                return {"level": "PCT2", "sell_pct": config.scale_degraded2_sell_pct, "sell_qty": sell_qty}

            if profit >= config.scale_fast4_pct and "FAST4" not in self.scales_done:
                sell_qty = max(1, int(qty * config.scale_fast4_sell_pct / 100))
                return {"level": "FAST4", "sell_pct": config.scale_fast4_sell_pct, "sell_qty": sell_qty}

            return None

        if (
            profit >= config.scale_fast4_pct
            and "FAST4" not in self.scales_done
            and "PCT2" not in self.scales_done
        ):
            sell_qty = max(1, int(qty * config.scale_fast4_sell_pct / 100))
            return {"level": "FAST4", "sell_pct": config.scale_fast4_sell_pct, "sell_qty": sell_qty}

        if (
            profit >= config.scale_normal2_pct
            and "PCT2" not in self.scales_done
            and "FAST4" not in self.scales_done
        ):
            sell_qty = max(1, int(qty * config.scale_normal2_sell_pct / 100))
            return {"level": "PCT2", "sell_pct": config.scale_normal2_sell_pct, "sell_qty": sell_qty}

        if (
            profit >= config.scale_4after2_pct
            and "PCT2" in self.scales_done
            and "PCT4_AFTER2" not in self.scales_done
        ):
            sell_qty = max(1, int(qty * config.scale_4after2_sell_pct / 100))
            return {
                "level": "PCT4_AFTER2",
                "sell_pct": config.scale_4after2_sell_pct,
                "sell_qty": sell_qty,
            }

        return None

    def apply_scale(self, level: str, sell_qty: int, exit_price: float = 0) -> None:
        self.scales_done.append(level)
        if exit_price > 0:
            self.scale_pnl += (exit_price - self.entry_price) * sell_qty
        self.quantity -= sell_qty
        if self.quantity < 0:
            self.quantity = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time,
            "quantity": self.quantity,
            "original_quantity": self.original_quantity,
            "current_price": self.current_price,
            "current_profit_pct": round(self.current_profit_pct, 2),
            "peak_profit_pct": round(self.peak_profit_pct, 2),
            "tier": self.tier,
            "floor_pct": round(self.floor_pct, 2) if self.floor_pct > -999 else None,
            "floor_price": round(self.floor_price, 4) if self.floor_price > 0 else None,
            "scales_done": list(self.scales_done),
            "bars_since_entry": self.bars_since_entry,
            "scale_profile": self.scale_profile,
        }

    def _calculate_floor_pct(self) -> float:
        peak = self.peak_profit_pct
        if peak >= 4.0:
            return peak - self.floor_trail_buffer_over_4pct_pct
        if peak >= 3.0:
            return self.floor_lock_at_3pct_peak_pct
        if peak >= 2.0:
            return self.floor_lock_at_2pct_peak_pct
        if peak >= 1.0:
            return self.floor_lock_at_1pct_peak_pct
        return -999.0
