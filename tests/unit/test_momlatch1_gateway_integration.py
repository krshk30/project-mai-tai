"""Real gateway recovery, preserving harm and original bounded read budgets."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import threading
import time
import json

import pytest

from project_mai_tai.market_data.momentum_warning_watchdog import WarningWatchdog
from tests.unit.test_momwarn1_gateway import (
    service, healthy, aggregate, event,
)
from tests.unit import test_momwarn1_gateway as fixture_source

pm_clock = fixture_source.pm_clock
health_reader = fixture_source.health_reader
FakeWS = fixture_source.FakeWS


async def arm(svc):
    for _ in range(3):
        await svc._warning_monitor_once()
    assert svc._warning_active


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "monitor_unreadable", "monitor_stale", "monitor_invalid", "monitor_sync_regressed",
    "monitor_sync_gap", "v2_startup_pending", "v2_startup_timeout", "publish_lag", "v2_bar_late",
])
async def test_every_monitor_noise_pauses_existing_socket_then_three_actual_clean_reads(pm_clock, reason):
    svc, provider, _, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await arm(svc)
    original_ws = provider._ws
    await svc._revert_warning(reason)
    assert svc._warning_revert_reason is None
    assert not provider._warning_failed and svc._warning_active
    assert provider._subscriptions == {"WFF"}
    for index in range(3):
        await svc._warning_monitor_once()
        assert svc._warning_active
    await svc._warning_monitor_once()
    assert provider._ws is original_ws
    assert provider._ws.added.count("A.*") == 1
    assert provider._ws.removed == []
    assert svc._warning_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["oms_sync_slow_twice", "v2_cohort_late_twice", "load", "ram", "live_error", "watched_tq_lag"])
async def test_harm_never_recovers_on_later_noise_or_clean_reads(pm_clock, reason):
    svc, provider, _, _ = service()
    await arm(svc)
    await svc._revert_warning(reason)
    for _ in range(5):
        await svc._warning_monitor_once()
    await svc._revert_warning("monitor_unreadable")
    assert svc._warning_revert_reason is None
    assert svc._warning_active
    assert provider._ws.added.count("A.*") == 1
    assert svc.warning_metrics()["pause_state"]["state"] == "active"


@pytest.mark.asyncio
async def test_actual_read_exception_pause_and_pending_read_never_counts_clean(pm_clock):
    svc, provider, _, _ = service()
    await arm(svc)
    def fail():
        raise PermissionError("controlled unreadable")
    svc._warning_health_reader = SimpleNamespace(read=fail)
    await svc._warning_monitor_once()
    svc._warning_health_reader = SimpleNamespace(read=healthy)
    await svc._warning_monitor_once()
    await svc._warning_monitor_once()
    svc._warning_health_reader = SimpleNamespace(read=lambda: replace(
        healthy(), v2_bar_lateness_seconds=None, v2_startup_pending=True,
        v2_startup_deadline_ns=healthy().observed_wall_ns + 1_000_000_000,
    ))
    await svc._warning_monitor_once()
    assert svc._warning_active
    svc._warning_health_reader = SimpleNamespace(read=healthy)
    for index in range(3):
        await svc._warning_monitor_once()
        assert svc._warning_active
    assert provider._ws.added.count("A.*") == 1


def test_sync_rebaseline_never_forgets_known_slow_passes_during_noise():
    watchdog = WarningWatchdog()
    assert watchdog.check_recoverable(healthy(), publish_lag_seconds=0) is None
    first = replace(healthy(), oms_sync_seq=8, oms_sync_seconds=6, v2_bar_lateness_seconds=10)
    assert watchdog.check_recoverable(first, publish_lag_seconds=3) == "v2_bar_late"
    second = replace(first, oms_sync_seq=9)
    assert watchdog.check_recoverable(second, publish_lag_seconds=3) == "oms_sync_slow_twice"
    assert watchdog.check_recoverable(healthy(), publish_lag_seconds=0) == "oms_sync_slow_twice"


@pytest.mark.parametrize("change,reason", [({"load_1m": 7}, "load"), ({"ram_available_bytes": 1}, "ram"), ({"new_live_error": True}, "v2_bar_late")])
def test_simultaneous_harm_wins_over_uncalibrated_late_noise(change, reason):
    health = replace(healthy(), v2_bar_lateness_seconds=10, **change)
    watchdog = WarningWatchdog()
    assert watchdog.check_recoverable(health, publish_lag_seconds=3) == reason
    assert watchdog.check_recoverable(healthy(), publish_lag_seconds=0) == (None if reason == "v2_bar_late" else reason)


@pytest.mark.asyncio
async def test_timed_out_worker_cannot_consume_a_real_error_and_allow_resume(health_reader):
    reader, _, directory = health_reader
    reader.retain_cancelled_harm = True
    reader.read()  # Actual cursor baseline before the injected live error.
    with (directory / "oms.log").open("a") as log:
        log.write("2026 ERROR real protective failure\n")
    consumed, release = threading.Event(), threading.Event()
    def delayed_result():
        result = reader.read()
        assert result.new_live_error
        consumed.set()
        release.wait(2)
        return result
    task = asyncio.create_task(asyncio.to_thread(delayed_result))
    try:
        assert await asyncio.to_thread(consumed.wait, 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, .01)
    finally:
        release.set()
    assert reader.read().new_live_error
    reader.reset_window()
    assert reader.read().new_live_error  # Neither reset nor lost result erases harm.


@pytest.mark.asyncio
async def test_publish_lag_pauses_but_writer_survives_and_publishes_new_generation(pm_clock):
    svc, provider, _, redis = service()
    await arm(svc)
    provider._handle_messages([aggregate()])
    seq, row = svc._warning_queue.get_nowait()
    svc._warning_queue.put_nowait((seq, replace(row, received_monotonic_ns=row.received_monotonic_ns-3_000_000_000)))
    stop = asyncio.Event()
    writer = asyncio.create_task(svc._warning_publish_loop(stop))
    try:
        for _ in range(100):
            if svc.warning_metrics()["watchdog_observed_reason"] == "publish_lag":
                break
            await asyncio.sleep(0)
        assert svc._warning_active and svc._warning_revert_reason is None
        assert not writer.done() and svc._warning_drop_count == 1
        await arm(svc)
        provider._handle_messages([aggregate()])
        for _ in range(100):
            if redis.entries:
                break
            await asyncio.sleep(0)
        assert len(redis.entries) == 1
        assert redis.entries[0][1]["sequence"] == 2
        assert redis.entries[0][1]["generation"] == 1
    finally:
        stop.set()
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


@pytest.mark.asyncio
async def test_timed_out_read_reuses_one_worker_until_actual_completion(pm_clock):
    svc, _, _, _ = service(market_data_momentum_warning_monitor_timeout_seconds=.01)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def read_live(*_):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return healthy()
    svc._warning_health_reader = SimpleNamespace(read_live=read_live)
    await svc._warning_monitor_once()
    await svc._warning_monitor_once()
    assert calls == 1 and entered.is_set()
    assert svc.warning_metrics()["watchdog_observed_reason"] == "monitor_unreadable" and svc._warning_active
    release.set()
    for index in range(3):
        await svc._warning_monitor_once()
        assert svc._warning_active
    assert calls == 3  # Completed result + two genuinely new reads, no overlap.


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disconnect", "policy_1008", "reconnect_1012"])
async def test_replacement_client_restores_A_and_TQ_but_only_three_clean_reads_resume(pm_clock, reason):
    svc, provider, _, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    await arm(svc)
    provider._connected = False
    provider._revert_warning(reason)
    assert svc._warning_active and not provider._warning_failed
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._pauses.clean_streak == 0
    replacement = FakeWS()
    async def connect(_processor):
        assert set(replacement.added) == {"A.*", "T.WFF", "Q.WFF", "T.NEW"}
        for index in range(3):
            await svc._warning_monitor_once()
            assert svc._warning_active
        assert replacement.added.count("A.*") == 1
        assert svc._warning_generation == 1
        provider._running = False
    replacement.connect = connect
    provider._build_client = lambda: replacement
    provider._running = True
    await provider._run_loop()  # Offline fake transport, no provider connection.
    assert svc._warning_revert_reason is None


@pytest.mark.asyncio
async def test_four_distinct_reconnects_in_ten_minutes_sticky_even_while_A_paused(pm_clock):
    svc, provider, _, _ = service()
    await arm(svc)
    for index in range(4):
        provider._revert_warning("disconnect")
        assert svc._warning_revert_reason is None
    assert not provider._warning_failed and provider._warning_reconnect_pending
    for _ in range(5):
        await svc._warning_monitor_once()
    assert svc._warning_active


@pytest.mark.asyncio
async def test_four_actual_disconnects_during_health_pause_still_escalate_without_A_restore(pm_clock):
    svc, provider, _, _ = service()
    await arm(svc)
    await svc._revert_warning("monitor_unreadable")
    assert not provider._warning_reconnect_pending
    for index in range(4):
        provider._revert_warning("disconnect")
        assert svc._warning_revert_reason is None
        assert provider._warning_reconnect_pending
    assert svc.warning_metrics()["pause_state"]["reconnect_event_count"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [ValueError("invalid JSON"), RuntimeError("authentication failed")])
async def test_unknown_connection_body_failure_stays_sticky_while_health_paused(pm_clock, fault):
    svc, provider, _, _ = service()
    await arm(svc)
    await svc._revert_warning("monitor_unreadable")
    replacement = FakeWS()
    async def failed(_processor):
        provider._running = False
        raise fault
    replacement.connect = failed
    provider._build_client = lambda: replacement
    provider._running = True
    await provider._run_loop()
    assert svc._warning_revert_reason is None
    assert svc.warning_metrics()["watchdog_observed_reason"] == "provider_error"
    assert not provider._warning_failed and provider._warning_reconnect_pending


@pytest.mark.asyncio
@pytest.mark.timing
async def test_gateway_A_240_per_second_60s_original_budget_and_TQ_progress(pm_clock, record_property):
    """Offline real gateway/provider publisher path; not a live provider load proof."""
    svc, provider, redis, warning_redis = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await arm(svc)
    provider._on_trade = svc._enqueue_trade
    provider._on_quote = lambda row: svc._enqueue_live(svc._quote_queue, row)
    stop = asyncio.Event()
    writer = asyncio.create_task(svc._warning_publish_loop(stop))
    live = asyncio.create_task(svc._stream_publish_loop(stop))
    monitor = asyncio.create_task(svc._warning_monitor_loop(stop))
    maximum = 0
    async def pulse():
        nonlocal maximum
        while not stop.is_set():
            started = time.monotonic()
            await asyncio.sleep(.001)
            maximum = max(maximum, time.monotonic()-started-.001)
    ticker = asyncio.create_task(pulse())
    template = provider._normalize_warning_aggregate(aggregate(), "WFF")
    started = time.monotonic()
    count = 0
    max_handler = 0
    try:
        while time.monotonic()-started < 60:
            target = started + count/240
            await asyncio.sleep(max(0, target-time.monotonic()))
            mark = time.monotonic()
            svc._enqueue_warning(replace(template, received_monotonic_ns=time.monotonic_ns(), received_utc_ns=time.time_ns()))
            # Watched-name paths continue on their unchanged queues.
            from project_mai_tai.market_data.models import TradeTickRecord, QuoteTickRecord
            svc._enqueue_trade(TradeTickRecord("WFF", 13.35, 100))
            svc._enqueue_live(svc._quote_queue, QuoteTickRecord("WFF", 13.34, 13.35))
            max_handler = max(max_handler, time.monotonic()-mark)
            count += 1
        elapsed = time.monotonic()-started
        deadline = time.monotonic()+2
        while (svc._warning_published < count or svc._trade_queue.qsize() or svc._quote_queue.qsize()) and time.monotonic() < deadline:
            await asyncio.sleep(.001)
        result = dict(scope="OFFLINE_ACTUAL_GATEWAY_NOT_LIVE_SOCKET", seconds=elapsed, A=count,
                      A_per_second=count/elapsed, loop_stall_ms=maximum*1000, handler_ms=max_handler*1000,
                      A_published=svc._warning_published, dropped=svc._warning_drop_count,
                      T=sum(row[1].get("event_type") == "trade_tick" for row in redis.entries),
                      Q=sum(row[1].get("event_type") == "quote_tick" for row in redis.entries))
        record_property("MOMLATCH1_A_60S", json.dumps(result))
        print("MOMLATCH1_A_60S="+json.dumps(result))
        assert elapsed >= 60 and count/elapsed >= 200
        assert maximum < .05 and max_handler < .05
        assert result["T"] == result["Q"] == count
        assert len(warning_redis.entries) == svc._warning_published == count
        assert not svc._warning_revert_reason and svc._warning_drop_count == 0
        assert provider._ws.added.count("A.*") == 1
    finally:
        stop.set()
        for task in (writer, live, monitor, ticker):
            task.cancel()
        await asyncio.gather(writer, live, monitor, ticker, return_exceptions=True)
