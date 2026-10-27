from __future__ import annotations

import copy
from datetime import UTC, datetime
import hashlib
import json
import multiprocessing
import os
import queue

import pytest

from project_mai_tai.events import SnapshotBatchEvent, SnapshotBatchPayload
from project_mai_tai.market_data.models import SnapshotRecord
from tools.l13_g2_controls import (
    DECODE_CPU_NS, EVENTS, SNAPSHOTS, CpuSnapshotProvider, SnapshotReceiptRedis,
    ControlledReferences, ProductionWorkerAdapter, TradeSink, assert_p1,
    consume_recorded_batches, load_adapter, load_p3_evidence, load_recorded_packet, offline_settings,
    datetime_epoch_ns, percentile_nearest_rank, write_fresh_result,
)


def complete_metrics():
    receipts = []
    for index in range(8):
        receipts.extend([
            {"kind": "fetch", "pid": 2, "count": SNAPSHOTS, "cpu_ns": DECODE_CPU_NS,
             "start_ns": index + 1, "end_ns": index + 2, "overlap_trades": 70},
            {"kind": "publish", "pid": 2, "count": SNAPSHOTS,
             "start_ns": index + 1, "end_ns": index + 2},
        ])
    heartbeats = [dict(kind="heartbeat", pid=2, runtime_pid=2, runtime_instance_id="controlled",
                       observed_ns=when, status=status)
                  for when, status in ((0, "starting"), (15, "healthy"), (30, "healthy"),
                                       (45, "healthy"), (101, "stopping"))]
    return dict(elapsed_seconds=60.01, received=EVENTS, sent=EVENTS,
                unique_sip_count=EVENTS, negative_lags=0, lag_p99_ms=120,
                cycle_lag_p99_ms=120, cycle_trade_count=1000, snapshot_cycles=[{}] * 8,
                loop_stall_max_ms=10, producer_alive_after_cleanup=False,
                pending_gateway_trades=0, pending_frames=0, task_errors=[],
                worker_receipts=receipts, gateway_pid=1, window_start_ns=0,
                window_end_ns=100, worker_heartbeats=heartbeats)


def test_p1_contract_accepts_only_complete_witness():
    assert_p1(complete_metrics())


@pytest.mark.parametrize("field,value", [
    ("elapsed_seconds", 59.99), ("elapsed_seconds", 73),
    ("received", EVENTS - 1), ("sent", EVENTS - 1),
    ("unique_sip_count", EVENTS - 1), ("negative_lags", 1),
    ("lag_p99_ms", 300), ("loop_stall_max_ms", 50),
    ("cycle_lag_p99_ms", 300), ("cycle_trade_count", 0), ("snapshot_cycles", []),
    ("producer_alive_after_cleanup", True), ("pending_gateway_trades", 1),
    ("pending_frames", 1), ("task_errors", ["failure"]),
    ("worker_receipts", []),
    ("worker_heartbeats", []),
])
def test_p1_contract_does_not_waive_any_gate(field, value):
    metrics = complete_metrics()
    metrics[field] = value
    with pytest.raises(AssertionError):
        assert_p1(metrics)


@pytest.mark.parametrize("change", ["same_process", "wrong_count", "sleep_not_cpu",
                                    "no_overlapping_trade", "outside_window"])
def test_worker_physical_cpu_and_overlap_witness_required(change):
    metrics = copy.deepcopy(complete_metrics())
    row = metrics["worker_receipts"][0]
    if change == "same_process":
        row["pid"] = metrics["gateway_pid"]
    elif change == "wrong_count":
        row["count"] -= 1
    elif change == "sleep_not_cpu":
        row["cpu_ns"] = 0
    elif change == "no_overlapping_trade":
        row["overlap_trades"] = 0
    else:
        row["start_ns"] = -1
    with pytest.raises(AssertionError):
        assert_p1(metrics)


def test_quantile_uses_nearest_rank_without_interpolation():
    assert percentile_nearest_rank(list(range(1, 101)), 99) == 99
    with pytest.raises(ValueError):
        percentile_nearest_rank([], 99)


def test_decode_fixture_calls_real_normalization_not_sleep():
    receipts = queue.Queue()
    provider = CpuSnapshotProvider(receipts, count=3, cpu_ns=0)
    snapshots = provider.fetch_all_snapshots()
    assert len(snapshots) == 3
    assert snapshots[0].symbol == "CPU0"
    assert snapshots[0].day_volume == 100_000
    assert snapshots[0].last_trade_price == 2.5
    assert receipts.get_nowait()["kind"] == "fetch"


