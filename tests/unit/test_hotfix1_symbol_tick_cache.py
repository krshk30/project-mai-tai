"""Recorded prices/events; DB delay, SDK and concurrency are controlled offline."""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sql_event

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeTickEvent, TradeTickPayload, TradeIntentEvent, TradeIntentPayload
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms.mirror_retained_hold import row_id
from tests.unit.test_mirrorhold1_retained_hold import lane, event_for, quote, state, queued
from tests.unit.test_oms_direct_cancel_dead_target_bound import _service, _seed_target, _DirectCancelAdapter

ROOT = Path(__file__).resolve().parents[2] / "docs/review-artifacts/hotfix1"
RECORDED = json.loads((ROOT / "recorded-cases.json").read_text())


def recorded_event(symbol="SXTC", account="live:orb"):
    row = next(r for r in RECORDED["intents"] if r["symbol"] == symbol and r["account"] == account)
    return TradeIntentEvent(event_id=row["payload"]["event_id"],
        source_service=row["payload"]["source_service"],
        produced_at=datetime.fromisoformat(row["created_at"]),
        payload=TradeIntentPayload(broker_account_name=account, strategy_code="schwab_1m_v2",
            symbol=symbol, side="buy", intent_type="open", quantity=Decimal(str(row["quantity"])),
            reason="recorded HOTFIX1 replay", metadata=row["payload"]["metadata"]))


def retain(lane, event):
    service, _, factory, clock = lane
    clock[0] = event.produced_at
    FanoutSegmentIdentityStore(factory).record(event.payload.symbol,
        int(event.payload.metadata["fanout_segment_id"]), True, "recorded replay", now=clock[0])
    with factory() as session:
        assert service._nfq_pre_submit(session, event) is not None
        session.commit()
    return row_id(event)


async def finish_workers(service):
    tasks = tuple(service.__dict__.get("_symbol_tick_work", {}).values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 3)


def test_recorded_biya_both_real_refusals_and_sxtc_hold_are_in_own_pull():
    assert [r["quantity"] for r in RECORDED["intents"] if r["symbol"] == "BIYA"] == [236, 118]
    assert all(r["payload"]["refusal_code"] == "NO_FRESH_QUOTE"
               for r in RECORDED["intents"] if r["symbol"] == "BIYA")
    row = next(r for r in RECORDED["intents"] if r["symbol"] == "SXTC" and r["account"] == "live:orb")
    assert row["payload"]["refusal_code"] == "webull_mirror_no_fresh_quote_held"
    assert row["payload"]["metadata"]["stop_price"] == "3.2300"


def test_recorded_sxtc_hold_restores_nfq_age_source_log(lane, caplog):
    service = lane[0]
    event = recorded_event()
    retain(lane, event)
    assert state(lane, event)["phase"] == "held"
    assert "[OMS-NFQ1] sym=SXTC" in caplog.text
    assert "decision=held reason=no_fresh_quote" in caplog.text
    assert "cache_age_ms=" in caplog.text and "cache_source=" in caplog.text
    assert row_id(event) in service._mirrorhold_by_symbol["SXTC"]


def test_cache_publication_is_commit_only_and_rollback_does_not_leak(lane):
    service, _, factory, _ = lane
    event = event_for(lane)
    key = row_id(event)
    with factory() as session:
        service._mirrorhold_upsert(session, event, no_wire=True)
        assert key not in service._mirrorhold_cache().get("IPDN", {})
        session.rollback()
        session.commit()
    assert key not in service._mirrorhold_cache().get("IPDN", {})
    with factory() as session:
        row = service._mirrorhold_upsert(session, event, no_wire=True)
        session.commit()
        assert key in service._mirrorhold_cache()["IPDN"]
        service._mirrorhold_write(session, row, {**row.payload, "phase": "retired"})
        assert key in service._mirrorhold_cache()["IPDN"]
        session.rollback()
        session.commit()
    assert key in service._mirrorhold_cache()["IPDN"]


def test_older_worker_revision_cannot_resurrect_retired_cache(lane):
    service, _, factory, _ = lane
    event = event_for(lane)
    with factory() as session:
        row = service._mirrorhold_upsert(session, event, no_wire=True)
        old = deepcopy(row.payload)
        session.commit()
        service._mirrorhold_write(session, row, {**row.payload, "phase": "retired"})
        session.commit()
    with factory() as session:
        service._mirrorhold_cache_after_commit(session, row_id(event), old)
        session.commit()
    assert not service._mirrorhold_cache().get("IPDN")


