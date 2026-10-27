from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import queue
import socket
from uuid import uuid4

import pytest

from project_mai_tai.events import HeartbeatEvent, HeartbeatPayload, SnapshotBatchEvent, SnapshotBatchPayload
from tools.l13_g2_controls import CaptureSnapshotRedis, SnapshotReceiptRedis, retain_worker_receipt


def heartbeat():
    now = datetime.now(UTC)
    identity = str(uuid4())
    return HeartbeatEvent(source_service="market-data-snapshot-worker", payload=HeartbeatPayload(
        service_name="market-data-snapshot-worker",
        instance_name=f"{socket.gethostname()}:{os.getpid()}:{identity}", status="healthy",
        details={"runtime_pid": str(os.getpid()), "runtime_instance_id": identity,
                 "process_started_at": (now - timedelta(seconds=1)).isoformat(),
                 "role": "independent_snapshot_worker", "reference_ready": "true",
                 "last_snapshot_completed_at": (now - timedelta(seconds=0.1)).isoformat(),
                 "last_snapshot_age_seconds": "0.1", "snapshot_progress_bound_seconds": "20",
                 "last_snapshot_symbols": "13000"}))


@pytest.mark.asyncio
async def test_capture_sink_keeps_snapshot_bytes_and_worker_heartbeat_separate():
    sink = CaptureSnapshotRedis()
    batch = SnapshotBatchEvent(source_service="market-data-gateway", payload=SnapshotBatchPayload(snapshots=[]))
    raw = batch.model_dump_json()
    await sink.xadd("l13-controlled:snapshot-batches", {"data": raw}, maxlen=4, approximate=True)
    await sink.xadd("l13-controlled:heartbeats", {"data": heartbeat().model_dump_json()}, maxlen=1000, approximate=True)
    assert sink.encoded == raw and sink.snapshot_publications == 1
    assert len(sink.heartbeats) == 1
    assert sink.heartbeats[0]["service_name"] == "market-data-snapshot-worker"
    assert sink.heartbeats[0]["runtime_pid"] == os.getpid()


@pytest.mark.asyncio
async def test_receipt_sink_does_not_count_heartbeat_as_snapshot_cycle():
    receipts = queue.Queue()
    sink = SnapshotReceiptRedis(receipts)
    await sink.xadd("l13-controlled:heartbeats", {"data": heartbeat().model_dump_json()}, maxlen=1000, approximate=True)
    metrics = {"worker_receipts": [], "worker_heartbeats": []}
    retain_worker_receipt(metrics, receipts.get_nowait())
    assert metrics["worker_receipts"] == []
    assert len(metrics["worker_heartbeats"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["source", "payload_service", "pid", "instance", "role"])
async def test_worker_identity_mismatch_is_not_ignored(mutation):
    event = heartbeat()
    if mutation == "source":
        event.source_service = "market-data-gateway"
    elif mutation == "payload_service":
        event.payload.service_name = "market-data-gateway"
    elif mutation == "pid":
        event.payload.details["runtime_pid"] = str(os.getpid() + 1)
    elif mutation == "instance":
        event.payload.instance_name = "foreign-instance"
    else:
        event.payload.details["role"] = "websocket"
    with pytest.raises(AssertionError):
        await CaptureSnapshotRedis().xadd("l13-controlled:heartbeats", {"data": event.model_dump_json()},
                                          maxlen=1000, approximate=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [("reference_ready", "false"),
                                       ("last_snapshot_completed_at", "UNKNOWN"),
                                       ("last_snapshot_age_seconds", "UNKNOWN")])
async def test_healthy_requires_actual_progress_fields(key, value):
    event = heartbeat()
    event.payload.details[key] = value
    with pytest.raises((AssertionError, ValueError)):
        await CaptureSnapshotRedis().xadd("l13-controlled:heartbeats", {"data": event.model_dump_json()},
                                          maxlen=1000, approximate=True)


@pytest.mark.asyncio
async def test_other_stream_is_not_silently_accepted():
    with pytest.raises(AssertionError):
        await CaptureSnapshotRedis().xadd("l13-controlled:foreign", {"data": heartbeat().model_dump_json()})


def test_unknown_receipt_kind_is_not_dropped():
    with pytest.raises(AssertionError):
        retain_worker_receipt({"worker_receipts": [], "worker_heartbeats": []}, {"kind": "foreign"})
