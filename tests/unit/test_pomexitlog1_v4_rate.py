"""MAIN SERIAL ONLY: original MOM native-rate controller, PM/RTH logging active.

SOURCE derived from test_momlive1_native_rate.py; same existing setup, positive
owned partial fill, second owner, earlier-execution seed, delayed off-loop ports,
240Hz physical feed, original >=200Hz/<50ms gates. Proposed bounded measurement:
retain the original seed at30s; start60s of active retries at the FIRST actual
saved-attempt INFO. Physical feed is bounded at120s, with incomplete activity
INCONCLUSIVE. No predicted first-attempt time or business clock stub.
Every10s active bucket needs actual saved-attempt INFO and>=200Hz quotes; else
activity is INCONCLUSIVE. No counter is cleared/increased. No new runner/fence/native
reader. The PM case binds the controlled session BEFORE start and republishes
the same fixture receipt clocks; it asserts the actual PM sole-target policy
instead of pretending a native RTH pair is required outside RTH.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from functools import wraps
import json
import logging
import sys
import threading
import time

import pytest

from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.positions_read_receipt import record_receipts
# STALL-CORRELATION diagnostics BEGIN
from tests.unit.pom_rate_stall_observer import StallCorrelation
# STALL-CORRELATION diagnostics END
from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup


class TimingCallCounter(logging.Handler):
    """Bounded scalar counts only; existing root StreamHandler still writes INFO.

    No getMessage/formatting, log-record history, futures or retained per-event data.
    This small test-only handler's cost remains inside the measured gate.
    """

    def __init__(self, runtime_instance):
        super().__init__(logging.INFO)
        self.runtime_instance = runtime_instance
        self.episodes = {}
        self.overflow = False
        self.window_start = None
        self.buckets = [{"gate": 0, "attempt": 0, "quote_events": 0,
                         "logger_calls": 0, "logger_max_ns": None, "logger_total_ns": 0}
                        for _ in range(6)]
        self.prelude = {"gate": 0, "attempt": 0, "quote_events": 0,
                        "logger_calls": 0, "logger_max_ns": None, "logger_total_ns": 0}
        self.post_window = {"gate": 0, "attempt": 0, "quote_events": 0,
                            "logger_calls": 0, "logger_max_ns": None, "logger_total_ns": 0}
        self.thread = threading.get_ident()
        self.wrong_log_thread = False
        self.allowed_episodes = None
        self.first_attempt_logger_ns = None

    def begin_window(self, started):
        assert self.window_start is None
        self.window_start = started  # Never clear APP counters OR total observed calls.

    def bucket(self, at):
        if self.window_start is None:
            return self.prelude
        elapsed = at - self.window_start
        if elapsed < 0:
            return self.prelude
        if 0 <= elapsed < 60:
            return self.buckets[int(elapsed // 10)]
        return self.post_window

    def observe_cost(self, started, elapsed_ns, is_attempt):
        target = self.bucket(started)
        target["logger_calls"] += 1
        target["logger_max_ns"] = max(target["logger_max_ns"] or 0, elapsed_ns)
        target["logger_total_ns"] += elapsed_ns
        if is_attempt and self.first_attempt_logger_ns is None:
            self.first_attempt_logger_ns = elapsed_ns

    def emit(self, record):
        if not isinstance(record.msg, str):
            return
        kind = ("gate" if record.msg.startswith("[MOMLIVE1-EXIT-GATE]") else "attempt"
                if record.msg.startswith("[MOMLIVE1-EXIT-ATTEMPT-TIMING]") else None)
        if kind is None:
            return
        if not isinstance(record.args, tuple) or len(record.args) < 2:
            self.overflow = True
            return
        if record.args[0] != self.runtime_instance:
            self.overflow = True
            return  # Another fixture/runtime cannot supply this window's proof.
        if kind == "attempt" and (len(record.args) < 3 or type(record.args[2]) is not int
                                  or record.args[2] <= 0):
            self.overflow = True
            return
        identity = record.args[1]
        if not isinstance(identity, str):
            self.overflow = True
            return
        if self.allowed_episodes is not None and identity not in self.allowed_episodes:
            self.overflow = True
            return
        at = time.monotonic()
        if kind == "attempt" and self.window_start is None:
            self.begin_window(at)  # Positive APP emission; no predicted start/reset.
        self.wrong_log_thread |= threading.get_ident() != self.thread
        self.bucket(at)[kind] += 1
        if identity not in self.episodes:
            if len(self.episodes) >= 2:
                self.overflow = True
                return
            self.episodes[identity] = {"gate": 0, "attempt": 0}
        self.episodes[identity][kind] += 1


@pytest.mark.asyncio
@pytest.mark.timing
@pytest.mark.parametrize("hour,session", [(12, "AM"), (14, "NORMAL")], ids=["Webull-PM", "Webull-RTH"])
async def test_existing_native_rate_60s_at_least200_stall_under50_with_timing_info_active(monkeypatch, hour, session):
    now, broker, service, runtime = setup()
    # Same controlled-fixture session binding used by prepared(), before actors.
    # No live/replay clock is retimed during measurement or positive prelude.
    now[0] = now[0].replace(hour=hour)
    service._latest_quotes_by_symbol["SCNI"]["received_at"] = now[0]
    with service.session_factory() as unit:
        record_receipts(unit, [(name, now[0], 0) for name in runtime.accounts], recorded_at=now[0])
        unit.commit()
    assert service.logger.isEnabledFor(logging.INFO) and service.logger.propagate
    assert any(isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout
               and handler.level <= logging.INFO for handler in logging.getLogger().handlers)
    counter = TimingCallCounter(runtime._exit_gate_instance)
    service.logger.addHandler(counter)
    original_info = service.logger.info

    def measured_info(message, *args, **kwargs):
        if not isinstance(message, str) or not message.startswith((
                "[MOMLIVE1-EXIT-GATE]", "[MOMLIVE1-EXIT-ATTEMPT-TIMING]")):
            return original_info(message, *args, **kwargs)
        # Test-only scalar cost observation; exactly ONE real logger call, no
        # handler bypass/replacement, no business clock or APP-state mutation.
        before = time.monotonic()
        before_ns = time.monotonic_ns()
        try:
            return original_info(message, *args, **kwargs)
        finally:
            counter.observe_cost(before, time.monotonic_ns() - before_ns,
                                 message.startswith("[MOMLIVE1-EXIT-ATTEMPT-TIMING]"))

    monkeypatch.setattr(service.logger, "info", measured_info)
    # STALL-CORRELATION diagnostics BEGIN
    correlation = StallCorrelation()
    correlation_physical_start = None
    # STALL-CORRELATION diagnostics END
    fixture_started = time.monotonic()
    try:
        # STALL-CORRELATION diagnostics BEGIN
        correlation.bind_steps(runtime, monkeypatch)
        correlation.install_gc()
        # STALL-CORRELATION diagnostics END
        assert await runtime.start()
        broker.entry_quantity, broker.entry_status = D(1), "partially_filled"
        await offer_and_drain(runtime, event(now[0]))
        held = next(ep for ep in runtime.book.episodes.values() if ep.symbol == "SCNI")
        # A queue join can retain the actor's pre-fill observation while this
        # fixture's business clock is frozen. Invalidate only that cached read
        # under its real owner lock, then await the actual observation/save/
        # protection path. No expected quantity, geometry or counter is seeded;
        # all original positive preconditions below still have to be earned.
        prelude_clock = now[0]
        async with runtime.state_lock:
            runtime.actor._observations.pop(held.episode_id, None)
        await asyncio.wait_for(runtime.actor._service_owned(held.episode_id), 10)
        await asyncio.wait_for(runtime.actor.work.queue.join(), 10)
        assert now[0] == prelude_clock
        broker.entry_quantity, broker.entry_status = D(0), "accepted"
        second = event(now[0])
        data = json.loads(second.payload.metadata["momlive_signal"])
        data.update(symbol="DLXY", source_id="sealed-dlxy")
        second = second.model_copy(update={"payload": second.payload.model_copy(update={
            "symbol": "DLXY", "metadata": {"momlive_signal": json.dumps(data)}})})
        service._latest_quotes_by_symbol["DLXY"] = dict(service._latest_quotes_by_symbol["SCNI"])
        await offer_and_drain(runtime, second)
        assert len(runtime.actor.work.owners) == 2
        counter.allowed_episodes = set(runtime.actor.work.owners)
        held = next(ep for ep in runtime.book.episodes.values() if ep.symbol == "SCNI")
        assert held.filled == 1 and held.remaining == 1
        protection = [request for request in broker.sent if request.reason == "MOMLIVE1_OWNED_PROTECTION"]
        assert protection and all(request.metadata["session"] == session for request in protection)
        if session == "NORMAL":
            assert any(request.metadata.get("webull_exit_only_pair") for request in protection)
        else:
            assert all(not request.metadata.get("webull_exit_only_pair")
                       and "bracket_stop_price" not in request.metadata for request in protection)
        service.settings.momentum_live_enabled = True
        service._momentum_live_actor = runtime.actor
        service._momentum_live_quote_dispatch = runtime.actor.dispatch
        main_thread = threading.get_ident()
        db_calls = broker_calls = 0
        wrong_thread = False
        original_db = service._run_db
        original_submit = broker.submit_order

        @wraps(original_submit)
        async def terminal_rejected_exit(request):
            reports = await original_submit(request)
            if request.intent_type == "close" and request.metadata.get("exit_reason"):
                # Existing test_momlive1_exit_recovery terminal-retry scenario:
                # a typed venue rejection, not a guard/permission bypass. The
                # original 2s durable pacing/terminal proof must cause retries.
                broker.orders[request.client_order_id].status = "rejected"
                return [replace(report, event_type="rejected") for report in reports]
            return reports

        monkeypatch.setattr(broker, "submit_order", terminal_rejected_exit)

        async def delayed_db(function, *args, **kwargs):
            def slow_unit(unit, *unit_args):
                nonlocal db_calls, wrong_thread
                wrong_thread |= threading.get_ident() == main_thread
                db_calls += 1
                time.sleep(.2)
                return function(unit, *unit_args)
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
                    return []  # Original controlled unknown ACK: no release/extra SELL.
                return await _original(*args, **kwargs)

            monkeypatch.setattr(broker, name, delayed_broker)

        stalls, stop = [], asyncio.Event()

        async def watch():
            deadline = time.monotonic()
            while not stop.is_set():
                deadline += .005
                await asyncio.sleep(max(0, deadline-time.monotonic()))
                stalls.append(max(0, time.monotonic()-deadline))
                # STALL-CORRELATION diagnostics BEGIN
                correlation.lag_sample(deadline, stalls[-1])
                # STALL-CORRELATION diagnostics END

        watcher = asyncio.create_task(watch())
        max_handler, max_queue, max_inbox = 0., 0, 0
        exit_seeded, seed_task = False, None

        async def seed_earlier_execution():
            async with runtime.state_lock:
                request = next(row for row in broker.sent if row.side == "buy" and row.symbol == "SCNI")
                report = await broker.fetch_order_update(request)
                await runtime._record(request, [replace(report,
                    reported_at=now[0]-timedelta(seconds=301),
                    metadata={**report.metadata, "momlive_execution_time_source": "broker_execution_time"})])

        # ORIGINAL seed at30s is retained, not moved earlier to force activity.
        # Active60s starts ONLY at the first real saved-attempt INFO, not at an
        # assumed latency after the seed. Loop-check budget120s proposal;
        # blocking awaits/IO still require Main's outer deadline and join proof.
        # Main reviews that caller envelope before execution. No clock stub.
        start, base, count = time.monotonic(), now[0], 0
        # STALL-CORRELATION diagnostics BEGIN
        correlation_physical_start = start
        # STALL-CORRELATION diagnostics END
        prelude_seconds = start - fixture_started
        try:
            while time.monotonic()-start < 120:
                if counter.window_start is not None and time.monotonic() >= counter.window_start + 60:
                    break
                elapsed = time.monotonic()-start
                now[0] = base + timedelta(seconds=elapsed)
                if elapsed >= 30 and not exit_seeded:
                    seed_task = asyncio.create_task(seed_earlier_execution())
                    exit_seeded = True
                symbol = "SCNI" if count % 2 else "DLXY"
                quote = QuoteTickEvent(source_service="controlled-native-rate", produced_at=now[0],
                    payload=QuoteTickPayload(symbol=symbol, bid_price=D(3), ask_price=D("3.001")))
                before = time.monotonic()
                await service._handle_quote_tick_event(quote)
                handled_at = time.monotonic()
                max_handler = max(max_handler, handled_at-before)
                counter.bucket(handled_at)["quote_events"] += 1
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
            # STALL-CORRELATION diagnostics BEGIN
            correlation.emit_summary(session=session, physical_start=correlation_physical_start,
                                     active_start=counter.window_start)
            # STALL-CORRELATION diagnostics END
        # STALL-CORRELATION diagnostics BEGIN
        correlation.close()
        # STALL-CORRELATION diagnostics END
        gate_calls = sum(value["gate"] for value in counter.episodes.values())
        attempt_calls = sum(value["attempt"] for value in counter.episodes.values())
        active_events = sum(bucket["quote_events"] for bucket in counter.buckets)
        active_seconds = (start + duration - counter.window_start) if counter.window_start is not None else 0
        activity = active_seconds >= 60 and all(bucket["attempt"] > 0 and bucket["quote_events"] >= 2000
                                               for bucket in counter.buckets)
        receipt = {"scope": "CONTROLLED existing MOM native rate; no real broker/network",
            "session": session, "entry_switch": True, "seconds": duration, "events": count,
            "runtime_instance": runtime._exit_gate_instance,
            "physical_start_monotonic": start, "physical_end_monotonic": start + duration,
            "active_start_monotonic": counter.window_start,
            "active_end_monotonic": counter.window_start + 60 if counter.window_start is not None else None,
            "events_per_second": count/duration, "max_loop_stall_ms": max(stalls)*1000,
            "active_seconds": active_seconds, "active_events": active_events,
            "active_events_per_second": active_events/active_seconds if active_seconds > 0 else None,
            "max_handler_ms": max_handler*1000, "work_queue_high_water": max_queue,
            "inbox_high_water": max_inbox, "db_units": db_calls, "broker_units": broker_calls,
            "delay_seconds": .2, "wrong_delay_thread": wrong_thread,
            "time_exit_eligible_seeded": exit_seeded, "new_gate_INFO_calls": gate_calls,
            "new_attempt_INFO_calls": attempt_calls, "existing_root_stream_active": True,
            "prelude_seconds": prelude_seconds, "prelude_log_counts": counter.prelude,
            "activity_status": "MEASURED_ACTIVE_10S_BUCKETS" if activity else "INCONCLUSIVE_ACTIVE_PATH_COVERAGE",
            "activity_time_basis": "FIRST saved-attempt INFO handler entry; six relative10s buckets; actual quote completions",
            "logger_cost_time_basis": "monotonic before/after ONE real INFO; grouped by call start",
            "first_saved_attempt_logger_ns": counter.first_attempt_logger_ns,
            "physical_budget_seconds": 120,
            "active_window_buckets": counter.buckets,
            "first_active_bucket_includes_cold_exit": counter.buckets[0],
            "post_window_log_counts_at_receipt": counter.post_window,
            "wrong_new_log_thread": counter.wrong_log_thread,
            "input_scenario": "typed terminal-rejected exits; actual original 2s pacing, no counter reset",
            "observed_episode_calls": counter.episodes,
            "app_episode_counters": {key: {"gate_call_attempts": value["lines"],
                "suppressed_total": value["suppressed"], "sink_errors": value["sink_errors"]}
                for key, value in runtime._exit_gate_states.items()},
            "owned_exit_wires": sum(row.side == "sell" and row.intent_type == "close"
                                    and bool(row.metadata.get("exit_reason"))
                                    for row in broker.sent)}
        # STALL-CORRELATION diagnostics BEGIN
        receipt["stall_correlation"] = correlation.receipt(
            active_start=counter.window_start,
            active_end=counter.window_start + 60 if counter.window_start is not None else None)
        # STALL-CORRELATION diagnostics END
        print("POMEXITLOG1_NATIVE_RATE="+json.dumps(receipt, sort_keys=True))
        assert activity, "POMEXITLOG1_ACTIVITY_INCONCLUSIVE: need saved-attempt INFO AND200Hz in every10s bucket"
        assert duration >= 60 and active_seconds >= 60 and count/duration >= 200
        assert active_events/active_seconds >= 200
        assert max(stalls) < .05 and max_handler < .05
        assert max_queue <= 2 and max_inbox <= 64 and len(runtime.actor.tasks) == 4
        assert db_calls > 0 and broker_calls > 0 and not wrong_thread
        assert exit_seeded and receipt["owned_exit_wires"] >= 1
        assert runtime.book._active_count == 2
        assert not counter.overflow and gate_calls > 0 and attempt_calls > 0 and not counter.wrong_log_thread
        assert all(value["gate"] <= 20 for value in counter.episodes.values())
        assert all(value["lines"] <= 20 and value["sink_errors"] == 0
                   and counter.episodes.get(key, {}).get("gate", 0) == value["lines"]
                   for key, value in runtime._exit_gate_states.items())
        assert len(runtime._exit_gate_states) <= 2 and len(runtime._exit_gate_frames) <= 2
    finally:
        # STALL-CORRELATION diagnostics BEGIN
        correlation.close()
        correlation.emit_summary(session=session, physical_start=correlation_physical_start,
                                 active_start=counter.window_start)
        # STALL-CORRELATION diagnostics END
        service._momentum_live_actor = None
        service._momentum_live_quote_dispatch = None
        try:
            await runtime.close()
        finally:
            service.logger.removeHandler(counter)
            counter.close()
