from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
import logging
from math import isclose

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
        self._pending: dict[str, dict[str, object]] = {}
        self._probe_state: dict[str, dict[str, object]] = {}
        self._probe_fail_bar: dict[str, int] = {}
        self._last_buy_bar: dict[str, int] = {}
        self._last_exit_bar: dict[str, int] = {}
        self._last_decision: dict[str, dict[str, str]] = {}
        self._recent_bars: dict[str, list[dict[str, float]]] = {}
        self._session_highs: dict[str, float] = {}

    def seed_recent_bars(
        self,
        ticker: str,
        indicators_history: Sequence[dict[str, float | bool]],
    ) -> None:
        recent: list[dict[str, float]] = []
        session_high = 0.0
        for indicators in indicators_history:
            snapshot = self._recent_bar_snapshot(indicators, bar_index=len(recent))
            if snapshot is None:
                continue
            session_high = max(session_high, snapshot["high"])
            recent.append(snapshot)

        if not recent:
            self._recent_bars.pop(ticker, None)
            self._session_highs.pop(ticker, None)
            return

        max_keep = max(
            24,
            self.config.entry_precondition_lookback_bars + 2,
            self.config.pretrigger_reclaim_lookback_bars + 2,
            self.config.pretrigger_reclaim_reentry_touch_lookback_bars + 2,
        )
        self._recent_bars[ticker] = recent[-max_keep:]
        self._session_highs[ticker] = session_high

    def check_entry(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, float | int | str] | None:
        if not indicators:
            return None
        if self.config.entry_logic_mode in {"pretrigger_probe", "pretrigger_reclaim", "pretrigger_retest"}:
            try:
                return self._check_pretrigger_entry(ticker, indicators, bar_index, position_tracker)
            finally:
                self._remember_bar(ticker, indicators, bar_index)

        try:
            if ticker in self._pending:
                gate_result = self._check_confirmation_guards(ticker, bar_index, position_tracker)
                if not gate_result["passed"]:
                    logger.info("[%s] %s confirmation CANCELLED: %s", self.name, ticker, gate_result["reason"])
                    del self._pending[ticker]
                    self._record_decision(ticker, status="blocked", reason=str(gate_result["reason"]))
                    return None
                return self._check_confirmation(ticker, indicators, bar_index)

            gate_result = self._check_hard_gates(ticker, indicators, bar_index, position_tracker)
            if not gate_result["passed"]:
                if ticker in self._pending:
                    logger.info("[%s] %s confirmation CANCELLED: %s", self.name, ticker, gate_result["reason"])
                    del self._pending[ticker]
                self._record_decision(ticker, status="blocked", reason=str(gate_result["reason"]))
                return None

            path = self._check_paths(ticker, indicators)
            if path is None:
                self._record_decision(ticker, status="idle", reason="no entry path matched")
                return None

            setup_result = self._check_setup_quality(ticker, path, indicators)
            if not setup_result["passed"]:
                self._record_decision(
                    ticker,
                    status="blocked",
                    reason=str(setup_result["reason"]),
                    path=path,
                    score=int(setup_result.get("score", 0) or 0),
                    score_details=str(setup_result.get("score_details", "")),
                )
                return None

            score = int(setup_result["score"])
            score_details = str(setup_result["score_details"])
            required_score = int(setup_result["required_score"])
            breakout_level = float(setup_result["breakout_level"])
            breakout_floor = breakout_level * (1.0 - self.config.confirmation_hold_tolerance_pct)

            if self.config.confirm_bars <= 0:
                self._last_buy_bar[ticker] = bar_index
                if self.config.min_score <= 0:
                    score = 0
                    score_details = "no_score"
                self._record_decision(
                    ticker,
                    status="signal",
                    reason=path,
                    path=path,
                    score=score,
                    score_details=score_details,
                )
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
                "trigger_score": score,
                "trigger_score_details": score_details,
                "required_score": required_score,
                "breakout_level": breakout_level,
                "breakout_floor": breakout_floor,
                "path": path,
                "bars_waiting": 0,
            }
            self._record_decision(ticker, status="pending", reason=f"{path} waiting confirmation", path=path)
            logger.info(
                "[%s] %s - %s triggered @ $%.4f | waiting %s bars",
                self.name,
                ticker,
                path,
                float(indicators["price"]),
                self.config.confirm_bars,
            )
            return None
        finally:
            self._remember_bar(ticker, indicators, bar_index)

    def record_exit(self, ticker: str, bar_index: int) -> None:
        self._last_exit_bar[ticker] = bar_index
        self._probe_state.pop(ticker, None)

    def cancel_pending(self, ticker: str) -> None:
        self._pending.pop(ticker, None)
        self._probe_state.pop(ticker, None)

    def pop_last_decision(self, ticker: str) -> dict[str, str] | None:
        return self._last_decision.pop(ticker, None)

    def reset(self) -> None:
        self._pending.clear()
        self._probe_state.clear()
        self._probe_fail_bar.clear()
        self._last_buy_bar.clear()
        self._last_exit_bar.clear()
        self._last_decision.clear()
        self._recent_bars.clear()
        self._session_highs.clear()

    def prune_tickers(self, keep: set[str]) -> None:
        self._pending = {
            ticker: payload
            for ticker, payload in self._pending.items()
            if ticker in keep
        }
        self._probe_state = {
            ticker: payload
            for ticker, payload in self._probe_state.items()
            if ticker in keep
        }
        self._probe_fail_bar = {
            ticker: bar
            for ticker, bar in self._probe_fail_bar.items()
            if ticker in keep
        }
        self._last_buy_bar = {
            ticker: bar
            for ticker, bar in self._last_buy_bar.items()
            if ticker in keep
        }
        self._last_exit_bar = {
            ticker: bar
            for ticker, bar in self._last_exit_bar.items()
            if ticker in keep
        }
        self._last_decision = {
            ticker: payload
            for ticker, payload in self._last_decision.items()
            if ticker in keep
        }
        self._recent_bars = {
            ticker: bars
            for ticker, bars in self._recent_bars.items()
            if ticker in keep
        }
        self._session_highs = {
            ticker: high
            for ticker, high in self._session_highs.items()
            if ticker in keep
        }

    def _check_pretrigger_entry(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, float | int | str] | None:
        probe_state = self._probe_state.get(ticker)
        has_any_position = bool(position_tracker and position_tracker.has_position(ticker))
        has_filled_position = self._has_filled_position(position_tracker, ticker)

        if probe_state and bool(probe_state.get("armed_only", False)) and not has_any_position and not has_filled_position:
            if self.config.entry_logic_mode == "pretrigger_retest":
                armed_signal = self._check_retest_armed_entry(ticker, probe_state, indicators, bar_index)
            else:
                armed_signal = self._check_reclaim_armed_entry(ticker, probe_state, indicators, bar_index)
            if armed_signal is not None:
                return armed_signal
            if ticker in self._probe_state:
                return None
            probe_state = None

        if probe_state and not has_any_position and not has_filled_position:
            self._probe_state.pop(ticker, None)
            probe_state = None

        if probe_state is not None and has_filled_position:
            exit_signal = self._check_pretrigger_failures(ticker, probe_state, indicators, bar_index)
            if exit_signal is not None:
                return exit_signal

            add_signal = self._check_pretrigger_confirmation(ticker, probe_state, indicators, bar_index, position_tracker)
            if add_signal is not None:
                return add_signal
            return None

        gate_result = self._check_pretrigger_gates(ticker, indicators, bar_index, position_tracker)
        if not gate_result["passed"]:
            self._record_decision(ticker, status="blocked", reason=str(gate_result["reason"]))
            return None

        candidate = self._build_pretrigger_candidate(ticker, indicators, position_tracker)
        if not candidate["passed"]:
            self._record_decision(
                ticker,
                status="blocked",
                reason=str(candidate["reason"]),
                score=int(candidate.get("score", 0) or 0),
                score_details=str(candidate.get("score_details", "")),
            )
            return None

        starter_qty = self._starter_quantity()
        if starter_qty <= 0:
            self._record_decision(ticker, status="blocked", reason="pretrigger starter quantity resolved to zero")
            return None

        resistance_level = float(candidate["resistance_level"])
        starter_path = self._pretrigger_starter_path()
        hold_floor = float(candidate.get("hold_floor", 0) or 0)
        if hold_floor <= 0:
            hold_floor = resistance_level - self.config.pretrigger_fail_hold_buf_atr * float(candidate["effective_atr"])
        if bool(candidate.get("armed_only", False)):
            self._probe_state[ticker] = {
                "armed_only": True,
                "armed_bar": bar_index,
                "armed_lookahead_bars": int(
                    candidate.get(
                        "armed_lookahead_bars",
                        self.config.pretrigger_reclaim_arm_break_lookahead_bars,
                    )
                ),
                "starter_qty": starter_qty,
                "remaining_qty": self._confirm_add_quantity(starter_qty),
                "arm_trigger_price": float(candidate.get("arm_trigger_price", resistance_level)),
                "hold_floor": hold_floor,
                "effective_atr": float(candidate.get("effective_atr", 0) or 0),
                "resistance_level": resistance_level,
                "retest_level": float(candidate.get("retest_level", resistance_level)),
                "pretrigger_score": int(candidate["score"]),
                "pretrigger_score_details": str(candidate["score_details"]),
                "confirmed": False,
                "confirm_reason": "",
                "starter_high": float(indicators.get("high", indicators["price"]) or indicators["price"]),
            }
            self._record_decision(
                ticker,
                status="pending",
                reason=str(candidate.get("armed_reason", "PRETRIGGER_RECLAIM_ARMED")),
                path=str(candidate.get("armed_path", "PRETRIGGER_RECLAIM_ARMED")),
                score=int(candidate["score"]),
                score_details=str(candidate["score_details"]),
            )
            return None
        self._probe_state[ticker] = {
            "entry_bar": bar_index,
            "starter_qty": starter_qty,
            "remaining_qty": self._confirm_add_quantity(starter_qty),
            "probe_entry_price": float(indicators["price"]),
            "resistance_level": resistance_level,
            "hold_floor": hold_floor,
            "pretrigger_score": int(candidate["score"]),
            "pretrigger_score_details": str(candidate["score_details"]),
            "confirmed": False,
            "confirm_reason": "",
            "starter_high": float(indicators.get("high", indicators["price"]) or indicators["price"]),
        }
        self._record_decision(
            ticker,
            status="signal",
            reason=starter_path,
            path=starter_path,
            score=int(candidate["score"]),
            score_details=str(candidate["score_details"]),
        )
        return self._build_buy_signal(
            ticker,
            starter_path,
            indicators,
            int(candidate["score"]),
            str(candidate["score_details"]),
            quantity=starter_qty,
            stage="starter",
        )

    def _check_pretrigger_gates(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, str | bool]:
        del indicators
        eastern_now = self.now_provider()
        if eastern_now.hour < self.config.trading_start_hour or eastern_now.hour >= self.config.trading_end_hour:
            return {"passed": False, "reason": f"outside trading hours ({eastern_now.hour}:00 ET)"}

        time_str = eastern_now.strftime("%H:%M")
        if self.config.dead_zone_start <= time_str < self.config.dead_zone_end:
            return {"passed": False, "reason": f"in dead zone ({time_str} ET)"}

        last_exit = self._last_exit_bar.get(ticker, -999)
        if last_exit >= 0:
            bars_since_exit = bar_index - last_exit
            if bars_since_exit < self.config.cooldown_bars:
                return {"passed": False, "reason": f"cooldown ({bars_since_exit}/{self.config.cooldown_bars} bars)"}

        if position_tracker and position_tracker.has_position(ticker):
            return {"passed": False, "reason": "already in position"}

        if self._last_buy_bar.get(ticker, -1) == bar_index:
            return {"passed": False, "reason": "dedup (already fired this bar)"}

        last_fail_bar = self._probe_fail_bar.get(ticker)
        if last_fail_bar is not None:
            if bar_index <= last_fail_bar + self.config.pretrigger_fail_cooldown_bars:
                return {
                    "passed": False,
                    "reason": f"pretrigger fail cooldown ({bar_index - last_fail_bar}/{self.config.pretrigger_fail_cooldown_bars} bars)",
                }

        return {"passed": True, "reason": ""}

    def _build_pretrigger_candidate(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        position_tracker=None,
    ) -> dict[str, object]:
        if self.config.entry_logic_mode == "pretrigger_reclaim":
            return self._build_reclaim_candidate(ticker, indicators, position_tracker)
        if self.config.entry_logic_mode == "pretrigger_retest":
            return self._build_retest_candidate(ticker, indicators, position_tracker)

        recent = self._recent_bars.get(ticker, [])
        lookback = self.config.pretrigger_lookback_compression_bars
        if len(recent) < max(lookback, 14):
            return {"passed": False, "reason": f"pretrigger warmup ({len(recent)}/{max(lookback, 14)} bars)"}

        comp_window = recent[-lookback:]
        atr14 = self._average_true_range(recent[-14:])
        price_floor_atr = float(indicators["price"]) * self.config.pretrigger_atr_floor_pct
        effective_atr = max(atr14, price_floor_atr)
        if atr14 <= 0:
            return {"passed": False, "reason": "pretrigger ATR unavailable"}

        def _compression_range(window: list[dict[str, float]]) -> tuple[float, float, float]:
            highs = sorted(float(bar["high"]) for bar in window)
            lows = sorted(float(bar["low"]) for bar in window)
            trim = min(self.config.pretrigger_compression_trim_extremes, max(0, len(window) - 2))
            if trim > 0:
                highs = highs[:-trim]
                lows = lows[trim:]
            comp_high_local = max(highs) if highs else max(float(bar["high"]) for bar in window)
            comp_low_local = min(lows) if lows else min(float(bar["low"]) for bar in window)
            comp_range_local = (comp_high_local - comp_low_local) / effective_atr if effective_atr > 0 else float("inf")
            return comp_high_local, comp_low_local, comp_range_local

        body_highs = [max(float(bar["open"]), float(bar["close"])) for bar in comp_window]
        comp_high, comp_low, comp_range_atr = _compression_range(comp_window)
        compression_ok = comp_range_atr <= self.config.pretrigger_max_compression_range_atr
        min_comp_bars = min(max(2, self.config.pretrigger_compression_min_bars), len(comp_window))
        if not compression_ok and min_comp_bars < len(comp_window):
            for start in range(0, len(comp_window) - min_comp_bars + 1):
                sub_high, sub_low, sub_range_atr = _compression_range(comp_window[start : start + min_comp_bars])
                if sub_range_atr <= self.config.pretrigger_max_compression_range_atr:
                    compression_ok = True
                    comp_range_atr = sub_range_atr
                    break

        higher_lows_count = 0
        upper_half_close_count = 0
        body_near_resistance_count = 0
        for previous, current in zip(comp_window[:-1], comp_window[1:]):
            if float(current["low"]) > float(previous["low"]) and float(current["close"]) >= float(current["open"]):
                higher_lows_count += 1
        resistance_level = max(body_highs)
        for bar in comp_window:
            bar_high = float(bar["high"])
            bar_low = float(bar["low"])
            bar_range = max(bar_high - bar_low, 0.000001)
            bar_close = float(bar["close"])
            bar_open = float(bar["open"])
            bar_close_pos = (bar_close - bar_low) / bar_range
            bar_body_high = max(bar_open, bar_close)
            if bar_close_pos >= self.config.pretrigger_pressure_close_pos_pct:
                upper_half_close_count += 1
            if bar_body_high >= resistance_level - (self.config.pretrigger_body_near_resistance_atr_factor * effective_atr):
                body_near_resistance_count += 1
        price_near_resistance = (
            float(indicators["price"]) >= resistance_level - (self.config.pretrigger_price_near_resistance_atr_factor * effective_atr)
            and resistance_level > float(comp_window[-1]["close"])
        )
        pressure_ok = (
            higher_lows_count >= self.config.pretrigger_min_higher_lows_count
            and upper_half_close_count >= self.config.pretrigger_pressure_min_upper_half_closes
            and body_near_resistance_count >= self.config.pretrigger_pressure_min_body_near_resistance_bars
            and price_near_resistance
        )

        selected_vwap = self._selected_vwap_value(indicators)
        ema9 = float(indicators.get("ema9", 0) or 0)
        dist_to_ema9_pct = self._pct_distance(float(indicators["price"]), ema9)
        ema9_reclaim_floor = ema9 * (1.0 - self.config.pretrigger_max_pullback_below_ema9_pct) if ema9 > 0 else 0.0
        support_ok = (
            float(indicators["price"]) >= ema9_reclaim_floor
            and float(indicators["price"]) > selected_vwap
            and dist_to_ema9_pct <= self.config.pretrigger_max_pullback_to_ema9_pct
        )

        macd_now = float(indicators.get("macd", 0) or 0)
        macd_prev = float(indicators.get("macd_prev", 0) or 0)
        signal_now = float(indicators.get("signal", 0) or 0)
        histogram_now = float(indicators.get("histogram", 0) or 0)
        histogram_prev = float(indicators.get("histogram_prev", 0) or 0)
        hist_rising = histogram_now > histogram_prev
        hist_prev2 = float(recent[-2].get("histogram", 0) or 0)
        hist_prev1 = float(recent[-1].get("histogram", 0) or 0)
        hist_improving3 = hist_prev2 < hist_prev1 < histogram_now
        macd_near_signal = (
            abs(macd_now - signal_now)
            <= effective_atr * self.config.pretrigger_macd_near_signal_atr_factor
        )
        macd_above_signal = macd_now > signal_now
        histogram_positive = histogram_now > 0.0
        early_momentum_ok = macd_near_signal and (
            hist_improving3 or (
            hist_rising and macd_now >= macd_prev
            )
        )

        bar_range = max(float(indicators["high"]) - float(indicators["low"]), 0.000001)
        current_open = float(indicators.get("open", indicators.get("price", 0)) or 0)
        current_price = float(indicators["price"])
        body_pct = abs(current_price - current_open) / bar_range
        close_pos_pct = (current_price - float(indicators["low"])) / bar_range
        upper_wick_pct = (float(indicators["high"]) - max(current_open, current_price)) / bar_range
        candle_ok = (
            current_price > current_open
            and body_pct >= self.config.pretrigger_min_body_pct
            and close_pos_pct >= self.config.pretrigger_min_close_pos_pct
            and upper_wick_pct <= self.config.pretrigger_max_upper_wick_pct
        )

        volume_avg_bars = min(len(recent), self.config.pretrigger_volume_avg_bars)
        avg_vol = sum(float(bar["volume"]) for bar in recent[-volume_avg_bars:]) / float(volume_avg_bars)
        bar_rel_vol = float(indicators["volume"]) / avg_vol if avg_vol > 0 else 0.0
        volume_ok = bar_rel_vol >= self.config.pretrigger_min_bar_rel_vol
        stoch_ok = float(indicators.get("stoch_k", 0) or 0) < self.config.stoch_entry_cap

        ema20_recent = [float(bar["ema20"]) for bar in recent[-3:]]
        trend_ok = (
            current_price > float(indicators.get("ema20", 0) or 0)
            and (
                ema9 >= float(indicators.get("ema20", 0) or 0)
                or current_price > resistance_level - (0.05 * effective_atr)
            )
            and (
                ema20_recent[-1] >= ema20_recent[0]
                or current_price > resistance_level
            )
            and current_price > selected_vwap
        )

        can_open = True
        risk_reason = ""
        if position_tracker is not None and hasattr(position_tracker, "positions"):
            can_open, risk_reason = position_tracker.positions.can_open_position(ticker)

        score = sum(
            1
            for passed in (
                pressure_ok,
                early_momentum_ok,
                volume_ok,
            )
            if passed
        )
        score_details = " ".join(
            [
                f"comp={'+' if compression_ok else '-'}",
                f"press={'+' if pressure_ok else '-'}",
                f"loc={'+' if support_ok else '-'}",
                f"hist={'+' if histogram_positive else '-'}",
                f"macd={'+' if macd_above_signal else '-'}",
                f"mom={'+' if early_momentum_ok else '-'}",
                f"stoch={'+' if stoch_ok else '-'}",
                f"candle={'+' if candle_ok else '-'}",
                f"vol={'+' if volume_ok else '-'}",
            ]
        )
        if not can_open:
            return {"passed": False, "reason": risk_reason, "score": score, "score_details": score_details}
        if not compression_ok:
            return {
                "passed": False,
                "reason": "pretrigger compression not ready",
                "score": score,
                "score_details": score_details,
            }
        if not support_ok:
            return {
                "passed": False,
                "reason": "pretrigger location not ready",
                "score": score,
                "score_details": score_details,
            }
        if not candle_ok:
            return {
                "passed": False,
                "reason": "pretrigger candle not ready",
                "score": score,
                "score_details": score_details,
            }
        if not pressure_ok:
            return {
                "passed": False,
                "reason": "pretrigger pressure not ready",
                "score": score,
                "score_details": score_details,
            }
        if not histogram_positive:
            return {
                "passed": False,
                "reason": "pretrigger histogram not ready",
                "score": score,
                "score_details": score_details,
            }
        if not macd_above_signal:
            return {
                "passed": False,
                "reason": "pretrigger MACD below signal",
                "score": score,
                "score_details": score_details,
            }
        if not stoch_ok:
            return {
                "passed": False,
                "reason": f"pretrigger stochK at or above cap ({self.config.stoch_entry_cap:.0f})",
                "score": score,
                "score_details": score_details,
            }
        if not volume_ok:
            return {
                "passed": False,
                "reason": "pretrigger volume not ready",
                "score": score,
                "score_details": score_details,
            }
        if not early_momentum_ok:
            return {
                "passed": False,
                "reason": "pretrigger momentum not ready",
                "score": score,
                "score_details": score_details,
            }
        if not trend_ok:
            return {"passed": False, "reason": "pretrigger trend not ready", "score": score, "score_details": score_details}
        if score < self.config.pretrigger_score_threshold:
            return {
                "passed": False,
                "reason": f"pretrigger score {score} below required {self.config.pretrigger_score_threshold}",
                "score": score,
                "score_details": score_details,
            }

        return {
            "passed": True,
            "reason": "",
            "score": score,
            "score_details": score_details,
            "atr14": atr14,
            "effective_atr": effective_atr,
            "resistance_level": resistance_level,
        }

    def _build_retest_candidate(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        position_tracker=None,
    ) -> dict[str, object]:
        recent = self._recent_bars.get(ticker, [])
        lookback = self.config.pretrigger_retest_lookback_bars
        warmup_bars = max(lookback, 14)
        if len(recent) < warmup_bars:
            return {"passed": False, "reason": f"pretrigger warmup ({len(recent)}/{warmup_bars} bars)"}

        breakout_window_bars = min(len(recent), max(2, self.config.pretrigger_retest_breakout_window_bars))
        setup_window = recent[-lookback:]
        breakout_window = setup_window[-breakout_window_bars:]
        prior_window = setup_window[:-breakout_window_bars]
        if len(prior_window) < 2:
            return {"passed": False, "reason": "pretrigger retest breakout history unavailable"}

        atr14 = self._average_true_range(recent[-14:])
        current_price = float(indicators["price"])
        price_floor_atr = current_price * self.config.pretrigger_atr_floor_pct
        effective_atr = max(atr14, price_floor_atr)
        if atr14 <= 0:
            return {"passed": False, "reason": "pretrigger ATR unavailable"}

        retest_level = max(max(float(bar["open"]), float(bar["close"])) for bar in prior_window)
        breakout_level = retest_level * (1.0 + self.config.pretrigger_retest_min_breakout_pct)
        average_prior_range = sum(max(float(bar["high"]) - float(bar["low"]), 0.0) for bar in prior_window) / float(len(prior_window))
        breakout_close_floor = breakout_level * (1.0 - self.config.pretrigger_retest_breakout_close_tolerance_pct)
        breakout_candidates: list[tuple[int, float, dict[str, float | bool]]] = []
        for index, bar in enumerate(breakout_window):
            breakout_high = float(bar["high"])
            breakout_close = float(bar["close"])
            breakout_open = float(bar["open"])
            breakout_low = float(bar["low"])
            breakout_range = max(breakout_high - breakout_low, 0.000001)
            breakout_close_pos = (breakout_close - breakout_low) / breakout_range
            breakout_range_expansion = breakout_range / max(average_prior_range, 0.000001)
            breakout_passed = (
                breakout_high >= breakout_level
                and breakout_close >= breakout_close_floor
                and breakout_close_pos >= self.config.pretrigger_retest_breakout_min_close_pos_pct
                and breakout_range_expansion >= self.config.pretrigger_retest_breakout_min_range_expansion
                and breakout_close > breakout_open
            )
            if breakout_passed:
                breakout_score = breakout_close_pos + breakout_range_expansion + (breakout_close / max(breakout_level, 0.000001))
                breakout_candidates.append((index, breakout_score, bar))
        breakout_bar = max(breakout_candidates, key=lambda item: (item[1], item[0]))[2] if breakout_candidates else None
        if breakout_bar is None:
            return {
                "passed": False,
                "reason": "pretrigger retest breakout not ready",
                "score": 0,
                "score_details": "mode=retest breakout=-",
            }

        current_open = float(indicators.get("open", current_price) or current_price)
        current_high = float(indicators.get("high", current_price) or current_price)
        current_low = float(indicators.get("low", current_price) or current_price)
        current_volume = float(indicators.get("volume", 0) or 0)
        ema9 = float(indicators.get("ema9", 0) or 0)
        ema20 = float(indicators.get("ema20", 0) or 0)
        selected_vwap = self._selected_vwap_value(indicators)
        touch_tol = self.config.pretrigger_retest_level_tolerance_pct

        breakout_high = float(breakout_bar["high"])
        pullback_from_breakout_pct = max(0.0, (breakout_high - current_price) / breakout_high) if breakout_high > 0 else 0.0
        retest_touch_ok = current_low <= retest_level * (1.0 + touch_tol)
        holds_level_ok = current_price >= retest_level * (1.0 - touch_tol)
        shallow_pullback_ok = pullback_from_breakout_pct <= self.config.pretrigger_retest_max_pullback_from_breakout_pct

        bar_range = max(current_high - current_low, 0.000001)
        body_pct = abs(current_price - current_open) / bar_range
        close_pos_pct = (current_price - current_low) / bar_range
        upper_wick_pct = (current_high - max(current_open, current_price)) / bar_range
        candle_ok = (
            current_price > current_open
            and body_pct >= self.config.pretrigger_retest_min_body_pct
            and close_pos_pct >= self.config.pretrigger_retest_min_close_pos_pct
            and upper_wick_pct <= self.config.pretrigger_retest_max_upper_wick_pct
        )

        bar_rel_vol = self._current_bar_rel_vol(ticker, current_volume)
        breakout_volume_ok = self._current_bar_rel_vol(ticker, float(breakout_bar.get("volume", 0) or 0)) >= self.config.pretrigger_retest_min_breakout_bar_rel_vol
        volume_ok = bar_rel_vol >= self.config.pretrigger_retest_min_bar_rel_vol

        macd_now = float(indicators.get("macd", 0) or 0)
        macd_prev = float(indicators.get("macd_prev", 0) or 0)
        signal_now = float(indicators.get("signal", 0) or 0)
        histogram_now = float(indicators.get("histogram", 0) or 0)
        histogram_prev = float(indicators.get("histogram_prev", 0) or 0)
        macd_near_signal = abs(macd_now - signal_now) <= effective_atr * self.config.pretrigger_macd_near_signal_atr_factor
        momentum_ok = histogram_now > 0.0 and histogram_now >= histogram_prev and (macd_now > signal_now or macd_near_signal) and macd_now >= macd_prev

        dual_anchor_ok = current_price >= ema9 and current_price >= selected_vwap if ema9 > 0 and selected_vwap > 0 else False
        trend_ok = current_price > ema20 and ema9 >= ema20 and (not self.config.pretrigger_retest_require_dual_anchor or dual_anchor_ok)

        can_open = True
        risk_reason = ""
        if position_tracker is not None and hasattr(position_tracker, "positions"):
            can_open, risk_reason = position_tracker.positions.can_open_position(ticker)

        score = sum(1 for passed in (breakout_volume_ok, momentum_ok, volume_ok) if passed)
        score_details = " ".join(
            [
                "mode=retest",
                f"breakout={'+' if breakout_bar is not None else '-'}",
                f"breakout_close={'+' if float(breakout_bar['close']) >= breakout_close_floor else '-'}",
                f"touch={'+' if retest_touch_ok else '-'}",
                f"hold={'+' if holds_level_ok else '-'}",
                f"pullback={'+' if shallow_pullback_ok else '-'}",
                f"candle={'+' if candle_ok else '-'}",
                f"vol={'+' if volume_ok else '-'}",
                f"breakout_vol={'+' if breakout_volume_ok else '-'}",
                f"mom={'+' if momentum_ok else '-'}",
                f"trend={'+' if trend_ok else '-'}",
            ]
        )
        if not can_open:
            return {"passed": False, "reason": risk_reason, "score": score, "score_details": score_details}
        if not retest_touch_ok:
            return {"passed": False, "reason": "pretrigger retest touch not ready", "score": score, "score_details": score_details}
        if not holds_level_ok:
            return {"passed": False, "reason": "pretrigger retest hold not ready", "score": score, "score_details": score_details}
        if not shallow_pullback_ok:
            return {"passed": False, "reason": "pretrigger retest pullback too deep", "score": score, "score_details": score_details}
        if not candle_ok:
            return {"passed": False, "reason": "pretrigger retest candle not ready", "score": score, "score_details": score_details}
        if self.config.pretrigger_retest_require_volume and not volume_ok:
            return {"passed": False, "reason": "pretrigger retest volume not ready", "score": score, "score_details": score_details}
        if not breakout_volume_ok:
            return {"passed": False, "reason": "pretrigger retest breakout volume not ready", "score": score, "score_details": score_details}
        if self.config.pretrigger_retest_require_momentum and not momentum_ok:
            return {"passed": False, "reason": "pretrigger retest momentum not ready", "score": score, "score_details": score_details}
        if self.config.pretrigger_retest_require_trend and not trend_ok:
            return {"passed": False, "reason": "pretrigger retest trend not ready", "score": score, "score_details": score_details}
        if score < self.config.pretrigger_retest_score_threshold:
            return {
                "passed": False,
                "reason": f"pretrigger retest score {score} below required {self.config.pretrigger_retest_score_threshold}",
                "score": score,
                "score_details": score_details,
            }

        hold_floor = max(
            retest_level * (1.0 - self.config.pretrigger_retest_level_tolerance_pct),
            selected_vwap if selected_vwap > 0 else 0.0,
            ema9 * (1.0 - self.config.pretrigger_max_pullback_below_ema9_pct) if ema9 > 0 else 0.0,
        ) - (self.config.pretrigger_fail_hold_buf_atr * effective_atr)

        return {
            "passed": True,
            "reason": "",
            "score": score,
            "score_details": score_details,
            "atr14": atr14,
            "effective_atr": effective_atr,
            "resistance_level": breakout_high,
            "retest_level": retest_level,
            "hold_floor": hold_floor,
            "armed_only": True,
            "armed_lookahead_bars": self.config.pretrigger_retest_arm_break_lookahead_bars,
            "arm_trigger_price": current_high,
            "armed_reason": "PRETRIGGER_RETEST_ARMED",
            "armed_path": "PRETRIGGER_RETEST_ARMED",
        }

    def _build_reclaim_candidate(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        position_tracker=None,
    ) -> dict[str, object]:
        recent = self._recent_bars.get(ticker, [])
        lookback = self.config.pretrigger_reclaim_lookback_bars
        if len(recent) < max(lookback, 14):
            return {"passed": False, "reason": f"pretrigger warmup ({len(recent)}/{max(lookback, 14)} bars)"}

        reclaim_window = recent[-lookback:]
        atr14 = self._average_true_range(recent[-14:])
        current_price = float(indicators["price"])
        price_floor_atr = current_price * self.config.pretrigger_atr_floor_pct
        effective_atr = max(atr14, price_floor_atr)
        if atr14 <= 0:
            return {"passed": False, "reason": "pretrigger ATR unavailable"}

        ema9 = float(indicators.get("ema9", 0) or 0)
        ema20 = float(indicators.get("ema20", 0) or 0)
        selected_vwap = self._selected_vwap_value(indicators)
        current_open = float(indicators.get("open", current_price) or current_price)
        current_high = float(indicators.get("high", current_price) or current_price)
        current_low = float(indicators.get("low", current_price) or current_price)
        current_volume = float(indicators.get("volume", 0) or 0)
        current_stoch = float(indicators.get("stoch_k", 0) or 0)

        recent_high = max(float(bar["high"]) for bar in reclaim_window)
        if recent_high <= 0:
            return {"passed": False, "reason": "pretrigger reclaim high unavailable"}
        spike_index = max(range(len(reclaim_window)), key=lambda idx: float(reclaim_window[idx]["high"]))
        spike_bar = reclaim_window[spike_index]
        pre_spike_window = reclaim_window[: spike_index + 1]
        pre_spike_price = min(float(bar["low"]) for bar in pre_spike_window)
        pullback_phase = reclaim_window[spike_index + 1 :]
        pullback_low = min(
            [current_low, *(float(bar["low"]) for bar in pullback_phase)],
        )
        spike_gain = max(0.0, recent_high - pre_spike_price)
        pullback_pct = max(0.0, (recent_high - current_price) / recent_high)
        retrace_fraction = (recent_high - pullback_low) / spike_gain if spike_gain > 0 else 0.0
        pullback_pct_ok = (
            pullback_pct >= self.config.pretrigger_reclaim_min_pullback_from_high_pct
            and pullback_pct <= self.config.pretrigger_reclaim_max_pullback_from_high_pct
        )
        leg_retrace_ok = (
            self.config.pretrigger_reclaim_use_leg_retrace_gate
            and spike_gain > 0
            and retrace_fraction >= self.config.pretrigger_reclaim_min_retrace_fraction_of_leg
            and retrace_fraction <= self.config.pretrigger_reclaim_max_retrace_fraction_of_leg
        )
        pullback_ok = pullback_pct_ok or leg_retrace_ok
        pullback_reason = self._reclaim_pullback_reason(
            pullback_pct=pullback_pct,
            retrace_fraction=retrace_fraction,
            pullback_pct_ok=pullback_pct_ok,
            leg_retrace_ok=leg_retrace_ok,
        )
        held_move_floor = pre_spike_price + (spike_gain * self.config.pretrigger_reclaim_min_held_spike_gain_ratio)
        higher_low_ok = (
            not self.config.pretrigger_reclaim_require_higher_low
            or pullback_low > pre_spike_price * (1.0 + self.config.pretrigger_reclaim_min_pullback_low_above_prespike_pct)
        )
        held_move_ok = (
            not self.config.pretrigger_reclaim_require_held_move
            or (spike_gain > 0 and pullback_low > held_move_floor)
        )
        pullback_volumes = [float(bar["volume"]) for bar in pullback_phase] or [current_volume]
        average_pullback_volume = sum(pullback_volumes) / float(len(pullback_volumes)) if pullback_volumes else 0.0
        spike_volume = float(spike_bar.get("volume", 0) or 0)
        pullback_absorption_ok = (
            not self.config.pretrigger_reclaim_require_pullback_absorption
            or (spike_volume > 0 and average_pullback_volume <= spike_volume * self.config.pretrigger_reclaim_pullback_volume_max_spike_ratio)
        )

        touch_window = recent[-min(len(recent), self.config.pretrigger_reclaim_touch_lookback_bars) :]
        touch_tol = self.config.pretrigger_reclaim_touch_tolerance_pct
        touched_ema9 = any(
            float(bar["low"]) <= float(bar["ema9"]) * (1.0 + touch_tol)
            for bar in touch_window
            if float(bar.get("ema9", 0) or 0) > 0
        )
        touched_vwap = any(
            float(bar["low"]) <= float(bar["selected_vwap"]) * (1.0 + touch_tol)
            for bar in touch_window
            if float(bar.get("selected_vwap", 0) or 0) > 0
        )
        if self.config.pretrigger_reclaim_allow_current_bar_touch:
            touched_ema9 = touched_ema9 or (
                ema9 > 0 and current_low <= ema9 * (1.0 + touch_tol)
            )
            touched_vwap = touched_vwap or (
                selected_vwap > 0 and current_low <= selected_vwap * (1.0 + touch_tol)
            )
        touched_anchor = touched_ema9 or touched_vwap
        touch_reason = self._reclaim_touch_reason(touched_ema9=touched_ema9, touched_vwap=touched_vwap)
        current_bar_rel_vol = self._current_bar_rel_vol(ticker, current_volume)
        reentry_reset_ok, reentry_reset_reason = self._reclaim_reentry_reset_result(
            ticker=ticker,
            recent=recent,
            current_high=current_high,
            current_low=current_low,
            ema9=ema9,
            selected_vwap=selected_vwap,
            touch_tol=touch_tol,
        )

        ema9_reclaim_floor = ema9 * (1.0 - self.config.pretrigger_max_pullback_below_ema9_pct) if ema9 > 0 else 0.0
        reclaim_floor = max(selected_vwap, ema9_reclaim_floor)
        above_support = current_price >= reclaim_floor
        same_bar_touch = (
            (ema9 > 0 and current_low <= ema9 * (1.0 + touch_tol))
            or (selected_vwap > 0 and current_low <= selected_vwap * (1.0 + touch_tol))
        )
        location_ok, location_reason = self._reclaim_location_result(
            current_price=current_price,
            current_open=current_open,
            current_low=current_low,
            current_high=current_high,
            ema9=ema9,
            selected_vwap=selected_vwap,
            above_support=above_support,
            same_bar_touch=same_bar_touch,
            current_bar_rel_vol=current_bar_rel_vol,
        )
        dual_anchor_location_ok = (
            above_support
            and ema9 > 0
            and selected_vwap > 0
            and current_price >= ema9
            and current_price >= selected_vwap
        )

        reclaim_reference = max(max(float(bar["open"]), float(bar["close"])) for bar in touch_window)
        reclaim_break_ok = current_price >= reclaim_reference
        arm_trigger_price = max(reclaim_reference, current_high)

        macd_now = float(indicators.get("macd", 0) or 0)
        macd_prev = float(indicators.get("macd_prev", 0) or 0)
        signal_now = float(indicators.get("signal", 0) or 0)
        histogram_now = float(indicators.get("histogram", 0) or 0)
        histogram_prev = float(indicators.get("histogram_prev", 0) or 0)
        hist_rising = histogram_now > histogram_prev
        macd_near_signal = abs(macd_now - signal_now) <= effective_atr * self.config.pretrigger_macd_near_signal_atr_factor
        momentum_ok = histogram_now > 0.0 and hist_rising and (macd_now > signal_now or macd_near_signal) and macd_now >= macd_prev

        bar_range = max(current_high - current_low, 0.000001)
        body_pct = abs(current_price - current_open) / bar_range
        close_pos_pct = (current_price - current_low) / bar_range
        upper_wick_pct = (current_high - max(current_open, current_price)) / bar_range
        candle_ok = (
            current_price > current_open
            and body_pct >= self.config.pretrigger_reclaim_min_body_pct
            and close_pos_pct >= self.config.pretrigger_reclaim_min_close_pos_pct
            and upper_wick_pct <= self.config.pretrigger_reclaim_max_upper_wick_pct
        )
        soft_candle_ok = (
            current_price > current_open
            and body_pct >= self.config.pretrigger_reclaim_soft_min_body_pct
            and close_pos_pct >= self.config.pretrigger_reclaim_soft_min_close_pos_pct
            and upper_wick_pct <= self.config.pretrigger_reclaim_soft_max_upper_wick_pct
        )

        bar_rel_vol = self._current_bar_rel_vol(ticker, current_volume)
        volume_ok = current_bar_rel_vol >= self.config.pretrigger_reclaim_min_bar_rel_vol
        stoch_ok = current_stoch < self.config.stoch_entry_cap
        trend_ok = current_price > ema20 and ema9 >= ema20
        trend_reason = self._reclaim_trend_reason(current_price=current_price, ema9=ema9, ema20=ema20)
        momentum_reason = self._reclaim_momentum_reason(
            histogram_now=histogram_now,
            histogram_prev=histogram_prev,
            macd_now=macd_now,
            macd_prev=macd_prev,
            signal_now=signal_now,
            macd_near_signal=macd_near_signal,
        )
        volume_reason = self._reclaim_volume_reason(bar_rel_vol=bar_rel_vol)
        soft_candle_reason = self._reclaim_candle_reason(
            current_price=current_price,
            current_open=current_open,
            body_pct=body_pct,
            close_pos_pct=close_pos_pct,
            upper_wick_pct=upper_wick_pct,
            min_body_pct=self.config.pretrigger_reclaim_soft_min_body_pct,
            min_close_pos_pct=self.config.pretrigger_reclaim_soft_min_close_pos_pct,
            max_upper_wick_pct=self.config.pretrigger_reclaim_soft_max_upper_wick_pct,
            label="recovery candle",
        )

        can_open = True
        risk_reason = ""
        if position_tracker is not None and hasattr(position_tracker, "positions"):
            can_open, risk_reason = position_tracker.positions.can_open_position(ticker)

        score = sum(1 for passed in (reclaim_break_ok, momentum_ok, volume_ok) if passed)
        score_details = " ".join(
            [
                "mode=reclaim",
                f"pullback_pct={'+' if pullback_pct_ok else '-'}",
                f"pullback_leg={'+' if leg_retrace_ok else '-'}",
                f"touch={'+' if touched_anchor else '-'}",
                f"higher_low={'+' if higher_low_ok else '-'}",
                f"held_move={'+' if held_move_ok else '-'}",
                f"absorb={'+' if pullback_absorption_ok else '-'}",
                f"loc={'+' if location_ok else '-'}",
                f"break={'+' if reclaim_break_ok else '-'}",
                f"mom={'+' if momentum_ok else '-'}",
                f"candle={'+' if candle_ok else '-'}",
                f"soft_candle={'+' if soft_candle_ok else '-'}",
                f"stoch={'+' if stoch_ok else '-'}",
                f"vol={'+' if volume_ok else '-'}",
            ]
        )

        if not can_open:
            return {"passed": False, "reason": risk_reason, "score": score, "score_details": score_details}
        if self.config.pretrigger_reclaim_require_pullback and not pullback_ok:
            return {"passed": False, "reason": pullback_reason, "score": score, "score_details": score_details}
        if self.config.pretrigger_reclaim_require_touch and not touched_anchor:
            return {"passed": False, "reason": touch_reason, "score": score, "score_details": score_details}
        if not higher_low_ok:
            return {
                "passed": False,
                "reason": self._reclaim_higher_low_reason(
                    pullback_low=pullback_low,
                    pre_spike_price=pre_spike_price,
                    threshold_pct=self.config.pretrigger_reclaim_min_pullback_low_above_prespike_pct,
                ),
                "score": score,
                "score_details": score_details,
            }
        if not held_move_ok:
            return {
                "passed": False,
                "reason": self._reclaim_held_move_reason(
                    pullback_low=pullback_low,
                    held_move_floor=held_move_floor,
                ),
                "score": score,
                "score_details": score_details,
            }
        if not pullback_absorption_ok:
            return {
                "passed": False,
                "reason": self._reclaim_absorption_reason(
                    average_pullback_volume=average_pullback_volume,
                    spike_volume=spike_volume,
                    max_ratio=self.config.pretrigger_reclaim_pullback_volume_max_spike_ratio,
                ),
                "score": score,
                "score_details": score_details,
            }
        if self.config.pretrigger_reclaim_require_reentry_reset and not reentry_reset_ok:
            return {
                "passed": False,
                "reason": reentry_reset_reason,
                "score": score,
                "score_details": score_details,
            }
        if self.config.pretrigger_reclaim_require_location and not location_ok:
            return {"passed": False, "reason": location_reason or "pretrigger reclaim location not ready", "score": score, "score_details": score_details}
        if self.config.pretrigger_reclaim_require_stoch and not stoch_ok:
            return {
                "passed": False,
                "reason": f"pretrigger reclaim stochK at or above cap ({self.config.stoch_entry_cap:.0f})",
                "score": score,
                "score_details": score_details,
            }
        if self.config.pretrigger_reclaim_require_trend and not trend_ok:
            return {"passed": False, "reason": trend_reason, "score": score, "score_details": score_details}
        if self.config.pretrigger_reclaim_require_momentum and not momentum_ok:
            return {"passed": False, "reason": momentum_reason, "score": score, "score_details": score_details}
        if self.config.pretrigger_reclaim_require_volume and not volume_ok:
            return {"passed": False, "reason": volume_reason, "score": score, "score_details": score_details}
        if score < self.config.pretrigger_reclaim_score_threshold:
            return {
                "passed": False,
                "reason": f"pretrigger reclaim score {score} below required {self.config.pretrigger_reclaim_score_threshold}",
                "score": score,
                "score_details": score_details,
            }
        if (
            self.config.pretrigger_reclaim_require_stoch_for_min_score
            and score == self.config.pretrigger_reclaim_score_threshold
            and not stoch_ok
        ):
            return {
                "passed": False,
                "reason": (
                    "pretrigger reclaim minimum-score starter requires stoch support"
                ),
                "score": score,
                "score_details": score_details,
            }

        starter_location_ok = (
            dual_anchor_location_ok
            if self.config.pretrigger_reclaim_require_dual_anchor_for_starter
            else location_ok
        )
        starter_ready = (
            reclaim_break_ok
            and starter_location_ok
            and (candle_ok or not self.config.pretrigger_reclaim_require_candle)
        )
        armed_ready = (above_support or location_ok) and (soft_candle_ok or not self.config.pretrigger_reclaim_require_candle)

        if starter_ready:
            return {
                "passed": True,
                "reason": "",
                "score": score,
                "score_details": score_details,
                "atr14": atr14,
                "effective_atr": effective_atr,
                "resistance_level": reclaim_reference,
                "hold_floor": reclaim_floor - self.config.pretrigger_fail_hold_buf_atr * effective_atr,
            }
        if armed_ready:
            return {
                "passed": True,
                "reason": "",
                "score": score,
                "score_details": score_details,
                "atr14": atr14,
                "effective_atr": effective_atr,
                "resistance_level": reclaim_reference,
                "hold_floor": reclaim_floor - self.config.pretrigger_fail_hold_buf_atr * effective_atr,
                "armed_only": True,
                "armed_lookahead_bars": self.config.pretrigger_reclaim_arm_break_lookahead_bars,
                "arm_trigger_price": arm_trigger_price,
            }
        if self.config.pretrigger_reclaim_require_candle and not soft_candle_ok:
            return {
                "passed": False,
                "reason": soft_candle_reason,
                "score": score,
                "score_details": score_details,
            }
        return {
            "passed": False,
            "reason": "pretrigger reclaim break not ready",
            "score": score,
            "score_details": score_details,
        }

    def _check_reclaim_armed_entry(
        self,
        ticker: str,
        probe_state: dict[str, object],
        indicators: dict[str, float | bool],
        bar_index: int,
    ) -> dict[str, float | int | str] | None:
        armed_bar = int(probe_state.get("armed_bar", bar_index))
        lookahead_bars = int(probe_state.get("armed_lookahead_bars", self.config.pretrigger_reclaim_arm_break_lookahead_bars))
        if bar_index > armed_bar + lookahead_bars:
            self._probe_state.pop(ticker, None)
            self._record_decision(ticker, status="blocked", reason="pretrigger reclaim arm expired")
            return None

        current_price = float(indicators["price"])
        current_open = float(indicators.get("open", current_price) or current_price)
        current_high = float(indicators.get("high", current_price) or current_price)
        current_low = float(indicators.get("low", current_price) or current_price)
        ema9 = float(indicators.get("ema9", 0) or 0)
        ema20 = float(indicators.get("ema20", 0) or 0)
        selected_vwap = self._selected_vwap_value(indicators)
        current_volume = float(indicators.get("volume", 0) or 0)
        current_stoch = float(indicators.get("stoch_k", 0) or 0)
        touch_tol = self.config.pretrigger_reclaim_touch_tolerance_pct
        current_bar_rel_vol = self._current_bar_rel_vol(ticker, current_volume)

        if current_price < float(probe_state.get("hold_floor", 0) or 0):
            self._probe_state.pop(ticker, None)
            self._record_decision(ticker, status="blocked", reason="pretrigger reclaim arm lost support")
            return None

        trigger_price = float(probe_state.get("arm_trigger_price", probe_state.get("resistance_level", current_high)) or current_high)
        reclaim_break_ok = current_price >= trigger_price or current_high >= trigger_price
        ema9_reclaim_floor = ema9 * (1.0 - self.config.pretrigger_max_pullback_below_ema9_pct) if ema9 > 0 else 0.0
        reclaim_floor = max(selected_vwap, ema9_reclaim_floor)
        above_support = current_price >= reclaim_floor
        same_bar_touch = (
            (ema9 > 0 and current_low <= ema9 * (1.0 + touch_tol))
            or (selected_vwap > 0 and current_low <= selected_vwap * (1.0 + touch_tol))
        )
        location_ok, location_reason = self._reclaim_location_result(
            current_price=current_price,
            current_open=current_open,
            current_low=current_low,
            current_high=current_high,
            ema9=ema9,
            selected_vwap=selected_vwap,
            above_support=above_support,
            same_bar_touch=same_bar_touch,
            current_bar_rel_vol=current_bar_rel_vol,
        )

        histogram_now = float(indicators.get("histogram", 0) or 0)
        histogram_prev = float(indicators.get("histogram_prev", 0) or 0)
        macd_now = float(indicators.get("macd", 0) or 0)
        macd_prev = float(indicators.get("macd_prev", 0) or 0)
        signal_now = float(indicators.get("signal", 0) or 0)
        effective_atr = max(float(probe_state.get("effective_atr", 0) or 0), current_price * self.config.pretrigger_atr_floor_pct)
        macd_near_signal = abs(macd_now - signal_now) <= effective_atr * self.config.pretrigger_macd_near_signal_atr_factor
        momentum_ok = histogram_now > 0.0 and histogram_now > histogram_prev and (macd_now > signal_now or macd_near_signal) and macd_now >= macd_prev
        volume_ok = current_bar_rel_vol >= self.config.pretrigger_reclaim_min_bar_rel_vol
        trend_ok = current_price > ema20 and ema9 >= ema20
        stoch_ok = current_stoch < self.config.stoch_entry_cap
        trend_reason = self._reclaim_trend_reason(current_price=current_price, ema9=ema9, ema20=ema20)
        momentum_reason = self._reclaim_momentum_reason(
            histogram_now=histogram_now,
            histogram_prev=histogram_prev,
            macd_now=macd_now,
            macd_prev=macd_prev,
            signal_now=signal_now,
            macd_near_signal=macd_near_signal,
        )
        volume_reason = self._reclaim_volume_reason(bar_rel_vol=current_bar_rel_vol)
        bar_range = max(float(indicators["high"]) - float(indicators["low"]), 0.000001)
        body_pct = abs(current_price - current_open) / bar_range
        close_pos_pct = (current_price - current_low) / bar_range
        upper_wick_pct = (current_high - max(current_open, current_price)) / bar_range
        soft_candle_ok = (
            current_price > current_open
            and body_pct >= self.config.pretrigger_reclaim_soft_min_body_pct
            and close_pos_pct >= self.config.pretrigger_reclaim_soft_min_close_pos_pct
            and upper_wick_pct <= self.config.pretrigger_reclaim_soft_max_upper_wick_pct
        )
        soft_candle_reason = self._reclaim_candle_reason(
            current_price=current_price,
            current_open=current_open,
            body_pct=body_pct,
            close_pos_pct=close_pos_pct,
            upper_wick_pct=upper_wick_pct,
            min_body_pct=self.config.pretrigger_reclaim_soft_min_body_pct,
            min_close_pos_pct=self.config.pretrigger_reclaim_soft_min_close_pos_pct,
            max_upper_wick_pct=self.config.pretrigger_reclaim_soft_max_upper_wick_pct,
            label="recovery candle",
        )

        if not reclaim_break_ok:
            self._record_decision(ticker, status="pending", reason="PRETRIGGER_RECLAIM_ARMED_WAIT", path="PRETRIGGER_RECLAIM_ARMED")
            return None
        if self.config.pretrigger_reclaim_require_location and not location_ok:
            self._record_decision(
                ticker,
                status="pending",
                reason=location_reason or "pretrigger reclaim location not ready",
                path="PRETRIGGER_RECLAIM_ARMED",
            )
            return None
        if self.config.pretrigger_reclaim_require_momentum and not momentum_ok:
            self._record_decision(ticker, status="pending", reason=momentum_reason, path="PRETRIGGER_RECLAIM_ARMED")
            return None
        if self.config.pretrigger_reclaim_require_volume and not volume_ok:
            self._record_decision(ticker, status="pending", reason=volume_reason, path="PRETRIGGER_RECLAIM_ARMED")
            return None
        if self.config.pretrigger_reclaim_require_trend and not trend_ok:
            self._record_decision(ticker, status="pending", reason=trend_reason, path="PRETRIGGER_RECLAIM_ARMED")
            return None
        if self.config.pretrigger_reclaim_require_stoch and not stoch_ok:
            self._record_decision(
                ticker,
                status="pending",
                reason=f"pretrigger reclaim stochK at or above cap ({self.config.stoch_entry_cap:.0f})",
                path="PRETRIGGER_RECLAIM_ARMED",
            )
            return None
        if self.config.pretrigger_reclaim_require_candle and not soft_candle_ok:
            self._record_decision(ticker, status="pending", reason=soft_candle_reason, path="PRETRIGGER_RECLAIM_ARMED")
            return None

        starter_qty = int(probe_state.get("starter_qty", 0) or 0)
        if starter_qty <= 0:
            self._probe_state.pop(ticker, None)
            self._record_decision(ticker, status="blocked", reason="pretrigger starter quantity resolved to zero")
            return None

        probe_state["armed_only"] = False
        probe_state["entry_bar"] = bar_index
        probe_state["probe_entry_price"] = current_price
        probe_state["confirmed"] = False
        probe_state["confirm_reason"] = ""
        probe_state["resistance_level"] = trigger_price
        probe_state["starter_high"] = current_high
        self._record_decision(
            ticker,
            status="signal",
            reason="PRETRIGGER_RECLAIM_BREAK",
            path="PRETRIGGER_RECLAIM_BREAK",
            score=int(probe_state.get("pretrigger_score", 0) or 0),
            score_details=str(probe_state.get("pretrigger_score_details", "")),
        )
        return self._build_buy_signal(
            ticker,
            "PRETRIGGER_RECLAIM_BREAK",
            indicators,
            int(probe_state.get("pretrigger_score", 0) or 0),
            str(probe_state.get("pretrigger_score_details", "")),
            quantity=starter_qty,
            stage="starter",
        )

    def _check_retest_armed_entry(
        self,
        ticker: str,
        probe_state: dict[str, object],
        indicators: dict[str, float | bool],
        bar_index: int,
    ) -> dict[str, float | int | str] | None:
        armed_bar = int(probe_state.get("armed_bar", bar_index))
        lookahead_bars = int(probe_state.get("armed_lookahead_bars", self.config.pretrigger_retest_arm_break_lookahead_bars))
        if bar_index > armed_bar + lookahead_bars:
            self._probe_state.pop(ticker, None)
            self._record_decision(ticker, status="blocked", reason="pretrigger retest arm expired")
            return None

        current_price = float(indicators["price"])
        current_open = float(indicators.get("open", current_price) or current_price)
        current_high = float(indicators.get("high", current_price) or current_price)
        current_low = float(indicators.get("low", current_price) or current_price)
        current_volume = float(indicators.get("volume", 0) or 0)
        ema9 = float(indicators.get("ema9", 0) or 0)
        ema20 = float(indicators.get("ema20", 0) or 0)
        selected_vwap = self._selected_vwap_value(indicators)

        hold_floor = float(probe_state.get("hold_floor", 0) or 0)
        if current_price < hold_floor:
            self._probe_state.pop(ticker, None)
            self._record_decision(ticker, status="blocked", reason="pretrigger retest arm lost support")
            return None

        trigger_price = float(probe_state.get("arm_trigger_price", current_high) or current_high)
        retest_level = float(probe_state.get("retest_level", probe_state.get("resistance_level", current_price)) or current_price)
        break_ok = current_high >= trigger_price and current_price >= retest_level

        bar_range = max(current_high - current_low, 0.000001)
        body_pct = abs(current_price - current_open) / bar_range
        close_pos_pct = (current_price - current_low) / bar_range
        upper_wick_pct = (current_high - max(current_open, current_price)) / bar_range
        candle_ok = (
            current_price > current_open
            and body_pct >= self.config.pretrigger_retest_min_body_pct
            and close_pos_pct >= self.config.pretrigger_retest_min_close_pos_pct
            and upper_wick_pct <= self.config.pretrigger_retest_max_upper_wick_pct
        )

        current_bar_rel_vol = self._current_bar_rel_vol(ticker, current_volume)
        volume_ok = current_bar_rel_vol >= self.config.pretrigger_retest_min_confirm_bar_rel_vol
        dual_anchor_ok = current_price >= ema9 and current_price >= selected_vwap if ema9 > 0 and selected_vwap > 0 else False
        trend_ok = current_price > ema20 and ema9 >= ema20 and (not self.config.pretrigger_retest_require_dual_anchor or dual_anchor_ok)

        histogram_now = float(indicators.get("histogram", 0) or 0)
        histogram_prev = float(indicators.get("histogram_prev", 0) or 0)
        macd_now = float(indicators.get("macd", 0) or 0)
        macd_prev = float(indicators.get("macd_prev", 0) or 0)
        signal_now = float(indicators.get("signal", 0) or 0)
        effective_atr = max(float(probe_state.get("effective_atr", 0) or 0), current_price * self.config.pretrigger_atr_floor_pct)
        macd_near_signal = abs(macd_now - signal_now) <= effective_atr * self.config.pretrigger_macd_near_signal_atr_factor
        momentum_ok = histogram_now > 0.0 and histogram_now >= histogram_prev and (macd_now > signal_now or macd_near_signal) and macd_now >= macd_prev

        if not break_ok:
            self._record_decision(ticker, status="pending", reason="PRETRIGGER_RETEST_ARMED_WAIT", path="PRETRIGGER_RETEST_ARMED")
            return None
        if self.config.pretrigger_retest_require_volume and not volume_ok:
            self._record_decision(ticker, status="pending", reason="pretrigger retest volume not ready", path="PRETRIGGER_RETEST_ARMED")
            return None
        if self.config.pretrigger_retest_require_momentum and not momentum_ok:
            self._record_decision(ticker, status="pending", reason="pretrigger retest momentum not ready", path="PRETRIGGER_RETEST_ARMED")
            return None
        if self.config.pretrigger_retest_require_trend and not trend_ok:
            self._record_decision(ticker, status="pending", reason="pretrigger retest trend not ready", path="PRETRIGGER_RETEST_ARMED")
            return None
        if not candle_ok:
            self._record_decision(ticker, status="pending", reason="pretrigger retest candle not ready", path="PRETRIGGER_RETEST_ARMED")
            return None

        starter_qty = int(probe_state.get("starter_qty", 0) or 0)
        if starter_qty <= 0:
            self._probe_state.pop(ticker, None)
            self._record_decision(ticker, status="blocked", reason="pretrigger starter quantity resolved to zero")
            return None

        probe_state["armed_only"] = False
        probe_state["entry_bar"] = bar_index
        probe_state["probe_entry_price"] = current_price
        probe_state["confirmed"] = True
        probe_state["confirm_reason"] = "R0_RETEST_BREAK"
        probe_state["remaining_qty"] = 0
        probe_state["resistance_level"] = max(trigger_price, current_high)
        probe_state["starter_high"] = current_high
        self._record_decision(
            ticker,
            status="signal",
            reason="PRETRIGGER_RETEST_BREAK",
            path="PRETRIGGER_RETEST_BREAK",
            score=int(probe_state.get("pretrigger_score", 0) or 0),
            score_details=str(probe_state.get("pretrigger_score_details", "")),
        )
        return self._build_buy_signal(
            ticker,
            "PRETRIGGER_RETEST_BREAK",
            indicators,
            int(probe_state.get("pretrigger_score", 0) or 0),
            str(probe_state.get("pretrigger_score_details", "")),
            quantity=starter_qty,
            stage="starter",
        )

    def _check_pretrigger_failures(
        self,
        ticker: str,
        probe_state: dict[str, object],
        indicators: dict[str, float | bool],
        bar_index: int,
    ) -> dict[str, float | int | str] | None:
        entry_bar = int(probe_state["entry_bar"])
        lookahead_bars = self.config.pretrigger_failed_break_lookahead_bars
        if bar_index > entry_bar + lookahead_bars and not bool(probe_state.get("confirmed", False)):
            self._probe_state.pop(ticker, None)
            self._probe_fail_bar[ticker] = bar_index
            self._record_decision(ticker, status="signal", reason="PRETRIGGER_NO_CONFIRM", path="PRETRIGGER_FAIL")
            return self._build_sell_signal(ticker, indicators, reason="PRETRIGGER_NO_CONFIRM")

        if bar_index > entry_bar + lookahead_bars:
            return None

        price_below_hold_floor = float(indicators["price"]) < float(probe_state["hold_floor"])
        macd_below_signal = float(indicators.get("macd", 0) or 0) < float(indicators.get("signal", 0) or 0)
        price_below_ema9 = float(indicators["price"]) < float(indicators.get("ema9", 0) or 0)
        fail_fast_on_macd = self.config.pretrigger_fail_fast_on_macd_below_signal
        fail_fast_on_ema9 = self.config.pretrigger_fail_fast_on_price_below_ema9
        if self.config.entry_logic_mode == "pretrigger_reclaim":
            fail_fast_on_macd = self.config.pretrigger_reclaim_fail_fast_on_macd_below_signal
            fail_fast_on_ema9 = self.config.pretrigger_reclaim_fail_fast_on_price_below_ema9
        if (
            price_below_hold_floor
            or (fail_fast_on_macd and macd_below_signal)
            or (fail_fast_on_ema9 and price_below_ema9)
        ):
            self._probe_state.pop(ticker, None)
            self._probe_fail_bar[ticker] = bar_index
            self._record_decision(ticker, status="signal", reason="PRETRIGGER_FAIL_FAST", path="PRETRIGGER_FAIL")
            return self._build_sell_signal(ticker, indicators, reason="PRETRIGGER_FAIL_FAST")
        return None

    def _check_pretrigger_confirmation(
        self,
        ticker: str,
        probe_state: dict[str, object],
        indicators: dict[str, float | bool],
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, float | int | str] | None:
        if self.config.entry_logic_mode == "pretrigger_reclaim":
            return self._check_reclaim_confirmation(ticker, probe_state, indicators, bar_index, position_tracker)
        if self.config.entry_logic_mode == "pretrigger_retest":
            return None
        del bar_index
        if bool(probe_state.get("confirmed", False)):
            return None

        bar_rel_vol = self._current_bar_rel_vol(ticker, float(indicators.get("volume", 0) or 0))
        macd_cross_confirm = bool(indicators.get("macd_cross_above", False))
        macd_surge_confirm = (
            bool(indicators.get("macd_above_signal", False))
            and float(indicators.get("histogram", 0) or 0) > float(indicators.get("histogram_prev", 0) or 0)
            and self._histogram_surge_ok(ticker, indicators)
        )
        vwap_break_confirm = (
            self._price_cross_above_selected_vwap(indicators)
            and bool(indicators.get("macd_above_signal", False))
            and bar_rel_vol >= self.config.pretrigger_min_bar_rel_vol_breakout
        )
        confirm_path = ""
        if macd_cross_confirm:
            confirm_path = "P1_MACD_CROSS"
        elif macd_surge_confirm:
            confirm_path = "P3_MACD_SURGE"
        elif vwap_break_confirm:
            confirm_path = "P2_VWAP_BREAKOUT"
        if not confirm_path:
            return None

        current_price = float(indicators["price"])
        current_open = float(indicators.get("open", current_price) or current_price)
        bar_range = max(float(indicators["high"]) - float(indicators["low"]), 0.000001)
        close_pos_pct = (current_price - float(indicators["low"])) / bar_range
        add_not_extended = self._pct_distance(current_price, float(indicators.get("ema9", 0) or 0)) <= self.config.pretrigger_add_max_distance_to_ema9_pct
        add_candle_ok = current_price > current_open and close_pos_pct >= max(0.55, self.config.pretrigger_min_close_pos_pct - 0.10)
        if (
            current_price < float(probe_state.get("probe_entry_price", current_price))
            or current_price < float(probe_state["resistance_level"])
            or not add_not_extended
            or not add_candle_ok
        ):
            return None

        add_qty = int(probe_state.get("remaining_qty", 0) or 0)
        probe_state["confirmed"] = True
        probe_state["confirm_reason"] = confirm_path
        self._record_decision(
            ticker,
            status="signal",
            reason=f"PRETRIGGER_ADD_{confirm_path}",
            path=confirm_path,
            score=int(probe_state.get("pretrigger_score", 0) or 0),
            score_details=str(probe_state.get("pretrigger_score_details", "")),
        )
        if add_qty <= 0:
            return None
        probe_state["remaining_qty"] = 0
        return self._build_buy_signal(
            ticker,
            confirm_path,
            indicators,
            int(probe_state.get("pretrigger_score", 0) or 0),
            str(probe_state.get("pretrigger_score_details", "")),
            quantity=add_qty,
            stage="confirm_add",
        )

    def _check_reclaim_confirmation(
        self,
        ticker: str,
        probe_state: dict[str, object],
        indicators: dict[str, float | bool],
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, float | int | str] | None:
        del bar_index
        if bool(probe_state.get("confirmed", False)):
            return None

        current_price = float(indicators["price"])
        current_open = float(indicators.get("open", current_price) or current_price)
        current_high = float(indicators.get("high", current_price) or current_price)
        current_low = float(indicators.get("low", current_price) or current_price)
        ema9 = float(indicators.get("ema9", 0) or 0)
        ema20 = float(indicators.get("ema20", 0) or 0)
        selected_vwap = self._selected_vwap_value(indicators)
        current_volume = float(indicators.get("volume", 0) or 0)
        current_stoch = float(indicators.get("stoch_k", 0) or 0)
        touch_tol = self.config.pretrigger_reclaim_touch_tolerance_pct
        current_bar_rel_vol = self._current_bar_rel_vol(ticker, current_volume)

        starter_high = float(probe_state.get("starter_high", probe_state.get("probe_entry_price", current_high)) or current_high)
        starter_break_level = max(
            starter_high,
            float(probe_state.get("resistance_level", starter_high) or starter_high),
            float(probe_state.get("probe_entry_price", current_price) or current_price),
        )
        break_confirm_ok = current_high >= starter_break_level and current_price >= max(
            float(probe_state.get("probe_entry_price", current_price) or current_price),
            float(probe_state.get("resistance_level", current_price) or current_price),
        )
        if not break_confirm_ok:
            return None

        ema9_reclaim_floor = ema9 * (1.0 - self.config.pretrigger_max_pullback_below_ema9_pct) if ema9 > 0 else 0.0
        reclaim_floor = max(selected_vwap, ema9_reclaim_floor)
        above_support = current_price >= reclaim_floor
        same_bar_touch = (
            (ema9 > 0 and current_low <= ema9 * (1.0 + touch_tol))
            or (selected_vwap > 0 and current_low <= selected_vwap * (1.0 + touch_tol))
        )
        location_ok, _location_reason = self._reclaim_location_result(
            current_price=current_price,
            current_open=current_open,
            current_low=current_low,
            current_high=current_high,
            ema9=ema9,
            selected_vwap=selected_vwap,
            above_support=above_support,
            same_bar_touch=same_bar_touch,
            current_bar_rel_vol=current_bar_rel_vol,
        )

        histogram_now = float(indicators.get("histogram", 0) or 0)
        histogram_prev = float(indicators.get("histogram_prev", 0) or 0)
        macd_now = float(indicators.get("macd", 0) or 0)
        macd_prev = float(indicators.get("macd_prev", 0) or 0)
        signal_now = float(indicators.get("signal", 0) or 0)
        effective_atr = max(float(probe_state.get("effective_atr", 0) or 0), current_price * self.config.pretrigger_atr_floor_pct)
        macd_near_signal = abs(macd_now - signal_now) <= effective_atr * self.config.pretrigger_macd_near_signal_atr_factor
        momentum_ok = histogram_now > 0.0 and histogram_now > histogram_prev and (macd_now > signal_now or macd_near_signal) and macd_now >= macd_prev
        volume_ok = self._current_bar_rel_vol(ticker, current_volume) >= self.config.pretrigger_reclaim_min_bar_rel_vol
        trend_ok = current_price > ema20 and ema9 >= ema20
        stoch_ok = current_stoch < self.config.stoch_entry_cap

        bar_range = max(current_high - current_low, 0.000001)
        body_pct = abs(current_price - current_open) / bar_range
        close_pos_pct = (current_price - current_low) / bar_range
        upper_wick_pct = (current_high - max(current_open, current_price)) / bar_range
        breakout_candle_ok = (
            current_price > current_open
            and body_pct >= self.config.pretrigger_reclaim_soft_min_body_pct
            and close_pos_pct >= self.config.pretrigger_reclaim_soft_min_close_pos_pct
            and upper_wick_pct <= self.config.pretrigger_reclaim_soft_max_upper_wick_pct
        )

        if self.config.pretrigger_reclaim_require_location and not location_ok:
            return None
        if self.config.pretrigger_reclaim_require_momentum and not momentum_ok:
            return None
        if self.config.pretrigger_reclaim_require_volume and not volume_ok:
            return None
        if self.config.pretrigger_reclaim_require_trend and not trend_ok:
            return None
        if self.config.pretrigger_reclaim_require_stoch and not stoch_ok:
            return None
        if self.config.pretrigger_reclaim_require_candle and not breakout_candle_ok:
            return None

        add_qty = int(probe_state.get("remaining_qty", 0) or 0)
        min_peak_profit_pct = float(self.config.pretrigger_reclaim_confirm_add_min_peak_profit_pct or 0.0)
        if add_qty > 0 and min_peak_profit_pct > 0.0:
            current_peak_profit_pct = self._position_peak_profit_pct(position_tracker, ticker)
            if current_peak_profit_pct is None or current_peak_profit_pct < min_peak_profit_pct:
                return None
        probe_state["confirmed"] = True
        probe_state["confirm_reason"] = "R1_BREAK_CONFIRM"
        probe_state["starter_high"] = max(starter_high, current_high)
        self._record_decision(
            ticker,
            status="signal",
            reason="PRETRIGGER_ADD_R1_BREAK_CONFIRM",
            path="R1_BREAK_CONFIRM",
            score=int(probe_state.get("pretrigger_score", 0) or 0),
            score_details=str(probe_state.get("pretrigger_score_details", "")),
        )
        if add_qty <= 0:
            return None
        probe_state["remaining_qty"] = 0
        return self._build_buy_signal(
            ticker,
            "R1_BREAK_CONFIRM",
            indicators,
            int(probe_state.get("pretrigger_score", 0) or 0),
            str(probe_state.get("pretrigger_score_details", "")),
            quantity=add_qty,
            stage="confirm_add",
        )

    def _reclaim_location_result(
        self,
        *,
        current_price: float,
        current_open: float,
        current_low: float,
        current_high: float,
        ema9: float,
        selected_vwap: float,
        above_support: bool,
        same_bar_touch: bool,
        current_bar_rel_vol: float,
    ) -> tuple[bool, str]:
        ema9_extension_pct = max(0.0, (current_price - ema9) / ema9) if ema9 > 0 else float("inf")
        vwap_extension_pct = max(0.0, (current_price - selected_vwap) / selected_vwap) if selected_vwap > 0 else float("inf")
        standard_location_ok = (
            above_support
            and (
                ema9_extension_pct <= self.config.pretrigger_reclaim_max_extension_above_ema9_pct
                or vwap_extension_pct <= self.config.pretrigger_reclaim_max_extension_above_vwap_pct
            )
        )
        if standard_location_ok:
            return True, ""
        standard_failure_reason = "pretrigger reclaim location not ready"
        if not above_support:
            below_ema9_support = ema9 > 0 and current_price < ema9 * (1.0 - self.config.pretrigger_max_pullback_below_ema9_pct)
            below_vwap = selected_vwap > 0 and current_price < selected_vwap
            if below_ema9_support and below_vwap:
                standard_failure_reason = "pretrigger reclaim below VWAP and EMA9 support"
            elif below_vwap:
                standard_failure_reason = "pretrigger reclaim below VWAP"
            elif below_ema9_support:
                standard_failure_reason = "pretrigger reclaim below EMA9 support"
            else:
                standard_failure_reason = "pretrigger reclaim below reclaim floor"
        elif (
            ema9_extension_pct > self.config.pretrigger_reclaim_max_extension_above_ema9_pct
            and vwap_extension_pct > self.config.pretrigger_reclaim_max_extension_above_vwap_pct
        ):
            standard_failure_reason = "pretrigger reclaim too extended from EMA9/VWAP"
        bar_range = max(current_high - current_low, 0.000001)
        body_pct = abs(current_price - current_open) / bar_range
        close_pos_pct = (current_price - current_low) / bar_range
        upper_wick_pct = (current_high - max(current_open, current_price)) / bar_range
        soft_candle_ok = (
            current_price > current_open
            and body_pct >= self.config.pretrigger_reclaim_soft_min_body_pct
            and close_pos_pct >= self.config.pretrigger_reclaim_soft_min_close_pos_pct
            and upper_wick_pct <= self.config.pretrigger_reclaim_soft_max_upper_wick_pct
        )
        single_anchor_candle_ok = (
            current_price > current_open
            and body_pct >= self.config.pretrigger_reclaim_single_anchor_min_body_pct
            and close_pos_pct >= self.config.pretrigger_reclaim_single_anchor_min_close_pos_pct
            and upper_wick_pct <= self.config.pretrigger_reclaim_single_anchor_max_upper_wick_pct
        )
        if not self.config.pretrigger_reclaim_allow_touch_recovery_location or not same_bar_touch or not soft_candle_ok:
            if not self.config.pretrigger_reclaim_allow_single_anchor_location or not same_bar_touch or current_price <= current_open:
                if standard_failure_reason != "pretrigger reclaim location not ready":
                    return False, standard_failure_reason
                if not same_bar_touch:
                    return False, "pretrigger reclaim no fresh anchor touch"
                if current_price <= current_open:
                    return False, "pretrigger reclaim close did not recover above open"
                if not self.config.pretrigger_reclaim_allow_single_anchor_location:
                    return False, standard_failure_reason
                if not soft_candle_ok:
                    return False, "pretrigger reclaim recovery candle too weak"
                return False, standard_failure_reason
            close_above_ema9 = ema9 > 0 and current_price >= ema9
            close_above_vwap = selected_vwap > 0 and current_price >= selected_vwap
            other_gap = self.config.pretrigger_reclaim_single_anchor_other_max_gap_pct
            near_other_anchor = (
                (close_above_ema9 and selected_vwap > 0 and current_price >= selected_vwap * (1.0 - other_gap))
                or (close_above_vwap and ema9 > 0 and current_price >= ema9 * (1.0 - other_gap))
            )
            if not near_other_anchor:
                return False, "pretrigger reclaim single-anchor too far from other anchor"
            if not single_anchor_candle_ok:
                return False, "pretrigger reclaim single-anchor candle too weak"
            if current_bar_rel_vol < self.config.pretrigger_reclaim_single_anchor_min_bar_rel_vol:
                return False, "pretrigger reclaim single-anchor volume too weak"
            return True, ""
        near_ema9 = ema9 > 0 and current_price >= ema9 * (1.0 - self.config.pretrigger_reclaim_touch_recovery_max_below_ema9_pct)
        near_vwap = selected_vwap > 0 and current_price >= selected_vwap * (1.0 - self.config.pretrigger_reclaim_touch_recovery_max_below_vwap_pct)
        if not (near_ema9 or near_vwap):
            return False, "pretrigger reclaim touch recovery too far from anchors"
        touch_retested = current_low <= max(
            ema9 * (1.0 + self.config.pretrigger_reclaim_touch_tolerance_pct) if ema9 > 0 else 0.0,
            selected_vwap * (1.0 + self.config.pretrigger_reclaim_touch_tolerance_pct) if selected_vwap > 0 else 0.0,
        )
        if not touch_retested:
            return False, "pretrigger reclaim touch recovery did not retest anchor"
        return True, ""

    def _reclaim_pullback_reason(
        self,
        *,
        pullback_pct: float,
        retrace_fraction: float,
        pullback_pct_ok: bool,
        leg_retrace_ok: bool,
    ) -> str:
        if pullback_pct_ok or leg_retrace_ok:
            return ""
        if pullback_pct < self.config.pretrigger_reclaim_min_pullback_from_high_pct:
            return (
                f"pretrigger reclaim pullback too shallow: {pullback_pct * 100:.2f}% "
                f"< {self.config.pretrigger_reclaim_min_pullback_from_high_pct * 100:.2f}%"
            )
        if pullback_pct > self.config.pretrigger_reclaim_max_pullback_from_high_pct:
            return (
                f"pretrigger reclaim pullback too deep: {pullback_pct * 100:.2f}% "
                f"> {self.config.pretrigger_reclaim_max_pullback_from_high_pct * 100:.2f}%"
            )
        return (
            f"pretrigger reclaim leg retrace {retrace_fraction:.2f} outside "
            f"{self.config.pretrigger_reclaim_min_retrace_fraction_of_leg:.2f}-"
            f"{self.config.pretrigger_reclaim_max_retrace_fraction_of_leg:.2f}"
        )

    def _reclaim_touch_reason(
        self,
        *,
        touched_ema9: bool,
        touched_vwap: bool,
    ) -> str:
        if touched_ema9 or touched_vwap:
            return ""
        return (
            "pretrigger reclaim no EMA9/VWAP touch in last "
            f"{self.config.pretrigger_reclaim_touch_lookback_bars} bars"
        )

    @staticmethod
    def _reclaim_higher_low_reason(*, pullback_low: float, pre_spike_price: float, threshold_pct: float) -> str:
        required = pre_spike_price * (1.0 + threshold_pct)
        return (
            f"pretrigger reclaim higher low failed: low {pullback_low:.4f} <= "
            f"required {required:.4f}"
        )

    @staticmethod
    def _reclaim_held_move_reason(*, pullback_low: float, held_move_floor: float) -> str:
        return (
            f"pretrigger reclaim held move failed: low {pullback_low:.4f} <= "
            f"held floor {held_move_floor:.4f}"
        )

    def _reclaim_reentry_reset_result(
        self,
        *,
        ticker: str,
        recent: Sequence[dict[str, float]],
        current_high: float,
        current_low: float,
        ema9: float,
        selected_vwap: float,
        touch_tol: float,
    ) -> tuple[bool, str]:
        if not self.config.pretrigger_reclaim_require_reentry_reset:
            return True, ""

        last_exit_bar = self._last_exit_bar.get(ticker)
        if last_exit_bar is None:
            return True, ""

        post_exit_bars = [
            bar
            for bar in recent
            if int(bar.get("bar_index", -1)) > last_exit_bar
        ]
        if not post_exit_bars:
            return True, ""

        reset_window = post_exit_bars[-max(1, self.config.pretrigger_reclaim_reentry_touch_lookback_bars) :]
        reset_high = max([current_high, *(float(bar["high"]) for bar in reset_window)])
        reset_low = min([current_low, *(float(bar["low"]) for bar in reset_window)])
        reset_pct = (reset_high - reset_low) / reset_high if reset_high > 0 else 0.0
        fresh_touch = any(
            (
                float(bar.get("ema9", 0) or 0) > 0
                and float(bar["low"]) <= float(bar["ema9"]) * (1.0 + touch_tol)
            )
            or (
                float(bar.get("selected_vwap", 0) or 0) > 0
                and float(bar["low"]) <= float(bar["selected_vwap"]) * (1.0 + touch_tol)
            )
            for bar in reset_window
        )
        fresh_touch = fresh_touch or (
            (ema9 > 0 and current_low <= ema9 * (1.0 + touch_tol))
            or (selected_vwap > 0 and current_low <= selected_vwap * (1.0 + touch_tol))
        )
        if not fresh_touch:
            return False, "pretrigger reclaim reentry reset missing fresh EMA9/VWAP touch"
        if reset_pct < self.config.pretrigger_reclaim_reentry_min_reset_from_high_pct:
            return (
                False,
                (
                    f"pretrigger reclaim reentry reset too shallow: {reset_pct * 100:.2f}% "
                    f"< {self.config.pretrigger_reclaim_reentry_min_reset_from_high_pct * 100:.2f}%"
                ),
            )
        return True, ""

    @staticmethod
    def _reclaim_absorption_reason(*, average_pullback_volume: float, spike_volume: float, max_ratio: float) -> str:
        ratio = average_pullback_volume / spike_volume if spike_volume > 0 else 999.0
        return (
            f"pretrigger reclaim pullback volume too heavy: ratio {ratio:.2f} > {max_ratio:.2f}"
        )

    @staticmethod
    def _reclaim_trend_reason(*, current_price: float, ema9: float, ema20: float) -> str:
        if current_price <= ema20 and ema9 < ema20:
            return (
                f"pretrigger reclaim trend weak: price {current_price:.4f} <= EMA20 {ema20:.4f} "
                f"and EMA9 {ema9:.4f} < EMA20"
            )
        if current_price <= ema20:
            return f"pretrigger reclaim trend weak: price {current_price:.4f} <= EMA20 {ema20:.4f}"
        if ema9 < ema20:
            return f"pretrigger reclaim trend weak: EMA9 {ema9:.4f} < EMA20 {ema20:.4f}"
        return "pretrigger reclaim trend not ready"

    @staticmethod
    def _reclaim_momentum_reason(
        *,
        histogram_now: float,
        histogram_prev: float,
        macd_now: float,
        macd_prev: float,
        signal_now: float,
        macd_near_signal: bool,
    ) -> str:
        if histogram_now <= 0:
            return f"pretrigger reclaim momentum weak: histogram {histogram_now:.4f} <= 0"
        if histogram_now <= histogram_prev:
            return (
                f"pretrigger reclaim momentum weak: histogram {histogram_now:.4f} "
                f"not above prior {histogram_prev:.4f}"
            )
        if macd_now < macd_prev:
            return f"pretrigger reclaim momentum weak: MACD {macd_now:.4f} < prior {macd_prev:.4f}"
        if macd_now <= signal_now and not macd_near_signal:
            return (
                f"pretrigger reclaim momentum weak: MACD {macd_now:.4f} "
                f"below signal {signal_now:.4f}"
            )
        return "pretrigger reclaim momentum not ready"

    def _reclaim_volume_reason(self, *, bar_rel_vol: float) -> str:
        return (
            f"pretrigger reclaim volume weak: rel vol {bar_rel_vol:.2f} < "
            f"{self.config.pretrigger_reclaim_min_bar_rel_vol:.2f}"
        )

    @staticmethod
    def _reclaim_candle_reason(
        *,
        current_price: float,
        current_open: float,
        body_pct: float,
        close_pos_pct: float,
        upper_wick_pct: float,
        min_body_pct: float,
        min_close_pos_pct: float,
        max_upper_wick_pct: float,
        label: str,
    ) -> str:
        if current_price <= current_open:
            return f"pretrigger reclaim {label}: close did not recover above open"
        if body_pct < min_body_pct:
            return (
                f"pretrigger reclaim {label}: body {body_pct * 100:.0f}% < "
                f"{min_body_pct * 100:.0f}%"
            )
        if close_pos_pct < min_close_pos_pct:
            return (
                f"pretrigger reclaim {label}: close position {close_pos_pct * 100:.0f}% < "
                f"{min_close_pos_pct * 100:.0f}%"
            )
        if upper_wick_pct > max_upper_wick_pct:
            return (
                f"pretrigger reclaim {label}: upper wick {upper_wick_pct * 100:.0f}% > "
                f"{max_upper_wick_pct * 100:.0f}%"
            )
        return f"pretrigger reclaim {label} not ready"

    def _starter_quantity(self) -> int:
        quantity = int(round(self.config.default_quantity * self.config.pretrigger_entry_size_factor))
        return max(1, quantity)

    def _pretrigger_starter_path(self) -> str:
        if self.config.entry_logic_mode == "pretrigger_reclaim":
            return "PRETRIGGER_RECLAIM"
        if self.config.entry_logic_mode == "pretrigger_retest":
            return "PRETRIGGER_RETEST"
        return "PRETRIGGER_PROBE"

    def _confirm_add_quantity(self, starter_quantity: int) -> int:
        target_total = int(round(self.config.default_quantity * self.config.pretrigger_confirm_entry_size_factor))
        return max(0, target_total - starter_quantity)

    def _has_filled_position(self, position_tracker, ticker: str) -> bool:
        positions = getattr(position_tracker, "positions", None)
        if positions is None or not hasattr(positions, "has_position"):
            return False
        return bool(positions.has_position(ticker))

    def _position_peak_profit_pct(self, position_tracker, ticker: str) -> float | None:
        positions = getattr(position_tracker, "positions", None)
        if positions is None or not hasattr(positions, "get_position"):
            return None
        position = positions.get_position(ticker)
        if position is None:
            return None
        return float(getattr(position, "peak_profit_pct", 0.0) or 0.0)

    def _current_bar_rel_vol(self, ticker: str, current_volume: float) -> float:
        recent = self._recent_bars.get(ticker, [])
        volume_avg_bars = min(len(recent), self.config.pretrigger_volume_avg_bars)
        if volume_avg_bars <= 0:
            return 0.0
        avg_vol = sum(float(bar["volume"]) for bar in recent[-volume_avg_bars:]) / float(volume_avg_bars)
        if avg_vol <= 0:
            return 0.0
        return current_volume / avg_vol

    def _histogram_surge_ok(self, ticker: str, indicators: dict[str, float | bool]) -> bool:
        recent = self._recent_bars.get(ticker, [])
        if len(recent) < 5:
            return False
        deltas = []
        previous_hist = float(recent[-5].get("histogram", 0) or 0)
        for bar in recent[-4:]:
            current_hist = float(bar.get("histogram", 0) or 0)
            deltas.append(abs(current_hist - previous_hist))
            previous_hist = current_hist
        average_delta = sum(deltas) / len(deltas) if deltas else 0.0
        current_delta = float(indicators.get("histogram", 0) or 0) - float(indicators.get("histogram_prev", 0) or 0)
        if isclose(average_delta, 0.0):
            return current_delta > 0
        return current_delta > 1.5 * average_delta

    def _average_true_range(self, bars: list[dict[str, float]]) -> float:
        if len(bars) < 2:
            return 0.0
        true_ranges: list[float] = []
        previous_close = float(bars[0]["close"])
        for bar in bars[1:]:
            high = float(bar["high"])
            low = float(bar["low"])
            tr = max(high - low, abs(high - previous_close), abs(low - previous_close))
            true_ranges.append(tr)
            previous_close = float(bar["close"])
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

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
        if float(indicators.get("stoch_k", 0) or 0) >= self.config.stoch_entry_cap:
            return {"passed": False, "reason": f"stochK at or above cap ({self.config.stoch_entry_cap:.0f})"}
        if self.config.ema9_max_distance_gate_enabled:
            ema9 = float(indicators.get("ema9", 0) or 0)
            price = float(indicators.get("price", 0) or 0)
            if ema9 > 0 and price > ema9:
                ema9_distance = (price - ema9) / ema9
                if ema9_distance >= self.config.ema9_max_distance_pct:
                    return {
                        "passed": False,
                        "reason": (
                            f"price {ema9_distance * 100:.2f}% above EMA9 "
                            f">= {self.config.ema9_max_distance_pct * 100:.2f}%"
                        ),
                    }

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

    def _check_confirmation_guards(
        self,
        ticker: str,
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, str | bool]:
        eastern_now = self.now_provider()
        if eastern_now.hour < self.config.trading_start_hour or eastern_now.hour >= self.config.trading_end_hour:
            return {"passed": False, "reason": f"outside trading hours ({eastern_now.hour}:00 ET)"}

        time_str = eastern_now.strftime("%H:%M")
        if self.config.dead_zone_start <= time_str < self.config.dead_zone_end:
            return {"passed": False, "reason": f"in dead zone ({time_str} ET)"}

        if position_tracker and position_tracker.has_position(ticker):
            return {"passed": False, "reason": "already in position"}

        if self._last_buy_bar.get(ticker, -1) == bar_index:
            return {"passed": False, "reason": "dedup (already fired this bar)"}

        return {"passed": True, "reason": ""}

    def _check_paths(self, ticker: str, indicators: dict[str, float | bool]) -> str | None:
        if self.config.entry_logic_mode == "tos_script":
            return self._check_tos_script_paths(ticker, indicators)

        if bool(indicators["macd_cross_above"]):
            if not self.config.p1_require_below_3bars or bool(indicators.get("macd_was_below_3bars", False)):
                logger.debug("[%s] %s - P1 MACD Cross triggered", self.name, ticker)
                return "P1_MACD_CROSS"

        if (
            self._price_cross_above_selected_vwap(indicators)
            and bool(indicators["macd_above_signal"])
            and bool(indicators["macd_increasing"])
        ):
            logger.debug("[%s] %s - P2 VWAP Breakout triggered", self.name, ticker)
            return "P2_VWAP_BREAKOUT"

        if (
            bool(indicators["macd_above_signal"])
            and not bool(indicators["macd_cross_above"])
            and float(indicators["macd_delta"]) >= self.config.surge_rate
            and bool(indicators.get("macd_delta_accelerating", False))
            and float(indicators["histogram"]) >= self.config.p3_histogram_floor
            and bool(indicators.get("price_above_ema9", False))
        ):
            logger.debug("[%s] %s - P3 MACD Surge triggered", self.name, ticker)
            return "P3_MACD_SURGE"

        return None

    def _check_tos_script_paths(self, ticker: str, indicators: dict[str, float | bool]) -> str | None:
        volume_ok = float(indicators.get("volume", 0) or 0) > self.config.vol_min
        vwap_filter = self._tos_vwap_filter(indicators)

        if (
            bool(indicators["macd_cross_above"])
            and bool(indicators["macd_increasing"])
            and volume_ok
            and vwap_filter
        ):
            logger.debug("[%s] %s - TOS P1 MACD Cross triggered", self.name, ticker)
            return "P1_MACD_CROSS"

        if (
            self._price_cross_above_selected_vwap(indicators)
            and bool(indicators["macd_above_signal"])
            and bool(indicators["macd_increasing"])
            and volume_ok
        ):
            logger.debug("[%s] %s - TOS P2 VWAP Breakout triggered", self.name, ticker)
            return "P2_VWAP_BREAKOUT"

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
            self._record_decision(ticker, status="blocked", reason="confirmation lost: MACD below signal")
            return None

        breakout_floor = float(pending.get("breakout_floor", pending.get("breakout_level", pending["trigger_price"])))
        if float(indicators["price"]) < breakout_floor:
            del self._pending[ticker]
            self._record_decision(ticker, status="blocked", reason="confirmation lost: close below breakout level")
            return None

        if int(pending["bars_waiting"]) < self.config.confirm_bars:
            self._record_decision(
                ticker,
                status="pending",
                reason=f'{pending["path"]} confirming ({pending["bars_waiting"]}/{self.config.confirm_bars})',
                path=str(pending["path"]),
            )
            return None

        path = str(pending["path"])
        score = int(pending.get("trigger_score", 0))
        details = str(pending.get("trigger_score_details", ""))
        del self._pending[ticker]
        self._last_buy_bar[ticker] = bar_index
        self._record_decision(
            ticker,
            status="signal",
            reason=path,
            path=path,
            score=score,
            score_details=details,
        )
        logger.info("[%s] BUY SIGNAL %s | %s | score=%s/6", self.name, ticker, path, score)
        return self._build_buy_signal(ticker, path, indicators, score, details)

    def _check_setup_quality(
        self,
        ticker: str,
        path: str,
        indicators: dict[str, float | bool],
    ) -> dict[str, str | bool | int | float]:
        for result in (
            self._check_precondition(ticker),
            self._check_structure(ticker, indicators),
            self._check_anti_chase(indicators),
        ):
            if not result["passed"]:
                return {
                    "passed": False,
                    "reason": str(result["reason"]),
                }

        score, score_details = self._quality_score(indicators)
        required_score = self._required_score_for_path(path)
        if score < required_score:
            return {
                "passed": False,
                "reason": f"score {score} below required {required_score}",
                "score": score,
                "score_details": score_details,
            }

        return {
            "passed": True,
            "reason": "",
            "score": score,
            "score_details": score_details,
            "required_score": required_score,
            "breakout_level": self._breakout_level(path, indicators),
        }

    def _check_precondition(self, ticker: str) -> dict[str, str | bool]:
        if not self.config.entry_preconditions_enabled:
            return {"passed": True, "reason": ""}

        recent = self._recent_bars.get(ticker, [])
        lookback_bars = self.config.entry_precondition_lookback_bars
        if len(recent) < lookback_bars:
            return {"passed": True, "reason": ""}

        lookback = recent[-lookback_bars:]
        latest = lookback[-1]

        vwap_dist = self._pct_distance(latest["price"], latest["selected_vwap"])
        if vwap_dist > self.config.entry_hard_block_max_vwap_dist_pct:
            return {
                "passed": False,
                "reason": (
                    f"precondition hard block: prior bar {vwap_dist * 100:.2f}% from VWAP "
                    f"> {self.config.entry_hard_block_max_vwap_dist_pct * 100:.2f}%"
                ),
            }
        if vwap_dist > self.config.entry_precondition_max_vwap_dist_pct:
            return {
                "passed": False,
                "reason": (
                    f"precondition failed: prior bar {vwap_dist * 100:.2f}% from VWAP "
                    f"> {self.config.entry_precondition_max_vwap_dist_pct * 100:.2f}%"
                ),
            }

        ema9_dist = self._pct_distance(latest["price"], latest["ema9"])
        if ema9_dist > self.config.entry_precondition_max_ema9_dist_pct:
            return {
                "passed": False,
                "reason": (
                    f"precondition failed: prior bar {ema9_dist * 100:.2f}% from EMA9 "
                    f"> {self.config.entry_precondition_max_ema9_dist_pct * 100:.2f}%"
                ),
            }

        avg_vol = sum(bar["volume"] for bar in lookback) / len(lookback)
        if avg_vol <= 0:
            return {"passed": False, "reason": "precondition failed: recent volume average is zero"}

        vol_ratio = latest["volume"] / avg_vol
        if vol_ratio < self.config.entry_precondition_min_vol_ratio:
            return {
                "passed": False,
                "reason": (
                    f"precondition failed: prior bar volume ratio {vol_ratio:.2f} "
                    f"< {self.config.entry_precondition_min_vol_ratio:.2f}"
                ),
            }
        if vol_ratio > self.config.entry_precondition_max_vol_ratio:
            return {
                "passed": False,
                "reason": (
                    f"precondition failed: prior bar volume ratio {vol_ratio:.2f} "
                    f"> {self.config.entry_precondition_max_vol_ratio:.2f}"
                ),
            }

        return {"passed": True, "reason": ""}

    def _check_structure(self, ticker: str, indicators: dict[str, float | bool]) -> dict[str, str | bool]:
        if not self.config.entry_structure_filter_enabled:
            return {"passed": True, "reason": ""}

        recent = self._recent_bars.get(ticker, [])
        if not recent:
            return {"passed": True, "reason": ""}

        lookback = recent[-self.config.entry_structure_recent_high_lookback_bars :]
        reference_high = max(
            self._session_highs.get(ticker, 0.0),
            max(bar["high"] for bar in lookback),
        )
        if reference_high <= 0:
            return {"passed": True, "reason": ""}

        current_price = float(indicators["price"])
        breakout_level = reference_high * (1 + self.config.entry_structure_breakout_margin_pct)
        if current_price >= breakout_level:
            return {"passed": True, "reason": ""}

        near_high_level = reference_high * (1 - self.config.entry_structure_near_high_block_pct)
        if current_price >= near_high_level:
            return {
                "passed": False,
                "reason": (
                    f"structure block: price {current_price:.4f} near high {reference_high:.4f} "
                    "without breakout"
                ),
            }

        return {"passed": True, "reason": ""}

    def _check_anti_chase(self, indicators: dict[str, float | bool]) -> dict[str, str | bool]:
        if not self.config.entry_preconditions_enabled:
            return {"passed": True, "reason": ""}

        distance = self._pct_distance(float(indicators["price"]), self._selected_vwap_value(indicators))
        if distance > self.config.entry_hard_block_max_vwap_dist_pct:
            return {
                "passed": False,
                "reason": (
                    f"anti-chase hard block: price {distance * 100:.2f}% from VWAP "
                    f"> {self.config.entry_hard_block_max_vwap_dist_pct * 100:.2f}%"
                ),
            }
        if distance > self.config.entry_anti_chase_max_vwap_dist_pct:
            return {
                "passed": False,
                "reason": (
                    f"anti-chase blocked: price {distance * 100:.2f}% from VWAP "
                    f"> {self.config.entry_anti_chase_max_vwap_dist_pct * 100:.2f}%"
                ),
            }
        return {"passed": True, "reason": ""}

    @staticmethod
    def _pct_distance(value: float, baseline: float) -> float:
        if baseline <= 0:
            return 999.0
        return abs(value - baseline) / baseline

    def _recent_bar_snapshot(
        self,
        indicators: dict[str, float | bool],
        *,
        bar_index: int | None = None,
    ) -> dict[str, float] | None:
        required_fields = ("open", "price", "high", "low", "volume", "ema9", "ema20", "vwap")
        if any(field not in indicators for field in required_fields):
            return None
        snapshot = {
            "open": float(indicators["open"]),
            "price": float(indicators["price"]),
            "close": float(indicators["price"]),
            "high": float(indicators["high"]),
            "low": float(indicators["low"]),
            "volume": float(indicators["volume"]),
            "ema9": float(indicators["ema9"]),
            "ema20": float(indicators["ema20"]),
            "macd": float(indicators.get("macd", 0) or 0),
            "signal": float(indicators.get("signal", 0) or 0),
            "histogram": float(indicators.get("histogram", 0) or 0),
            "selected_vwap": self._selected_vwap_value(indicators),
        }
        if bar_index is not None:
            snapshot["bar_index"] = float(bar_index)
        return snapshot

    def _remember_bar(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int | None = None,
    ) -> None:
        if bar_index is None:
            recent = self._recent_bars.get(ticker, [])
            if recent:
                last_bar_index = int(recent[-1].get("bar_index", len(recent) - 1))
                bar_index = last_bar_index + 1
            else:
                bar_index = 0
        snapshot = self._recent_bar_snapshot(indicators, bar_index=bar_index)
        if snapshot is None:
            return
        recent = self._recent_bars.setdefault(ticker, [])
        high = snapshot["high"]
        self._session_highs[ticker] = max(self._session_highs.get(ticker, high), high)
        recent.append(snapshot)
        max_keep = max(
            24,
            self.config.entry_precondition_lookback_bars + 2,
            self.config.pretrigger_reclaim_lookback_bars + 2,
            self.config.pretrigger_reclaim_reentry_touch_lookback_bars + 2,
        )
        if len(recent) > max_keep:
            del recent[:-max_keep]

    def _quality_score(self, indicators: dict[str, float | bool]) -> tuple[int, str]:
        score = 0
        parts: list[str] = []

        for passed, label in (
            (bool(indicators["histogram_growing"]), "hist"),
            (bool(indicators["stoch_k_rising"]), "stK"),
            (self._price_above_selected_vwap(indicators), "vwap"),
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

    def _required_score_for_path(self, path: str) -> int:
        if self.config.min_score <= 0:
            return 0
        return 5 if path == "P3_MACD_SURGE" else self.config.min_score

    def _breakout_level(self, path: str, indicators: dict[str, float | bool]) -> float:
        selected_vwap = self._selected_vwap_value(indicators)
        ema9 = float(indicators.get("ema9", 0) or 0)
        if path == "P2_VWAP_BREAKOUT":
            return selected_vwap
        if path == "P3_MACD_SURGE":
            return max(selected_vwap, ema9)
        return max(selected_vwap, ema9)

    def _build_buy_signal(
        self,
        ticker: str,
        path: str,
        indicators: dict[str, float | bool],
        score: int,
        score_details: str,
        *,
        quantity: int | None = None,
        stage: str = "",
    ) -> dict[str, float | int | str]:
        return {
            "action": "BUY",
            "ticker": ticker,
            "path": path,
            "quantity": int(quantity or self.config.default_quantity),
            "entry_stage": stage,
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
            "extended_vwap": float(indicators.get("extended_vwap", indicators["vwap"])),
            "decision_vwap": self._selected_vwap_value(indicators),
            "bar_volume": float(indicators["volume"]),
        }

    def _build_sell_signal(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        *,
        reason: str,
    ) -> dict[str, float | int | str]:
        return {
            "action": "SELL",
            "ticker": ticker,
            "reason": reason,
            "price": float(indicators["price"]),
        }

    def _selected_vwap_value(self, indicators: dict[str, float | bool]) -> float:
        if self._use_extended_vwap():
            return float(indicators.get("extended_vwap", indicators["vwap"]))
        return float(indicators["vwap"])

    def _price_above_selected_vwap(self, indicators: dict[str, float | bool]) -> bool:
        if self._use_extended_vwap():
            return bool(indicators.get("price_above_extended_vwap", indicators["price_above_vwap"]))
        return bool(indicators["price_above_vwap"])

    def _price_cross_above_selected_vwap(self, indicators: dict[str, float | bool]) -> bool:
        if self._use_extended_vwap():
            return bool(indicators.get("price_cross_above_extended_vwap", indicators["price_cross_above_vwap"]))
        return bool(indicators["price_cross_above_vwap"])

    def _tos_vwap_filter(self, indicators: dict[str, float | bool]) -> bool:
        if not self.config.require_vwap_filter:
            return True
        if self._price_above_selected_vwap(indicators):
            return True
        return self.config.allow_vwap_cross_entry and self._price_cross_above_selected_vwap(indicators)

    def _use_extended_vwap(self) -> bool:
        if self.config.entry_vwap_mode == "extended":
            return True
        if self.config.entry_vwap_mode != "session_aware":
            return False
        now = self.now_provider()
        return (now.hour * 60 + now.minute) < (9 * 60 + 30)

    def _record_decision(
        self,
        ticker: str,
        *,
        status: str,
        reason: str,
        path: str | None = None,
        score: int | None = None,
        score_details: str | None = None,
    ) -> None:
        decision = {
            "status": status,
            "reason": reason,
        }
        if path:
            decision["path"] = path
        if score is not None:
            decision["score"] = str(score)
        if score_details:
            decision["score_details"] = score_details
        self._last_decision[ticker] = decision
