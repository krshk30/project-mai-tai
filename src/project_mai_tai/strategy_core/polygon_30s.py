from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import time

from project_mai_tai.strategy_core.indicators import ema, macd, sma, stoch_k, vwap
from project_mai_tai.strategy_core.models import OHLCVBar
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ, now_eastern
from project_mai_tai.strategy_core.trading_config import TradingConfig

logger = logging.getLogger(__name__)


def _bar_value(bar: OHLCVBar | Mapping[str, float | int], field: str) -> float:
    if isinstance(bar, OHLCVBar):
        return float(getattr(bar, field))
    return float(bar[field])


def _bar_int_value(bar: OHLCVBar | Mapping[str, float | int], field: str, default: int = 0) -> int:
    if isinstance(bar, OHLCVBar):
        return int(getattr(bar, field, default))
    value = bar.get(field, default) if isinstance(bar, Mapping) else default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_synthetic_bar(bar: OHLCVBar | Mapping[str, float | int]) -> bool:
    return _bar_int_value(bar, "trade_count", 0) <= 0 and _bar_value(bar, "volume") <= 0.0


def _resolve_timestamp(timestamp_ns: int, fallback: Callable[[], float]) -> float:
    if timestamp_ns and timestamp_ns > 1_000_000_000_000_000_000:
        return timestamp_ns / 1_000_000_000
    if timestamp_ns and timestamp_ns > 1_000_000_000_000:
        return timestamp_ns / 1_000
    return fallback()


def _series_cross(current_value: float, previous_value: float, current_level: float, previous_level: float) -> bool:
    return (current_value > current_level and previous_value <= previous_level) or (
        current_value < current_level and previous_value >= previous_level
    )


def _atr_value(bars: Sequence[dict[str, float]], length: int) -> float:
    if not bars or length <= 0:
        return 0.0
    true_ranges: list[float] = []
    previous_close: float | None = None
    for bar in bars:
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(max(0.0, true_range))
        previous_close = close
    atr = true_ranges[0]
    for true_range in true_ranges[1:]:
        atr = ((atr * (length - 1)) + true_range) / length
    return atr


