"""Controlled DB outages: no external Redis, PostgreSQL, or broker calls."""
import asyncio
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy.exc import OperationalError

from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms import service as oms
from tests.unit.test_mirrorhold1_retained_hold import event_for, quote, state
from tests.unit.test_mirrorhold1_retained_hold import lane as retained_lane
from tests.unit.test_oms_spof_resilience import _armed_stop, _bare_service, _loop_settings


@pytest.fixture
def lane(monkeypatch):
    return retained_lane.__wrapped__(monkeypatch)


def outage():
    return OperationalError("controlled SELECT", {}, RuntimeError("controlled DB restart"))


def control(service, monkeypatch, *, until=5.0):
    """Advance only the control-loop cadence clock; real asyncio/thread work is unchanged."""
    clock = [0.0]
    monkeypatch.setattr(oms, "asyncio", SimpleNamespace(
        **{**vars(asyncio), "get_running_loop": lambda: SimpleNamespace(time=lambda: clock[0])}))
    stop = asyncio.Event()
    service.logger = Mock()
    service.settings.service_heartbeat_interval_seconds = 1
    service.settings.oms_broker_sync_interval_seconds = 1
    service._intent_offsets = {"controlled-intents": "$"}
    service._broker_sync_interval_seconds = AsyncMock(return_value=1.0)
    service._publish_heartbeat = AsyncMock()
    for name in (
        "sync_broker_state", "_check_webull_uncovered_shares", "_window_flatten_armed_stops",
        "_orb_schwab_eod_close", "_v2_eod_oco_transition", "_retry_webull_eh_ladder_pending",
        "_v2_eod_cancel_and_reexit", "_v2_rth_edge_bracket", "_v2_overnight_flatten",
    ):
        setattr(service, name, AsyncMock())

    async def read(*args, **kwargs):
        clock[0] += 0.25
        await asyncio.sleep(0)
        if clock[0] >= until:
            stop.set()
        return []

    service.redis.xread = read
    return clock, stop


@pytest.mark.asyncio
async def test_active_retained_hold_recovers_after_worker_select_outage(lane, monkeypatch):
    service, client, factory, _ = lane
    event = event_for(lane)
    quote(lane, event, "4.7")
    await service.process_trade_intent(event)
    before = state(lane, event)
    before_messages = list(service.redis.entries)
    assert before["phase"] == "held" and client.calls.get("place", 0) == 0
    clock, stop = control(service, monkeypatch)
    loop_thread = get_ident()
    failures, sql_threads, attempt_times = [], [], []
    engine = factory.kw["bind"]
    original = service._mirrorhold_prepare_queue

    def prepare(symbol, keys):
        attempt_times.append(clock[0])
        return original(symbol, keys)

    def restart_error(conn, cursor, statement, params, context, executemany):
        sql_threads.append(get_ident())
        if len(failures) < 2:
            failures.append(statement)
            raise OperationalError(statement, params, RuntimeError("controlled restart"))

    monkeypatch.setattr(service, "_mirrorhold_prepare_queue", prepare)
    sql_event.listen(engine, "before_cursor_execute", restart_error)
    try:
        await asyncio.wait_for(service._run_control_loop(stop), 3)
    finally:
        sql_event.remove(engine, "before_cursor_execute", restart_error)
    assert len(failures) == 2 and all("dashboard_snapshots" in sql for sql in failures)
    assert attempt_times[:3] == [0.0, 1.0, 3.0]
    assert sql_threads and all(thread != loop_thread for thread in sql_threads)
    assert state(lane, event) == before
    assert client.calls.get("place", 0) == 0 and service.redis.entries == before_messages
    assert not service.__dict__["_mirrorhold_evaluating"]
    assert service._check_webull_uncovered_shares.await_count >= 4
    details = [call.args[1] for call in service._publish_heartbeat.await_args_list]
    assert any(d.get("periodic_db_recovering") == "nfq_holds" for d in details)
    assert "periodic_db_recovering" not in details[-1]
    assert any("phase=recovered" in call.args[0] for call in service.logger.info.call_args_list)


@pytest.mark.asyncio
async def test_actual_quote_and_webull_protective_close_continue_during_periodic_sql_failure(
        lane, monkeypatch):
    service, client, factory, _ = lane
    held = event_for(lane)
    quote(lane, held, "4.7")
    await service.process_trade_intent(held)
    _, stop_loop = control(service, monkeypatch)
    stop = _armed_stop()
    stop.broker_account_name = "live:orb"  # Webull; no Schwab OCO/RESERVE proof bypass.
    key = service._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
    service._armed_hard_stops[key] = stop
    monkeypatch.setattr(oms, "_is_regular_market_session", lambda: True)
    service._has_active_native_stop_guard_order = AsyncMock(side_effect=outage())
    service.process_trade_intent = AsyncMock(return_value=[
        SimpleNamespace(payload=SimpleNamespace(status="accepted", reason="HARD_STOP"))])
    engine = factory.kw["bind"]
    started, release = Event(), Event()
    sql_threads = []
    loop_thread = get_ident()

    def fail_select(conn, cursor, statement, params, context, executemany):
        sql_threads.append(get_ident())
        if "dashboard_snapshots" in statement:
            started.set()
            assert release.wait(2), "controlled SQL worker was not released"
            raise OperationalError(statement, params, RuntimeError("controlled restart"))

    sql_event.listen(engine, "before_cursor_execute", fail_select)
    task = asyncio.create_task(service._run_control_loop(stop_loop))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        before_sql = list(sql_threads)
        tick = QuoteTickEvent(source_service="controlled-market-data", produced_at=lane[3][0],
            payload=QuoteTickPayload(symbol=stop.symbol, bid_price="1.18", ask_price="1.19"))
        await asyncio.wait_for(service._handle_quote_tick_event(tick), 1)
        service.process_trade_intent.assert_awaited_once()
        close = service.process_trade_intent.await_args.args[0].payload
        assert (close.symbol, close.side, close.intent_type, close.reason, close.quantity) == (
            "KIDZ", "sell", "close", "HARD_STOP", stop.quantity)
        assert service._latest_quotes_by_symbol["KIDZ"]["bid"] == 1.18
        assert sql_threads == before_sql  # Quote + protective-close decision added zero SQL.
        assert all(thread != loop_thread for thread in sql_threads)
        assert client.calls.get("place", 0) == 0  # Close response is CONTROLLED, not a live ACK.
    finally:
        stop_loop.set()
        release.set()
        await task
        sql_event.remove(engine, "before_cursor_execute", fail_select)
    assert service.logger.exception.call_args.args[0].startswith("[OMS-PERIODIC-DB]")