@pytest.mark.asyncio
async def test_trade_sink_uses_produced_timestamp_not_receipt_clock():
    sink = TradeSink()
    sip = int(datetime(2026, 10, 22, 14, tzinfo=UTC).timestamp() * 1e9)
    await sink.xadd("l13-controlled:market-data", {"data": json.dumps({
        "event_type": "trade_tick", "produced_at": "2026-10-22T14:00:00.250000Z",
        "payload": {"timestamp_ns": sip},
    })})
    assert sink.lags_ms == [250.0]


@pytest.mark.parametrize("stamp,expected_ns", [
    ("1970-01-01T00:00:00.000001+00:00", 1_000),
    ("1969-12-31T23:59:59.999999+00:00", -1_000),
    ("2038-01-19T03:14:08.123456+00:00", 2_147_483_648_123_456_000),
    ("2100-01-01T00:00:00.123456+00:00", 4_102_444_800_123_456_000),
    ("2100-01-01T05:30:00.123456+05:30", 4_102_444_800_123_456_000),
])
def test_produced_epoch_uses_exact_integer_utc_delta(stamp, expected_ns):
    assert datetime_epoch_ns(datetime.fromisoformat(stamp)) == expected_ns


def test_produced_epoch_never_defaults_naive_timestamp_to_local_timezone():
    with pytest.raises(ValueError, match="timezone required"):
        datetime_epoch_ns(datetime(2026, 10, 22, 14))


@pytest.mark.asyncio
async def test_trade_sink_retains_one_nanosecond_difference_at_large_epoch():
    sink = TradeSink()
    await sink.xadd("l13-controlled:market-data", {"data": json.dumps({
        "event_type": "trade_tick", "produced_at": "2100-01-01T00:00:00.123456Z",
        "payload": {"timestamp_ns": 4_102_444_800_123_455_999},
    })})
    assert sink.lags_ms == [0.000001]


@pytest.mark.asyncio
async def test_trade_sink_non_utc_offset_keeps_exact_quarter_second():
    sink = TradeSink()
    await sink.xadd("l13-controlled:market-data", {"data": json.dumps({
        "event_type": "trade_tick", "produced_at": "2100-01-01T05:30:00.250000+05:30",
        "payload": {"timestamp_ns": 4_102_444_800_000_000_000},
    })})
    assert sink.lags_ms == [250.0]


@pytest.mark.asyncio
async def test_snapshot_receipt_validates_production_envelope():
    receipts = queue.Queue()
    sink = SnapshotReceiptRedis(receipts)
    event = SnapshotBatchEvent(source_service="market-data-gateway",
                               payload=SnapshotBatchPayload(snapshots=[]))
    await sink.xadd("l13-controlled:snapshot-batches", {"data": event.model_dump_json()},
                    maxlen=4, approximate=True)
    assert receipts.get_nowait()["count"] == 0
    with pytest.raises(AssertionError):
        await sink.xadd("wrong-stream", {"data": event.model_dump_json()})


def test_result_export_is_fresh_only(tmp_path):
    target = tmp_path / "result.json"
    write_fresh_result(target, {"gate_status": "FAILED"})
    assert json.loads(target.read_text())["gate_status"] == "FAILED"
    with pytest.raises(FileExistsError):
        write_fresh_result(target, {})


@pytest.mark.parametrize("spec", ["", "module", ":factory", "module:factory.method"])
def test_missing_or_ambiguous_adapter_is_never_favorable_fallback(spec):
    with pytest.raises(ValueError):
        load_adapter(spec)


def test_recorded_packet_requires_provenance_before_any_replay(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"kind": "synthetic_snapshot_batches"}))
    with pytest.raises(ValueError, match="provenance"):
        load_recorded_packet(path)


def test_recorded_packet_hash_mismatch_is_material(tmp_path):
    (tmp_path / "batches.jsonl").write_text("{}\n")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"kind": "recorded_snapshot_batches", "provenance": "controlled",
                                "file": "batches.jsonl", "sha256": "0" * 64}))
    with pytest.raises(ValueError, match="SHA256"):
        load_recorded_packet(path)


