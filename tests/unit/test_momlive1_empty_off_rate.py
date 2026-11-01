"""After-close serial only for 60s node; short lifecycle controls are non-perf."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
import threading
import time

import pytest
from sqlalchemy import text

from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal
from project_mai_tai.momentum_live.owner_proof import owner_proof
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from tests.unit import test_hotfix1_symbol_tick_cache as original

lane = original.lane


@asynccontextmanager
async def active_empty_off(lane, monkeypatch):
    service = lane[0]
    service.settings = service.settings.model_copy(update={
        "momentum_live_enabled": False,
        "strategy_schwab_1m_v2_account_name": "live:schwab_1m_v2",
        "strategy_schwab_1m_v2_webull_account_name": "live:orb",
    })
    runtime = OmsLiveRuntime(service, identity="123:456")
    prior = owner_proof(EpisodeLedger.restore([]), observed_ns=time.time_ns(),
        producer_instance_id="111:222", account_names=runtime.accounts,
        bindings={}, entry_admission_enabled=False)
    thread_state = threading.local()
    stats = {"proof_reads": 0, "proof_writes": 0, "cadence_transactions": 0,
             "wrong_db_thread": False, "slow_db_seconds": .2}
    refreshed = asyncio.Event()
    main_thread = threading.get_ident()
    read_empty = runtime.db.prove_empty_owned_state

    def prove_empty(session, **kwargs):
        thread_state.cadence = True
        try:
            stats["proof_reads"] += 1
            stats["wrong_db_thread"] |= threading.get_ident() == main_thread
            time.sleep(.2)
            return read_empty(session, **kwargs)
        finally:
            thread_state.cadence = False

    async def retained_proof(stream, count=1):
        assert count == 1
        if stream.endswith(":momentum-live-owner-proof"):
            return [("1-0", {"data": json.dumps(prior)})]
        return []

    xadd = service.redis.xadd

    async def counted_xadd(stream, fields, **kwargs):
        await asyncio.sleep(0)  # Controlled asynchronous Redis, not wire evidence.
        result = await xadd(stream, fields, **kwargs)
        if stream.endswith(":momentum-live-owner-proof"):
            proof = json.loads(fields["data"])
            assert proof["episodes"] == [] and proof["quote_symbols"] == []
            assert proof["entry_admission_enabled"] is False
            stats["proof_writes"] += 1
            if stats["proof_writes"] > 1:
                refreshed.set()
        return result

    monkeypatch.setattr(runtime.db, "prove_empty_owned_state", prove_empty)
    monkeypatch.setattr(service.redis, "xrevrange", retained_proof)
    monkeypatch.setattr(service.redis, "xadd", counted_xadd)
    started = time.monotonic()
    try:
        assert await runtime.start() is False
        assert runtime.actor is None and runtime.book.ready
        assert runtime.publication_task is not None and not runtime.publication_task.done()
        assert service._momentum_live_actor is None
        assert service._momentum_live_quote_dispatch is None
        stats["startup_seconds"] = time.monotonic() - started
        stats["initial_proof_reads"] = stats["proof_reads"]
        yield runtime, stats, refreshed, thread_state
    finally:
        await runtime.close()
        if runtime.publication_task is not None:
            assert runtime.publication_task.done()


def partitioned_begin(callback, thread_state, stats):
    def begin(connection):
        if getattr(thread_state, "cadence", False):
            stats["cadence_transactions"] += 1
        else:
            callback(connection)
    return begin


@pytest.mark.asyncio
async def test_short_actual_empty_off_cadence_refreshes_and_cleans_up(lane, monkeypatch):
    async with active_empty_off(lane, monkeypatch) as (runtime, stats, refreshed, state):
        ticks = []
        callback = partitioned_begin(ticks.append, state, stats)
        engine = lane[2].kw["bind"]
        original.sql_event.listen(engine, "begin", callback)
        try:
            await asyncio.wait_for(refreshed.wait(), 3)
            assert stats["proof_reads"] > stats["initial_proof_reads"]
            assert stats["proof_writes"] >= 2 and not stats["wrong_db_thread"]
            assert stats["cadence_transactions"] >= 1 and ticks == []
            await lane[0]._run_db(lambda session: session.execute(text("SELECT 1")).scalar(), commit=False)
            assert len(ticks) == 1  # Ordinary non-cadence DB remains visible.
            assert runtime.actor is None
            task = runtime.publication_task
        finally:
            original.sql_event.remove(engine, "begin", callback)
    assert task.done()


def test_short_transaction_partition_keeps_tick_assertions_strict():
    state = threading.local()
    stats = {"cadence_transactions": 0}
    ticks = []
    callback = partitioned_begin(ticks.append, state, stats)
    callback("tick-before")
    state.cadence = True
    callback("cadence-proof")
    state.cadence = False
    callback("tick-after")
    assert ticks == ["tick-before", "tick-after"]
    assert stats["cadence_transactions"] == 1


@pytest.mark.asyncio
async def test_short_invalid_prior_proof_cannot_claim_active_cadence(lane, monkeypatch):
    async def malformed(*args, **kwargs):
        return [("1-0", {"data": "{}"})]
    monkeypatch.setattr(lane[0].redis, "xrevrange", malformed)
    lane[0].settings = lane[0].settings.model_copy(update={"momentum_live_enabled": False})
    runtime = OmsLiveRuntime(lane[0], identity="123:456")
    with pytest.raises(Refusal, match="previous_proof_UNKNOWN"):
        await runtime.start()
    assert runtime.publication_task is None


@pytest.mark.timing
@pytest.mark.asyncio
async def test_original_off_with_empty_off_cadence_active_60s(lane, monkeypatch):
    """Same original workload and thresholds, plus actual same-loop B cadence."""
    async with active_empty_off(lane, monkeypatch) as (runtime, stats, _, thread_state):
        listen, remove = original.sql_event.listen, original.sql_event.remove
        callbacks = {}

        def register(target, name, callback, *args, **kwargs):
            wrapped = partitioned_begin(callback, thread_state, stats) if name == "begin" else callback
            callbacks[callback] = wrapped
            return listen(target, name, wrapped, *args, **kwargs)

        def unregister(target, name, callback, *args, **kwargs):
            return remove(target, name, callbacks.pop(callback, callback), *args, **kwargs)

        monkeypatch.setattr(original.sql_event, "listen", register)
        monkeypatch.setattr(original.sql_event, "remove", unregister)
        before = {key: stats[key] for key in ("proof_reads", "proof_writes", "cadence_transactions")}
        try:
            await original.test_recorded_240_events_per_second_60_seconds_slow_db_flat_transactions(
                lane, monkeypatch, True,
            )
        finally:
            receipt = {**stats, "cadence_reads_during_rate": stats["proof_reads"] - before["proof_reads"],
                       "cadence_writes_during_rate": stats["proof_writes"] - before["proof_writes"],
                       "cadence_transactions_during_rate": stats["cadence_transactions"] - before["cadence_transactions"],
                       "scope": "CONTROLLED same service/event loop; real empty-OFF task, SQL thread, fake async Redis",
                       "observed_at": datetime.now(UTC).isoformat()}
            print("MOMLIVE1_EMPTY_OFF_ACTIVE=" + json.dumps(receipt, sort_keys=True))
        assert receipt["cadence_reads_during_rate"] >= 30
        assert receipt["cadence_writes_during_rate"] >= 30
        assert receipt["cadence_transactions_during_rate"] >= 30
        assert not stats["wrong_db_thread"]
        assert runtime.actor is None and not runtime.publication_task.done()