@pytest.mark.asyncio
async def test_unrelated_empty_outside_and_rpg_owned_ticks_make_no_db_call(lane, monkeypatch):
    service = lane[0]
    event = recorded_event()
    retain(lane, event)
    quote(lane, event, "2.37")  # Recorded SXTC ask, outside 8%.
    service._latest_quotes_by_symbol["OTHER"] = {"ask": Decimal("3.23"), "received_at": lane[3][0]}
    monkeypatch.setattr(service, "session_factory", lambda: pytest.fail("tick opened a DB session"))
    for _ in range(200):
        service._schedule_webull_mirror_tick("OTHER")
        service._schedule_webull_mirror_tick("SXTC")
    assert not service.__dict__.get("_symbol_tick_work")
    assert not queued(lane)


@pytest.mark.asyncio
async def test_slow_database_worker_does_not_delay_tick_or_exit_and_deduplicates(lane, monkeypatch):
    service = lane[0]
    event = recorded_event()
    retain(lane, event)
    quote(lane, event, "3.23")  # Controlled in-band signal, not historical cache timing.
    started, release = threading.Event(), threading.Event()
    original = service._mirrorhold_prepare_queue
    calls = []
    def slow(symbol, keys):
        calls.append((symbol, keys))
        started.set()
        assert release.wait(2)
        return original(symbol, keys)
    monkeypatch.setattr(service, "_mirrorhold_prepare_queue", slow)
    for _ in range(100):
        service._schedule_webull_mirror_tick("SXTC")
    await asyncio.sleep(0.02)
    assert started.is_set()
    assert len(calls) == 1
    exit_calls = AsyncMock()
    monkeypatch.setattr(service, "_evaluate_hard_stop_market_event", exit_calls)
    service._armed_hard_stops = {"controlled-active-reader": object()}
    ladder_calls = AsyncMock()
    monkeypatch.setattr(service, "_evaluate_v2_managed_exit", ladder_calls)
    service._managed_v2_symbols = {(account, "BIYA") for account in service._v2_accounts()}
    raw = next(q for q in RECORDED["quotes"] if q["symbol"] == "BIYA")
    tick = QuoteTickEvent(source_service="recorded", produced_at=lane[3][0],
        payload=QuoteTickPayload(symbol="BIYA", bid_price=raw["bid_price"], ask_price=raw["ask_price"]))
    await asyncio.wait_for(service._handle_quote_tick_event(tick), 0.05)
    exit_calls.assert_awaited_once_with("BIYA")
    assert ladder_calls.await_count == len(service._v2_accounts())
    assert {call.args for call in ladder_calls.await_args_list} == service._managed_v2_symbols
    release.set()
    await finish_workers(service)
    assert len(queued(lane)) == 1


@pytest.mark.asyncio
async def test_durable_retirement_fences_already_admitted_stale_worker(lane):
    service, _, factory, _ = lane
    event = recorded_event()
    key = retain(lane, event)
    quote(lane, event, "3.23")
    service._schedule_webull_mirror_tick("SXTC")
    with factory() as session:
        row = session.get(DashboardSnapshot, key)
        service._mirrorhold_write(session, row, {**row.payload, "phase": "retired", "token": ""})
        session.commit()
    await finish_workers(service)
    assert not queued(lane)
    assert not service._mirrorhold_cache().get("SXTC")


@pytest.mark.asyncio
async def test_unknown_gate_is_once_per_revision_not_per_tick(lane, monkeypatch):
    service = lane[0]
    event = recorded_event()
    retain(lane, event)
    quote(lane, event, "3.23")
    calls = []
    original = service._mirrorhold_prepare_queue
    def counted(symbol, keys):
        calls.append(symbol)
        return original(symbol, keys)
    monkeypatch.setattr(service, "_mirrorhold_prepare_queue", counted)
    monkeypatch.setattr(service, "_mirrorhold_gate", lambda *args, **kwargs: "segment_identity_unproven")
    for _ in range(20):
        service._schedule_webull_mirror_tick("SXTC")
        await finish_workers(service)
    assert calls == ["SXTC"]
    assert not queued(lane)


@pytest.mark.asyncio
async def test_shutdown_drains_db_and_invalidates_reserved_token(lane):
    service = lane[0]
    event = recorded_event()
    retain(lane, event)
    quote(lane, event, "3.23")
    service._schedule_webull_mirror_tick("SXTC")
    await asyncio.wait_for(service._shutdown_symbol_tick_work(), 3)
    assert not queued(lane)
    assert state(lane, event)["phase"] == "held"
    assert state(lane, event)["token"] == ""
    service._schedule_webull_mirror_tick("SXTC")
    assert not service._symbol_tick_work


