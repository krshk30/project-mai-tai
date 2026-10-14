"""A-only pool/retry/log controls; no sockets or performance measurements."""
import asyncio
import inspect
import logging
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError

from project_mai_tai.market_data import gateway as runtime
from project_mai_tai.market_data.gateway import MarketDataGatewayService, WarningPublishConnectionPool
from project_mai_tai.market_data.publisher import WarningBatchWriteError
from project_mai_tai.settings import Settings
from tests.unit.test_momwarn1_gateway import (
    FakeRedis, FakeReferenceCache, FakeSnapshotProvider, aggregate, healthy, service, stream,
)


@pytest.mark.asyncio
async def test_production_roles_have_isolated_four_slots_and_no_tq_pool(monkeypatch):
    monkeypatch.setattr(ConnectionPool, "ensure_connection", AsyncMock(side_effect=AssertionError("socket attempted")))
    ordinary = FakeRedis()
    svc = MarketDataGatewayService(
        Settings(_env_file=None, market_data_momentum_warning_enabled=True), ordinary,
        trade_stream=stream(), snapshot_provider=FakeSnapshotProvider(), reference_cache=FakeReferenceCache(),
        warning_health_reader=SimpleNamespace(read=healthy))
    clients = [svc._warning_redis, svc._warning_metrics_redis, svc._warning_health_redis]
    assert len({id(c.connection_pool) for c in clients}) == 3
    assert [c.connection_pool.max_connections for c in clients] == [2, 1, 1]
    assert isinstance(clients[0].connection_pool, WarningPublishConnectionPool)
    assert svc.redis is ordinary and svc.publisher.redis is ordinary
    assert svc._warning_publisher.redis is clients[0]
    assert svc._warning_metrics_publisher.redis is clients[1]
    assert all(not c.connection_pool._in_use_connections for c in clients)
    for client in clients:
        await client.aclose()


@pytest.mark.asyncio
async def test_disabled_A_allocates_no_role_clients():
    svc = MarketDataGatewayService(Settings(_env_file=None, market_data_momentum_warning_enabled=False),
        FakeRedis(), trade_stream=stream(), snapshot_provider=FakeSnapshotProvider(),
        reference_cache=FakeReferenceCache())
    assert svc._warning_redis is svc._warning_metrics_redis is svc._warning_health_redis is None


@pytest.mark.asyncio
async def test_known_unsent_pool_capacity_retries_once_before_any_send(monkeypatch):
    connection = object()
    acquire = AsyncMock(side_effect=[RedisConnectionError("Too many connections"), connection])
    monkeypatch.setattr(ConnectionPool, "get_connection", acquire)
    pool = WarningPublishConnectionPool(max_connections=2)
    assert await pool.get_connection() is connection
    assert acquire.await_count == 2 and pool.known_unsent_retries == 1


@pytest.mark.asyncio
async def test_known_unsent_second_failure_is_not_retried_third_time(monkeypatch):
    acquire = AsyncMock(side_effect=RedisConnectionError("Too many connections"))
    monkeypatch.setattr(ConnectionPool, "get_connection", acquire)
    pool = WarningPublishConnectionPool(max_connections=2)
    with pytest.raises(RedisConnectionError, match="Too many connections"):
        await pool.get_connection()
    assert acquire.await_count == 2 and pool.known_unsent_retries == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RedisConnectionError("socket lost"), TimeoutError(), asyncio.CancelledError()])
async def test_other_acquisition_failures_or_cancellation_not_retried(monkeypatch, error):
    acquire = AsyncMock(side_effect=error)
    monkeypatch.setattr(ConnectionPool, "get_connection", acquire)
    pool = WarningPublishConnectionPool(max_connections=2)
    with pytest.raises(type(error)):
        await pool.get_connection()
    assert acquire.await_count == 1 and not hasattr(pool, "known_unsent_retries")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), RedisConnectionError("Too many connections")])
async def test_dispatch_exception_never_retries_unknown_write_and_counts_once(error):
    svc, provider, ordinary, warning = service()
    await svc._warning_monitor_once()
    provider._handle_messages([aggregate()])
    stop = asyncio.Event()
    async def fail(*args, **kwargs):
        stop.set()
        raise error
    warning.xadd = AsyncMock(side_effect=fail)
    await svc._warning_publish_loop(stop)
    assert warning.xadd.await_count == 1
    assert svc._warning_drop_count == 1 and svc._warning_ack_unknown == 1
    assert svc._warning_published == 0
    assert svc._warning_active and svc._warning_revert_reason is None
    assert not ordinary.entries and provider._ws.removed == []


@pytest.mark.asyncio
async def test_partial_batch_ack_not_resubmitted_or_double_counted():
    svc, provider, _, _ = service()
    await svc._warning_monitor_once()
    provider._handle_messages([aggregate(), aggregate(symbol="OTHER")])
    stop = asyncio.Event()
    async def partial(*args, **kwargs):
        stop.set()
        raise WarningBatchWriteError(1, 1)
    svc._warning_publisher.publish_momentum_warning_batch = AsyncMock(side_effect=partial)
    await svc._warning_publish_loop(stop)
    assert svc._warning_publisher.publish_momentum_warning_batch.await_count == 1
    assert svc._warning_published == 1 and svc._warning_drop_count == 1 and svc._warning_ack_unknown == 1


