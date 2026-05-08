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
        del cumulative_volume
        if price <= 0:
            return []

        now = self._resolve_timestamp(timestamp_ns)
        bar_start = (now // self.interval_secs) * self.interval_secs
        completed: list[OHLCVBar] = []

        if self._current_bar is None and self.bars and bar_start <= self.bars[-1].timestamp:
            logger.debug(
                "[BAR] Ignoring stale trade for %s at %.3f (<= last closed %.3f)",
                self.ticker,
                bar_start,
                self.bars[-1].timestamp,
            )
            return completed

        if self._current_bar is None:
            self._current_bar = OHLCVBar.from_trade(price, size, bar_start)
            self._current_bar_start = bar_start
            return completed

        if bar_start < self._current_bar_start:
            logger.debug(
                "[BAR] Ignoring stale trade for %s at %.3f (< current %.3f)",
                self.ticker,
                bar_start,
                self._current_bar_start,
            )
            return completed

        if bar_start > self._current_bar_start:
            closed = self._close_current_bar()
            if closed is not None:
                completed.append(closed)

            self._current_bar = OHLCVBar.from_trade(price, size, bar_start)
            self._current_bar_start = bar_start
            return completed

        self._current_bar.update(price, size)
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
                "[BAR] Ignoring stale aggregate bar for %s at %.3f (<= last closed %.3f)",
                self.ticker,
                bar_start,
                self.bars[-1].timestamp,
            )
            return completed

        component_bar = OHLCVBar.from_bar(bar, timestamp=component_timestamp)

        if self._current_bar is None:
            self._current_bar_components = {component_timestamp: component_bar}
            self._current_bar = self._build_bar_from_components(
                bar_start=bar_start,
                component_bars=self._current_bar_components,
            )
            self._current_bar_start = bar_start
            return completed

        if bar_start < self._current_bar_start:
            if self._revise_last_closed_bar(component_timestamp, bar):
                return completed
            logger.debug(
                "[BAR] Ignoring stale aggregate bar for %s at %.3f (< current %.3f)",
                self.ticker,
                bar_start,
                self._current_bar_start,
            )
            return completed

        if bar_start > self._current_bar_start:
            closed = self._close_current_bar()
            if closed is not None:
                completed.append(closed)

            self._current_bar_components = {component_timestamp: component_bar}
            self._current_bar = self._build_bar_from_components(
                bar_start=bar_start,
                component_bars=self._current_bar_components,
            )
            self._current_bar_start = bar_start
            return completed

        self._current_bar_components[component_timestamp] = component_bar
        self._current_bar = self._build_bar_from_components(
            bar_start=bar_start,
            component_bars=self._current_bar_components,
        )
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

    def get_bars_with_current_as_dicts(self) -> list[dict[str, float | int]]:
        bars = self.get_bars_as_dicts()
        if self._current_bar is not None:
            bars.append(self._current_bar.as_dict())
        return bars

    def reset(self) -> None:
        self.bars.clear()
        self._current_bar = None
        self._current_bar_start = 0.0
        self._bar_count = 0
        self._current_bar_components.clear()
        self._last_closed_bar_components.clear()
        self._recent_revised_closed_bar = None

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
        self._last_closed_bar_components = {
            timestamp: OHLCVBar.from_bar(component, timestamp=component.timestamp)
            for timestamp, component in self._current_bar_components.items()
        }
        self.bars.append(bar)
        self._bar_count += 1
        self._trim_history()
        self._current_bar_components = {}

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

        component_bar = OHLCVBar.from_bar(bar, timestamp=component_timestamp)
        self._last_closed_bar_components[component_timestamp] = component_bar
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
            "[BAR] Revised last closed aggregate bar for %s at %.3f from late component %.3f",
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
            logger.debug("[BAR] Created bar builder for %s (%ss bars)", ticker, self.interval_secs)
        return self._builders[ticker]

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

    def get_builder(self, ticker: str) -> BarBuilder | None:
        return self._builders.get(ticker)

    def get_bars(self, ticker: str) -> list[dict[str, float | int]]:
        builder = self._builders.get(ticker)
        return builder.get_bars_as_dicts() if builder else []

    def consume_recent_revised_closed_bar(self, ticker: str) -> OHLCVBar | None:
        builder = self._builders.get(ticker)
        if builder is None:
            return None
        return builder.consume_recent_revised_closed_bar()

    def check_all_bar_closes(self) -> list[tuple[str, OHLCVBar]]:
        completed: list[tuple[str, OHLCVBar]] = []
        for ticker, builder in self._builders.items():
            bar = builder.check_bar_close()
            if bar is not None:
                completed.append((ticker, bar))
        return completed

    def get_all_tickers(self) -> list[str]:
        return list(self._builders.keys())

    def remove_tickers(self, tickers: set[str] | list[str]) -> None:
        for ticker in tickers:
            self._builders.pop(ticker, None)

    def reset(self) -> None:
        self._builders.clear()