@pytest.mark.asyncio
async def test_cancellation_during_worker_select_propagates(lane, monkeypatch):
    service, _, _, _ = lane
    event = event_for(lane)
    quote(lane, event, "4.7")
    await service.process_trade_intent(event)
    _, stop = control(service, monkeypatch)
    started, release, finished = Event(), Event(), Event()

    def blocked_prepare(symbol, keys):
        started.set()
        try:
            assert release.wait(2)
            raise outage()
        finally:
            finished.set()

    monkeypatch.setattr(service, "_mirrorhold_prepare_queue", blocked_prepare)
    task = asyncio.create_task(service._run_control_loop(stop))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        service.logger.exception.assert_not_called()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert state(lane, event)["phase"] == "held"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_task", ["_evaluate_nfq_holds", "_evaluate_nfq2_holds"])
async def test_backoff_is_bounded_independent_and_resets_after_recovery(monkeypatch, failed_task):
    service = _bare_service()
    service.settings = _loop_settings()
    service.redis = SimpleNamespace()
    clock, stop = control(service, monkeypatch, until=124.0)
    attempts = []

    async def check():
        attempts.append(clock[0])
        if len(attempts) <= 7 or len(attempts) == 9:
            raise outage()

    other = "_evaluate_nfq2_holds" if failed_task == "_evaluate_nfq_holds" else "_evaluate_nfq_holds"
    setattr(service, failed_task, check)
    setattr(service, other, AsyncMock())
    await service._run_control_loop(stop)
    assert attempts[:8] == [0.0, 1.0, 3.0, 7.0, 15.0, 31.0, 61.0, 91.0]
    assert attempts[8:10] == [91.25, 92.25]  # recovered streak starts again at 1s
    assert getattr(service, other).await_count == 496
    assert service._v2_overnight_flatten.await_count == 124
    assert service._publish_heartbeat.await_count == 124


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("bug"), RuntimeError("not a DB outage"),
                                 asyncio.CancelledError()])
@pytest.mark.parametrize("failed_task", ["_evaluate_nfq_holds", "_evaluate_nfq2_holds"])
async def test_periodic_non_db_errors_and_cancellation_propagate(monkeypatch, error, failed_task):
    service = _bare_service()
    service.settings = _loop_settings()
    service.redis = SimpleNamespace()
    _, stop = control(service, monkeypatch)
    service._evaluate_nfq_holds = AsyncMock()
    service._evaluate_nfq2_holds = AsyncMock()
    setattr(service, failed_task, AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await service._run_control_loop(stop)
    assert getattr(service, failed_task).await_count == 1
    service.logger.exception.assert_not_called()


@pytest.mark.asyncio
async def test_stop_during_backoff_does_not_wait_for_retry(monkeypatch):
    service = _bare_service()
    service.settings = _loop_settings()
    service.redis = SimpleNamespace()
    clock, stop = control(service, monkeypatch, until=0.25)
    service._evaluate_nfq_holds = AsyncMock(side_effect=outage())
    service._evaluate_nfq2_holds = AsyncMock()
    await service._run_control_loop(stop)
    assert clock[0] == 0.25 and service._evaluate_nfq_holds.await_count == 1


@pytest.mark.asyncio
async def test_failed_intent_is_not_replayed_by_periodic_recovery(monkeypatch):
    service = _bare_service()
    service.settings = _loop_settings()
    service.redis = SimpleNamespace()
    clock, stop = control(service, monkeypatch)
    calls = []

    async def sweep():
        calls.append(clock[0])
        if len(calls) == 1:
            raise outage()

    service._evaluate_nfq_holds = AsyncMock(side_effect=sweep)
    service._evaluate_nfq2_holds = AsyncMock()
    service._handle_stream_message = AsyncMock(side_effect=outage())
    original = service.redis.xread

    async def read(*args, **kwargs):
        result = await original(*args, **kwargs)
        if clock[0] == 0.25:
            return [("controlled-intents", [("1-0", {"data": "controlled ambiguous BUY"})])]
        return result

    service.redis.xread = read
    await service._run_control_loop(stop)
    service._handle_stream_message.assert_awaited_once()
    assert service._intent_offsets == {"controlled-intents": "1-0"}
    assert service._evaluate_nfq_holds.await_count > 1
