"""Offline combined quote-handler proof; no broker or Redis network calls."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import timedelta
from decimal import Decimal
import json
from threading import Event, get_ident
from time import monotonic, sleep

import pytest
from sqlalchemy import event as sql_event, select

from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, TradeIntent
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from tests.unit.test_nfq2_eh_fresh_price import NOW, biya, hold, retry
from tests.unit.test_nfq2_eh_fresh_price import service as recorded_service

service = recorded_service
WORK = ContextVar("combined_proof_work", default="setup")


def tick(symbol="BIYA", *, stale=False, ask="2.54"):
    return QuoteTickEvent(
        source_service="offline-combined-proof",
        produced_at=NOW - timedelta(seconds=11) if stale else NOW,
        payload=QuoteTickPayload(symbol=symbol, bid_price="2.53", ask_price=ask),
    )


def seed_drift(service, symbol="BIYA"):
    with service.session_factory() as session:
        strategy = service.store.ensure_strategy(
            session, "schwab_1m_v2", name="v2", execution_mode="paper")
        account = service.store.ensure_broker_account(
            session, "paper:drift-proof", provider="simulated", environment="paper")
        intent = TradeIntent(
            strategy_id=strategy.id, broker_account_id=account.id, symbol=symbol,
            side="buy", intent_type="open", quantity=Decimal("10"),
            reason="offline drift cache control", status="submitted", payload={}, created_at=NOW)
        session.add(intent)
        session.flush()
        order = BrokerOrder(
            intent_id=intent.id, strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id=f"combined-drift-control-{symbol}",
            broker_order_id=f"simulated-drift-control-{symbol}",
            symbol=symbol, side="buy", order_type="limit", time_in_force="day",
            quantity=Decimal("10"), status="accepted",
            payload={"order_type": "limit", "limit_price": "2.55"})
        session.add(order)
        session.commit()
        return order.id


class Meter:
    def __init__(self, service, monkeypatch):
        self.loop_thread = get_ident()
        self.sessions = []
        self.transactions = []
        self.statements = []
        self.factory = service.session_factory
        self.engine = self.factory.kw["bind"]

        def factory():
            self.sessions.append((monotonic(), get_ident(), WORK.get()))
            return self.factory()

        monkeypatch.setattr(service, "session_factory", factory)
        self.begin = lambda conn: self.transactions.append(
            (monotonic(), get_ident(), WORK.get()))
        self.sql = lambda *args: self.statements.append(
            (monotonic(), get_ident(), WORK.get()))
        sql_event.listen(self.engine, "begin", self.begin)
        sql_event.listen(self.engine, "before_cursor_execute", self.sql)

    def close(self):
        sql_event.remove(self.engine, "begin", self.begin)
        sql_event.remove(self.engine, "before_cursor_execute", self.sql)

    def counts(self):
        return {"sessions": dict(Counter(row[2] for row in self.sessions)),
                "transactions": dict(Counter(row[2] for row in self.transactions)),
                "statements": dict(Counter(row[2] for row in self.statements)),
                "on_loop_sql": sum(row[1] == self.loop_thread for row in self.statements)}


async def tagged(label, function, *args):
    token = WORK.set(label)
    try:
        return await function(*args)
    finally:
        WORK.reset(token)


async def configured(service):
    service.settings.oms_quote_drift_cancel_tolerance_cents = 1.0
    service.settings.oms_v2_webull_mirror_retained_hold_enabled = True
    service.settings.oms_broker_sync_interval_seconds = 5
    seed_drift(service)
    await service._refresh_drift_working_cache()
    assert service._drift_working_by_symbol["BIYA"][0].limit_price == "2.55"
    assert service._quote_drift_tolerance_dollars() > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["irrelevant-active", "stale-active", "no-hold"])
async def test_combined_memory_admission_zero_sql(service, monkeypatch, case):
    await configured(service)
    active = hold(service, biya()) if case != "no-hold" else None
    reading = service._nfq2_reading
    reads = []

    def counted(symbol):
        reads.append(symbol)
        return reading(symbol)

    def forbidden(*args, **kwargs):
        pytest.fail("ineligible full-handler tick copied or queried hold ownership")

    monkeypatch.setattr(service, "_nfq2_reading", counted)
    monkeypatch.setattr(service, "_nfq2_copy", forbidden)
    monkeypatch.setattr(service, "_nfq2_reason", forbidden)
    meter = Meter(service, monkeypatch)
    try:
        event = tick("OTHER") if case == "irrelevant-active" else tick(stale=active is not None)
        for _ in range(1000):
            await tagged("tick", service._handle_quote_tick_event, event)
        assert not meter.sessions, meter.counts()
        assert not meter.transactions and not meter.statements, meter.counts()
        assert len(reads) == (1000 if case == "stale-active" else 0)
        assert not service.redis.entries
        if active:
            assert service._nfq2_holds[service._nfq2_key(active.event)] is active
            assert active.phase == "held"
        print("COMBINED-MEMORY", json.dumps({"case": case, "events": 1000, **meter.counts()}))
    finally:
        meter.close()


@pytest.mark.asyncio
async def test_combined_slow_eligible_persistence_offloop_duplicate_quotes(service, monkeypatch):
    await configured(service)
    active = hold(service, biya())
    original = service._nfq2_transition
    started = Event()
    transitions = []

    def slow(*args, **kwargs):
        transitions.append(get_ident())
        started.set()
        sleep(0.15)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_nfq2_transition", slow)
    meter = Meter(service, monkeypatch)
    event = tick()
    first = asyncio.create_task(tagged("eligible", service._handle_quote_tick_event, event))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        begin = monotonic()
        await asyncio.gather(*(tagged("duplicate", service._handle_quote_tick_event, event)
                               for _ in range(1000)))
        duplicate_seconds = monotonic() - begin
        await asyncio.wait_for(first, 2)
        assert duplicate_seconds < 0.05
        assert transitions and all(thread != meter.loop_thread for thread in transitions)
        assert len(transitions) == 1 and len(service.redis.entries) == 1
        assert active.phase == "queued"
        assert meter.transactions and meter.statements
        assert all(row[1] != meter.loop_thread for row in meter.sessions + meter.statements)
        assert Counter(row[2] for row in meter.transactions) == {"eligible": 1}
        print("COMBINED-SLOW", json.dumps({"duplicate_events": 1000,
              "duplicate_seconds": duplicate_seconds, "retry_messages": 1, **meter.counts()}))
    finally:
        await first
        meter.close()


@pytest.mark.asyncio
async def test_combined_drift_dispatch_enabled_offloop_and_isolated(service, monkeypatch):
    from tests.unit.test_oms_direct_cancel_dead_target_bound import _DirectCancelAdapter

    await configured(service)
    active = hold(service, biya())
    seed_drift(service, "DRIFT")
    await service._refresh_drift_working_cache()
    adapter = _DirectCancelAdapter(cancel_event_type="cancelled", cancel_reason="offline control")
    monkeypatch.setattr(service, "broker_adapter", adapter)
    original = service._collect_drift_cancel_candidates
    started, release = Event(), Event()

    def slow(*args):
        started.set()
        assert release.wait(2)
        return original(*args)

    monkeypatch.setattr(service, "_collect_drift_cancel_candidates", slow)
    meter = Meter(service, monkeypatch)
    try:
        await tagged("drift-eligible", service._handle_quote_tick_event, tick("DRIFT", ask="2.70"))
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(1000):
            await tagged("irrelevant", service._handle_quote_tick_event, tick("OTHER"))
            await tagged("drift-duplicate", service._handle_quote_tick_event, tick("DRIFT", ask="2.70"))
        release.set()
        tasks = tuple(service.__dict__.get("_symbol_tick_work", {}).values())
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert len(adapter.requests) == 1
        assert adapter.requests[0].intent_type == "cancel"
        assert adapter.requests[0].symbol == "DRIFT"
        assert active.phase == "held" and len(service._nfq2_holds) == 1
        assert not service.redis.entries
        assert meter.statements and all(row[1] != meter.loop_thread for row in meter.statements)
        assert set(row[2] for row in meter.transactions) == {"drift-eligible"}
        print("COMBINED-DRIFT", json.dumps({"cancel_requests": 1, "buys": 0,
              "irrelevant_events": 1000, "duplicate_events": 1000, **meter.counts()}))
    finally:
        release.set()
        await service._shutdown_symbol_tick_work()
        meter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("webull", [False, True])
@pytest.mark.parametrize("reactive", [False, True])
async def test_combined_full_handler_serial_delivery_once(service, monkeypatch, webull, reactive):
    service.settings.oms_quote_drift_cancel_tolerance_cents = 1.0
    service.settings.oms_v2_webull_mirror_retained_hold_enabled = True
    monkeypatch.setattr(service, "_market_is_fillable", lambda *args: True)
    # Same disclosed simulated-account collision boundary as the prior NFQ2 serial replay.
    monkeypatch.setattr(service, "_fanout_webull_collision_reason", lambda **kwargs: None)
    event = biya(webull=webull, reactive=reactive)
    assert await service.process_trade_intent(event) == []
    assert len(service._nfq2_holds) == 1
    meter = Meter(service, monkeypatch)
    try:
        await tagged("eligible", service._handle_quote_tick_event, tick(ask="2.56"))
        for _ in range(100):
            await tagged("duplicate", service._handle_quote_tick_event, tick(ask="2.56"))
        assert len(service.redis.entries) == 1
        assert meter.transactions and all(row[1] != meter.loop_thread for row in meter.statements)
        assert Counter(row[2] for row in meter.transactions) == {"eligible": 1}
    finally:
        meter.close()
    message = retry(service)
    await service._handle_stream_message({"data": message.model_dump_json()})
    await service._handle_stream_message({"data": message.model_dump_json()})
    with service.session_factory() as session:
        orders = session.scalars(select(BrokerOrder)).all()
        assert len(orders) == 1 and orders[0].status == "filled"
        assert Decimal(orders[0].payload["limit_price"]) <= Decimal("2.5654")
    print("COMBINED-SERIAL", json.dumps({"webull": webull, "reactive": reactive,
          "quote_events": 101, "serial_deliveries": 2, "simulated_orders": len(orders)}))


@pytest.mark.asyncio
async def test_combined_240_events_per_second_60_seconds_active_hold(service, monkeypatch):
    await configured(service)
    active = hold(service, biya())
    meter = Meter(service, monkeypatch)
    loop = asyncio.get_running_loop()
    cadence = service.settings.oms_broker_sync_interval_seconds
    drift_calls = 0
    drift_price_checks = 0
    drift = service._cancel_drifted_working_orders
    price_check = service._cached_quote_drift
    handler = service._handle_quote_tick_event
    stale, irrelevant = tick(stale=True), tick("OTHER")
    stalls, handler_times, periodic_times = [], [], []
    counts = Counter()
    phase_violations = 0
    periodic_end = asyncio.Event()

    async def counted_drift(symbol):
        nonlocal drift_calls
        drift_calls += 1
        await drift(symbol)

    monkeypatch.setattr(service, "_cancel_drifted_working_orders", counted_drift)

    def counted_price_check(candidate, quote):
        nonlocal drift_price_checks
        drift_price_checks += 1
        return price_check(candidate, quote)

    monkeypatch.setattr(service, "_cached_quote_drift", counted_price_check)
    start = loop.time()
    end = start + 60

    async def watchdog():
        due = loop.time() + 0.005
        while loop.time() < end:
            await asyncio.sleep(max(0, due - loop.time()))
            stalls.append(max(0, loop.time() - due))
            due = loop.time() + 0.005

    async def periodic():
        next_drift = start
        while not periodic_end.is_set():
            await tagged("periodic-nfq2", service._evaluate_nfq2_holds)
            if loop.time() >= next_drift:
                periodic_times.append(loop.time() - start)
                await tagged("periodic-drift-cache", service._refresh_drift_working_cache)
                next_drift = loop.time() + cadence
            try:
                await asyncio.wait_for(periodic_end.wait(), 0.25)
            except TimeoutError:
                pass

    watcher, sweeper = asyncio.create_task(watchdog()), asyncio.create_task(periodic())
    error = None
    try:
        while loop.time() < end:
            event = stale if sum(counts.values()) % 2 else irrelevant
            label = "tick-stale" if event is stale else "tick-irrelevant"
            before = loop.time()
            await tagged(label, handler, event)
            handler_times.append(loop.time() - before)
            counts[label] += 1
            phase_violations += int(len(service._nfq2_holds) != 1 or active.phase != "held")
            due = start + sum(counts.values()) / 240
            await asyncio.sleep(max(0, due - loop.time()))
    except BaseException as exc:
        error = exc
    finally:
        elapsed = loop.time() - start
        periodic_end.set()
        await asyncio.gather(watcher, sweeper)
        receipt = {"seconds": elapsed, "events": sum(counts.values()),
            "events_by_lane": dict(counts), "rate": sum(counts.values()) / elapsed,
            "max_loop_stall_ms": max(stalls, default=0) * 1000,
            "max_handler_ms": max(handler_times, default=0) * 1000,
            "watchdog_samples": len(stalls), "drift_reader_calls": drift_calls,
            "drift_price_checks": drift_price_checks,
            "drift_tolerance_cents": service.settings.oms_quote_drift_cancel_tolerance_cents,
            "drift_cache_candidates": len(service._drift_working_by_symbol.get("BIYA", ())),
            "periodic_drift_times_seconds": periodic_times, "periodic_cadence_seconds": cadence,
            "holds": len(service._nfq2_holds), "hold_phase": active.phase,
            "phase_violations": phase_violations, "retry_messages": len(service.redis.entries),
            "transaction_times": [[row[0] - start, row[2], row[1] == meter.loop_thread]
                                  for row in meter.transactions],
            "tick_transactions_per_second": [sum(int(row[0] - start) == second
                and row[2].startswith("tick") for row in meter.transactions) for second in range(60)],
            **meter.counts()}
        print("COMBINED-60S-RECEIPT", json.dumps(receipt, sort_keys=True))
        meter.close()
    if error:
        raise error
    assert elapsed >= 60 and receipt["rate"] >= 200
    assert receipt["max_loop_stall_ms"] < 50
    assert drift_calls == receipt["events"] and receipt["drift_cache_candidates"] == 1
    assert drift_price_checks == counts["tick-stale"]
    assert receipt["drift_tolerance_cents"] == 1.0
    assert not phase_violations and active.phase == "held" and len(service._nfq2_holds) == 1
    assert not service.redis.entries
    assert meter.transactions and meter.statements
    assert all(row[1] != meter.loop_thread for row in meter.sessions + meter.statements)
    assert set(receipt["transactions"]) == {"periodic-nfq2", "periodic-drift-cache"}
    for lane in receipt["transactions"]:
        times = [row[0] for row in meter.transactions if row[2] == lane]
        assert len(times) <= 1 + int(elapsed / cadence)
        assert all(right - left >= cadence * 0.95 for left, right in zip(times, times[1:]))
    with meter.factory() as session:
        row = session.get(DashboardSnapshot, active.row_id)
        assert row.payload["phase"] == "held"
        order = session.scalar(select(BrokerOrder))
        assert order.status == "accepted"
