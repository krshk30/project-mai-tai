"""Real v2 quote handler during off-loop joined-seed parsing and ATR rebuild."""
import asyncio
import time

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from tests.unit.test_line_candidate_live_set import CANDIDATE, ROWS


@pytest.mark.asyncio
async def test_joined_seed_60s_quote_rate_has_no_50ms_loop_stall(monkeypatch):
    replay = __import__("line_repair_preopen_replay")
    monkeypatch.setattr(replay, "LIVE_CONTROLS", CANDIDATE)
    row = next(r for r in ROWS if r["day"] == "2026-10-09" and r["symbol"] == "MI")
    current = row["boundary_ms"] + 9 * 60_000
    case, provider = replay.setup(row, current, admission_control=True)
    case.bot._boot_state_restoration_complete = True
    case.bot._cw_boot_hold_check()
    original_fetch = case.bot._atr_massive_seed_client.fetch_preopen

    def slow_recorded_fetch(*args):
        time.sleep(0.5)
        return original_fetch(*args)

    monkeypatch.setattr(case.bot._atr_massive_seed_client, "fetch_preopen", slow_recorded_fetch)
    quote = Quote("MI", 1.0, 1.01, 1.0, case.now_ms, trade_time_ms=case.now_ms)
    loop = asyncio.get_running_loop()
    start = loop.time()
    stop = start + 60.0
    stalls = []
    handler_ms = []

    async def heartbeat():
        deadline = loop.time() + 0.002
        while loop.time() < stop:
            await asyncio.sleep(max(0, deadline - loop.time()))
            stalls.append(max(0, loop.time() - deadline) * 1000)
            deadline = loop.time() + 0.002

    with case.clock():
        watcher = asyncio.create_task(heartbeat())
        seed = asyncio.create_task(replay.repair(row, case, current))
        events = 0
        while loop.time() < stop:
            tick_start = loop.time()
            await case.bot._handle_quote("MI", quote)
            handler_ms.append((loop.time() - tick_start) * 1000)
            events += 1
            await asyncio.sleep(max(0, start + events / 225.0 - loop.time()))
        assert await seed
        await watcher
    duration = loop.time() - start
    print(f"LINE-SEED-RATE events={events} duration_s={duration:.3f} "
          f"events_s={events / duration:.3f} max_stall_ms={max(stalls):.3f} "
          f"max_quote_ms={max(handler_ms):.3f} seed_requests={provider.get_aggs.call_count}")
    assert duration >= 60
    assert events / duration >= 200
    assert max(stalls) < 50
    assert max(handler_ms) < 50
    assert provider.get_aggs.call_count == 1
    assert case.snapshot()["buy_count"] == 0
