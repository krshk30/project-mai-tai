"""Controlled remaining L13 seams, NOT systemd/native-wire/full-ALL-ON proof."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import multiprocessing
import os
import time

import pytest

from project_mai_tai.events import (
    MarketDataSubscriptionEvent, MarketDataSubscriptionPayload, ReferenceDataPayload, SnapshotBatchEvent,
)
from project_mai_tai.market_data import gateway as gateway_module
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.market_data.models import SnapshotRecord, TradeTickRecord
from project_mai_tai.services import control_plane
from project_mai_tai.settings import Settings
from tests.unit.test_market_data_gateway import FakeRedis, FakeReferenceCache, FakeSnapshotProvider, FakeTradeStream
from tools import l13_g2_controls as bindings
from tools.l13_p3_retained import ScannerConsumer


class OneSnapshot(FakeSnapshotProvider):
    def fetch_all_snapshots(self):
        return [SnapshotRecord("UGRO", last_trade_price=2.5)]


class SnapshotReceipts(FakeRedis):
    def __init__(self, receipts):
        super().__init__()
        self.receipts = receipts

    async def xadd(self, stream, fields, **kwargs):
        if stream.endswith(":snapshot-batches"):
            self.receipts.put_nowait((os.getpid(), time.monotonic_ns(), fields["data"]))
        else:
            assert stream.endswith(":heartbeats")
        return await super().xadd(stream, fields, **kwargs)

    async def hset(self, key, *, mapping):
        assert key == "l13-controlled:snapshot-worker-health", "worker touched subscription ownership"
        return await super().hset(key, mapping=mapping)


class WaitingRedis(FakeRedis):
    async def xread(self, *args, **kwargs):
        await asyncio.sleep(.005)
        return []


class StreamingControl(FakeTradeStream):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.stopped = False
        self.warning_owners = []

    async def start(self, on_trade, on_quote=None, on_agg=None):
        self.on_trade = on_trade
        self.started.set()

    async def stop(self):
        self.stopped = True

    def configure_momentum_warning(self, *args, **kwargs):
        self.warning_configuration = (args, kwargs)

    async def sync_warning_trade_symbols(self, symbols):
        self.warning_owners.append(set(symbols))


async def wait_for_trade(redis, start=0):
    deadline = asyncio.get_running_loop().time() + 3
    while asyncio.get_running_loop().time() < deadline:
        trades = [event for stream, event, _ in redis.entries[start:]
                  if stream.endswith(":market-data") and event["event_type"] == "trade_tick"]
        if trades:
            return trades
        await asyncio.sleep(.005)
    pytest.fail("controlled real gateway did not publish trade")


@pytest.mark.asyncio
async def test_gateway_real_run_restart_keeps_same_spawned_worker_publishing(monkeypatch):
    settings = bindings.offline_settings().model_copy(update={"market_data_snapshot_interval_seconds": 1})
    context = multiprocessing.get_context("spawn")
    receipts = context.Queue(maxsize=32)
    stop_events = []
    monkeypatch.setattr(gateway_module, "_install_signal_handlers", stop_events.append)
    owner_key = "l13-controlled:market-data-subscription-owners"
    original_owners = {"schwab-1m-v2": '["UGRO"]', "orb-schwab": '["UGRO"]',
                       "_migration_complete": "1", "_last_applied_id": "123-4"}
    redis = WaitingRedis()
    redis.hashes[owner_key] = dict(original_owners)
    worker_pid = None
    gateway_tasks = []
    try:
        async with bindings.ProductionWorkerAdapter().open_worker(
            settings=settings, snapshot_provider=OneSnapshot(), reference_cache=FakeReferenceCache(),
            redis_client=SnapshotReceipts(receipts),
        ) as child:
            worker_pid = child.pid
            first = await asyncio.to_thread(receipts.get, True, 5)
            assert first[0] == worker_pid != os.getpid()
            assert len(SnapshotBatchEvent.model_validate_json(first[2]).payload.snapshots) == 1
            for _ in range(2):
                stream = StreamingControl()
                gateway = MarketDataGatewayService(settings, redis, snapshot_provider=FakeSnapshotProvider(),
                    trade_stream=stream, reference_cache=FakeReferenceCache())
                async def forbidden(*args, **kwargs):
                    pytest.fail("gateway restart scheduled whole-market snapshot/reference work")
                monkeypatch.setattr(gateway, "_snapshot_loop", forbidden)
                monkeypatch.setattr(gateway, "_reference_refresh_loop", forbidden)
                monkeypatch.setattr(gateway, "_ensure_reference_data", forbidden)
                before = len(redis.entries)
                task = asyncio.create_task(gateway.run())
                gateway_tasks.append(task)
                await asyncio.wait_for(stream.started.wait(), 3)
                stream.on_trade(TradeTickRecord("UGRO", 2.5, 1, timestamp_ns=time.time_ns()))
                await wait_for_trade(redis, before)
                assert child.is_alive() and child.pid == worker_pid
                assert gateway.active_symbols() == {"UGRO"}
                stop_events[-1].set()
                await asyncio.wait_for(task, 3)
                assert stream.stopped and redis.hashes[owner_key] == original_owners
            # A receipt published AFTER both gateway shutdown/start cycles proves
            # worker progress, not merely survival of the same process object.
            boundary = time.monotonic_ns()
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                last = await asyncio.to_thread(receipts.get, True,
                    max(.001, deadline - asyncio.get_running_loop().time()))
                if last[1] > boundary:
                    break
            assert last[1] > boundary, "no post-restart worker progress receipt"
            assert last[0] == worker_pid and child.is_alive()
    finally:
        for stop in stop_events:
            stop.set()
        for task in gateway_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*gateway_tasks, return_exceptions=True)
        receipts.close()
        receipts.join_thread()


@pytest.mark.parametrize("age,expected", [(0, True), (20, True), (21, False), (-1, False)])
def test_existing_snapshot_freshness_boundary_does_not_change(age, expected, monkeypatch):
    completed = datetime(2026, 10, 22, 18, 30, tzinfo=UTC)
    monkeypatch.setattr(control_plane, "utcnow", lambda: completed + timedelta(seconds=age))
    label = control_plane._datetime_str(completed)
    assert control_plane._is_recent_eastern_label(label, max_age_seconds=20) is expected


@pytest.mark.parametrize("value", [None, "", "UNKNOWN", "not-a-timestamp"])
def test_unknown_snapshot_age_is_not_recent(value):
    assert not control_plane._is_recent_eastern_label(value, max_age_seconds=20)


COMPOSITION_FLAGS = {
    "orb_enabled": True, "orb_country_routing_enabled": True, "orb_live_schwab_orders_enabled": True,
    "strategy_schwab_1m_v2_enabled": True, "strategy_schwab_1m_v2_dual_broker_fanout_enabled": True,
    "strategy_schwab_1m_v2_removed_wait_clear_rules_enabled": True,
    "strategy_schwab_1m_v2_fresh_sell_owner_release_enabled": True,
    "strategy_schwab_1m_v2_gap_trade_conditions_filter_enabled": True,
    "market_data_momentum_warning_enabled": True, "momentum_paper_enabled": True,
    "momentum_live_enabled": True, "strategy_macd_1m_enabled": True,
}


def composition_settings():
    assert COMPOSITION_FLAGS.keys() <= Settings.model_fields.keys()
    settings = bindings.offline_settings().model_copy(update=COMPOSITION_FLAGS)
    assert all(getattr(settings, name) is True for name in COMPOSITION_FLAGS)
    return settings


@pytest.mark.asyncio
async def test_l13_enabled_feed_owners_do_not_erase_atr_orb_or_momentum_owners():
    settings, redis, stream = composition_settings(), FakeRedis(), StreamingControl()
    gateway = MarketDataGatewayService(settings, redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=stream, reference_cache=FakeReferenceCache(), warning_redis_client=redis,
        warning_metrics_redis_client=redis, warning_health_redis_client=redis)
    for index, (owner, symbols) in enumerate((
        ("schwab-1m-v2", ["UGRO", "ATRONLY"]), ("orb-schwab", ["UGRO", "ORBONLY"]),
        ("momentum-warning", ["UGRO", "MOMONLY"]),
    ), 1):
        await gateway.apply_subscription_event(MarketDataSubscriptionEvent(
            source_service="CONTROLLED", payload=MarketDataSubscriptionPayload(
                consumer_name=owner, symbols=symbols)), message_id=f"{index}-0")
    assert set(stream.synced[-1]) == {"UGRO", "ATRONLY", "ORBONLY"}
    assert stream.warning_owners[-1] == {"UGRO", "MOMONLY"}
    await gateway.apply_subscription_event(MarketDataSubscriptionEvent(
        source_service="CONTROLLED", payload=MarketDataSubscriptionPayload(
            consumer_name="orb-schwab", mode="remove", symbols=["UGRO"])), message_id="4-0")
    assert "UGRO" in gateway.active_symbols() and "UGRO" in stream.synced[-1]
    saved = dict(redis.hashes[gateway._subscription_state_key])
    await bindings.ProductionWorkerAdapter().publish_recorded_batch(
        snapshots=[SnapshotRecord("UGRO", last_trade_price=2.5)], reference_data=[])
    assert redis.hashes[gateway._subscription_state_key] == saved
    assert saved["_last_applied_id"] == "4-0"


@pytest.mark.asyncio
async def test_l13_new_batch_enters_actual_enabled_scanner_without_clearing_manual_stop(monkeypatch):
    settings = composition_settings()
    monkeypatch.setattr(bindings, "offline_settings", lambda: settings)
    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.configure_logging",
                        lambda *args, **kwargs: logging.getLogger("CONTROLLED-l13-composition"))
    clock = [datetime.now(UTC)]
    consumer = ScannerConsumer(clock)
    # Restore the REAL preload boundary with positive controlled storage facts;
    # unlike the generic parity harness's no-op, this exercises restart STOP load.
    from project_mai_tai.services.strategy_engine_app import StrategyEngineService
    loaded = []
    def stopped():
        loaded.append("global-stop-loaded")
        return {"UGRO"}
    monkeypatch.setattr(consumer.service, "_load_global_manual_stop_symbols", stopped)
    monkeypatch.setattr(consumer.service, "_load_manual_stop_symbols", lambda: {})
    monkeypatch.setattr(consumer.service, "_preload_manual_stop_state",
        StrategyEngineService._preload_manual_stop_state.__get__(consumer.service))
    raw = await bindings.ProductionWorkerAdapter().publish_recorded_batch(
        snapshots=[SnapshotRecord("UGRO", last_trade_price=2.5)],
        reference_data=[ReferenceDataPayload(symbol="UGRO", shares_outstanding=50000, avg_daily_volume=390000)])
    clock[0] = SnapshotBatchEvent.model_validate_json(raw).produced_at
    actual = await consumer.consume(raw)
    assert loaded == ["global-stop-loaded"]
    assert actual["warmup"]["history_cycles"] == 1
    assert "UGRO" in consumer.service.state.global_manual_stop_symbols
    assert "UGRO" not in actual["watchlist"]
    assert "macd_1m" in consumer.service.state.bots, "nonvacuous real scanner bot required"
    assert "UGRO" in consumer.service.state.bots["macd_1m"].manual_stop_symbols
    assert all("UGRO" not in bot.watchlist for bot in consumer.service.state.bots.values())
    assert all(getattr(consumer.service.settings, name) is True for name in COMPOSITION_FLAGS)