@pytest.mark.asyncio
async def test_health_and_metric_roles_do_not_use_publication_client(monkeypatch):
    svc, _, _, publication = service()
    health_client, metric_client = FakeRedis(), FakeRedis()
    svc._warning_health_redis = health_client
    from project_mai_tai.market_data.publisher import MarketDataPublisher
    svc._warning_metrics_publisher = MarketDataPublisher(metric_client, "test", "fixture")
    seen = []
    async def read_live(client, prefix):
        seen.append(client)
        return healthy()
    svc._warning_health_reader = SimpleNamespace(read_live=read_live)
    await svc._warning_monitor_once()
    assert seen == [health_client]
    stop = asyncio.Event()
    original_wait = asyncio.wait_for
    async def immediate_sample(operation, timeout):
        if timeout == 10:
            operation.close()
            raise TimeoutError
        result = await original_wait(operation, timeout)
        stop.set()
        return result
    monkeypatch.setattr(runtime.asyncio, "wait_for", immediate_sample)
    await svc._warning_metrics_loop(stop)
    assert metric_client.entries and not publication.entries


@pytest.mark.asyncio
async def test_timeout_reuses_one_health_reader_not_one_per_pass():
    svc, _, _, _ = service(market_data_momentum_warning_monitor_timeout_seconds=0.001)
    release = asyncio.Event()
    calls = active = maximum = 0
    async def blocked_read(client, prefix):
        nonlocal calls, active, maximum
        calls += 1
        active += 1
        maximum = max(maximum, active)
        try:
            await release.wait()
            return healthy()
        finally:
            active -= 1
    svc._warning_health_reader = SimpleNamespace(read_live=blocked_read)
    for _ in range(5):
        await svc._warning_monitor_once()
        assert svc._warning_health_task is not None
        assert calls == active == maximum == 1
    release.set()
    await svc._warning_health_task
    await svc._warning_monitor_once()
    assert svc._warning_health_task is None and active == 0 and calls == 1


def test_reason_change_and_exact_sixty_second_repetition_keep_every_counter(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=runtime.SERVICE_NAME)
    svc, provider, _, _ = service()
    now = [100_000_000_000]
    monkeypatch.setattr(runtime.time, "monotonic_ns", lambda: now[0])
    for _ in range(3):
        svc._latch_warning_revert("disconnect", diagnostic="first")
    assert len(caplog.records) == 1
    assert svc._pauses.reconnect_event_count == 3
    assert svc.warning_metrics()["watchdog_observed_reason"] == "disconnect"
    now[0] += 59_999_999_999
    svc._latch_warning_revert("disconnect", diagnostic="changed detail")
    assert len(caplog.records) == 1
    now[0] += 1
    svc._latch_warning_revert("disconnect", diagnostic="latest")
    assert len(caplog.records) == 2 and "latest" in caplog.text
    svc._latch_warning_revert("publish_error")
    svc._latch_warning_revert("disconnect")
    assert len(caplog.records) == 4 and svc._pauses.reconnect_event_count == 6
    assert svc._warning_revert_reason is None and provider._ws.removed == []


@pytest.mark.asyncio
async def test_clean_monitor_restarts_reason_segment_without_actions(caplog):
    svc, provider, _, _ = service()
    svc._latch_warning_revert("load")
    await svc._warning_monitor_once()
    assert svc.warning_metrics()["watchdog_observed_reason"] is None
    svc._latch_warning_revert("load")
    assert len([r for r in caplog.records if "MOMSIMPLE1-OBSERVATION" in r.message]) == 2
    assert svc._warning_active and provider._ws.removed == []


@pytest.mark.asyncio
async def test_queue_stays_bounded_and_overflow_counter_semantics_unchanged():
    svc, provider, _, _ = service(market_data_momentum_warning_queue_maxsize=4)
    await svc._warning_monitor_once()
    provider._handle_messages([aggregate() for _ in range(10)])
    assert svc._warning_queue.qsize() == 4
    assert svc._warning_seq == 10
    assert svc._warning_drop_count == svc._warning_queue_drop_count == 6
    assert [seq for seq, _ in svc._warning_queue._queue] == [7, 8, 9, 10]


@pytest.mark.asyncio
async def test_batch32_bound_keeps_every_sequence_and_current_queue_8192():
    svc, provider, _, _ = service()
    await svc._warning_monitor_once()
    provider._handle_messages([aggregate() for _ in range(65)])
    assert svc._warning_queue.maxsize == 8192
    stop = asyncio.Event()
    sizes = []
    async def batch(records, **kwargs):
        sizes.append(len(records))
        return len(records)
    async def single(*args, **kwargs):
        stop.set()
        return "1-0"
    svc._warning_publisher.publish_momentum_warning_batch = batch
    svc._warning_publisher.publish_momentum_warning_aggregate = single
    await svc._warning_publish_loop(stop)
    assert sizes == [32, 32]
    assert svc._warning_published == 65 and svc._warning_drop_count == 0


def test_remove_log_throttle_mutation_fails_repeat_control(monkeypatch, caplog):
    source = textwrap.dedent(inspect.getsource(MarketDataGatewayService._latch_warning_revert))
    guard = ('    if (reason == self._warning_observation_log_reason\n'
             '            and now - self._warning_observation_log_ns < 60_000_000_000):\n'
             '        return\n')
    assert guard in source
    namespace = dict(runtime.__dict__)
    exec(compile(source.replace(guard, ""), "<closeout-no-log-throttle>", "exec"), namespace)
    monkeypatch.setattr(MarketDataGatewayService, "_latch_warning_revert", namespace['_latch_warning_revert'])
    with pytest.raises(AssertionError):
        test_reason_change_and_exact_sixty_second_repetition_keep_every_counter(monkeypatch, caplog)
