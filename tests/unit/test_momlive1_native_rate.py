"""MAIN SERIAL ONLY: controlled active native runtime, never a live-order proof."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
import threading
import time

import pytest

from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup
from tests.unit.momlive1_owned_fixtures import complete_owned_observation


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True], ids=["OFF", "ON"])
async def test_active_mom_native_240_per_second_60_seconds_slow_offloop(enabled, monkeypatch):
    now, broker, service, runtime = setup()
    await runtime.start()
    try:
        broker.entry_quantity, broker.entry_status = D(1), "partially_filled"
        await offer_and_drain(runtime, event(now[0]))
        await complete_owned_observation(runtime, next(iter(runtime.book.episodes)))
        broker.entry_quantity, broker.entry_status = D(0), "accepted"
        second = event(now[0])
        data = json.loads(second.payload.metadata["momlive_signal"])
        data.update(symbol="DLXY", source_id="sealed-dlxy")
        second = second.model_copy(update={"payload": second.payload.model_copy(update={
            "symbol": "DLXY", "metadata": {"momlive_signal": json.dumps(data)}})})
        service._latest_quotes_by_symbol["DLXY"] = dict(service._latest_quotes_by_symbol["SCNI"])
        await offer_and_drain(runtime, second)
        assert len(runtime.actor.work.owners) == 2
        held = next(ep for ep in runtime.book.episodes.values() if ep.symbol == "SCNI")
        assert held.filled == 1 and held.remaining == 1
        assert any(row.metadata.get("webull_exit_only_pair") for row in broker.sent)
        service.settings.momentum_live_enabled = enabled
        service._momentum_live_actor = runtime.actor
        service._momentum_live_quote_dispatch = runtime.actor.dispatch
        main_thread = threading.get_ident()
        db_calls, broker_calls, wrong_thread = 0, 0, False
        original_db = service._run_db

        async def delayed_db(function, *args, **kwargs):
            def slow_unit(session, *unit_args):
                nonlocal db_calls, wrong_thread
                wrong_thread |= threading.get_ident() == main_thread
                db_calls += 1
                time.sleep(.2)
                return function(session, *unit_args)
            return await original_db(slow_unit, *args, **kwargs)

        monkeypatch.setattr(service, "_run_db", delayed_db)
        for name in ("submit_order", "fetch_order_update", "cancel_exit_pair", "list_account_positions"):
            original = getattr(broker, name)
            async def delayed_broker(*args, _original=original, **kwargs):
                nonlocal broker_calls, wrong_thread
                def delay():
                    nonlocal wrong_thread
                    wrong_thread |= threading.get_ident() == main_thread
                    time.sleep(.2)
                await asyncio.to_thread(delay)
                broker_calls += 1
                if (_original.__name__ == "submit_order" and args
                        and args[0].intent_type == "cancel" and args[0].symbol == "DLXY"):
                    return []  # Cancel ACK unknown; exact order stays working/reserved.
                return await _original(*args, **kwargs)
            monkeypatch.setattr(broker, name, delayed_broker)

        stalls, stop = [], asyncio.Event()
        async def watch():
            deadline = time.monotonic()
            while not stop.is_set():
                deadline += .005
                await asyncio.sleep(max(0, deadline-time.monotonic()))
                stalls.append(max(0, time.monotonic()-deadline))
        watcher = asyncio.create_task(watch())
        start, base, count = time.monotonic(), now[0], 0
        max_handler, max_queue, max_inbox = 0., 0, 0
        exit_seeded = False
        seed_task = None
        async def seed_earlier_execution():
            async with runtime.state_lock:
                request = next(row for row in broker.sent if row.side == "buy" and row.symbol == "SCNI")
                report = await broker.fetch_order_update(request)
                await runtime._record(request, [replace(report,
                    reported_at=now[0]-timedelta(seconds=301),
                    metadata={**report.metadata, "momlive_execution_time_source": "broker_execution_time"})])
        try:
            while time.monotonic()-start < 60:
                elapsed = time.monotonic()-start
                now[0] = base + timedelta(seconds=elapsed)
                if elapsed >= 30 and not exit_seeded:
                    # Controlled earlier broker timestamp, not a retained live fill.
                    # Exercises P2 minimum-first-fill and owned 300s exit under load.
                    seed_task = asyncio.create_task(seed_earlier_execution())
                    exit_seeded = True
                symbol = "SCNI" if count % 2 else "DLXY"
                quote = QuoteTickEvent(source_service="controlled-native-rate", produced_at=now[0],
                    payload=QuoteTickPayload(symbol=symbol, bid_price=D(3), ask_price=D("3.001")))
                before = time.monotonic()
                await service._handle_quote_tick_event(quote)
                max_handler = max(max_handler, time.monotonic()-before)
                max_queue = max(max_queue, runtime.actor.work.queue.qsize())
                max_inbox = max(max_inbox, runtime.actor.inbox.qsize())
                count += 1
                await asyncio.sleep(max(0, start+count/240-time.monotonic()))
            duration = time.monotonic()-start
        finally:
            stop.set()
            await watcher
            if seed_task is not None:
                await seed_task
        receipt = {"scope": "CONTROLLED active owned native runtime; no real broker/network",
            "entry_switch": enabled, "seconds": duration, "events": count,
            "events_per_second": count/duration, "max_loop_stall_ms": max(stalls)*1000,
            "max_handler_ms": max_handler*1000, "work_queue_high_water": max_queue,
            "inbox_high_water": max_inbox, "db_units": db_calls, "broker_units": broker_calls,
            "delay_seconds": .2, "wrong_delay_thread": wrong_thread,
            "pending_partial_protected": True, "time_exit_eligible_seeded": exit_seeded,
            "owned_exit_wires": sum(row.side == "sell" and bool(row.metadata.get("exit_reason"))
                                    for row in broker.sent)}
        print("MOMLIVE1_NATIVE_RATE="+json.dumps(receipt, sort_keys=True))
        assert duration >= 60 and count/duration >= 200
        assert max(stalls) < .05 and max_handler < .05
        assert max_queue <= 2 and max_inbox <= 64 and len(runtime.actor.tasks) == 4
        assert db_calls > 0 and broker_calls > 0 and not wrong_thread
        assert exit_seeded and receipt["owned_exit_wires"] >= 1
        assert runtime.book._active_count == 2
    finally:
        service._momentum_live_actor = None
        service._momentum_live_quote_dispatch = None
        await runtime.close()
