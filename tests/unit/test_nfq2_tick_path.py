"""NFQ2 tick-loop controls using the recorded BIYA hold (no broker traffic)."""
import asyncio
from datetime import timedelta
from threading import Event, get_ident
from time import sleep

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms.eh_fresh_price import SNAPSHOT_TYPE
from tests.unit.test_nfq2_eh_fresh_price import NOW, biya, hold, quote, retry
from tests.unit.test_nfq2_eh_fresh_price import service as _recorded_service

service = _recorded_service


@pytest.mark.asyncio
@pytest.mark.parametrize("tick", ["OTHER", "BIYA"])
async def test_nfq2_unrelated_or_stale_tick_never_opens_session_or_copies_hold_map(service, monkeypatch, tick):
    active = hold(service, biya())
    quote(service, age=10001)

    def forbidden(*args, **kwargs):
        pytest.fail("tick performed DB/proof/copy work before eligibility")

    monkeypatch.setattr(service, "session_factory", forbidden)
    monkeypatch.setattr(service, "_nfq2_reason", forbidden)
    monkeypatch.setattr(service, "_nfq2_copy", forbidden)
    if tick == "OTHER":
        monkeypatch.setattr(service, "_nfq2_reading", forbidden)
    for _ in range(1000):
        await service._evaluate_nfq2_holds(tick)
    assert service._nfq2_holds[service._nfq2_key(active.event)] is active
    assert not service.redis.entries


@pytest.mark.asyncio
async def test_nfq2_quote_transition_persists_on_worker_without_tick_ownership_queries(service, monkeypatch):
    hold(service, biya())
    quote(service)
    loop_thread = get_ident()
    threads = []
    factory = service.session_factory

    def counted_factory():
        threads.append(get_ident())
        return factory()

    def forbidden_reason(*args):
        pytest.fail("ownership queries ran from the tick path")

    monkeypatch.setattr(service, "session_factory", counted_factory)
    monkeypatch.setattr(service, "_nfq2_reason", forbidden_reason)
    await service._evaluate_nfq2_holds("BIYA")
    assert threads and all(thread != loop_thread for thread in threads)
    assert len(service.redis.entries) == 1
    for _ in range(1000):
        await service._evaluate_nfq2_holds("BIYA")
    assert len(threads) == 1 and len(service.redis.entries) == 1


@pytest.mark.asyncio
async def test_nfq2_periodic_proof_runs_off_loop_and_is_not_tick_rate_driven(service, monkeypatch):
    hold(service, biya())
    loop_thread = get_ident()
    threads = []
    reason = service._nfq2_reason

    def measured(session, active):
        threads.append(get_ident())
        return reason(session, active)

    monkeypatch.setattr(service, "_nfq2_reason", measured)
    await service._evaluate_nfq2_holds()
    for _ in range(1000):
        await service._evaluate_nfq2_holds("OTHER")
        await service._evaluate_nfq2_holds()
    assert threads == [threads[0]] and threads[0] != loop_thread
    assert not service.redis.entries