@pytest.mark.asyncio
async def test_drift_cache_empty_and_inside_limit_quote_has_zero_db_calls(monkeypatch):
    service, factory = _service(_DirectCancelAdapter())
    with factory() as session:
        _seed_target(session, symbol="BIYA", order_type="limit")
    await service._refresh_drift_working_cache()
    monkeypatch.setattr(service, "_run_db", AsyncMock(side_effect=AssertionError("tick queried DB")))
    service._latest_quotes_by_symbol["BIYA"] = {"ask": 2.0}
    for _ in range(200):
        await service._cancel_drifted_working_orders("BIYA")
        await service._cancel_drifted_working_orders("SXTC")
    service._run_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_drift_cache_revalidates_terminal_order_before_broker_cancel():
    adapter = _DirectCancelAdapter()
    service, factory = _service(adapter)
    with factory() as session:
        _, _, _, order = _seed_target(session, symbol="BIYA", order_type="limit")
        key = order.id
    await service._refresh_drift_working_cache()
    from project_mai_tai.db.models import BrokerOrder
    with factory() as session:
        session.get(BrokerOrder, key).status = "filled"
        session.commit()
    service._latest_quotes_by_symbol["BIYA"] = {"ask": 2.70}
    await service._cancel_drifted_working_orders("BIYA")
    await finish_workers(service)
    assert not adapter.requests


@pytest.mark.asyncio
async def test_tick_drift_slow_worker_returns_immediately_and_only_one_cancel(monkeypatch):
    adapter = _DirectCancelAdapter()
    service, factory = _service(adapter)
    with factory() as session:
        _seed_target(session, symbol="BIYA", order_type="limit")
    await service._refresh_drift_working_cache()
    original = service._collect_drift_cancel_candidates
    def slow(*args):
        time.sleep(0.15)
        return original(*args)
    monkeypatch.setattr(service, "_collect_drift_cancel_candidates", slow)
    service._latest_quotes_by_symbol["BIYA"] = {"ask": 2.70}
    for _ in range(100):
        await asyncio.wait_for(service._cancel_drifted_working_orders("BIYA"), 0.05)
    await finish_workers(service)
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_broker_sync_refreshes_detached_working_order_snapshot(monkeypatch):
    adapter = _DirectCancelAdapter()
    service, factory = _service(adapter)
    with factory() as session:
        _seed_target(session, symbol="BIYA", order_type="limit")
    await service.sync_broker_orders()
    assert service._drift_working_by_symbol["BIYA"][0].limit_price == "2.55"
    from project_mai_tai.db.models import BrokerOrder
    from sqlalchemy import select
    with factory() as session:
        row = session.scalar(select(BrokerOrder))
        row.status = "filled"
        session.commit()
    await service.sync_broker_orders()
    assert not service._drift_working_by_symbol


@pytest.mark.asyncio
async def test_last_tick_new_committed_generation_is_not_lost_while_worker_busy(lane, monkeypatch):
    service, _, factory, _ = lane
    event = recorded_event()
    key = retain(lane, event)
    quote(lane, event, "3.23")
    started, release = threading.Event(), threading.Event()
    original = service._mirrorhold_prepare_queue
    calls = []
    def blocked_first(symbol, keys):
        calls.append(symbol)
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
            return []
        return original(symbol, keys)
    monkeypatch.setattr(service, "_mirrorhold_prepare_queue", blocked_first)
    service._schedule_webull_mirror_tick("SXTC")
    await asyncio.sleep(0.02)
    assert started.is_set()
    with factory() as session:
        row = session.get(DashboardSnapshot, key)
        service._mirrorhold_write(session, row, {**row.payload, "reason": "controlled_new_generation_wake"})
        session.commit()
    service._schedule_webull_mirror_tick("SXTC")  # Last tick: no periodic help.
    release.set()
    for _ in range(100):
        await finish_workers(service)
        if queued(lane):
            break
        await asyncio.sleep(0.005)
    assert calls == ["SXTC", "SXTC"]
    assert len(queued(lane)) == 1


@pytest.mark.asyncio
async def test_generic_worker_dedup_allows_one_active_task_per_symbol(lane):
    service = lane[0]
    release = asyncio.Event()
    calls = []
    async def slow():
        calls.append("started")
        await release.wait()
    for _ in range(100):
        service._schedule_symbol_tick_work(("controlled-dedup", "SXTC"), slow)
    await asyncio.sleep(0)
    assert calls == ["started"]
    release.set()
    await finish_workers(service)


@pytest.mark.parametrize("phase", ["held", "queued", "retired", "filled", "accepted", "uncertain", "capped"])
def test_each_committed_owner_phase_refreshes_cache_without_orm_objects(lane, phase):
    service, _, factory, _ = lane
    event = event_for(lane)
    with factory() as session:
        row = service._mirrorhold_upsert(session, event, no_wire=True)
        session.commit()
        service._mirrorhold_write(session, row, {**row.payload, "phase": phase})
        session.commit()
    cached = service._mirrorhold_cache().get("IPDN", {}).get(row_id(event))
    if phase in {"held", "queued"}:
        assert type(cached) is dict and cached["phase"] == phase
        assert cached["revision"] == 1
    else:
        assert cached is None


