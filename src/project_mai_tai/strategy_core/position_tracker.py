from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from project_mai_tai.strategy_core.time_utils import now_eastern_str, today_eastern_str
from project_mai_tai.strategy_core.trading_config import TradingConfig

logger = logging.getLogger(__name__)


class Position:
    def __init__(
        self,
        ticker: str,
        entry_price: float,
        quantity: int,
        entry_time: str = "",
        path: str = "",
    ):
        self.ticker = ticker
        self.entry_price = entry_price
        self.quantity = quantity
        self.original_quantity = quantity
        self.entry_time = entry_time or now_eastern_str()
        self.entry_path = path

        self.peak_profit_pct = 0.0
        self.current_profit_pct = 0.0
        self.current_price = entry_price

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
        }

    def _calculate_floor_pct(self) -> float:
        peak = self.peak_profit_pct
        if peak >= 4.0:
            return peak - 1.5
        if peak >= 3.0:
            return 1.5
        if peak >= 2.0:
            return 0.5
        if peak >= 1.0:
            return 0.0
        return -999.0


class PositionTracker:
    def __init__(
        self,
        config: TradingConfig,
        positions_file: str = "data/cache/positions_macd.json",
        closed_file_prefix: str = "macdbot",
        history_dir: str = "data/history",
    ):
        self.config = config
        self._positions: dict[str, Position] = {}
        self._daily_pnl = 0.0
        self._closed_today: list[dict[str, object]] = []
        self._positions_file = positions_file
        self._closed_file_prefix = closed_file_prefix
        self._history_dir = history_dir

    def has_position(self, ticker: str) -> bool:
        return ticker in self._positions

    def get_position(self, ticker: str) -> Position | None:
        return self._positions.get(ticker)

    def drop_position(self, ticker: str) -> Position | None:
        return self._positions.pop(ticker, None)

    def get_all_positions(self) -> list[dict[str, object]]:
        return [position.to_dict() for position in self._positions.values()]

    def get_position_count(self) -> int:
        return len(self._positions)

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_closed_today(self) -> list[dict[str, object]]:
        return list(self._closed_today)

    def open_position(
        self,
        ticker: str,
        entry_price: float,
        quantity: int = 0,
        path: str = "",
    ) -> Position:
        qty = quantity or self.config.default_quantity
        position = Position(ticker, entry_price, qty, path=path)
        self._positions[ticker] = position
        return position

    def close_position(self, ticker: str, exit_price: float, reason: str = "") -> dict[str, object] | None:
        position = self._positions.pop(ticker, None)
        if not position:
            return None

        close_pnl = (exit_price - position.entry_price) * position.quantity
        total_pnl = close_pnl + position.scale_pnl
        pnl_pct = (
            total_pnl / (position.entry_price * position.original_quantity) * 100
            if position.entry_price > 0
            else 0
        )
        self._daily_pnl += total_pnl

        result = {
            "ticker": ticker,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "quantity": position.quantity,
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "scale_pnl": round(position.scale_pnl, 2),
            "reason": reason,
            "entry_time": position.entry_time,
            "exit_time": now_eastern_str(),
            "peak_profit_pct": round(position.peak_profit_pct, 2),
            "tier": position.tier,
            "scales_done": list(position.scales_done),
            "path": position.entry_path,
        }
        self._closed_today.append(result)
        self._save_closed_trade(result)
        return result

    def can_open_position(self) -> tuple[bool, str]:
        if len(self._positions) >= self.config.max_positions:
            return False, f"max positions ({self.config.max_positions})"
        if self._daily_pnl <= self.config.max_daily_loss:
            return False, f"daily loss limit (${self._daily_pnl:.2f})"
        return True, ""

    def update_all_prices(self, price_map: dict[str, float]) -> None:
        for ticker, price in price_map.items():
            position = self._positions.get(ticker)
            if position:
                position.update_price(price)

    def increment_bars(self, ticker: str, count: int = 1) -> None:
        position = self._positions.get(ticker)
        if position:
            position.increment_bars(count)

    def save_positions(self, filepath: str | None = None) -> None:
        target = filepath or self._positions_file
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            data = {"date": today_eastern_str(), "positions": {}}
            for ticker, position in self._positions.items():
                data["positions"][ticker] = {
                    "entry_price": position.entry_price,
                    "entry_time": position.entry_time,
                    "quantity": position.quantity,
                    "original_quantity": position.original_quantity,
                    "peak_profit_pct": position.peak_profit_pct,
                    "tier": position.tier,
                    "floor_pct": position.floor_pct,
                    "floor_price": position.floor_price,
                    "scales_done": position.scales_done,
                    "bars_since_entry": position.bars_since_entry,
                    "last_exit_bar": position.last_exit_bar,
                    "entry_path": position.entry_path,
                }
            Path(target).write_text(json.dumps(data, indent=2))
        except Exception:
            logger.exception("Failed to save positions: %s", target)

    def load_positions(self, filepath: str | None = None) -> None:
        target = filepath or self._positions_file
        path = Path(target)
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            if raw.get("date") != today_eastern_str():
                return
            for ticker, pdata in raw.get("positions", {}).items():
                position = Position(
                    ticker=ticker,
                    entry_price=pdata["entry_price"],
                    quantity=pdata["quantity"],
                    entry_time=pdata.get("entry_time", ""),
                    path=pdata.get("entry_path", ""),
                )
                position.original_quantity = pdata.get("original_quantity", position.quantity)
                position.peak_profit_pct = pdata.get("peak_profit_pct", 0)
                position.tier = pdata.get("tier", 1)
                position.floor_pct = pdata.get("floor_pct", -999)
                position.floor_price = pdata.get("floor_price", 0)
                position.scales_done = pdata.get("scales_done", [])
                position.bars_since_entry = pdata.get("bars_since_entry", 0)
                position.last_exit_bar = pdata.get("last_exit_bar", -999)
                self._positions[ticker] = position
        except Exception:
            logger.exception("Failed to load positions: %s", target)

    def load_closed_trades(self) -> None:
        filepath = Path(self._history_dir) / f"{self._closed_file_prefix}_closed_{today_eastern_str()}.csv"
        if not filepath.exists():
            return
        try:
            with filepath.open("r", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    closed = {
                        "ticker": row.get("ticker", ""),
                        "entry_price": float(row.get("entry_price", 0) or 0),
                        "exit_price": float(row.get("exit_price", 0) or 0),
                        "quantity": int(float(row.get("quantity", 0) or 0)),
                        "pnl": float(row.get("pnl", 0) or 0),
                        "pnl_pct": float(row.get("pnl_pct", 0) or 0),
                        "reason": row.get("reason", ""),
                        "entry_time": row.get("entry_time", ""),
                        "exit_time": row.get("exit_time", ""),
                        "peak_profit_pct": float(row.get("peak_profit_pct", 0) or 0),
                        "tier": int(float(row.get("tier", 0) or 0)),
                        "scales_done": row.get("scales_done", "").split(",") if row.get("scales_done") else [],
                        "path": row.get("path", ""),
                    }
                    self._closed_today.append(closed)
                    self._daily_pnl += float(closed["pnl"])
        except Exception:
            logger.exception("Failed to load closed trades")

    def reset(self) -> None:
        self._daily_pnl = 0.0
        self._closed_today.clear()

    def _save_closed_trade(self, closed: dict[str, object]) -> None:
        filepath = Path(self._history_dir) / f"{self._closed_file_prefix}_closed_{today_eastern_str()}.csv"
        headers = [
            "ticker",
            "entry_price",
            "exit_price",
            "quantity",
            "pnl",
            "pnl_pct",
            "reason",
            "entry_time",
            "exit_time",
            "peak_profit_pct",
            "tier",
            "scales_done",
            "path",
        ]
        write_header = not filepath.exists()
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            with filepath.open("a", newline="") as handle:
                writer = csv.writer(handle)
                if write_header:
                    writer.writerow(headers)
                row = [closed.get(header, "") for header in headers]
                row[headers.index("scales_done")] = ",".join(closed.get("scales_done", []))
                writer.writerow(row)
        except Exception:
            logger.exception("Failed to save closed trade")
