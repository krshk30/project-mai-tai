from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging

from project_mai_tai.strategy_core.config import IndicatorConfig
from project_mai_tai.strategy_core.models import OHLCVBar

logger = logging.getLogger(__name__)


def ema(values: list[float], period: int) -> list[float]:
    if not values or period <= 0:
        return []
    result = [0.0] * len(values)
    k = 2.0 / (period + 1)
    result[0] = values[0]
    for index in range(1, len(values)):
        result[index] = values[index] * k + result[index - 1] * (1 - k)
    return result


def sma(values: list[float], period: int) -> list[float]:
    if not values or period <= 0:
        return values
    result: list[float] = []
    for index in range(len(values)):
        if index < period - 1:
            result.append(values[index])
        else:
            result.append(sum(values[index - period + 1 : index + 1]) / period)
    return result


def macd(closes: list[float], fast: int = 12, slow: int = 26, sig: int = 9) -> dict[str, list[float]]:
    if len(closes) < slow:
        return {"macd": [], "signal": [], "histogram": []}

    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line = [fast_value - slow_value for fast_value, slow_value in zip(fast_ema, slow_ema)]
    signal_line = ema(macd_line, sig)
    histogram = [macd_value - signal_value for macd_value, signal_value in zip(macd_line, signal_line)]

    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def stoch_k(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    k_period: int = 5,
    smooth_k: int = 1,
) -> list[float]:
    if len(closes) < k_period:
        return []

    raw_k: list[float] = []
    for index in range(len(closes)):
        if index < k_period - 1:
            raw_k.append(50.0)
            continue
        period_high = max(highs[index - k_period + 1 : index + 1])
        period_low = min(lows[index - k_period + 1 : index + 1])
        if period_high == period_low:
            raw_k.append(50.0)
        else:
            raw_k.append((closes[index] - period_low) / (period_high - period_low) * 100)

    if smooth_k > 1:
        return sma(raw_k, smooth_k)
    return raw_k


def vwap(highs: list[float], lows: list[float], closes: list[float], volumes: list[float]) -> list[float]:
    if not closes:
        return []

    result: list[float] = []
    cumulative_tp_volume = 0.0
    cumulative_volume = 0.0

    for index in range(len(closes)):
        typical_price = (highs[index] + lows[index] + closes[index]) / 3
        volume = volumes[index] if index < len(volumes) else 0
        cumulative_tp_volume += typical_price * volume
        cumulative_volume += volume
        result.append(cumulative_tp_volume / cumulative_volume if cumulative_volume > 0 else closes[index])

    return result


def _bar_value(bar: OHLCVBar | Mapping[str, float | int], field: str) -> float:
    if isinstance(bar, OHLCVBar):
        return float(getattr(bar, field))
    return float(bar[field])


class IndicatorEngine:
    def __init__(self, config: IndicatorConfig):
        self.config = config

    def calculate(self, bars: Sequence[OHLCVBar | Mapping[str, float | int]]) -> dict[str, float | bool] | None:
        minimum_bars = self.config.macd_slow + self.config.macd_signal
        if len(bars) < minimum_bars:
            return None

        closes = [_bar_value(bar, "close") for bar in bars]
        highs = [_bar_value(bar, "high") for bar in bars]
        lows = [_bar_value(bar, "low") for bar in bars]
        volumes = [_bar_value(bar, "volume") for bar in bars]

        macd_data = macd(closes, self.config.macd_fast, self.config.macd_slow, self.config.macd_signal)
        macd_line = macd_data["macd"]
        signal_line = macd_data["signal"]
        histogram = macd_data["histogram"]

        stoch = stoch_k(highs, lows, closes, self.config.stoch_len, self.config.stoch_smooth_k)
        ema9 = ema(closes, self.config.ema1_len)
        ema20 = ema(closes, self.config.ema2_len)
        vwap_values = vwap(highs, lows, closes, volumes)

        index = len(closes) - 1
        previous_index = index - 1 if index > 0 else 0

        return {
            "price": closes[index],
            "price_prev": closes[previous_index],
            "high": highs[index],
            "low": lows[index],
            "volume": volumes[index],
            "macd": macd_line[index],
            "macd_prev": macd_line[previous_index],
            "signal": signal_line[index],
            "signal_prev": signal_line[previous_index],
            "histogram": histogram[index],
            "histogram_prev": histogram[previous_index],
            "stoch_k": stoch[index] if index < len(stoch) else 50.0,
            "stoch_k_prev": stoch[previous_index] if previous_index < len(stoch) else 50.0,
            "ema9": ema9[index],
            "ema20": ema20[index],
            "vwap": vwap_values[index],
            "macd_above_signal": macd_line[index] > signal_line[index],
            "macd_cross_above": macd_line[index] > signal_line[index]
            and macd_line[previous_index] <= signal_line[previous_index],
            "macd_cross_below": macd_line[index] < signal_line[index]
            and macd_line[previous_index] >= signal_line[previous_index],
            "macd_increasing": macd_line[index] > macd_line[previous_index],
            "macd_delta": macd_line[index] - macd_line[previous_index],
            "macd_delta_prev": macd_line[previous_index] - macd_line[max(0, previous_index - 1)]
            if previous_index > 0
            else 0.0,
            "macd_delta_accelerating": (
                (macd_line[index] - macd_line[previous_index])
                > (macd_line[previous_index] - macd_line[max(0, previous_index - 1)])
            )
            if previous_index > 0
            else False,
            "histogram_growing": histogram[index] > histogram[previous_index],
            "stoch_k_rising": stoch[index] > stoch[previous_index]
            if index < len(stoch) and previous_index < len(stoch)
            else False,
            "stoch_k_below_exit": stoch[index] < self.config.stoch_exit_level
            if index < len(stoch)
            else False,
            "stoch_k_falling": stoch[index] < stoch[previous_index]
            if index < len(stoch) and previous_index < len(stoch)
            else False,
            "price_above_vwap": closes[index] > vwap_values[index],
            "price_above_ema9": closes[index] > ema9[index],
            "price_above_ema20": closes[index] > ema20[index],
            "price_above_both_emas": closes[index] > ema9[index] and closes[index] > ema20[index],
            "price_cross_above_vwap": closes[index] > vwap_values[index]
            and closes[previous_index] <= vwap_values[previous_index],
            "macd_was_below_3bars": (
                index >= 4
                and macd_line[previous_index] <= signal_line[previous_index]
                and macd_line[max(0, index - 2)] <= signal_line[max(0, index - 2)]
                and macd_line[max(0, index - 3)] <= signal_line[max(0, index - 3)]
            ),
        }
