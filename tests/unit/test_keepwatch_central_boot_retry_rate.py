"""Main-only quiet-window rate gate; NOT executed by the source author."""

import asyncio
import time
from unittest.mock import Mock

import pytest

from tests.unit.test_keepwatch_central_boot_retry import controlled_bot, controlled_proof


@pytest.mark.asyncio
async def test_central_retry_240_per_second_60s_incomplete_first_10s(monkeypatch, record_property):
    bot = controlled_bot()
    actual = bot._try_complete_boot_state_restoration
    spy = Mock(wraps=actual)
    monkeypatch.setattr(bot, "_try_complete_boot_state_restoration", spy)
    rate, seconds, incomplete_seconds = 240, 60, 10
    count, max_handler_ns, max_stall_ns = 0, 0, 0
    stop = asyncio.Event()

    async def heartbeat():
        nonlocal max_stall_ns
        previous = time.perf_counter_ns()
        while not stop.is_set():
            await asyncio.sleep(.001)
            current = time.perf_counter_ns()
            max_stall_ns = max(max_stall_ns, current - previous - 1_000_000)
            previous = current

    monitor = asyncio.create_task(heartbeat())
    start = time.perf_counter_ns()
    latch_calls = None
    try:
        for index in range(rate * seconds):
            deadline = start + index * 1_000_000_000 // rate
            await asyncio.sleep(max(0, (deadline - time.perf_counter_ns()) / 1e9))
            if index == rate * incomplete_seconds:
                controlled_proof(bot)
            before = time.perf_counter_ns()
            bot._cw_boot_hold_check()
            max_handler_ns = max(max_handler_ns, time.perf_counter_ns() - before)
            count += 1
            if index < rate * incomplete_seconds:
                assert not bot._boot_state_restoration_complete and bot.strategy._entries_held
            else:
                assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
                if latch_calls is None:
                    latch_calls = spy.call_count
                assert spy.call_count == latch_calls
        await asyncio.sleep(max(0, (start + seconds * 1_000_000_000 - time.perf_counter_ns()) / 1e9))
    finally:
        stop.set()
        await monitor
    elapsed_ns = time.perf_counter_ns() - start
    measured_rate = count * 1e9 / elapsed_ns
    for key, value in {"events": count, "elapsed_ns": elapsed_ns,
        "measured_rate": measured_rate, "max_handler_ns": max_handler_ns,
        "max_stall_ns": max_stall_ns, "retry_calls": spy.call_count}.items():
        record_property(key, value)
    assert count == 14_400 and elapsed_ns >= 60_000_000_000
    assert measured_rate >= 200
    assert max_stall_ns < 50_000_000
    assert max_handler_ns < 50_000_000
    assert latch_calls == rate * incomplete_seconds + 1
