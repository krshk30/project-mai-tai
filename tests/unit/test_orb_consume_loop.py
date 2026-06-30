"""ORB market-data consume-loop throughput (the 2026-06-30 open-burst latency fix).

The bug: a single ``xread(count=500)`` per 1s loop fell ~3x behind the open burst
(~196 ticks/s effective vs ~700/s arrival), surfacing the 09:30 bar + its entry ~1:47
late. The fix mirrors strategy-engine #175/#179: drain-to-budget with non-blocking
follow-up passes so ORB keeps up. These tests pin that drain behaviour.
"""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.settings import Settings


def _tick(i: int) -> tuple[str, dict]:
    data = {"event_type": "trade_tick", "payload": {"symbol": "AAA", "price": 1.0, "size": 1, "timestamp_ns": 1}}
    return (f"{i}-0", {"data": json.dumps(data)})


class _ScriptedRedis:
    """xread returns one pre-scripted batch per call, then []. Records the block arg."""

    def __init__(self, batches: list[list]) -> None:
        self._batches = list(batches)
        self.blocks: list = []

    async def xread(self, offsets, block=0, count=0):
        del offsets, count
        self.blocks.append(block)
        if not self._batches:
            return []
        return [("mai_tai:market-data", self._batches.pop(0))]


class _InfiniteRedis:
    """xread always returns a full `n`-entry batch (never drains) — exercises the budget."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.calls = 0

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        self.calls += 1
        return [("mai_tai:market-data", [_tick(i) for i in range(self.n)])]


def _svc(redis) -> OrbService:
    svc = OrbService.__new__(OrbService)
    svc.settings = Settings(redis_stream_prefix="mai_tai")
    svc.redis = redis
    svc._md_offset = "0"
    svc._last_gateway_symbols = ["AAA"]
    svc._handle_market_data = MagicMock()  # isolate the drain loop from tick handling
    return svc


@pytest.mark.asyncio
async def test_drain_processes_whole_backlog_in_one_call() -> None:
    # 3 full passes (== count) then a short pass -> caught up, all drained in ONE call.
    count = OrbService._MARKET_DATA_XREAD_COUNT
    batches = [[_tick(i) for i in range(count)] for _ in range(3)] + [[_tick(0)]]
    r = _ScriptedRedis(batches)
    svc = _svc(r)
    processed = await svc._drain_market_data()
    assert processed == 3 * count + 1
    assert svc._handle_market_data.call_count == 3 * count + 1
    # first pass BLOCKs, every follow-up pass is non-blocking
    assert r.blocks[0] == 500
    assert all(b is None for b in r.blocks[1:])


@pytest.mark.asyncio
async def test_drain_stops_at_budget_when_never_caught_up() -> None:
    r = _InfiniteRedis(OrbService._MARKET_DATA_XREAD_COUNT)
    svc = _svc(r)
    processed = await svc._drain_market_data()
    assert processed == OrbService._MARKET_DATA_DRAIN_BUDGET  # bounded — can't starve the loop
    assert svc._handle_market_data.call_count == OrbService._MARKET_DATA_DRAIN_BUDGET


@pytest.mark.asyncio
async def test_drain_caught_up_single_pass() -> None:
    # one short batch -> one read, caught up immediately.
    r = _ScriptedRedis([[_tick(0), _tick(1)]])
    svc = _svc(r)
    processed = await svc._drain_market_data()
    assert processed == 2
    assert r.blocks == [500]  # exactly one (blocking) read


@pytest.mark.asyncio
async def test_drain_noop_without_symbols() -> None:
    r = _ScriptedRedis([[_tick(0)]])
    svc = _svc(r)
    svc._last_gateway_symbols = []
    processed = await svc._drain_market_data()
    assert processed == 0
    assert r.blocks == []  # never even reads the stream
