from __future__ import annotations

import logging
import time
from collections.abc import Callable

from project_mai_tai.strategy_core.models import OHLCVBar

logger = logging.getLogger(__name__)


class BarBuilder:
    """Build OHLCV bars from trade ticks, preserving legacy interval logic."""

    def __init__(
        self,
        ticker: str,
        interval_secs: int = 30,
        on_bar_complete: Callable[[str, dict, list[dict]], None] | None = None,
        max_bars: int = 2000,
        time_provider: Callable[[], float] | None = None,
    ):
        self.ticker = ticker
        self.interval_secs = interval_secs
        self.on_bar_complete = on_bar_complete
        self.max_bars = max_bars
        self.time_provider = time_provider or time.time

        self.bars: list[OHLCVBar] = []
        self._current_bar: OHLCVBar | None = None
        self._current_bar_start = 0.0
        self._bar_count = 0

    def on_trade(self, price: float, size: int, timestamp_ns: int = 0) -> list[OHLCVBar]:
        if price <= 0:
            return []

        if size < 100:
            return []

        now = self._resolve_timestamp(timestamp_ns)
        bar_start = (now // self.interval_secs) * self.interval_secs
        completed: list[OHLCVBar] = []

        if self._current_bar is None:
            self._current_bar = OHLCVBar.from_trade(price, size, bar_start)
            self._current_bar_start = bar_start
            return completed

        if bar_start > self._current_bar_start:
            closed = self._close_current_bar()
            if closed is not None:
                completed.append(closed)

            if self.bars:
                last_close = self.bars[-1].close
                gap_start = self._current_bar_start + self.interval_secs
                filled = 0
                while gap_start < bar_start and filled < 120:
                    gap_bar = OHLCVBar.flat_fill(last_close, gap_start)
                    self.bars.append(gap_bar)
                    completed.append(gap_bar)
                    self._trim_history()
                    filled += 1
                    gap_start += self.interval_secs

            self._current_bar = OHLCVBar.from_trade(price, size, bar_start)
            self._current_bar_start = bar_start
            return completed

        self._current_bar.update(price, size)
        return completed

    def check_bar_close(self) -> OHLCVBar | None:
        if self._current_bar is None:
            return None

        now = self.time_provider()
        bar_end = self._current_bar_start + self.interval_secs
        if now >= bar_end:
            closed = self._close_current_bar()
            self._current_bar = None
            return closed

        return None

    def get_current_price(self) -> float | None:
        if self._current_bar:
            return self._current_bar.close
        if self.bars:
            return self.bars[-1].close
        return None

    def get_bar_count(self) -> int:
        return self._bar_count

    def get_bars_as_dicts(self) -> list[dict[str, float | int]]:
        return [bar.as_dict() for bar in self.bars]

    def reset(self) -> None:
        self.bars.clear()
        self._current_bar = None
        self._current_bar_start = 0.0
        self._bar_count = 0

    def _resolve_timestamp(self, timestamp_ns: int) -> float:
        if timestamp_ns and timestamp_ns > 1_000_000_000_000_000_000:
            return timestamp_ns / 1_000_000_000
        if timestamp_ns and timestamp_ns > 1_000_000_000_000:
            return timestamp_ns / 1_000
        return self.time_provider()

    def _trim_history(self) -> None:
        if len(self.bars) > self.max_bars:
            self.bars = self.bars[-self.max_bars :]

    def _close_current_bar(self) -> OHLCVBar | None:
        if self._current_bar is None:
            return None

        bar = self._current_bar
        self.bars.append(bar)
        self._bar_count += 1
        self._trim_history()

        if self.on_bar_complete:
            self.on_bar_complete(
                self.ticker,
                bar.as_dict(),
                self.get_bars_as_dicts(),
            )

        logger.debug(
            "[BAR] %s #%s | O=%.3f H=%.3f L=%.3f C=%.3f V=%s",
            self.ticker,
            self._bar_count,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
        )
        return bar


class BarBuilderManager:
    def __init__(
        self,
        interval_secs: int = 30,
        on_bar_complete: Callable[[str, dict, list[dict]], None] | None = None,
        time_provider: Callable[[], float] | None = None,
    ):
        self.interval_secs = interval_secs
        self.on_bar_complete = on_bar_complete
        self.time_provider = time_provider
        self._builders: dict[str, BarBuilder] = {}

    def get_or_create(self, ticker: str) -> BarBuilder:
        if ticker not in self._builders:
            self._builders[ticker] = BarBuilder(
                ticker=ticker,
                interval_secs=self.interval_secs,
                on_bar_complete=self.on_bar_complete,
                time_provider=self.time_provider,
            )
            logger.info("[BAR] Created bar builder for %s (%ss bars)", ticker, self.interval_secs)
        return self._builders[ticker]

    def on_trade(self, ticker: str, price: float, size: int, timestamp_ns: int = 0) -> list[OHLCVBar]:
        return self.get_or_create(ticker).on_trade(price, size, timestamp_ns)

    def get_bars(self, ticker: str) -> list[dict[str, float | int]]:
        builder = self._builders.get(ticker)
        return builder.get_bars_as_dicts() if builder else []

    def check_all_bar_closes(self) -> list[OHLCVBar]:
        completed: list[OHLCVBar] = []
        for builder in self._builders.values():
            bar = builder.check_bar_close()
            if bar is not None:
                completed.append(bar)
        return completed

    def get_all_tickers(self) -> list[str]:
        return list(self._builders.keys())

    def reset(self) -> None:
        self._builders.clear()
