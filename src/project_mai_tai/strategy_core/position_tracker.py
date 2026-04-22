from __future__ import annotations

import csv
import json
import logging
from datetime import timedelta
from pathlib import Path

from project_mai_tai.strategy_core.time_utils import (
    now_eastern,
    now_eastern_str,
    session_day_eastern_str,
    today_eastern_str,
)
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
        self.entry_time = entry_time or now_eastern_str()
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
        self._ticker_loss_streaks: dict[str, int] = {}
        self._ticker_pause_until: dict[str, object] = {}
        self._ticker_entry_counts: dict[str, int] = {}
        self._ticker_hard_stop_streaks: dict[str, int] = {}
        self._ticker_hard_stop_pause_until: dict[str, object] = {}
        self._positions_file = positions_file
        self._closed_file_prefix = closed_file_prefix
        self._history_dir = history_dir

    def _preferred_history_dir(self) -> Path:
        configured = Path(self._history_dir)
        if configured.is_absolute():
            return configured

        cwd = Path.cwd()
        repo_relative = cwd / configured
        sibling_data_root = cwd.with_name(f"{cwd.name}-data")
        sibling_data_dir = sibling_data_root / configured.name

        if sibling_data_dir.exists() or sibling_data_root.exists():
            return sibling_data_dir
        return repo_relative

    def _history_dir_candidates(self) -> list[Path]:
        configured = Path(self._history_dir)
        preferred = self._preferred_history_dir()
        if configured.is_absolute():
            return [preferred]

        cwd = Path.cwd()
        repo_relative = cwd / configured
        candidates: list[Path] = []

        def add_candidate(path: Path) -> None:
            if path not in candidates:
                candidates.append(path)

        add_candidate(preferred)
        add_candidate(repo_relative)

        return candidates

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
        scale_profile: str = "NORMAL",
    ) -> Position:
        qty = quantity or self.config.default_quantity
        position = Position(
            ticker,
            entry_price,
            qty,
            path=path,
            scale_profile=scale_profile,
            floor_lock_at_1pct_peak_pct=self.config.profit_floor_lock_at_1pct_peak_pct,
            floor_lock_at_2pct_peak_pct=self.config.profit_floor_lock_at_2pct_peak_pct,
            floor_lock_at_3pct_peak_pct=self.config.profit_floor_lock_at_3pct_peak_pct,
            floor_trail_buffer_over_4pct_pct=self.config.profit_floor_trail_buffer_over_4pct_pct,
        )
        self._positions[ticker] = position
        normalized = str(ticker or "").upper()
        if normalized:
            self._ticker_entry_counts[normalized] = self._ticker_entry_counts.get(normalized, 0) + 1
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
            "scale_profile": position.scale_profile,
        }
        self._closed_today.append(result)
        self._record_ticker_outcome(ticker, total_pnl, position.peak_profit_pct, reason=reason)
        self._save_closed_trade(result)
        return result

    def can_open_position(self, ticker: str | None = None) -> tuple[bool, str]:
        if len(self._positions) >= self.config.max_positions:
            return False, f"max positions ({self.config.max_positions})"
        if self._daily_pnl <= self.config.max_daily_loss:
            return False, f"daily loss limit (${self._daily_pnl:.2f})"
        if ticker:
            entry_reason = self._ticker_entry_limit_reason(ticker)
            if entry_reason:
                return False, entry_reason
            pause_reason = self._ticker_pause_reason(ticker)
            if pause_reason:
                return False, pause_reason
            hard_stop_reason = self._ticker_hard_stop_pause_reason(ticker)
            if hard_stop_reason:
                return False, hard_stop_reason
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
            data = {"date": session_day_eastern_str(), "positions": {}}
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
                    "scale_profile": position.scale_profile,
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
            if raw.get("date") not in self._active_day_keys():
                return
            for ticker, pdata in raw.get("positions", {}).items():
                position = Position(
                    ticker=ticker,
                    entry_price=pdata["entry_price"],
                    quantity=pdata["quantity"],
                    entry_time=pdata.get("entry_time", ""),
                    path=pdata.get("entry_path", ""),
                    scale_profile=pdata.get("scale_profile", "NORMAL"),
                    floor_lock_at_1pct_peak_pct=self.config.profit_floor_lock_at_1pct_peak_pct,
                    floor_lock_at_2pct_peak_pct=self.config.profit_floor_lock_at_2pct_peak_pct,
                    floor_lock_at_3pct_peak_pct=self.config.profit_floor_lock_at_3pct_peak_pct,
                    floor_trail_buffer_over_4pct_pct=self.config.profit_floor_trail_buffer_over_4pct_pct,
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
        seen_rows: set[tuple[object, ...]] = set()
        for day_key in self._active_day_keys():
            for history_dir in self._history_dir_candidates():
                filepath = history_dir / f"{self._closed_file_prefix}_closed_{day_key}.csv"
                if not filepath.exists():
                    continue
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
                                "scale_profile": row.get("scale_profile", "NORMAL"),
                            }
                            row_key = (
                                day_key,
                                closed["ticker"],
                                closed["entry_time"],
                                closed["exit_time"],
                                closed["entry_price"],
                                closed["exit_price"],
                                closed["quantity"],
                                closed["reason"],
                                closed["path"],
                            )
                            if row_key in seen_rows:
                                continue
                            seen_rows.add(row_key)
                            self._closed_today.append(closed)
                            self._daily_pnl += float(closed["pnl"])
                            self._record_ticker_outcome(
                                closed["ticker"],
                                float(closed["pnl"]),
                                float(closed.get("peak_profit_pct", 0.0) or 0.0),
                                reason=str(closed.get("reason", "") or ""),
                            )
                            normalized_ticker = str(closed["ticker"]).upper()
                            if normalized_ticker:
                                self._ticker_entry_counts[normalized_ticker] = (
                                    self._ticker_entry_counts.get(normalized_ticker, 0) + 1
                                )
                except Exception:
                    logger.exception("Failed to load closed trades from %s", filepath)

    def reset(self) -> None:
        self._daily_pnl = 0.0
        self._closed_today.clear()
        self._ticker_loss_streaks.clear()
        self._ticker_pause_until.clear()
        self._ticker_entry_counts.clear()
        self._ticker_hard_stop_streaks.clear()
        self._ticker_hard_stop_pause_until.clear()

    def _save_closed_trade(self, closed: dict[str, object]) -> None:
        filepath = self._preferred_history_dir() / f"{self._closed_file_prefix}_closed_{session_day_eastern_str()}.csv"
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
            "scale_profile",
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

    @staticmethod
    def _active_day_keys() -> tuple[str, ...]:
        session_key = session_day_eastern_str()
        today_key = today_eastern_str()
        if today_key == session_key:
            return (session_key,)
        return (session_key, today_key)

    def _record_ticker_outcome(
        self,
        ticker: str,
        pnl: float,
        peak_profit_pct: float = 0.0,
        *,
        reason: str = "",
    ) -> None:
        normalized = str(ticker or "").upper()
        if not normalized:
            return
        normalized_reason = str(reason or "").strip().upper()
        if pnl >= 0:
            self._ticker_loss_streaks[normalized] = 0
            self._ticker_pause_until.pop(normalized, None)
            self._ticker_hard_stop_streaks[normalized] = 0
            self._ticker_hard_stop_pause_until.pop(normalized, None)
            return
        if self.config.hard_stop_pause_streak_limit > 0:
            if "HARD_STOP" in normalized_reason:
                hard_stop_streak = self._ticker_hard_stop_streaks.get(normalized, 0) + 1
                self._ticker_hard_stop_streaks[normalized] = hard_stop_streak
                if hard_stop_streak >= self.config.hard_stop_pause_streak_limit:
                    self._ticker_hard_stop_pause_until[normalized] = now_eastern() + timedelta(
                        minutes=self.config.hard_stop_pause_minutes
                    )
            else:
                self._ticker_hard_stop_streaks[normalized] = 0
                self._ticker_hard_stop_pause_until.pop(normalized, None)
        if self.config.ticker_loss_pause_streak_limit <= 0:
            return

        if (
            self.config.ticker_loss_pause_only_on_cold_losses
            and peak_profit_pct >= self.config.ticker_loss_pause_cold_peak_profit_pct
        ):
            self._ticker_loss_streaks[normalized] = 0
            self._ticker_pause_until.pop(normalized, None)
            return

        streak = self._ticker_loss_streaks.get(normalized, 0) + 1
        self._ticker_loss_streaks[normalized] = streak
        if streak >= self.config.ticker_loss_pause_streak_limit:
            self._ticker_pause_until[normalized] = now_eastern() + timedelta(
                minutes=self.config.ticker_loss_pause_minutes
            )

    def _ticker_entry_limit_reason(self, ticker: str) -> str:
        normalized = str(ticker or "").upper()
        limit = max(0, int(self.config.max_entries_per_symbol_per_session))
        if not normalized or limit <= 0:
            return ""
        count = self._ticker_entry_counts.get(normalized, 0)
        if count < limit:
            return ""
        return f"{normalized} reached session entry cap ({count}/{limit})"

    def _ticker_pause_reason(self, ticker: str) -> str:
        normalized = str(ticker or "").upper()
        if not normalized or self.config.ticker_loss_pause_streak_limit <= 0:
            return ""

        paused_until = self._ticker_pause_until.get(normalized)
        if paused_until is None:
            return ""

        now = now_eastern()
        if paused_until <= now:
            self._ticker_pause_until.pop(normalized, None)
            self._ticker_loss_streaks[normalized] = 0
            return ""

        minutes_left = max(1, int((paused_until - now).total_seconds() // 60))
        streak = self._ticker_loss_streaks.get(normalized, 0)
        return (
            f"{normalized} paused ({minutes_left} min left after "
            f"{streak} consecutive losses)"
        )

    def _ticker_hard_stop_pause_reason(self, ticker: str) -> str:
        normalized = str(ticker or "").upper()
        if not normalized or self.config.hard_stop_pause_streak_limit <= 0:
            return ""

        paused_until = self._ticker_hard_stop_pause_until.get(normalized)
        if paused_until is None:
            return ""

        now = now_eastern()
        if paused_until <= now:
            self._ticker_hard_stop_pause_until.pop(normalized, None)
            self._ticker_hard_stop_streaks[normalized] = 0
            return ""

        minutes_left = max(1, int((paused_until - now).total_seconds() // 60))
        streak = self._ticker_hard_stop_streaks.get(normalized, 0)
        return (
            f"{normalized} paused ({minutes_left} min left after "
            f"{streak} hard stops)"
        )
