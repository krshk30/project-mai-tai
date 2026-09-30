"""Assessment-only reproduction of the gateway's trade/quote queue scheduling."""

import asyncio
from dataclasses import dataclass

import pytest

from project_mai_tai.market_data.gateway import MarketDataGatewayService


@dataclass(frozen=True)
class Arrival:
    second: int


@pytest.mark.asyncio
async def test_quote_heavy_batches_defer_trades_under_one_trade_per_cycle():
    class ScheduledTrades(asyncio.Queue):
        async def get(self):
            if self.empty() and gateway.next_second < gateway.seconds:
                gateway.now = max(gateway.now, float(gateway.next_second))
                gateway.enqueue_due()
            return await super().get()

    class Gateway:
        seconds = 15
        next_second = 0
        now = 0.0
        publish_seconds = 0.0032

        def __init__(self):
            self._trade_queue = ScheduledTrades()
            self._quote_queue = asyncio.Queue()
            self._bar_queue = asyncio.Queue()
            self.trade_lag_by_second = {second: [] for second in range(self.seconds)}
            self.quote_lag_by_second = {second: [] for second in range(self.seconds)}
            self.trades_done = 0

        def enqueue_due(self):
            while self.next_second < self.seconds and self.next_second <= self.now:
                for _ in range(50):
                    self._trade_queue.put_nowait(Arrival(self.next_second))
                for _ in range(300):
                    self._quote_queue.put_nowait(Arrival(self.next_second))
                self.next_second += 1

        async def _publish_trade_tick_safely(self, item):
            self.trade_lag_by_second[item.second].append(self.now - item.second)
            self.now += self.publish_seconds
            self.enqueue_due()
            self.trades_done += 1
            if self.trades_done == 50 * self.seconds:
                stop.set()

        async def _publish_quote_tick_safely(self, item):
            self.quote_lag_by_second[item.second].append(self.now - item.second)
            self.now += self.publish_seconds
            self.enqueue_due()

        async def _publish_live_bar_safely(self, _item):
            pytest.fail("no bar was queued")

    gateway = Gateway()
    stop = asyncio.Event()
    gateway.enqueue_due()
    await asyncio.wait_for(MarketDataGatewayService._stream_publish_loop(gateway, stop), timeout=5)

    assert gateway.trades_done == 750
    assert sum(map(len, gateway.quote_lag_by_second.values())) == 4500
    early_trades = gateway.trade_lag_by_second[0]
    late_trades = gateway.trade_lag_by_second[14]
    early_quotes = gateway.quote_lag_by_second[0]
    late_quotes = gateway.quote_lag_by_second[14]
    assert early_trades[0] == 0
    assert max(early_trades) > 3.0
    assert min(late_trades) > 2.0
    assert max(early_quotes) < 1.0
    assert max(late_quotes) < 1.0
    assert abs(max(late_quotes) - max(early_quotes)) < 0.1