def test_startup_restores_held_symbol_index(lane):
    from tests.unit.test_mirrorhold1_retained_hold import restart
    event = recorded_event()
    retain(lane, event)
    restored = restart(lane)[0]
    assert row_id(event) in restored._mirrorhold_cache()["SXTC"]
    assert restored._mirrorhold_cache()["SXTC"][row_id(event)]["phase"] == "held"


@pytest.mark.asyncio
async def test_recorded_240_events_per_second_60_seconds_slow_db_flat_transactions(lane, monkeypatch):
    """Offline replay. Broker exits mocked; real SQLite transactions instrumented.

    A held/in-band but authoritative-blocked owner is stressed too. Its first
    attempted dispatch reads DB once; subsequent ticks cannot turn it into 240
    transactions/s. Periodic checks are measured separately, not attributed to ticks.
    """
    service, _, factory, clock = lane
    event = recorded_event()
    retain(lane, event)
    original_prepare = service._mirrorhold_prepare_queue
    def slow_prepare(*args):
        time.sleep(0.2)  # Controlled realistic DB delay, not measured production latency.
        return original_prepare(*args)
    monkeypatch.setattr(service, "_mirrorhold_prepare_queue", slow_prepare)
    monkeypatch.setattr(service, "_mirrorhold_gate", lambda *args, **kwargs: "segment_identity_unproven")
    # Controlled confirming-price cache makes the recorded owner eligible; the
    # quote/trade replay prices below remain exactly those in the own pull.
    original_reading = service._mirror_reading
    from dataclasses import replace
    def in_band_reading(symbol):
        reading = original_reading(symbol)
        return replace(reading, price=Decimal("3.23"), fresh=True) if symbol == "SXTC" else reading
    monkeypatch.setattr(service, "_mirror_reading", in_band_reading)
    service._drift_working_by_symbol = {}
    # A bounded counting stub avoids building an AsyncMock call-history list at
    # feed rate. Neither this stub nor the earlier mock is a real broker exit.
    exit_count = 0
    async def count_exit(_symbol):
        nonlocal exit_count
        exit_count += 1
    monkeypatch.setattr(service, "_evaluate_hard_stop_market_event", count_exit)
    service._armed_hard_stops = {"controlled-active-reader": object()}
    transactions = []
    engine = factory.kw["bind"]
    def begin(_connection):
        transactions.append(time.monotonic())
    sql_event.listen(engine, "begin", begin)
    stop = asyncio.Event()
    stalls = []
    async def watchdog():
        deadline = time.monotonic()
        while not stop.is_set():
            deadline += 0.005
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            stalls.append(max(0, time.monotonic() - deadline))
    watch = asyncio.create_task(watchdog())
    trades = json.loads((ROOT / "recorded-trades.json").read_text())
    replay = []
    for raw in RECORDED["quotes"]:
        replay.append(QuoteTickEvent(source_service="recorded-offline", produced_at=clock[0],
            payload=QuoteTickPayload(symbol=raw["symbol"], bid_price=raw["bid_price"], ask_price=raw["ask_price"])))
    for raw in trades:
        replay.append(TradeTickEvent(source_service="recorded-offline", produced_at=clock[0],
            payload=TradeTickPayload(symbol=raw["symbol"], price=raw["price"], size=raw["size"])))
    count, start = 0, time.monotonic()
    try:
        while time.monotonic() - start < 60:
            tick = replay[count % len(replay)]
            if isinstance(tick, QuoteTickEvent):
                await service._handle_quote_tick_event(tick)
            else:
                await service._handle_trade_tick_event(tick)
            count += 1
            await asyncio.sleep(max(0, start + count / 240 - time.monotonic()))
        elapsed = time.monotonic() - start
        await finish_workers(service)
    finally:
        stop.set()
        await watch
        sql_event.remove(engine, "begin", begin)
    receipt = {"scope": "offline real recorded quote/trade prices; controlled in-band gate and 200ms DB delay; exits mocked",
        "seconds": elapsed, "events": count, "events_per_second": count / elapsed,
        "max_loop_stall_ms": max(stalls) * 1000, "database_transactions": len(transactions),
        "transactions_after_first_second": sum(t >= start + 1 for t in transactions),
        "hard_stop_reader_calls": exit_count, "buys_queued": len(queued(lane))}
    print("HOTFIX1_BENCHMARK=" + json.dumps(receipt, sort_keys=True))
    assert elapsed >= 60 and count / elapsed >= 200
    assert max(stalls) < 0.05
    assert len(transactions) == 1
    assert not any(t >= start + 1 for t in transactions)
    assert exit_count == count
    assert not queued(lane)
