"""G1 install finding controls. Mock Redis/provider only; author UNRUN."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.market_data import snapshot_worker as module
from project_mai_tai.market_data.models import SnapshotRecord
from project_mai_tai.market_data.snapshot_worker import SnapshotWorkerService, WORKER_SERVICE_NAME
from project_mai_tai.post_restart_health_gate import GateOutcome, inspect_health_payload
from project_mai_tai.settings import Settings
from tests.unit.test_market_data_gateway import FakeRedis, FakeReferenceCache, FakeSnapshotProvider


def worker(redis=None):
    return SnapshotWorkerService(Settings(redis_stream_prefix="l13-health",
        market_data_snapshot_interval_seconds=5, service_heartbeat_interval_seconds=15,
        market_data_reference_refresh_interval_seconds=0), redis or FakeRedis(),
        snapshot_provider=FakeSnapshotProvider(), reference_cache=FakeReferenceCache())


def heartbeat(service):
    stream, event, options = service.redis.entries[-1]
    assert stream == "l13-health:heartbeats"
    assert event["source_service"] == event["payload"]["service_name"] == WORKER_SERVICE_NAME
    assert options["maxlen"] == service.settings.redis_heartbeat_stream_maxlen
    return event


@pytest.mark.asyncio
async def test_worker_heartbeat_has_distinct_service_and_actual_process_instance():
    service = worker()
    await service._publish_worker_heartbeat("starting")
    event = heartbeat(service)
    details = event["payload"]["details"]
    assert event["payload"]["instance_name"] == service.instance_name
    assert details["runtime_pid"] == str(service.runtime_pid)
    assert details["runtime_instance_id"] == service.runtime_instance_id
    assert details["process_started_at"] == service.process_started_at.isoformat()
    assert service.publisher.service_name == "market-data-gateway"
    assert service.heartbeat_publisher is not service.publisher


@pytest.mark.asyncio
async def test_retained_counter_hash_cannot_manufacture_healthy_worker():
    service = worker()
    await service.redis.hset("l13-health:snapshot-worker-health", mapping={
        "reference_tickers": "13000", "last_snapshot_symbols": "13000",
        "snapshots_without_reference": "0"})
    await service._publish_worker_heartbeat()
    assert heartbeat(service)["payload"]["status"] == "starting"
    await service._ensure_reference_data()
    await service._publish_worker_heartbeat()
    assert heartbeat(service)["payload"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_actual_batch_publication_required_and_stale_progress_degrades(monkeypatch):
    now = [1000000000]
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: now[0])
    service = worker()
    await service._ensure_reference_data()
    await service.publish_snapshot_batch_once([SnapshotRecord("UGRO")])
    await service._publish_worker_heartbeat()
    assert heartbeat(service)["payload"]["status"] == "healthy"
    now[0] += 20000000001
    await service._publish_worker_heartbeat()
    assert heartbeat(service)["payload"]["status"] == "degraded"
    assert heartbeat(service)["payload"]["details"]["snapshot_progress_bound_seconds"] == "20"


@pytest.mark.asyncio
async def test_failed_batch_publish_does_not_advance_healthy_progress():
    service = worker()
    await service._ensure_reference_data()
    service.publisher.publish_snapshot_batch = AsyncMock(side_effect=RuntimeError("redis failed"))
    with pytest.raises(RuntimeError, match="redis failed"):
        await service.publish_snapshot_batch_once([SnapshotRecord("UGRO")])
    assert service._last_snapshot_completed_ns is None
    await service._publish_worker_heartbeat()
    assert heartbeat(service)["payload"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_heartbeat_write_failure_is_fatal_and_cleanup_runs():
    service = worker()
    service.redis.aclose = AsyncMock()
    service.heartbeat_publisher.publish_heartbeat = AsyncMock(side_effect=RuntimeError("health write failed"))
    with pytest.raises(RuntimeError, match="health write failed"):
        await service.run(asyncio.Event())
    service.redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["returned", "cancelled", "failed"])
async def test_heartbeat_task_death_is_process_failure_for_restart(mode):
    service = worker()
    service.redis.aclose = AsyncMock()

    async def waiting(stop):
        await stop.wait()

    async def dead(stop):
        if mode == "cancelled":
            raise asyncio.CancelledError
        if mode == "failed":
            raise RuntimeError("synthetic heartbeat task error")

    service._snapshot_loop = waiting
    service._heartbeat_loop = dead
    with pytest.raises(RuntimeError, match="heartbeat"):
        await service.run(asyncio.Event())
    service.redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_gets_new_boot_identity_and_no_inherited_progress():
    old = worker()
    await old._ensure_reference_data()
    await old.publish_snapshot_batch_once([SnapshotRecord("UGRO")])
    successor = worker(old.redis)
    assert old.runtime_instance_id != successor.runtime_instance_id
    assert old.instance_name != successor.instance_name
    await successor._publish_worker_heartbeat()
    assert heartbeat(successor)["payload"]["status"] == "starting"


@pytest.mark.asyncio
async def test_normal_stop_publishes_stopping_not_healthy_and_closes():
    service = worker()
    service.redis.aclose = AsyncMock()
    stop = asyncio.Event()
    stop.set()
    await service.run(stop)
    assert heartbeat(service)["payload"]["status"] == "stopping"
    service.redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_boot_adds_no_gateway_or_websocket_constructor(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("heartbeat worker constructed gateway")

    monkeypatch.setattr(module.MarketDataGatewayService, "__init__", forbidden)
    service = worker()
    await service._publish_worker_heartbeat("starting")
    assert heartbeat(service)["payload"]["status"] == "starting"


def test_deployment_mapping_matches_worker_heartbeat_identity_exactly():
    root = Path(__file__).resolve().parents[2]
    deploy = (root / "ops/systemd/deploy_service.sh").read_text()
    mapping = deploy.split("heartbeat_service_for_unit() {", 1)[1].split("\n}", 1)[0]
    assert f'project-mai-tai-snapshot-worker.service) echo "{WORKER_SERVICE_NAME}" ;;' in mapping


@pytest.mark.parametrize("age_seconds,status,expected", [(-1, "healthy", GateOutcome.NOT_HEALTHY),
    (1, "starting", GateOutcome.NOT_HEALTHY), (1, "degraded", GateOutcome.NOT_HEALTHY),
    (1, "healthy", GateOutcome.HEALTHY)])
def test_existing_health_gate_requires_fresh_own_worker_service(age_seconds, status, expected):
    started = datetime(2026, 10, 22, 19, tzinfo=UTC)
    payload = {"services": [{"service_name": WORKER_SERVICE_NAME,
        "observed_at_raw": (started + timedelta(seconds=age_seconds)).isoformat(), "raw_status": status}]}
    assert inspect_health_payload(payload, service_name=WORKER_SERVICE_NAME,
        process_started_at=started).outcome == expected
    assert inspect_health_payload({"services": [{**payload["services"][0],
        "service_name": "market-data-gateway"}]}, service_name=WORKER_SERVICE_NAME,
        process_started_at=started).outcome == GateOutcome.NOT_HEALTHY