def test_recorded_single_batch_cannot_prove_alerts_without_prior_state(tmp_path):
    event = SnapshotBatchEvent(source_service="market-data-gateway",
                               payload=SnapshotBatchPayload(snapshots=[]))
    raw = (event.model_dump_json() + "\n").encode()
    (tmp_path / "batches.jsonl").write_bytes(raw)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"kind": "recorded_snapshot_batches", "provenance": "controlled",
                                "file": "batches.jsonl", "sha256": hashlib.sha256(raw).hexdigest()}))
    manifest, rows = load_recorded_packet(path)
    with pytest.raises(ValueError, match="prior_consumer_state"):
        load_p3_evidence(path, manifest, rows)


def test_recorded_packet_never_invents_missing_header_timestamps(tmp_path):
    raw = json.dumps({"event_type": "snapshot_batch", "source_service": "market-data-gateway",
                      "payload": {"snapshots": [], "reference_data": []}}).encode()
    (tmp_path / "batches.jsonl").write_bytes(raw)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"kind": "recorded_snapshot_batches", "provenance": "controlled",
                                "file": "batches.jsonl", "sha256": hashlib.sha256(raw).hexdigest()}))
    with pytest.raises(ValueError, match="no model-default timestamps"):
        load_recorded_packet(path)


def test_expected_results_missing_is_not_old_equals_new_proof(tmp_path):
    prior = {"kind": "recorded_scanner_prior_state", "provenance": "controlled"}
    raw = json.dumps(prior).encode()
    (tmp_path / "prior.json").write_bytes(raw)
    manifest = {"prior_consumer_state": {"file": "prior.json", "sha256": hashlib.sha256(raw).hexdigest()}}
    with pytest.raises(ValueError, match="expected_consumer_results"):
        load_p3_evidence(tmp_path / "manifest.json", manifest, [])


@pytest.mark.asyncio
async def test_real_worker_binding_one_batch_in_separate_process():
    adapter = ProductionWorkerAdapter()
    raw = await adapter.publish_recorded_batch(
        snapshots=[SnapshotRecord(symbol="BIND", last_trade_price=2.5)], reference_data=[])
    event = SnapshotBatchEvent.model_validate_json(raw)
    assert event.source_service == "market-data-gateway"
    assert event.payload.snapshots[0].symbol == "BIND"
    assert event.payload.snapshots[0].last_trade_price == 2.5
    assert event.payload.reference_data == []


@pytest.mark.asyncio
async def test_real_worker_run_binding_receipt_and_owned_cleanup():
    import asyncio

    receipts = multiprocessing.get_context("spawn").Queue(maxsize=128)
    adapter = ProductionWorkerAdapter()
    worker = None
    try:
        async with adapter.open_worker(
            settings=offline_settings(),
            snapshot_provider=CpuSnapshotProvider(receipts, count=3, cpu_ns=0),
            reference_cache=ControlledReferences(), redis_client=SnapshotReceiptRedis(receipts),
        ) as worker:
            pid = worker.pid
            assert pid != os.getpid()
            deadline = asyncio.get_running_loop().time() + 5
            observed = []
            while asyncio.get_running_loop().time() < deadline:
                try:
                    observed.append(receipts.get_nowait())
                except queue.Empty:
                    await asyncio.sleep(0.01)
                if any(row["kind"] == "publish" for row in observed):
                    break
            assert {row["kind"] for row in observed} == {"fetch", "publish", "heartbeat"}
            assert all(row["pid"] == pid for row in observed)
            assert all(row["count"] == 3 for row in observed if row["kind"] != "heartbeat")
        assert worker._closed is True
        assert pid not in [child.pid for child in multiprocessing.active_children()]
        while True:
            try:
                observed.append(receipts.get_nowait())
            except queue.Empty:
                break
        heartbeats = [row for row in observed if row["kind"] == "heartbeat"]
        assert {"starting", "stopping"} <= {row["status"] for row in heartbeats}
        assert len({row["runtime_instance_id"] for row in heartbeats}) == 1
    finally:
        receipts.close()
        receipts.join_thread()


@pytest.mark.asyncio
async def test_real_service_dispatch_reaches_real_scanner_without_external_io():
    event = SnapshotBatchEvent(
        source_service="market-data-gateway", produced_at=datetime(2026, 10, 22, 14, tzinfo=UTC),
        payload=SnapshotBatchPayload(snapshots=[SnapshotRecord(symbol="BIND", last_trade_price=2.5).to_payload()]),
    )
    rows = await consume_recorded_batches([event])
    assert len(rows) == 1
    assert rows[0]["warmup"]["history_cycles"] == 1
    assert rows[0]["confirmed"] == []
    assert rows[0]["alerts"] == []
    assert set(rows[0]["subscriptions"]) == {"market_data_symbols", "schwab_stream_symbols"}
