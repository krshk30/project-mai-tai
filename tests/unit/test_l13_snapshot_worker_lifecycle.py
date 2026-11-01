"""G1 P4 real-process mock-native lifecycle controls; author UNRUN.

The kill control uses actual spawned Python processes and the real worker run
method. Its tiny test supervisor models the reviewed five-second systemd policy;
it is not a claim that a production systemd kill/restart has been performed.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from project_mai_tai.market_data import gateway as gateway_module
from project_mai_tai.market_data import snapshot_worker as worker_module
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.market_data.models import SnapshotRecord, TradeTickRecord
from project_mai_tai.market_data.snapshot_worker import SnapshotWorkerService
from project_mai_tai.settings import Settings
from tests.unit.test_market_data_gateway import FakeRedis, FakeReferenceCache, FakeSnapshotProvider, FakeTradeStream


ROOT = Path(__file__).resolve().parents[2]


class ChildRedis:
    def __init__(self, pipe):
        self.pipe = pipe

    async def xadd(self, stream, fields, **kwargs):
        if stream == "l13-process:heartbeats":
            event = json.loads(fields["data"])
            assert event["source_service"] == event["payload"]["service_name"] == "market-data-snapshot-worker"
            return "1-0"
        assert stream == "l13-process:snapshot-batches"
        self.pipe.send(("snapshot", os.getpid(), time.monotonic()))
        return "1-0"

    async def hset(self, *args, **kwargs):
        pass

    async def aclose(self):
        self.pipe.close()


def run_fake_worker(pipe):
    # Spawn, never fork a running event loop/SDK client. All native I/O is fake.
    settings = Settings(redis_stream_prefix="l13-process", market_data_snapshot_interval_seconds=1,
                        market_data_reference_refresh_interval_seconds=0, market_data_warmup_enabled=False)
    worker = SnapshotWorkerService(settings, ChildRedis(pipe),
        snapshot_provider=FakeSnapshotProvider(), reference_cache=FakeReferenceCache())
    asyncio.run(worker.run())


async def wait_snapshot(pipe, process, timeout=10.):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pipe.poll():
            value = pipe.recv()
            assert value[0] == "snapshot" and value[1] == process.pid
            return value
        assert process.is_alive(), "fake worker exited before first snapshot"
        await asyncio.sleep(.01)
    pytest.fail("mock snapshot worker startup deadline exceeded")


def trade_count(redis):
    return sum(payload.get("event_type") == "trade_tick" for _, payload, _ in redis.entries)


@pytest.mark.timing
@pytest.mark.asyncio
async def test_p4_worker_sigkill_independent_restart_while_real_gateway_publishes_trades():
    settings = Settings(redis_stream_prefix="l13-process", market_data_warmup_enabled=False)
    redis = FakeRedis()
    gateway = MarketDataGatewayService(settings, redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache())
    stop = asyncio.Event()
    publishing = asyncio.create_task(gateway._stream_publish_loop(stop))

    async def trades():
        while not stop.is_set():
            gateway._enqueue_trade(TradeTickRecord("TEST", 2., 1, timestamp_ns=time.time_ns()))
            await asyncio.sleep(.01)

    producer = asyncio.create_task(trades())
    context = multiprocessing.get_context("spawn")
    children, pipes = [], []

    def launch():
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=run_fake_worker, args=(send,))
        process.start()
        send.close()
        children.append(process)
        pipes.append(receive)
        return process, receive

    try:
        first, first_pipe = launch()
        first_snapshot = await wait_snapshot(first_pipe, first)
        assert first_snapshot[1] != os.getpid()
        before = trade_count(redis)
        first.kill()
        await asyncio.to_thread(first.join, 2.)
        assert not first.is_alive() and first.exitcode != 0
        killed = time.monotonic()
        await asyncio.sleep(5.)  # Exact production RestartSec, not a reduced test substitute.
        assert trade_count(redis) > before + 10 and not publishing.done()
        # During death the sink receives no more snapshots; consumers naturally
        # retain the last batch and existing staleness logic remains unchanged.
        second, second_pipe = launch()
        second_snapshot = await wait_snapshot(second_pipe, second)
        assert second_snapshot[1] != first_snapshot[1]
        assert second_snapshot[2] - killed < 15.  # Restart5s + bounded spawn startup.
        restarted_count = trade_count(redis)
        await asyncio.sleep(.1)
        assert trade_count(redis) > restarted_count and not publishing.done()
    finally:
        stop.set()
        producer.cancel()
        publishing.cancel()
        await asyncio.gather(producer, publishing, return_exceptions=True)
        for process in children:
            if process.is_alive():
                process.kill()
            await asyncio.to_thread(process.join, 2.)
            assert not process.is_alive()
        for pipe in pipes:
            pipe.close()


@pytest.mark.asyncio
async def test_gateway_run_never_schedules_snapshot_or_reference_jobs(monkeypatch):
    stops = []
    monkeypatch.setattr(gateway_module, "_install_signal_handlers", stops.append)

    class WaitingRedis(FakeRedis):
        async def xread(self, *args, **kwargs):
            await asyncio.sleep(.001)
            return []

    service = MarketDataGatewayService(Settings(market_data_warmup_enabled=False), WaitingRedis(),
        snapshot_provider=FakeSnapshotProvider(), trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache())

    async def forbidden(*args, **kwargs):
        pytest.fail("whole-market job executed inside gateway process")

    for name in ("_ensure_reference_data", "_snapshot_loop", "_reference_refresh_loop"):
        monkeypatch.setattr(service, name, forbidden)

    async def finish(stop, heartbeat):
        await asyncio.sleep(0)
        stop.set()

    monkeypatch.setattr(service, "_wait_for_stop_or_heartbeat_failure", finish)
    await service.run()
    assert len(stops) == 1 and stops[0].is_set()


def test_worker_constructs_one_rest_provider_and_no_gateway_or_websocket(monkeypatch):
    constructed = []

    class Rest(FakeSnapshotProvider):
        def __init__(self, key):
            constructed.append(key)

    def forbidden(*args, **kwargs):
        pytest.fail("snapshot worker constructed a gateway or websocket")

    monkeypatch.setattr(worker_module, "MassiveSnapshotProvider", Rest)
    monkeypatch.setattr(MarketDataGatewayService, "__init__", forbidden)
    monkeypatch.setattr(gateway_module, "MassiveTradeStream", forbidden)
    worker = SnapshotWorkerService(Settings(massive_api_key="synthetic-only"), FakeRedis(),
        reference_cache=FakeReferenceCache())
    assert constructed == ["synthetic-only"] and isinstance(worker.snapshot_provider, Rest)


def test_concurrent_rest_access_constructs_exactly_one_sdk_client(monkeypatch):
    import massive
    from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider

    constructed = []

    def fake_client(**kwargs):
        time.sleep(.02)  # Release the GIL to expose concurrent first construction.
        constructed.append(kwargs)
        return SimpleNamespace(synthetic=True)

    monkeypatch.setattr(massive, "RESTClient", fake_client)
    provider = MassiveSnapshotProvider("synthetic-only")
    with ThreadPoolExecutor(max_workers=8) as pool:
        clients = list(pool.map(lambda _: provider._get_rest_client(), range(8)))
    assert len(constructed) == 1 and all(client is clients[0] for client in clients)


@pytest.mark.asyncio
async def test_unexpected_poll_return_is_process_failure_for_supervisor(monkeypatch):
    redis = FakeRedis()
    worker = SnapshotWorkerService(Settings(), redis,
        snapshot_provider=FakeSnapshotProvider(), reference_cache=FakeReferenceCache())

    async def returned(stop):
        return

    monkeypatch.setattr(worker, "_snapshot_loop", returned)
    with pytest.raises(RuntimeError, match="returned unexpectedly"):
        await worker.run(asyncio.Event())


@pytest.mark.asyncio
async def test_rest_error_retries_then_publishes_without_exiting_worker():
    class FailingThenSuccess(FakeSnapshotProvider):
        calls = 0

        def fetch_all_snapshots(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic REST failure")
            return [SnapshotRecord("UGRO")]

    provider, redis = FailingThenSuccess(), FakeRedis()
    worker = SnapshotWorkerService(Settings(market_data_snapshot_interval_seconds=1,
        market_data_reference_refresh_interval_seconds=0), redis,
        snapshot_provider=provider, reference_cache=FakeReferenceCache())
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        deadline = time.monotonic() + 5.
        while not any(stream.endswith(":snapshot-batches") for stream, _, _ in redis.entries) and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        batches = [row for row in redis.entries if row[0].endswith(":snapshot-batches")]
        assert provider.calls >= 2 and len(batches) >= 1 and not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, 2.)


@pytest.mark.asyncio
async def test_gateway_health_copies_old_fields_and_retains_them_on_worker_loss():
    redis = FakeRedis()
    gateway = MarketDataGatewayService(Settings(redis_stream_prefix="l13-test"), redis,
        snapshot_provider=FakeSnapshotProvider(), trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache())
    await redis.hset("l13-test:snapshot-worker-health", mapping={
        "reference_tickers": "17", "snapshots_without_reference": "3", "last_snapshot_symbols": "13000"})
    await gateway._read_snapshot_worker_health()
    assert (gateway._snapshot_reference_tickers, gateway._snapshots_without_reference,
            gateway._last_snapshot_symbol_count) == (17, 3, 13000)
    redis.hashes.clear()
    await gateway._read_snapshot_worker_health()
    assert gateway._snapshot_reference_tickers == 17 and gateway._last_snapshot_symbol_count == 13000


@pytest.mark.asyncio
async def test_worker_restarts_do_not_rewrite_subscription_owners_or_checkpoints():
    redis = FakeRedis()
    key = "l13-test:market-data-subscription-owners"
    original = {"momentum-warning": '["TEST"]', "schwab-1m-v2": '["UGRO"]',
                "_migration_complete": "1", "_last_applied_id": "123-4"}
    await redis.hset(key, mapping=original)
    for _ in range(2):
        worker = SnapshotWorkerService(Settings(redis_stream_prefix="l13-test"), redis,
            snapshot_provider=FakeSnapshotProvider(), reference_cache=FakeReferenceCache())
        stop = asyncio.Event()
        stop.set()
        await worker.run(stop)
        assert redis.hashes[key] == original


def test_unit_and_deployment_lifecycle_are_independent_and_on_by_default():
    unit = (ROOT / "ops/systemd/project-mai-tai-snapshot-worker.service").read_text()
    assert "Restart=always" in unit and "RestartSec=5" in unit and "StartLimitIntervalSec=0" in unit
    assert "BindsTo=" not in unit and "Requires=" not in unit
    assert "After=network-online.target redis-server.service project-mai-tai-market-data.service" in unit
    assert "-m project_mai_tai.services.market_data_snapshot_worker" in unit
    assert "project-mai-tai-snapshot-worker.service" in (ROOT / "ops/systemd/project-mai-tai.target").read_text()
    assert "project-mai-tai-snapshot-worker.service" in (ROOT / "ops/systemd/install_units.sh").read_text()
    deploy = (ROOT / "ops/systemd/deploy_service.sh").read_text()
    branch = deploy[deploy.index('  market-data)\n    stop_unit'):]
    assert branch.index('restart_unit "$PRIMARY_UNIT"') < branch.index('restart_unit "project-mai-tai-snapshot-worker.service"')
    assert branch.index('restart_unit "project-mai-tai-snapshot-worker.service"') < branch.index('start_unit "project-mai-tai-strategy.service"')
