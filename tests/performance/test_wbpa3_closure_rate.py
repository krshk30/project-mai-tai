"""Unchanged real OMS 60s callback test + actual WBPA3 DB readers off-loop.

Two separate private SQLite copies share the event loop/executor. This does not
prove native/SDK/full-fleet/PG performance or a common production transaction.
"""
import asyncio
import json
import os
import threading
from time import monotonic
from types import SimpleNamespace

import pytest

from project_mai_tai.brokerdecides1_sync import local_evidence, now_ms
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from tests.performance import test_wbpa2_country_rate as original
from tests.unit.wbpa3_episode_port import ACCOUNT_NAMES, OPPORTUNITY, seed_episode

pytestmark = pytest.mark.skipif(not os.environ.get("WBPA3_SOURCE_DIRECTORY"),
    reason="Requires reviewed private WBPA3 inputs and the bounded Main rate profile")


@pytest.mark.asyncio
async def test_actual_oms_quote_rate_with_wbpa3_closure_readers_for_60_seconds(
    tmp_path, monkeypatch, record_property,
):
    case = seed_episode(tmp_path)
    settings = Settings(_env_file=None, strategy_schwab_1m_v2_country_route_enabled=True,
        strategy_schwab_1m_v2_account_name=ACCOUNT_NAMES[0],
        strategy_schwab_1m_v2_webull_account_name=ACCOUNT_NAMES[1])
    bot = SimpleNamespace(settings=settings)
    loop_thread = threading.get_ident()
    spans, timing = [], {}
    go, stop = asyncio.Event(), asyncio.Event()
    original_monitor = original.LoopStallMonitor

    class ObservedMonitor(original_monitor):
        def __init__(self, begin):
            super().__init__(begin)
            timing["rate_begin"] = begin
            go.set()

        def finish(self, end):
            timing["rate_end"] = end
            return super().finish(end)

    monkeypatch.setattr(original, "LoopStallMonitor", ObservedMonitor)

    def scan():
        started = monotonic()
        worker = threading.get_ident()
        with case.factory() as session:
            inflight, fills, terminals = local_evidence(session, ACCOUNT_NAMES)
            recovered, evaluated = SchwabV2BotService._terminal_unfilled_first_rest_opportunities(
                bot, session, account_names=ACCOUNT_NAMES, unknown_opportunities={"CHOW": OPPORTUNITY})
        ended = monotonic()
        assert worker != loop_thread
        assert not inflight and not fills and len(terminals) == 4
        assert recovered == {"CHOW": frozenset({OPPORTUNITY})} and evaluated == 1
        return {"start": started, "end": ended, "seconds": ended-started,
            "worker_thread": worker, "observed_ms": now_ms(), "terminal_tokens": len(terminals)}

    async def read_actor():
        await go.wait()
        while not stop.is_set():
            # This task owns its awaited thread/session for its full lifetime.
            row = await asyncio.to_thread(scan)
            row["joined_on_loop_at"] = monotonic()
            spans.append(row)
            due = timing["rate_begin"] + len(spans)
            try:
                await asyncio.wait_for(stop.wait(), max(.001, due-monotonic()))
            except TimeoutError:
                pass

    actor = asyncio.create_task(read_actor(), name="CONTROLLED-WBPA3-owned-offloop-reader")
    completed = False
    try:
        # Original function/assertions/200eps/60s/50ms ceilings are unchanged.
        await original.test_actual_country_deferred_slot_240_quote_callbacks_per_second_for_60_seconds(
            tmp_path, monkeypatch, record_property)
        completed = True
    finally:
        stop.set()
        go.set()
        try:
            await original.physical_join(actor)
        finally:
            case.engine.dispose()
        measured = {"timing": timing, "loop_thread": loop_thread, "reader_spans": spans,
            "reader_count": len(spans), "actor_physically_joined": actor.done() and not actor.cancelled(),
            "original_rate_completed": completed,
            "scope": "REAL_OMS_CALLBACK_SERIAL_PLUS_REAL_WBPA3_READERS_SEPARATE_CONTROLLED_SQLITE_COPIES_SHARED_LOOP_EXECUTOR_NOT_NATIVE_PG_FLEET"}
        print("WBPA3_RATE_READER_EVIDENCE=" + json.dumps(measured, sort_keys=True))
        record_property("WBPA3_reader_evidence", json.dumps(measured, sort_keys=True))
    inside = [s for s in spans if timing["rate_begin"] <= s["start"] <= s["end"] <= timing["rate_end"]]
    assert len(inside) >= 50
    assert all(s["worker_thread"] != loop_thread and s["end"] <= s["joined_on_loop_at"] for s in spans)
    assert actor.done() and not actor.cancelled()