@pytest.mark.asyncio
async def test_nfq2_cancel_during_worker_save_never_resurrects_durable_hold_or_queues_buy(service, monkeypatch):
    event = biya()
    active = hold(service, event)
    quote(service)
    started, release = Event(), Event()
    run_db = service._run_db

    async def paused(fn, **kwargs):
        def worker(session):
            started.set()
            assert release.wait(2), "test worker was not released"
            return fn(session)
        return await run_db(worker, **kwargs)

    monkeypatch.setattr(service, "_run_db", paused)
    task = asyncio.create_task(service._evaluate_nfq2_holds("BIYA"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        cancel = event.model_copy(deep=True)
        cancel.payload.intent_type = "cancel"
        service._nfq2_observe(cancel)
    finally:
        release.set()
    await asyncio.wait_for(task, 2)
    assert not service._nfq2_holds and not service.redis.entries
    with service.session_factory() as session:
        row = session.get(DashboardSnapshot, active.row_id)
        assert row.payload["phase"] == "retired"


@pytest.mark.asyncio
async def test_nfq2_slow_save_does_not_stall_loop_and_concurrent_quotes_queue_once(service, monkeypatch):
    hold(service, biya())
    quote(service)
    original = service._nfq2_transition
    started = Event()
    transitions = []

    def slow(session, *args, **kwargs):
        transitions.append(get_ident())
        started.set()
        sleep(0.15)
        return original(session, *args, **kwargs)

    monkeypatch.setattr(service, "_nfq2_transition", slow)
    pending = asyncio.create_task(service._evaluate_nfq2_holds("BIYA"))
    assert await asyncio.to_thread(started.wait, 2)
    start = asyncio.get_running_loop().time()
    await asyncio.gather(*(service._evaluate_nfq2_holds("BIYA") for _ in range(1000)))
    assert asyncio.get_running_loop().time() - start < 0.05
    await asyncio.wait_for(pending, 2)
    assert len(transitions) == 1
    assert len(service.redis.entries) == 1
    assert service._claim_nfq2_retry(retry(service))


def test_nfq2_worker_cas_refuses_retired_generation_without_feedback(service):
    event = biya()
    active = hold(service, event)
    before = service._nfq2_copy(active)
    after = service._nfq2_copy(active, phase="queued", token="stale-save")
    cancel = event.model_copy(deep=True)
    cancel.payload.intent_type = "cancel"
    service._nfq2_observe(cancel)
    with service.session_factory() as session:
        assert not service._nfq2_transition(session, before, after)
        session.commit()
        row = session.get(DashboardSnapshot, active.row_id)
        assert row.payload["phase"] == "retired"
    assert not service.redis.entries


@pytest.mark.asyncio
async def test_nfq2_worker_failure_keeps_hold_and_next_quote_can_queue_once(service, monkeypatch):
    active = hold(service, biya())
    quote(service)
    run_db = service._run_db

    async def broken(*args, **kwargs):
        raise RuntimeError("recorded hold; counterfactual database outage")

    monkeypatch.setattr(service, "_run_db", broken)
    await service._evaluate_nfq2_holds("BIYA")
    assert active.phase == "held" and not service.redis.entries
    assert not service.__dict__["_eh_price_hold_inflight"]
    monkeypatch.setattr(service, "_run_db", run_db)
    await service._evaluate_nfq2_holds("BIYA")
    assert active.phase == "queued" and len(service.redis.entries) == 1


@pytest.mark.asyncio
async def test_nfq2_sustained_200_ticks_per_second_with_active_hold_for_60_seconds(service, monkeypatch):
    # Isolate NFQ2; the unchanged legacy drift reader is HOTFIX1's separate concern.
    service.settings.oms_quote_drift_cancel_tolerance_cents = 0
    active = hold(service, biya())
    loop = asyncio.get_running_loop()
    loop_thread = get_ident()
    transactions = []
    factory = service.session_factory

    def counted_factory():
        transactions.append((loop.time(), get_ident()))
        return factory()

    monkeypatch.setattr(service, "session_factory", counted_factory)
    matched_stale = QuoteTickEvent(source_service="market-data", produced_at=NOW - timedelta(seconds=11),
                                  payload=QuoteTickPayload(symbol="BIYA", bid_price="2.53", ask_price="2.54"))
    unrelated = QuoteTickEvent(source_service="market-data", produced_at=NOW,
                              payload=QuoteTickPayload(symbol="OTHER", bid_price="2.53", ask_price="2.54"))
    start = loop.time()
    end = start + 60
    stalls = []
    ticks = 0

    async def probe():
        while loop.time() < end:
            due = loop.time() + 0.005
            await asyncio.sleep(0.005)
            stalls.append(max(0, loop.time() - due))

    async def periodic():
        while loop.time() < end:
            await service._evaluate_nfq2_holds()
            await asyncio.sleep(0.25)

    watcher = asyncio.create_task(probe())
    sweeper = asyncio.create_task(periodic())
    try:
        while loop.time() < end:
            batch_start = loop.time()
            for number in range(10):
                await service._handle_quote_tick_event(matched_stale if number % 2 else unrelated)
                ticks += 1
            await asyncio.sleep(max(0, 0.04 - (loop.time() - batch_start)))
        await watcher
        await sweeper
    finally:
        watcher.cancel()
        sweeper.cancel()
        await asyncio.gather(watcher, sweeper, return_exceptions=True)
    elapsed = loop.time() - start
    assert ticks / elapsed >= 200
    assert max(stalls) < 0.05
    assert transactions and all(thread != loop_thread for _, thread in transactions)
    # The periodic loop uses the existing broker-sync cadence, never the quote rate.
    cadence = max(1.0, service.settings.oms_broker_sync_interval_seconds)
    assert len(transactions) <= 1 + int(elapsed / cadence)
    assert all(right[0] - left[0] >= cadence * 0.95
               for left, right in zip(transactions, transactions[1:]))
    assert service._nfq2_holds[service._nfq2_key(active.event)].phase == "held"
    assert not service.redis.entries
    with factory() as session:
        assert session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)).one().payload["phase"] == "held"
    print(f"NFQ2-TICK-RECEIPT seconds={elapsed:.3f} events={ticks} rate={ticks / elapsed:.2f}/s "
          f"max_loop_stall_ms={max(stalls) * 1000:.3f} db_transactions={len(transactions)} "
          f"db_tx_rate={len(transactions) / elapsed:.4f}/s held=1 wires=0 drift_isolated=1")