class Polygon30sBarBuilder:
    def __init__(
        self,
        ticker: str,
        interval_secs: int = 30,
        max_bars: int = 2000,
        time_provider: Callable[[], float] | None = None,
        close_grace_seconds: float = 0.0,
        fill_gap_bars: bool = True,
    ) -> None:
        self.ticker = ticker
        self.interval_secs = interval_secs
        self.max_bars = max_bars
        self.time_provider = time_provider or time.time
        self.close_grace_seconds = max(0.0, float(close_grace_seconds))
        self.fill_gap_bars = bool(fill_gap_bars)
        self.bars: list[OHLCVBar] = []
        self._current_bar: OHLCVBar | None = None
        self._current_bar_start = 0.0
        self._current_bar_last_cum_volume: int | None = None
        self._bar_count = 0
        self._current_bar_components: dict[float, OHLCVBar] = {}
        self._last_closed_bar_components: dict[float, OHLCVBar] = {}
        self._recent_revised_closed_bar: OHLCVBar | None = None

    def on_trade(
        self,
        price: float,
        size: int,
        timestamp_ns: int = 0,
        cumulative_volume: int | None = None,
    ) -> list[OHLCVBar]:
        if price <= 0:
            return []

        now_ts = _resolve_timestamp(timestamp_ns, self.time_provider)
        bucket_start = (now_ts // self.interval_secs) * self.interval_secs
        completed: list[OHLCVBar] = []
        delta_volume = self._resolve_volume_delta(size, cumulative_volume)

        if self._current_bar is None and self.bars and bucket_start <= self.bars[-1].timestamp:
            if bucket_start == self.bars[-1].timestamp and _is_synthetic_bar(self.bars[-1]):
                self._pop_last_closed_bar()
                self._current_bar = OHLCVBar.from_trade(price, max(0, delta_volume), bucket_start)
                self._current_bar_start = bucket_start
                self._current_bar_last_cum_volume = cumulative_volume
                return completed
            logger.debug(
                "[POLYGON30] Ignoring stale trade for %s at %.3f (<= last closed %.3f)",
                self.ticker,
                bucket_start,
                self.bars[-1].timestamp,
            )
            return completed

        if self._current_bar is None:
            if self.fill_gap_bars:
                completed.extend(self._fill_missing_gaps_until(bucket_start))
            self._current_bar = OHLCVBar.from_trade(price, max(0, delta_volume), bucket_start)
            self._current_bar_start = bucket_start
            self._current_bar_last_cum_volume = cumulative_volume
            return completed

        if bucket_start < self._current_bar_start:
            logger.debug(
                "[POLYGON30] Ignoring stale trade for %s at %.3f (< current %.3f)",
                self.ticker,
                bucket_start,
                self._current_bar_start,
            )
            return completed

        if bucket_start > self._current_bar_start:
            completed.append(self._close_current_bar())
            if self.fill_gap_bars:
                completed.extend(self._fill_gap_bars(self._current_bar_start + self.interval_secs, bucket_start))
            self._current_bar = OHLCVBar.from_trade(price, max(0, delta_volume), bucket_start)
            self._current_bar_start = bucket_start
            self._current_bar_last_cum_volume = cumulative_volume
            return completed

        self._current_bar.update(price, max(0, delta_volume))
        self._current_bar_last_cum_volume = cumulative_volume
        return completed

    def on_bar(self, bar: OHLCVBar) -> list[OHLCVBar]:
        if bar.close <= 0:
            return []

        self._recent_revised_closed_bar = None
        component_timestamp = float(bar.timestamp)
        bar_start = (component_timestamp // self.interval_secs) * self.interval_secs
        completed: list[OHLCVBar] = []

        if self._current_bar is None and self.bars and bar_start <= self.bars[-1].timestamp:
            if self._revise_last_closed_bar(component_timestamp, bar):
                return completed
            logger.debug(
                "[POLYGON30] Ignoring stale aggregate bar for %s at %.3f (<= last closed %.3f)",
                self.ticker,
                bar_start,
                self.bars[-1].timestamp,
            )
            return completed

        if self._current_bar is None:
            self._current_bar_components = {
                component_timestamp: OHLCVBar.from_bar(bar, timestamp=component_timestamp)
            }
            self._current_bar = self._build_bar_from_components(
                bar_start=bar_start,
                component_bars=self._current_bar_components,
            )
            self._current_bar_start = bar_start
            self._current_bar_last_cum_volume = None
            return completed

        if bar_start < self._current_bar_start:
            if self._revise_last_closed_bar(component_timestamp, bar):
                return completed
            logger.debug(
                "[POLYGON30] Ignoring stale aggregate bar for %s at %.3f (< current %.3f)",
                self.ticker,
                bar_start,
                self._current_bar_start,
            )
            return completed

        if bar_start > self._current_bar_start:
            completed.append(self._close_current_bar())
            self._current_bar_components = {
                component_timestamp: OHLCVBar.from_bar(bar, timestamp=component_timestamp)
            }
            self._current_bar = self._build_bar_from_components(
                bar_start=bar_start,
                component_bars=self._current_bar_components,
            )
            self._current_bar_start = bar_start
            self._current_bar_last_cum_volume = None
            return completed

        self._current_bar_components[component_timestamp] = OHLCVBar.from_bar(bar, timestamp=component_timestamp)
        self._current_bar = self._build_bar_from_components(
            bar_start=bar_start,
            component_bars=self._current_bar_components,
        )
        return completed

    def on_final_bar(self, bar: OHLCVBar) -> list[OHLCVBar]:
        if bar.close <= 0:
            return []

        bar_start = (bar.timestamp // self.interval_secs) * self.interval_secs
        aligned_bar = OHLCVBar.from_bar(bar, timestamp=bar_start)

        if self._current_bar is not None and bar_start <= self._current_bar_start:
            if bar_start < self._current_bar_start:
                logger.debug(
                    "[POLYGON30] Ignoring stale final aggregate bar for %s at %.3f (< current %.3f)",
                    self.ticker,
                    bar_start,
                    self._current_bar_start,
                )
                return []
            self._current_bar = None
            self._current_bar_start = 0.0
            self._current_bar_last_cum_volume = None

        if self.bars:
            last_bar_start = self.bars[-1].timestamp
            if bar_start < last_bar_start:
                logger.debug(
                    "[POLYGON30] Ignoring stale final aggregate bar for %s at %.3f (< last %.3f)",
                    self.ticker,
                    bar_start,
                    last_bar_start,
                )
                return []
            if bar_start == last_bar_start:
                self.bars[-1] = aligned_bar
                return []

        self.bars.append(aligned_bar)
        self._bar_count += 1
        self._trim_history()
        return [aligned_bar]

    def check_bar_closes(self) -> list[OHLCVBar]:
        completed: list[OHLCVBar] = []
        now_ts = self.time_provider()
        effective_now_ts = max(0.0, now_ts - self.close_grace_seconds)
        now_bucket = (effective_now_ts // self.interval_secs) * self.interval_secs

        if self._current_bar is not None and effective_now_ts >= self._current_bar_start + self.interval_secs:
            completed.append(self._close_current_bar())
            if self.fill_gap_bars:
                completed.extend(self._fill_gap_bars(self._current_bar_start + self.interval_secs, now_bucket))
            self._current_bar = None
            # Keep _current_bar_last_cum_volume so the next trade computes a real
            # cumulative-volume delta. Resetting it here forces _resolve_volume_delta
            # to fall back to last_size for the first trade of every periodically-
        # closed bucket, which under-counts volume/trade count on the Polygon 1s aggregate path.
            return completed

        if self._current_bar is None and self.fill_gap_bars:
            completed.extend(self._fill_missing_gaps_until(now_bucket))
        return completed

    def get_current_price(self) -> float | None:
        if self._current_bar is not None:
            return self._current_bar.close
        if self.bars:
            return self.bars[-1].close
        return None

    def get_bar_count(self) -> int:
        return self._bar_count

    def get_bars_as_dicts(self) -> list[dict[str, float | int]]:
        return [bar.as_dict() for bar in self.bars]

    def get_bars_with_current_as_dicts(self) -> list[dict[str, float | int]]:
        bars = self.get_bars_as_dicts()
        if self._current_bar is not None:
            bars.append(self._current_bar.as_dict())
        return bars

    def reset(self) -> None:
        self.bars.clear()
        self._current_bar = None
        self._current_bar_start = 0.0
        self._current_bar_last_cum_volume = None
        self._bar_count = 0
        self._current_bar_components.clear()
        self._last_closed_bar_components.clear()
        self._recent_revised_closed_bar = None

    def _resolve_volume_delta(self, size: int, cumulative_volume: int | None) -> int:
        if cumulative_volume is None:
            return max(0, int(size))
        if self._current_bar_last_cum_volume is None:
            return max(0, int(size))
        return max(0, int(cumulative_volume - self._current_bar_last_cum_volume))

    def _close_current_bar(self) -> OHLCVBar:
        if self._current_bar is None:
            raise RuntimeError("cannot close missing current bar")
        bar = self._current_bar
        self._last_closed_bar_components = {
            timestamp: OHLCVBar.from_bar(component, timestamp=component.timestamp)
            for timestamp, component in self._current_bar_components.items()
        }
        self.bars.append(bar)
        self._bar_count += 1
        self._trim_history()
        self._current_bar_components = {}
        return bar

    def _pop_last_closed_bar(self) -> OHLCVBar | None:
        if not self.bars:
            return None
        bar = self.bars.pop()
        self._bar_count = max(0, self._bar_count - 1)
        return bar

    def _append_flat_bar(self, start: float) -> OHLCVBar | None:
        last_price = self.get_current_price()
        if last_price is None:
            return None
        bar = OHLCVBar.flat_fill(last_price, start)
        self.bars.append(bar)
        self._bar_count += 1
        self._trim_history()
        return bar

    def _fill_gap_bars(self, start: float, end_exclusive: float) -> list[OHLCVBar]:
        completed: list[OHLCVBar] = []
        gap_start = start
        while gap_start < end_exclusive:
            flat = self._append_flat_bar(gap_start)
            if flat is not None:
                completed.append(flat)
            gap_start += self.interval_secs
        return completed

    def _fill_missing_gaps_until(self, next_bucket_start: float) -> list[OHLCVBar]:
        if not self.bars:
            return []
        expected_start = self.bars[-1].timestamp + self.interval_secs
        if next_bucket_start <= expected_start:
            return []
        return self._fill_gap_bars(expected_start, next_bucket_start)

    def _trim_history(self) -> None:
        if len(self.bars) > self.max_bars:
            self.bars = self.bars[-self.max_bars :]

    def consume_recent_revised_closed_bar(self) -> OHLCVBar | None:
        revised = self._recent_revised_closed_bar
        self._recent_revised_closed_bar = None
        return revised

    def _revise_last_closed_bar(self, component_timestamp: float, bar: OHLCVBar) -> bool:
        if not self.bars:
            return False

        last_closed = self.bars[-1]
        last_closed_start = float(last_closed.timestamp)
        component_bucket_start = (component_timestamp // self.interval_secs) * self.interval_secs
        if component_bucket_start != last_closed_start:
            return False

        self._last_closed_bar_components[component_timestamp] = OHLCVBar.from_bar(
            bar,
            timestamp=component_timestamp,
        )
        revised = self._build_bar_from_components(
            bar_start=last_closed_start,
            component_bars=self._last_closed_bar_components,
        )
        last_closed.open = revised.open
        last_closed.high = revised.high
        last_closed.low = revised.low
        last_closed.close = revised.close
        last_closed.volume = revised.volume
        last_closed.trade_count = revised.trade_count
        self._recent_revised_closed_bar = OHLCVBar.from_bar(revised, timestamp=revised.timestamp)
        logger.debug(
            "[POLYGON30] Revised last closed aggregate bar for %s at %.3f from late component %.3f",
            self.ticker,
            last_closed_start,
            component_timestamp,
        )
        return True

    @staticmethod
    def _build_bar_from_components(
        *,
        bar_start: float,
        component_bars: dict[float, OHLCVBar],
    ) -> OHLCVBar:
        ordered = [component_bars[key] for key in sorted(component_bars)]
        first = ordered[0]
        last = ordered[-1]
        return OHLCVBar(
            open=first.open,
            high=max(item.high for item in ordered),
            low=min(item.low for item in ordered),
            close=last.close,
            volume=sum(int(item.volume) for item in ordered),
            timestamp=float(bar_start),
            trade_count=sum(int(item.trade_count) for item in ordered),
        )


class Polygon30sBarBuilderManager:
    def __init__(
        self,
        interval_secs: int = 30,
        time_provider: Callable[[], float] | None = None,
        close_grace_seconds: float = 0.0,
        fill_gap_bars: bool = True,
    ) -> None:
        self.interval_secs = interval_secs
        self.time_provider = time_provider or time.time
        self.close_grace_seconds = max(0.0, float(close_grace_seconds))
        self.fill_gap_bars = bool(fill_gap_bars)
        self._builders: dict[str, Polygon30sBarBuilder] = {}

    def get_or_create(self, ticker: str) -> Polygon30sBarBuilder:
        if ticker not in self._builders:
            self._builders[ticker] = Polygon30sBarBuilder(
                ticker=ticker,
                interval_secs=self.interval_secs,
                time_provider=self.time_provider,
                close_grace_seconds=self.close_grace_seconds,
                fill_gap_bars=self.fill_gap_bars,
            )
        return self._builders[ticker]

    def get_builder(self, ticker: str) -> Polygon30sBarBuilder | None:
        return self._builders.get(ticker)

    def get_bars(self, ticker: str) -> list[dict[str, float | int]]:
        builder = self._builders.get(ticker)
        return builder.get_bars_as_dicts() if builder else []

    def on_trade(
        self,
        ticker: str,
        price: float,
        size: int,
        timestamp_ns: int = 0,
        cumulative_volume: int | None = None,
    ) -> list[OHLCVBar]:
        return self.get_or_create(ticker).on_trade(price, size, timestamp_ns, cumulative_volume)

    def on_bar(self, ticker: str, bar: OHLCVBar) -> list[OHLCVBar]:
        return self.get_or_create(ticker).on_bar(bar)

    def on_final_bar(self, ticker: str, bar: OHLCVBar) -> list[OHLCVBar]:
        return self.get_or_create(ticker).on_final_bar(bar)

    def consume_recent_revised_closed_bar(self, ticker: str) -> OHLCVBar | None:
        builder = self._builders.get(ticker)
        if builder is None:
            return None
        return builder.consume_recent_revised_closed_bar()

    def check_all_bar_closes(self) -> list[tuple[str, OHLCVBar]]:
        completed: list[tuple[str, OHLCVBar]] = []
        for ticker, builder in self._builders.items():
            for bar in builder.check_bar_closes():
                completed.append((ticker, bar))
        return completed

    def remove_tickers(self, tickers: set[str] | list[str]) -> None:
        for ticker in tickers:
            self._builders.pop(ticker, None)

    def get_all_tickers(self) -> list[str]:
        return list(self._builders.keys())

    def reset(self) -> None:
        self._builders.clear()


class Polygon30sIndicatorEngine:
    def __init__(self, config) -> None:
        self.config = config

    def calculate(self, bars: Sequence[OHLCVBar | Mapping[str, float | int]]) -> dict[str, float | bool] | None:
        minimum_bars = max(
            self.config.macd_slow + self.config.macd_signal,
            int(getattr(self.config, "schwab_native_warmup_bars_required", 50)),
        )
        synthetic_mask = [_is_synthetic_bar(bar) for bar in bars]
        real_bars = [bar for bar, synthetic in zip(bars, synthetic_mask, strict=False) if not synthetic]
        if len(real_bars) < minimum_bars:
            return None

        closes = [_bar_value(bar, "close") for bar in bars]
        opens = [_bar_value(bar, "open") for bar in bars]
        highs = [_bar_value(bar, "high") for bar in bars]
        lows = [_bar_value(bar, "low") for bar in bars]
        volumes = [_bar_value(bar, "volume") for bar in bars]
        timestamps = [_bar_value(bar, "timestamp") for bar in bars]

        real_closes = [_bar_value(bar, "close") for bar in real_bars]
        real_highs = [_bar_value(bar, "high") for bar in real_bars]
        real_lows = [_bar_value(bar, "low") for bar in real_bars]
        real_volumes = [_bar_value(bar, "volume") for bar in real_bars]
        real_timestamps = [_bar_value(bar, "timestamp") for bar in real_bars]

        macd_data = macd(real_closes, self.config.macd_fast, self.config.macd_slow, self.config.macd_signal)
        real_macd_line = macd_data["macd"]
        real_signal_line = macd_data["signal"]
        real_histogram = macd_data["histogram"]
        real_stoch = stoch_k(real_highs, real_lows, real_closes, self.config.stoch_len, self.config.stoch_smooth_k)
        real_stoch_d = sma(real_stoch, self.config.stoch_smooth_d) if real_stoch else []
        real_ema9 = ema(real_closes, self.config.ema1_len)
        real_ema20 = ema(real_closes, self.config.ema2_len)
        real_vwap_values = vwap(
            real_highs,
            real_lows,
            real_closes,
            real_volumes,
            real_timestamps,
            session_start_hour=9,
            session_start_minute=30,
            session_end_hour=16,
            session_end_minute=0,
        )
        real_vol_avg20 = sma(real_volumes, 20)
        real_vol_avg5 = sma(real_volumes, 5)

        macd_line = self._expand_real_indicator_series(real_macd_line, synthetic_mask)
        signal_line = self._expand_real_indicator_series(real_signal_line, synthetic_mask)
        histogram = self._expand_real_indicator_series(real_histogram, synthetic_mask)
        stoch = self._expand_real_indicator_series(real_stoch, synthetic_mask, default=50.0)
        stoch_d = self._expand_real_indicator_series(real_stoch_d, synthetic_mask, default=50.0)
        ema9 = self._expand_real_indicator_series(real_ema9, synthetic_mask)
        ema20 = self._expand_real_indicator_series(real_ema20, synthetic_mask)
        vwap_values = self._expand_real_indicator_series(real_vwap_values, synthetic_mask)
        vol_avg20 = self._expand_real_indicator_series(real_vol_avg20, synthetic_mask)
        vol_avg5 = self._expand_real_indicator_series(real_vol_avg5, synthetic_mask)

        index = len(closes) - 1
        prev = max(0, index - 1)
        prev2 = max(0, index - 2)
        current_ts = timestamps[index]
        current_et = datetime.fromtimestamp(current_ts, UTC).astimezone(EASTERN_TZ)
        current_minutes = current_et.hour * 60 + current_et.minute
        in_regular_session = 9 * 60 + 30 <= current_minutes < 16 * 60

        bars_below_signal = self._count_recent_real_bars_below_signal(index, synthetic_mask, macd_line, signal_line)
        bars_below_signal_prev = self._count_recent_real_bars_below_signal(prev, synthetic_mask, macd_line, signal_line)

        ema9_dist_pct = ((closes[index] - ema9[index]) / ema9[index]) * 100 if ema9[index] > 0 else 999.0
        vwap_dist_pct = ((closes[index] - vwap_values[index]) / vwap_values[index]) * 100 if vwap_values[index] > 0 else 999.0

        return {
            "open": opens[index],
            "open_prev": opens[prev],
            "price": closes[index],
            "close_prev": closes[prev],
            "high": highs[index],
            "high_prev": highs[prev],
            "low": lows[index],
            "low_prev": lows[prev],
            "volume": volumes[index],
            "volume_prev": volumes[prev],
            "bar_timestamp": current_ts,
            "macd": macd_line[index],
            "macd_prev": macd_line[prev],
            "signal": signal_line[index],
            "signal_prev": signal_line[prev],
            "histogram": histogram[index],
            "histogram_prev": histogram[prev],
            "hist_value": histogram[index],
            "stoch_k": stoch[index] if index < len(stoch) else 50.0,
            "stoch_k_prev": stoch[prev] if prev < len(stoch) else 50.0,
            "stoch_d": stoch_d[index] if index < len(stoch_d) else 50.0,
            "ema9": ema9[index],
            "ema9_prev": ema9[prev],
            "ema9_prev2": ema9[prev2],
            "ema20": ema20[index],
            "ema20_prev": ema20[prev],
            "vwap": vwap_values[index],
            "vol_avg20": vol_avg20[index] if index < len(vol_avg20) else volumes[index],
            "vol_avg5": vol_avg5[index] if index < len(vol_avg5) else volumes[index],
            "macd_above_signal": macd_line[index] > signal_line[index],
            "macd_cross_above": macd_line[index] > signal_line[index] and macd_line[prev] <= signal_line[prev],
            "macd_cross_below": macd_line[index] < signal_line[index] and macd_line[prev] >= signal_line[prev],
            "macd_increasing": macd_line[index] > macd_line[prev],
            "macd_delta": macd_line[index] - macd_line[prev],
            "macd_delta_prev": macd_line[prev] - macd_line[prev2],
            "histogram_growing": histogram[index] > histogram[prev],
            "hist_growing": histogram[index] > histogram[prev],
            "stoch_k_rising": (stoch[index] > stoch[prev]) if index < len(stoch) and prev < len(stoch) else False,
            "stoch_cross_below_exit": (
                stoch[index] < self.config.stoch_exit_level and stoch[prev] >= self.config.stoch_exit_level
            )
            if index < len(stoch) and prev < len(stoch)
            else False,
            "price_above_vwap": closes[index] > vwap_values[index],
            "price_cross_above_vwap": closes[index] > vwap_values[index] and closes[prev] <= vwap_values[prev],
            "price_above_ema9": closes[index] > ema9[index],
            "price_above_ema20": closes[index] > ema20[index],
            "price_above_both_emas": closes[index] > ema9[index] and closes[index] > ema20[index],
            "bars_below_signal": bars_below_signal,
            "bars_below_signal_prev": bars_below_signal_prev,
            "ema9_dist_pct": ema9_dist_pct,
            "vwap_dist_pct": vwap_dist_pct,
            "ema9_trend_rising": ema9[index] > ema9[prev] > ema9[prev2],
            "in_regular_session": in_regular_session,
            "bar_index": index + 1,
        }

    @staticmethod
    def _expand_real_indicator_series(
        real_values: Sequence[float],
        synthetic_mask: Sequence[bool],
        *,
        default: float | None = None,
    ) -> list[float]:
        if not synthetic_mask:
            return []
        if not real_values:
            fallback = 0.0 if default is None else default
            return [fallback] * len(synthetic_mask)
        expanded: list[float] = []
        real_index = 0
        previous_value = real_values[0]
        fallback = real_values[0] if default is None else default
        for synthetic in synthetic_mask:
            if synthetic:
                expanded.append(previous_value if expanded else fallback)
                continue
            current_value = real_values[min(real_index, len(real_values) - 1)]
            expanded.append(current_value)
            previous_value = current_value
            real_index += 1
        return expanded

    @staticmethod
    def _count_recent_real_bars_below_signal(
        start_index: int,
        synthetic_mask: Sequence[bool],
        macd_line: Sequence[float],
        signal_line: Sequence[float],
    ) -> int:
        count = 0
        probe = start_index
        while probe >= 0:
            if synthetic_mask[probe]:
                probe -= 1
                continue
            if macd_line[probe] > signal_line[probe]:
                break
            count += 1
            probe -= 1
        return count


@dataclass
class _PendingConfirmation:
    trigger_bar_idx: int
    trigger_path: str
    cross_price: float
    required_score: int
    bars_waiting: int = 0


@dataclass
class _ChopEvaluation:
    active: bool
    valid: bool
    hit_count: int
    reasons: list[str]
    blocks_p1p2: bool
    blocks_p3: bool
    extreme_p3_override: bool


class Polygon30sEntryEngine:
    def __init__(
        self,
        config: TradingConfig,
        *,
        name: str = "POLYGON30",
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.name = name
        self.now_provider = now_provider or now_eastern
        self._pending: dict[str, _PendingConfirmation] = {}
        self._recent_bars: dict[str, list[dict[str, float]]] = {}
        self._last_buy_bar: dict[str, int] = {}
        self._last_exit_bar: dict[str, int] = {}
        self._rejected_open_until_bar: dict[str, int] = {}
        self._last_decision: dict[str, dict[str, str]] = {}
        self._session_highs: dict[str, float] = {}
        self._spike_anchor_bar: dict[str, int] = {}
        self._spike_anchor_high: dict[str, float] = {}
        self._active_day_by_ticker: dict[str, str] = {}
        self._chop_lock_active: dict[str, bool] = {}
        self._p3_hard_stop_pause_until: dict[str, datetime] = {}

    def seed_recent_bars(
        self,
        ticker: str,
        indicators_history: Sequence[dict[str, float | bool]],
    ) -> None:
        recent: list[dict[str, float]] = []
        session_high = 0.0
        for index, indicators in enumerate(indicators_history, start=1):
            snapshot = self._snapshot_from_indicators(indicators, bar_index=index)
            if snapshot is None:
                continue
            session_high = max(session_high, snapshot["high"])
            recent.append(snapshot)
            self._update_spike_state(ticker, snapshot)
        if recent:
            self._recent_bars[ticker] = recent[-100:]
            self._session_highs[ticker] = session_high

    def check_entry(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, float | int | str] | None:
        self._roll_day_if_needed(ticker)
        try:
            return self._check_entry_inner(ticker, indicators, bar_index, position_tracker)
        finally:
            snapshot = self._snapshot_from_indicators(indicators, bar_index=bar_index)
            if snapshot is not None:
                self._remember_bar(ticker, snapshot)

    def record_exit(self, ticker: str, bar_index: int) -> None:
        self._last_exit_bar[ticker] = bar_index
        self._pending.pop(ticker, None)

    def record_path_exit(self, ticker: str, *, path: str, reason: str) -> None:
        pause_minutes = int(getattr(self.config, "p3_hard_stop_pause_minutes", 0) or 0)
        if path != "P3_SURGE" or pause_minutes <= 0:
            return
        if "HARD_STOP" not in str(reason or "").upper():
            return
        self._p3_hard_stop_pause_until[ticker] = self.now_provider() + timedelta(minutes=pause_minutes)
        self._pending.pop(ticker, None)

    def record_rejected_open(self, ticker: str, bar_index: int, cooldown_bars: int) -> None:
        if cooldown_bars <= 0:
            self._pending.pop(ticker, None)
            return
        self._rejected_open_until_bar[ticker] = bar_index + cooldown_bars
        self._pending.pop(ticker, None)

    def cancel_pending(self, ticker: str) -> None:
        self._pending.pop(ticker, None)

    def pop_last_decision(self, ticker: str) -> dict[str, str] | None:
        return self._last_decision.pop(ticker, None)

    def reset(self) -> None:
        self._pending.clear()
        self._recent_bars.clear()
        self._last_buy_bar.clear()
        self._last_exit_bar.clear()
        self._rejected_open_until_bar.clear()
        self._last_decision.clear()
        self._session_highs.clear()
        self._spike_anchor_bar.clear()
        self._spike_anchor_high.clear()
        self._active_day_by_ticker.clear()
        self._chop_lock_active.clear()
        self._p3_hard_stop_pause_until.clear()

    def prune_tickers(self, keep: set[str]) -> None:
        for mapping in (
            self._pending,
            self._recent_bars,
            self._last_buy_bar,
            self._rejected_open_until_bar,
            self._last_decision,
            self._session_highs,
            self._spike_anchor_bar,
            self._spike_anchor_high,
            self._active_day_by_ticker,
            self._chop_lock_active,
            self._p3_hard_stop_pause_until,
        ):
            stale = [ticker for ticker in mapping if ticker not in keep]
            for ticker in stale:
                mapping.pop(ticker, None)

    def _check_entry_inner(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
        position_tracker=None,
    ) -> dict[str, float | int | str] | None:
        gate = self._check_hard_gates(ticker, bar_index, position_tracker)
        if not gate["passed"]:
            self._record_decision(ticker, status="blocked", reason=str(gate["reason"]))
            return None

        if bar_index < self.config.schwab_native_warmup_bars_required:
            self._record_decision(
                ticker,
                status="blocked",
                reason=f"warmup ({bar_index}/{self.config.schwab_native_warmup_bars_required} bars)",
            )
            return None

        if ticker in self._pending:
            confirmed = self._advance_confirmation(ticker, indicators, bar_index)
            if confirmed is not None:
                return confirmed
            if ticker in self._pending:
                return None

        path, score, score_details, chop = self._evaluate_paths(ticker, indicators, bar_index)
        if path is None:
            if chop.active:
                self._record_decision(
                    ticker,
                    status="blocked",
                    reason=self._format_chop_reason(chop),
                    score_details=score_details,
                )
            else:
                self._record_decision(
                    ticker,
                    status="idle",
                    reason="no entry path matched",
                    score_details=score_details,
                )
            return None

        p3_pause_reason = self._p3_pause_reason(ticker) if path == "P3_SURGE" else None
        if p3_pause_reason:
            self._record_decision(
                ticker,
                status="blocked",
                reason=p3_pause_reason,
                path="P3_SURGE",
            )
            return None

        if path == "P3_SURGE" and self.config.p3_entry_stoch_k_cap is not None:
            stoch_k_val = float(indicators.get("stoch_k", 0) or 0)
            if stoch_k_val >= self.config.p3_entry_stoch_k_cap:
                self._record_decision(
                    ticker,
                    status="blocked",
                    reason=(
                        f"P3 entry stoch_k cap ({stoch_k_val:.1f} >= "
                        f"{self.config.p3_entry_stoch_k_cap})"
                    ),
                    path="P3_SURGE",
                )
                return None

        if (
            path in {"P4_BURST", "P5_PULLBACK"}
            or not self.config.schwab_native_use_confirmation
            or self.config.confirm_bars <= 0
        ):
            self._last_buy_bar[ticker] = bar_index
            self._record_decision(ticker, status="signal", reason=path, path=path, score=score, score_details=score_details)
            return self._build_buy_signal(ticker, path, indicators, score, score_details)

        self._pending[ticker] = _PendingConfirmation(
            trigger_bar_idx=bar_index,
            trigger_path=path,
            cross_price=float(indicators["price"]),
            required_score=self.config.p3_min_score if path == "P3_SURGE" else self.config.min_score,
        )
        self._record_decision(ticker, status="pending", reason=f"{path} waiting confirmation", path=path)
        return None

    def _advance_confirmation(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
    ) -> dict[str, float | int | str] | None:
        pending = self._pending[ticker]
        current = self._snapshot_from_indicators(indicators, bar_index=bar_index)
        recent = self._recent_bars.get(ticker, [])
        if current is not None:
            chop = self._evaluate_chop_lock(ticker, indicators, current, recent)
            if self._path_blocked_by_chop(pending.trigger_path, chop):
                self._pending.pop(ticker, None)
                self._record_decision(
                    ticker,
                    status="blocked",
                    reason=f"{pending.trigger_path} blocked by {self._format_chop_reason(chop)}",
                    path=pending.trigger_path,
                )
                return None
        pending.bars_waiting += 1
        if bool(indicators.get("macd_cross_below", False)) or bool(indicators.get("stoch_cross_below_exit", False)):
            self._pending.pop(ticker, None)
            self._record_decision(ticker, status="blocked", reason="confirmation deteriorated")
            return None
        if pending.bars_waiting < self.config.confirm_bars:
            self._record_decision(
                ticker,
                status="pending",
                reason=f"{pending.trigger_path} confirming ({pending.bars_waiting}/{self.config.confirm_bars})",
                path=pending.trigger_path,
            )
            return None
        score, score_details = self._quality_score(indicators)
        if score < pending.required_score or float(indicators.get("volume", 0) or 0) < self.config.vol_min:
            self._pending.pop(ticker, None)
            self._record_decision(
                ticker,
                status="blocked",
                reason=f"confirmation score {score} below required {pending.required_score}",
                path=pending.trigger_path,
                score=score,
                score_details=score_details,
            )
            return None
        self._pending.pop(ticker, None)
        self._last_buy_bar[ticker] = bar_index
        self._record_decision(
            ticker,
            status="signal",
            reason=pending.trigger_path,
            path=pending.trigger_path,
            score=score,
            score_details=score_details,
        )
        return self._build_buy_signal(ticker, pending.trigger_path, indicators, score, score_details)

    def _evaluate_paths(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        bar_index: int,
    ) -> tuple[str | None, int, str, _ChopEvaluation]:
        common = self._common_gate_state(indicators)
        vol_ok = bool(common["vol_ok"])
        time_allowed = self._time_allowed()
        recent = self._recent_bars.get(ticker, [])
        current = self._snapshot_from_indicators(indicators, bar_index=bar_index)
        if current is None:
            return None, 0, "diag: g[current=missing]", _ChopEvaluation(False, False, 0, [], False, False, False)
        previous = recent[-1] if recent else None
        chop = self._evaluate_chop_lock(ticker, indicators, current, recent)

        raw_p1 = (
            bool(indicators.get("macd_cross_above", False))
            and int(indicators.get("bars_below_signal_prev", 0) or 0) >= self.config.p1_min_bars_below_signal
            and bool(common["p1p2_ok"])
        )
        p1_vol_ratio_ok = (
            self.config.p1_min_vol_ratio is None
            or current["vol_avg20"] <= 0
            or current["volume"] >= current["vol_avg20"] * self.config.p1_min_vol_ratio
        )
        p1_abs_vol_ok = (
            self.config.p1_min_volume_abs is None
            or current["volume"] >= self.config.p1_min_volume_abs
        )
        p1_dollar_vol_ok = (
            self.config.p1_min_dollar_volume_abs is None
            or (current["close"] * current["volume"]) >= self.config.p1_min_dollar_volume_abs
        )
        p1_available = (
            raw_p1
            and vol_ok
            and time_allowed
            and p1_vol_ratio_ok
            and p1_abs_vol_ok
            and p1_dollar_vol_ok
            and not chop.blocks_p1p2
        )
        if raw_p1 and vol_ok and time_allowed and p1_vol_ratio_ok and p1_abs_vol_ok and p1_dollar_vol_ok:
            if chop.blocks_p1p2:
                return None, 0, self._build_path_diagnostics(
                    ticker=ticker,
                    indicators=indicators,
                    common=common,
                    time_allowed=time_allowed,
                    chop=chop,
                    raw_p1=raw_p1,
                    raw_p2=False,
                    raw_p3=False,
                    raw_p4=False,
                    raw_p5=False,
                    current=current,
                    previous=previous,
                    recent=recent,
                ), chop
            score, details = self._quality_score(indicators)
            return "P1_CROSS", score, details, chop

        raw_p2 = (
            bool(indicators.get("price_cross_above_vwap", False))
            and bool(indicators.get("macd_above_signal", False))
            and bool(indicators.get("macd_increasing", False))
            and bool(common["p1p2_ok"])
        )
        p2_available = raw_p2 and vol_ok and time_allowed and not chop.blocks_p1p2
        if raw_p2 and vol_ok and time_allowed:
            if chop.blocks_p1p2:
                return None, 0, self._build_path_diagnostics(
                    ticker=ticker,
                    indicators=indicators,
                    common=common,
                    time_allowed=time_allowed,
                    chop=chop,
                    raw_p1=raw_p1,
                    raw_p2=raw_p2,
                    raw_p3=False,
                    raw_p4=False,
                    raw_p5=False,
                    current=current,
                    previous=previous,
                    recent=recent,
                ), chop
            score, details = self._quality_score(indicators)
            return "P2_VWAP", score, details, chop

        raw_p3 = (
            bool(indicators.get("macd_above_signal", False))
            and not bool(indicators.get("macd_cross_above", False))
            and float(indicators.get("macd_delta", 0) or 0) >= self.config.surge_rate
            and float(indicators.get("macd_delta", 0) or 0) > float(indicators.get("macd_delta_prev", 0) or 0)
            and float(indicators.get("hist_value", 0) or 0) >= self.config.p3_histogram_floor
            and bool(indicators.get("price_above_ema9", False))
            and (
                self.config.p3_min_volume_abs is None
                or current["volume"] >= self.config.p3_min_volume_abs
            )
            and (
                self.config.p3_min_dollar_volume_abs is None
                or (current["close"] * current["volume"]) >= self.config.p3_min_dollar_volume_abs
            )
            and (
                self.config.p3_min_vol_ratio is None
                or float(indicators.get("volume", 0) or 0)
                >= float(indicators.get("vol_avg20", 0) or 0) * self.config.p3_min_vol_ratio
            )
            and (
                self.config.p3_max_ema9_dist_pct is None
                or float(indicators.get("ema9_dist_pct", 0) or 0) < self.config.p3_max_ema9_dist_pct
            )
            and self._p3_cross_age_ok(ticker)
            and self._p3_recent_runup_ok(current, recent)
            and bool(common["p3_ok"])
        )
        p3_available = raw_p3 and vol_ok and time_allowed and not chop.blocks_p3
        if raw_p3 and vol_ok and time_allowed:
            if chop.blocks_p3:
                return None, 0, self._build_path_diagnostics(
                    ticker=ticker,
                    indicators=indicators,
                    common=common,
                    time_allowed=time_allowed,
                    chop=chop,
                    raw_p1=raw_p1,
                    raw_p2=raw_p2,
                    raw_p3=raw_p3,
                    raw_p4=False,
                    raw_p5=False,
                    current=current,
                    previous=previous,
                    recent=recent,
                ), chop
            score, details = self._quality_score(indicators)
            return "P3_SURGE", score, details, chop

        raw_p4 = False
        if self.config.p4_enabled and previous is not None:
            p4_body_pct = ((current["close"] - current["open"]) / current["open"]) * 100 if current["open"] > 0 else 0.0
            p4_range_pct = ((current["high"] - current["low"]) / current["open"]) * 100 if current["open"] > 0 else 0.0
            p4_close_near_high = (
                current["close"] >= current["low"] + (current["high"] - current["low"]) * (1 - self.config.p4_close_top_pct / 100.0)
                if current["high"] > current["low"]
                else True
            )
            recent_high = max(bar["high"] for bar in recent[-self.config.p4_breakout_lookback :]) if recent else 0.0
            p4_ema9_dist_ok = (
                self.config.p4_max_ema9_dist_pct is None
                or float(indicators.get("ema9_dist_pct", 0.0) or 0.0) < self.config.p4_max_ema9_dist_pct
            )
            raw_p4_classic = (
                not p1_available
                and not p2_available
                and not p3_available
                and current["close"] > current["open"]
                and (p4_body_pct >= self.config.p4_body_pct or p4_range_pct >= self.config.p4_range_pct)
                and p4_close_near_high
                and current["volume"] >= current["vol_avg20"] * self.config.p4_vol_mult20
                and current["high"] > recent_high
                and (not self.config.p4_require_close_above_ema9 or current["close"] > current["ema9"])
                and p4_ema9_dist_ok
                and time_allowed
            )
            raw_p4_prev_bar = (
                not p1_available
                and not p2_available
                and not p3_available
                and time_allowed
                and self._p4_prev_bar_entry_ok(previous, current)
            )
            raw_p4 = (raw_p4_classic and current["high"] > recent_high) or raw_p4_prev_bar
            if raw_p4:
                score, details = self._quality_score(indicators)
                return "P4_BURST", score, details, chop

        raw_p5 = self._is_pullback_entry_ready(ticker, current, recent)
        if raw_p5 and time_allowed:
            score, details = self._quality_score(indicators)
            return "P5_PULLBACK", score, details, chop

        return (
            None,
            0,
            self._build_path_diagnostics(
                ticker=ticker,
                indicators=indicators,
                common=common,
                time_allowed=time_allowed,
                chop=chop,
                raw_p1=raw_p1,
                raw_p2=raw_p2,
                raw_p3=raw_p3,
                raw_p4=raw_p4,
                raw_p5=raw_p5,
                current=current,
                previous=previous,
                recent=recent,
            ),
            chop,
        )

    def _is_pullback_entry_ready(
        self,
        ticker: str,
        current: dict[str, float],
        recent: list[dict[str, float]],
    ) -> bool:
        spike_anchor_bar = self._spike_anchor_bar.get(ticker)
        spike_anchor_high = self._spike_anchor_high.get(ticker)
        session_high = self._session_highs.get(ticker, current["high"])
        if spike_anchor_bar is None or spike_anchor_high is None or not recent:
            return False
        bars_since_spike = int(current["bar_index"] - spike_anchor_bar)
        if bars_since_spike < 2 or bars_since_spike > self.config.p5_spike_lookback:
            return False
        from_high_pct = ((session_high - current["close"]) / session_high) * 100 if session_high > 0 else 999.0
        if from_high_pct > self.config.p5_max_from_high_pct:
            return False
        giveback_pct = ((spike_anchor_high - current["open"]) / spike_anchor_high) * 100 if spike_anchor_high > 0 else 0.0
        if giveback_pct < self.config.p5_giveback_pct:
            return False
        open_near_ema9 = abs((current["open"] - current["ema9"]) / current["ema9"]) * 100 <= self.config.p5_near_ema9_pct if current["ema9"] > 0 else False
        low_near_ema9 = abs((current["low"] - current["ema9"]) / current["ema9"]) * 100 <= self.config.p5_near_ema9_pct if current["ema9"] > 0 else False
        support_touch_ok = open_near_ema9 or low_near_ema9 or current["low"] <= current["ema9"]
        resume_body_pct = ((current["close"] - current["open"]) / current["open"]) * 100 if current["open"] > 0 else 0.0
        resume_close_pos = ((current["close"] - current["low"]) / (current["high"] - current["low"])) if current["high"] > current["low"] else 1.0
        recent_resistance = max(bar["high"] for bar in recent[-self.config.p5_breakout_bars :])
        recent_low = min(bar["low"] for bar in recent[-self.config.p5_momentum_lookback :]) if recent else current["low"]
        upmove_pct = ((current["close"] - recent_low) / recent_low) * 100 if recent_low > 0 else 0.0
        return (
            support_touch_ok
            and current["close"] > current["open"]
            and current["close"] > current["ema9"]
            and resume_body_pct < self.config.p5_max_body_pct
            and resume_close_pos >= self.config.p5_close_ratio
            and current["volume"] >= current["vol_avg5"] * self.config.p5_vol_mult5
            and current["close"] > recent_resistance
            and current["ema9"] >= current["ema9_prev"] * 0.995
            and upmove_pct >= self.config.p5_momentum_min_pct
        )

    def _evaluate_chop_lock(
        self,
        ticker: str,
        indicators: dict[str, float | bool],
        current: dict[str, float],
        recent: list[dict[str, float]],
    ) -> _ChopEvaluation:
        if not self.config.schwab_native_use_chop_regime:
            self._chop_lock_active.pop(ticker, None)
            return _ChopEvaluation(False, False, 0, [], False, False, False)

        series = [*recent, current]
        minimum_history = max(
            self.config.chop_atr_len,
            self.config.chop_flat_bars + 1,
            self.config.chop_cross_bars + 1,
            self.config.chop_clean_bars,
        )
        max_lookback = max(
            minimum_history,
            self.config.chop_atr_len,
            self.config.chop_flat_bars + 1,
            self.config.chop_cross_bars + 1,
            self.config.chop_clean_bars,
            self.config.chop_restart_vwap_closes,
            self.config.chop_restart_breakout_bars + 1,
            self.config.chop_restart_pullback_hold_bars + 1,
            self.config.p3_extreme_hist_lookback,
            3,
        )
        series = series[-max_lookback:]
        atr = _atr_value(series[-self.config.chop_atr_len :], self.config.chop_atr_len)
        valid = (
            bool(indicators.get("in_regular_session", False))
            and current["vwap"] > 0
            and atr > 0
            and len(series) >= minimum_history
        )

        reasons: list[str] = []
        hit_count = 0
        if valid:
            compression = abs(current["ema20"] - current["vwap"]) < atr * self.config.chop_compress_mult
            if compression:
                reasons.append("COMPRESS")
                hit_count += 1

            ema20_flat = False
            if len(series) > self.config.chop_flat_bars:
                ema20_then = series[-(self.config.chop_flat_bars + 1)]["ema20"]
                ema20_flat = abs(current["ema20"] - ema20_then) < atr * self.config.chop_flat_mult
            if ema20_flat:
                reasons.append("EMA20_FLAT")
                hit_count += 1

            cross_count = 0
            cross_window = series[-(self.config.chop_cross_bars + 1) :]
            for previous_bar, current_bar in zip(cross_window, cross_window[1:]):
                crossed_ema20 = _series_cross(
                    current_bar["close"],
                    previous_bar["close"],
                    current_bar["ema20"],
                    previous_bar["ema20"],
                )
                crossed_vwap = _series_cross(
                    current_bar["close"],
                    previous_bar["close"],
                    current_bar["vwap"],
                    previous_bar["vwap"],
                )
                if crossed_ema20 or crossed_vwap:
                    cross_count += 1
            whipsaw = cross_count >= self.config.chop_cross_min
            if whipsaw:
                reasons.append("WHIPSAW")
                hit_count += 1

            clean_window = series[-self.config.chop_clean_bars :]
            clean_bull_count = sum(1 for bar in clean_window if bar["close"] > bar["ema20"] and bar["close"] > bar["vwap"])
            clean_bear_count = sum(1 for bar in clean_window if bar["close"] < bar["ema20"] and bar["close"] < bar["vwap"])
            clean_side_count = max(clean_bull_count, clean_bear_count)
            no_clean_side = clean_side_count < self.config.chop_clean_min
            if no_clean_side:
                reasons.append("NO_CLEAN_SIDE")
                hit_count += 1

        active = self._chop_lock_active.get(ticker, False)
        trigger = valid and hit_count >= self.config.chop_trigger_min_hits
        restart_long = valid and self._restart_long_ready(current, series)
        if trigger and not active:
            active = True
        elif active and restart_long:
            active = False

        if active:
            self._chop_lock_active[ticker] = True
        else:
            self._chop_lock_active.pop(ticker, None)

        extreme_p3_override = active and valid and self._p3_extreme_momentum_override(indicators, current, series, atr)
        return _ChopEvaluation(
            active=active,
            valid=valid,
            hit_count=hit_count,
            reasons=reasons,
            blocks_p1p2=active,
            blocks_p3=active and not extreme_p3_override,
            extreme_p3_override=extreme_p3_override,
        )

    def _restart_long_ready(self, current: dict[str, float], series: list[dict[str, float]]) -> bool:
        if len(series) < max(self.config.chop_restart_vwap_closes, self.config.chop_restart_breakout_bars + 1, 3):
            return False
        restart_above_vwap = all(
            bar["close"] > bar["vwap"] for bar in series[-self.config.chop_restart_vwap_closes :]
        )
        restart_ema20_up = series[-1]["ema20"] > series[-2]["ema20"] > series[-3]["ema20"]
        pullback_window = series[-(self.config.chop_restart_pullback_hold_bars + 1) :]
        restart_pullback_held = any(
            (bar["low"] <= bar["ema20"] or bar["low"] <= bar["vwap"])
            and bar["close"] > bar["ema20"]
            and bar["close"] > bar["vwap"]
            for bar in pullback_window
        )
        prior_highs = [bar["high"] for bar in series[-(self.config.chop_restart_breakout_bars + 1) : -1]]
        restart_breakout = bool(prior_highs) and current["close"] > max(prior_highs)
        return restart_above_vwap and restart_ema20_up and restart_pullback_held and restart_breakout

    def _p3_extreme_momentum_override(
        self,
        indicators: dict[str, float | bool],
        current: dict[str, float],
        series: list[dict[str, float]],
        atr: float,
    ) -> bool:
        hist_window = series[-self.config.p3_extreme_hist_lookback :]
        hist_abs_avg = (
            sum(abs(float(bar.get("hist_value", 0.0))) for bar in hist_window) / len(hist_window)
            if hist_window
            else 0.0
        )
        hist_abs_base = max(self.config.p3_histogram_floor, hist_abs_avg)
        range_ok = (current["high"] - current["low"]) >= atr * self.config.p3_extreme_range_atr
        vol_ok = current["volume"] >= current["vol_avg20"] * self.config.p3_extreme_vol_mult
        macd_ok = (
            bool(indicators.get("macd_above_signal", False))
            and float(indicators.get("macd_delta", 0.0) or 0.0) >= self.config.surge_rate * self.config.p3_extreme_delta_mult
            and bool(indicators.get("hist_growing", False))
            and float(indicators.get("hist_value", 0.0) or 0.0)
            >= max(self.config.p3_histogram_floor, hist_abs_base * self.config.p3_extreme_hist_mult)
        )
        clear_ok = (
            current["close"] > current["ema20"]
            and current["close"] > current["vwap"]
            and (current["close"] - max(current["ema20"], current["vwap"])) >= atr * self.config.p3_extreme_clear_atr
        )
        return range_ok and vol_ok and macd_ok and clear_ok

    def _format_chop_reason(self, chop: _ChopEvaluation) -> str:
        if not chop.active:
            return ""
        if not chop.valid:
            return "chop lock active (current 0/4, awaiting regular-session restart); P1/P2/P3 gated"
        reasons = "|".join(chop.reasons) if chop.reasons else "NO_ACTIVE_FLAGS"
        suffix = "; P1/P2 gated, P3 override active" if chop.extreme_p3_override else "; P1/P2/P3 gated"
        return f"chop lock active (current {chop.hit_count}/4): {reasons}{suffix}"

    def _path_blocked_by_chop(self, path: str, chop: _ChopEvaluation) -> bool:
        if path in {"P1_CROSS", "P2_VWAP"}:
            return chop.blocks_p1p2
        if path == "P3_SURGE":
            return chop.blocks_p3
        return False

    def _common_gate_state(self, indicators: dict[str, float | bool]) -> dict[str, bool]:
        close = float(indicators["price"])
        ema20 = float(indicators.get("ema20", 0) or 0)
        stoch_k = float(indicators.get("stoch_k", 50) or 50)
        ema9_dist_pct = float(indicators.get("ema9_dist_pct", 999) or 999)
        vwap_dist_pct = float(indicators.get("vwap_dist_pct", 999) or 999)
        ema_gate_ok = (not self.config.require_above_ema20) or (ema20 > 0 and close > ema20)
        stoch_gate_ok = (not self.config.use_stoch_k_cap) or stoch_k < self.config.stoch_k_cap_level
        ema9_gate_ok = (not self.config.use_ema9_max_dist) or ema9_dist_pct < self.config.ema9_max_dist_pct
        vwap_gate_ok = (
            vwap_dist_pct < self.config.vwap_max_dist_pct
            if self.config.vwap_max_dist_pct > 0 and bool(indicators.get("in_regular_session", False))
            else True
        )
        p3_high_vwap_ok = (
            self.config.p3_allow_high_vwap
            and not vwap_gate_ok
            and vwap_dist_pct < self.config.p3_high_vwap_max_pct
            and close > float(indicators.get("ema9", 0) or 0)
            and float(indicators.get("ema9", 0) or 0) > float(indicators.get("ema20", 0) or 0)
            and ema9_dist_pct <= self.config.p3_high_vwap_max_ema9_pct
            and bool(indicators.get("ema9_trend_rising", False))
        )
        p3_momentum_override_ok = (
            self.config.p3_allow_momentum_override
            and ema_gate_ok
            and close > float(indicators.get("ema9", 0) or 0)
            and float(indicators.get("ema9", 0) or 0) > float(indicators.get("ema20", 0) or 0)
            and bool(indicators.get("ema9_trend_rising", False))
            and ema9_dist_pct <= self.config.p3_momentum_max_ema9_pct
            and stoch_k <= self.config.p3_momentum_max_stoch_k
            and float(indicators.get("volume", 0) or 0) >= float(indicators.get("vol_avg20", 0) or 0) * self.config.p3_momentum_vol_mult
            and (not bool(indicators.get("in_regular_session", False)) or vwap_dist_pct <= self.config.p3_high_vwap_max_pct)
        )
        common_ok = ema_gate_ok and stoch_gate_ok and ema9_gate_ok
        return {
            "vol_ok": float(indicators.get("volume", 0) or 0) > self.config.vol_min,
            "p1p2_ok": common_ok and vwap_gate_ok,
            "p3_ok": (common_ok and (vwap_gate_ok or p3_high_vwap_ok)) or p3_momentum_override_ok,
        }

    def _quality_score(self, indicators: dict[str, float | bool]) -> tuple[int, str]:
        score = 0
        parts: list[str] = []
        checks = (
            (bool(indicators.get("hist_growing", False)), "hist"),
            (bool(indicators.get("stoch_k_rising", False)), "stK"),
            (bool(indicators.get("price_above_vwap", False)), "vwap"),
            (float(indicators.get("volume", 0) or 0) > self.config.vol_min, "vol"),
            (bool(indicators.get("macd_increasing", False)), "macd"),
            (bool(indicators.get("price_above_ema9", False)) and bool(indicators.get("price_above_ema20", False)), "emas"),
        )
        for passed, label in checks:
            if passed:
                score += 1
                parts.append(f"{label}+")
            else:
                parts.append(f"{label}-")
        return score, " ".join(parts)

    @staticmethod
    def _compact_failures(labels: list[str], *, max_labels: int = 4) -> str:
        if not labels:
            return "hit"
        if len(labels) <= max_labels:
            return "|".join(labels)
        shown = labels[:max_labels]
        shown.append(f"+{len(labels) - max_labels}")
        return "|".join(shown)

    def _build_path_diagnostics(
        self,
        *,
        ticker: str,
        indicators: dict[str, float | bool],
        common: dict[str, bool],
        time_allowed: bool,
        chop: _ChopEvaluation,
        raw_p1: bool,
        raw_p2: bool,
        raw_p3: bool,
        raw_p4: bool,
        raw_p5: bool,
        current: dict[str, float],
        previous: dict[str, float] | None,
        recent: list[dict[str, float]],
    ) -> str:
        vol_ok = bool(common["vol_ok"])
        p1p2_ok = bool(common["p1p2_ok"])
        p3_ok = bool(common["p3_ok"])
        p4_body_pct = ((current["close"] - current["open"]) / current["open"]) * 100 if current["open"] > 0 else 0.0
        p4_range_pct = ((current["high"] - current["low"]) / current["open"]) * 100 if current["open"] > 0 else 0.0
        p4_close_near_high = (
            current["close"] >= current["low"] + (current["high"] - current["low"]) * (1 - self.config.p4_close_top_pct / 100.0)
            if current["high"] > current["low"]
            else True
        )
        recent_high = max((bar["high"] for bar in recent[-self.config.p4_breakout_lookback :]), default=0.0)
        p4_volume_ok = current["volume"] >= current["vol_avg20"] * self.config.p4_vol_mult20
        p4_breakout_ok = current["high"] > recent_high
        p4_ema9_ok = (not self.config.p4_require_close_above_ema9) or current["close"] > current["ema9"]
        p4_ema9_dist_ok = (
            self.config.p4_max_ema9_dist_pct is None
            or float(indicators.get("ema9_dist_pct", 0.0) or 0.0) < self.config.p4_max_ema9_dist_pct
        )
        p1_vol_ratio_ok = (
            self.config.p1_min_vol_ratio is None
            or current["vol_avg20"] <= 0
            or current["volume"] >= current["vol_avg20"] * self.config.p1_min_vol_ratio
        )
        p1_abs_vol_ok = (
            self.config.p1_min_volume_abs is None
            or current["volume"] >= self.config.p1_min_volume_abs
        )
        p1_dollar_vol_ok = (
            self.config.p1_min_dollar_volume_abs is None
            or (current["close"] * current["volume"]) >= self.config.p1_min_dollar_volume_abs
        )
        p5_ready = raw_p5
        chop_text = "off"
        if chop.active:
            reasons = "|".join(chop.reasons[:2]) if chop.reasons else "gated"
            if len(chop.reasons) > 2:
                reasons += f"|+{len(chop.reasons) - 2}"
            chop_text = f"{chop.hit_count}/4:{reasons}"

        p1_failures = []
        if not bool(indicators.get("macd_cross_above", False)):
            p1_failures.append("cross")
        if int(indicators.get("bars_below_signal_prev", 0) or 0) < self.config.p1_min_bars_below_signal:
            p1_failures.append("below_sig")
        if not p1p2_ok:
            p1_failures.append("p12_gate")
        if not vol_ok:
            p1_failures.append("vol")
        if not p1_vol_ratio_ok:
            p1_failures.append("vol20")
        if not p1_abs_vol_ok:
            p1_failures.append("p1_vol")
        if not p1_dollar_vol_ok:
            p1_failures.append("p1_dollar")
        if not time_allowed:
            p1_failures.append("time")
        if raw_p1 and vol_ok and time_allowed and p1_vol_ratio_ok and p1_abs_vol_ok and p1_dollar_vol_ok and chop.blocks_p1p2:
            p1_failures = ["chop"]

        p2_failures = []
        if not bool(indicators.get("price_cross_above_vwap", False)):
            p2_failures.append("vwap_cross")
        if not bool(indicators.get("macd_above_signal", False)):
            p2_failures.append("macd_above")
        if not bool(indicators.get("macd_increasing", False)):
            p2_failures.append("macd_up")
        if not p1p2_ok:
            p2_failures.append("p12_gate")
        if not vol_ok:
            p2_failures.append("vol")
        if not time_allowed:
            p2_failures.append("time")
        if raw_p2 and vol_ok and time_allowed and chop.blocks_p1p2:
            p2_failures = ["chop"]

        p3_failures = []
        if not bool(indicators.get("macd_above_signal", False)):
            p3_failures.append("macd_above")
        if bool(indicators.get("macd_cross_above", False)):
            p3_failures.append("fresh_cross")
        if float(indicators.get("macd_delta", 0) or 0) < self.config.surge_rate:
            p3_failures.append("delta")
        if float(indicators.get("macd_delta", 0) or 0) <= float(indicators.get("macd_delta_prev", 0) or 0):
            p3_failures.append("delta_prev")
        if float(indicators.get("hist_value", 0) or 0) < self.config.p3_histogram_floor:
            p3_failures.append("hist")
        if not bool(indicators.get("price_above_ema9", False)):
            p3_failures.append("ema9")
        if self.config.p3_min_volume_abs is not None and current["volume"] < self.config.p3_min_volume_abs:
            p3_failures.append("p3_vol")
        if (
            self.config.p3_min_dollar_volume_abs is not None
            and (current["close"] * current["volume"]) < self.config.p3_min_dollar_volume_abs
        ):
            p3_failures.append("p3_dollar")
        if (
            self.config.p3_min_vol_ratio is not None
            and current["vol_avg20"] > 0
            and current["volume"] < current["vol_avg20"] * self.config.p3_min_vol_ratio
        ):
            p3_failures.append("vol20")
        if not p3_ok:
            p3_failures.append("p3_gate")
        if not self._p3_cross_age_ok(ticker):
            p3_failures.append("cross_age")
        if not self._p3_recent_runup_ok(current, recent):
            p3_failures.append("runup")
        if not vol_ok:
            p3_failures.append("vol")
        if not time_allowed:
            p3_failures.append("time")
        if raw_p3 and vol_ok and time_allowed and chop.blocks_p3:
            p3_failures = ["chop"]
        p1_available = (
            raw_p1
            and vol_ok
            and time_allowed
            and p1_vol_ratio_ok
            and p1_abs_vol_ok
            and p1_dollar_vol_ok
            and not chop.blocks_p1p2
        )
        p2_available = raw_p2 and vol_ok and time_allowed and not chop.blocks_p1p2
        p3_available = raw_p3 and vol_ok and time_allowed and not chop.blocks_p3

        if not self.config.p4_enabled:
            p4_failures = ["disabled"]
        elif previous is None:
            p4_failures = ["history"]
        else:
            p4_failures = []
            if p1_available or p2_available or p3_available:
                p4_failures.append("higher_path")
            if current["close"] <= current["open"]:
                p4_failures.append("green")
            if p4_body_pct < self.config.p4_body_pct and p4_range_pct < self.config.p4_range_pct:
                p4_failures.append("body_range")
            if not p4_close_near_high:
                p4_failures.append("close_high")
            if not p4_volume_ok:
                p4_failures.append("vol20")
            if not p4_breakout_ok:
                p4_failures.append("breakout")
            if not p4_ema9_ok:
                p4_failures.append("ema9")
            if not p4_ema9_dist_ok:
                p4_failures.append("ema9_ext")
            if not time_allowed:
                p4_failures.append("time")
            if raw_p4:
                p4_failures = ["hit"]

        p5_failures = []
        if not p5_ready:
            p5_failures.append("pullback")
        if not time_allowed:
            p5_failures.append("time")

        path_failures = {
            "P1": p1_failures,
            "P2": p2_failures,
            "P3": p3_failures,
            "P4": p4_failures,
            "P5": p5_failures,
        }
        best_path = min(path_failures.items(), key=lambda item: len([label for label in item[1] if label != "hit"]))
        best_detail = self._compact_failures([label for label in best_path[1] if label != "hit"])
        return (
            "diag: "
            f"g[t={'Y' if time_allowed else 'N'} vol={'Y' if vol_ok else 'N'} "
            f"p12={'Y' if p1p2_ok else 'N'} p3={'Y' if p3_ok else 'N'} chop={chop_text}] "
            f"best={best_path[0]}:{best_detail} "
            f"P1:{self._compact_failures([label for label in p1_failures if label != 'hit'])} "
            f"P2:{self._compact_failures([label for label in p2_failures if label != 'hit'])} "
            f"P3:{self._compact_failures([label for label in p3_failures if label != 'hit'])} "
            f"P4:{self._compact_failures([label for label in p4_failures if label != 'hit'])} "
            f"P5:{self._compact_failures([label for label in p5_failures if label != 'hit'])}"
        )

    def _snapshot_from_indicators(
        self,
        indicators: dict[str, float | bool],
        *,
        bar_index: int,
    ) -> dict[str, float] | None:
        required = ("open", "price", "high", "low", "volume", "ema9", "ema20", "vwap", "vol_avg20", "vol_avg5")
        if any(field not in indicators for field in required):
            return None
        return {
            "bar_index": float(bar_index),
            "open": float(indicators["open"]),
            "close": float(indicators["price"]),
            "high": float(indicators["high"]),
            "low": float(indicators["low"]),
            "volume": float(indicators["volume"]),
            "ema9": float(indicators["ema9"]),
            "ema20": float(indicators["ema20"]),
            "vwap": float(indicators["vwap"]),
            "vol_avg20": float(indicators["vol_avg20"]),
            "vol_avg5": float(indicators["vol_avg5"]),
            "ema9_prev": float(indicators.get("ema9_prev", indicators["ema9"]) or indicators["ema9"]),
            "hist_value": float(indicators.get("hist_value", indicators.get("histogram", 0.0)) or 0.0),
            "macd_cross_above": bool(indicators.get("macd_cross_above", False)),
        }

    def _remember_bar(self, ticker: str, snapshot: dict[str, float]) -> None:
        recent = self._recent_bars.setdefault(ticker, [])
        snapshot_bar_index = int(snapshot.get("bar_index", 0) or 0)
        if recent:
            recent_bar_index = int(recent[-1].get("bar_index", 0) or 0)
            if snapshot_bar_index > 0 and recent_bar_index == snapshot_bar_index:
                recent[-1] = snapshot
            else:
                recent.append(snapshot)
        else:
            recent.append(snapshot)
        if len(recent) > 100:
            del recent[:-100]
        self._session_highs[ticker] = max(self._session_highs.get(ticker, snapshot["high"]), snapshot["high"])
        self._update_spike_state(ticker, snapshot)

    def _update_spike_state(self, ticker: str, snapshot: dict[str, float]) -> None:
        ema9 = snapshot["ema9"]
        if ema9 <= 0:
            return
        current_spike_ext = ((snapshot["high"] - ema9) / ema9) * 100
        is_spike = snapshot["close"] > snapshot["open"] and current_spike_ext >= self.config.p5_spike_ext_pct
        if is_spike:
            self._spike_anchor_bar[ticker] = int(snapshot["bar_index"])
            self._spike_anchor_high[ticker] = snapshot["high"]

    def _check_hard_gates(self, ticker: str, bar_index: int, position_tracker=None) -> dict[str, str | bool]:
        if position_tracker and position_tracker.has_position(ticker):
            return {"passed": False, "reason": "already in position"}
        if self._last_buy_bar.get(ticker, -1) == bar_index:
            return {"passed": False, "reason": "dedup (already fired this bar)"}
        last_exit = self._last_exit_bar.get(ticker, -999)
        if last_exit >= 0 and bar_index - last_exit < self.config.cooldown_bars:
            return {"passed": False, "reason": f"cooldown ({bar_index - last_exit}/{self.config.cooldown_bars} bars)"}
        rejected_open_until = self._rejected_open_until_bar.get(ticker, -1)
        if rejected_open_until > bar_index:
            bars_remaining = rejected_open_until - bar_index
            return {
                "passed": False,
                "reason": f"open rejection cooldown ({bars_remaining} bars remaining)",
            }
        if not self._time_allowed():
            return {"passed": False, "reason": "outside trading hours"}
        return {"passed": True, "reason": ""}

    def _time_allowed(self) -> bool:
        current = self.now_provider()
        if current.hour < self.config.trading_start_hour or current.hour >= self.config.trading_end_hour:
            return False
        time_str = current.strftime("%H:%M")
        if self.config.dead_zone_start <= time_str < self.config.dead_zone_end:
            return False
        return True

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
            "quantity": int(self.config.default_quantity),
            "entry_stage": "",
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
            "extended_vwap": float(indicators["vwap"]),
            "decision_vwap": float(indicators["vwap"]),
            "bar_volume": float(indicators["volume"]),
        }

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
        decision = {"status": status, "reason": reason}
        if path:
            decision["path"] = path
        if score is not None:
            decision["score"] = str(score)
        if score_details:
            decision["score_details"] = score_details
        self._last_decision[ticker] = decision

    def _roll_day_if_needed(self, ticker: str) -> None:
        day_key = self.now_provider().astimezone(EASTERN_TZ).strftime("%Y-%m-%d")
        existing = self._active_day_by_ticker.get(ticker)
        if existing is None:
            self._active_day_by_ticker[ticker] = day_key
            return
        if existing == day_key:
            return
        self._active_day_by_ticker[ticker] = day_key
        self._recent_bars.pop(ticker, None)
        self._pending.pop(ticker, None)
        self._session_highs.pop(ticker, None)
        self._spike_anchor_bar.pop(ticker, None)
        self._spike_anchor_high.pop(ticker, None)
        self._chop_lock_active.pop(ticker, None)
        self._p3_hard_stop_pause_until.pop(ticker, None)

    def _p3_pause_reason(self, ticker: str) -> str | None:
        pause_until = self._p3_hard_stop_pause_until.get(ticker)
        if pause_until is None:
            return None
        now = self.now_provider()
        if pause_until <= now:
            self._p3_hard_stop_pause_until.pop(ticker, None)
            return None
        remaining_minutes = max(1, int((pause_until - now).total_seconds() // 60) + 1)
        return f"P3 hard-stop pause active ({remaining_minutes} min remaining)"

    def _p3_cross_age_ok(self, ticker: str) -> bool:
        max_bars_since_cross = self.config.p3_max_bars_since_macd_cross
        if max_bars_since_cross is None or max_bars_since_cross <= 0:
            return True
        recent = self._recent_bars.get(ticker, [])
        if not recent:
            return True
        bars_since_cross = 1
        for bar in reversed(recent):
            if bool(bar.get("macd_cross_above", False)):
                return bars_since_cross <= max_bars_since_cross
            bars_since_cross += 1
        return False

    def _p3_recent_runup_ok(self, current: dict[str, float], recent: list[dict[str, float]]) -> bool:
        max_recent_runup_pct = self.config.p3_max_recent_runup_pct
        lookback_bars = int(self.config.p3_recent_runup_lookback_bars or 0)
        if max_recent_runup_pct is None or lookback_bars <= 0:
            return True
        window = list(recent[-max(0, lookback_bars - 1) :])
        window.append(current)
        lows = [float(bar.get("low", 0.0) or 0.0) for bar in window if float(bar.get("low", 0.0) or 0.0) > 0]
        highs = [float(bar.get("high", 0.0) or 0.0) for bar in window]
        if not lows or not highs:
            return True
        recent_low = min(lows)
        recent_high = max(highs)
        if recent_low <= 0:
            return True
        runup_pct = ((recent_high - recent_low) / recent_low) * 100.0
        return runup_pct <= max_recent_runup_pct

    def _p4_prev_bar_entry_ok(
        self,
        previous: dict[str, float] | None,
        current: dict[str, float],
    ) -> bool:
        if not bool(getattr(self.config, "p4_prev_bar_entry_enabled", False)):
            return False
        if previous is None:
            return False

        previous_close = float(previous.get("close", 0.0) or 0.0)
        previous_open = float(previous.get("open", previous_close) or previous_close)
        previous_vwap = float(previous.get("vwap", 0.0) or 0.0)
        current_open = float(current.get("open", 0.0) or 0.0)
        current_close = float(current.get("close", 0.0) or 0.0)
        current_high = float(current.get("high", 0.0) or 0.0)
        current_low = float(current.get("low", current_close) or current_close)

        if previous_close <= 0 or current_open <= 0:
            return False

        if bool(getattr(self.config, "p4_prev_bar_require_prev_above_vwap_or_green", False)):
            previous_green = previous_close > previous_open
            previous_above_vwap = previous_vwap > 0 and previous_close > previous_vwap
            if not (previous_green or previous_above_vwap):
                return False

        max_breakdown_pct = getattr(self.config, "p4_prev_bar_next_open_max_breakdown_pct", None)
        if max_breakdown_pct is not None:
            min_allowed_open = previous_close * (1.0 - (float(max_breakdown_pct) / 100.0))
            if current_open < min_allowed_open:
                return False

        if bool(getattr(self.config, "p4_prev_bar_require_break_prev_high", False)):
            if current_high <= float(previous.get("high", 0.0) or 0.0):
                return False

        if bool(getattr(self.config, "p4_prev_bar_require_close_above_prev_close", False)):
            if current_close <= previous_close:
                return False

        close_top_pct = getattr(self.config, "p4_prev_bar_confirm_close_top_pct", None)
        if close_top_pct is not None and current_high > current_low:
            min_close_for_band = current_low + (current_high - current_low) * (1.0 - (float(close_top_pct) / 100.0))
            if current_close < min_close_for_band:
                return False

        return True
