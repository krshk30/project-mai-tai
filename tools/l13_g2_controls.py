"""Opt-in L13 controls. No provider, Redis server, or database connection is used.

The G1 adapter must invoke the production snapshot worker, not a replacement
test loop. Its interface is documented in tools/l13_g2/README.md.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import fields
from datetime import UTC, datetime
import hashlib
import importlib
import json
import logging
import math
import multiprocessing
import os
from pathlib import Path
import queue
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID
from zoneinfo import ZoneInfo

from project_mai_tai.events import HeartbeatEvent, ReferenceDataPayload, SnapshotBatchEvent
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider, MassiveTradeStream
from project_mai_tai.market_data.models import SnapshotRecord
from project_mai_tai.market_data.publisher import MarketDataPublisher
from project_mai_tai.settings import Settings

RATE = 240
SECONDS = 60
EVENTS = RATE * SECONDS
SNAPSHOTS = 13_000
DECODE_CPU_NS = 350_000_000
LEGACY_PUBLISHER_SHA256 = "7deeccbce10266043dd4ad118b706d57ee1558ccb3f9eae2f616e51056e59517"


def percentile_nearest_rank(values, percentile):
    if not values or not 0 < percentile <= 100:
        raise ValueError("nonempty observations and percentile in (0,100] required")
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * percentile / 100) - 1]


def assert_p1(metrics):
    assert metrics["elapsed_seconds"] >= SECONDS
    assert metrics["received"] == metrics["sent"] == EVENTS
    assert metrics["received"] / metrics["elapsed_seconds"] >= 200
    assert metrics["unique_sip_count"] == EVENTS
    assert metrics["negative_lags"] == 0
    assert metrics["lag_p99_ms"] < 300
    assert metrics["cycle_lag_p99_ms"] < 300
    assert metrics["cycle_trade_count"] > 0
    assert len(metrics["snapshot_cycles"]) >= 8
    assert metrics["loop_stall_max_ms"] < 50
    assert metrics["producer_alive_after_cleanup"] is False
    assert metrics["pending_gateway_trades"] == 0
    assert metrics["pending_frames"] == 0
    assert metrics["task_errors"] == []
    receipts = [row for row in metrics["worker_receipts"]
                if metrics["window_start_ns"] <= row["start_ns"]
                and row["end_ns"] <= metrics["window_end_ns"]]
    fetches = [row for row in receipts if row["kind"] == "fetch"]
    publishes = [row for row in receipts if row["kind"] == "publish"]
    assert len(fetches) >= 8 and len(publishes) >= 8
    assert all(row["pid"] != metrics["gateway_pid"] for row in receipts)
    assert all(row["count"] == SNAPSHOTS for row in receipts)
    assert all(row["cpu_ns"] >= DECODE_CPU_NS for row in fetches)
    assert all(row["overlap_trades"] > 0 for row in fetches)
    heartbeats = metrics["worker_heartbeats"]
    assert {"starting", "stopping"} <= {row["status"] for row in heartbeats}
    assert len({row["runtime_instance_id"] for row in heartbeats}) == 1
    assert all(row["pid"] == row["runtime_pid"] != metrics["gateway_pid"] for row in heartbeats)
    assert {row["pid"] for row in heartbeats} == {row["pid"] for row in fetches}
    healthy = [row for row in heartbeats if row["status"] == "healthy"
               and metrics["window_start_ns"] <= row["observed_ns"] <= metrics["window_end_ns"]]
    assert len(healthy) >= 3, "actual15s worker heartbeat load must remain present during60s P1"


def load_adapter(spec):
    module, separator, name = spec.partition(":")
    if not separator or not module or not name or "." in name:
        raise ValueError("G1 adapter must be explicit module:factory")
    return getattr(importlib.import_module(module), name)()


def offline_settings():
    # Construction only, outside measured work: do not load operator credentials
    # or feature switches into the controlled child via BaseSettings sources.
    with patch.dict(os.environ, {}, clear=True):
        return Settings(
            _env_file=None,
            massive_api_key=None,
            redis_url="redis://127.0.0.1:1/0",
            redis_stream_prefix="l13-controlled",
            market_data_momentum_warning_enabled=False,
            market_data_snapshot_interval_seconds=5,
            market_data_reference_refresh_interval_seconds=0,
            market_data_warmup_enabled=False,
            news_enabled=False,
            strategy_macd_30s_enabled=False,
            strategy_macd_1m_enabled=False,
            strategy_polygon_30s_enabled=False,
            strategy_schwab_1m_enabled=False,
            strategy_schwab_1m_v2_enabled=False,
            strategy_runner_enabled=False,
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            scanner_confirmed_capture_enabled=False,
        )


class CpuSnapshotProvider:
    """Picklable input for the actual worker; decode uses CPU, never sleep."""

    def __init__(self, receipts, *, count=SNAPSHOTS, cpu_ns=DECODE_CPU_NS):
        self.receipts = receipts
        self.cpu_ns = cpu_ns
        self.wire = json.dumps([
            {"ticker": f"CPU{index}", "day": {"close": 2.5, "volume": 100_000},
             "min": {"close": 2.5, "accumulated_volume": 100_000},
             "prev_day": {"close": 2.0}, "last_trade": {"price": 2.5}}
            for index in range(count)
        ])

    def fetch_all_snapshots(self):
        start = time.monotonic_ns()
        cpu_start = time.thread_time_ns()
        normalizer = object.__new__(MassiveSnapshotProvider)
        while True:
            decoded = json.loads(self.wire, object_hook=lambda row: SimpleNamespace(**row))
            snapshots = [normalizer._normalize_snapshot(row) for row in decoded]
            if time.thread_time_ns() - cpu_start >= self.cpu_ns:
                break
        self.receipts.put_nowait({
            "kind": "fetch", "pid": os.getpid(), "thread_id": threading.get_ident(),
            "start_ns": start, "end_ns": time.monotonic_ns(),
            "cpu_ns": time.thread_time_ns() - cpu_start, "count": len(snapshots),
        })
        return snapshots


class ControlledReferences:
    def load_from_cache(self):
        return True

    def ticker_count(self):
        return SNAPSHOTS

    def as_payloads(self, symbols):
        return [ReferenceDataPayload(symbol=symbol, shares_outstanding=1_000_000,
                                     avg_daily_volume=100_000) for symbol in symbols]


def validate_worker_heartbeat(encoded, kwargs):
    """Validate and retain a DISTINCT worker identity; never count as a batch."""
    event = HeartbeatEvent.model_validate_json(encoded)
    payload = event.payload
    details = payload.details
    assert event.source_service == payload.service_name == "market-data-snapshot-worker"
    assert details["role"] == "independent_snapshot_worker"
    assert details["runtime_pid"] == str(os.getpid())
    instance = UUID(details["runtime_instance_id"])
    assert str(instance) == details["runtime_instance_id"]
    assert payload.instance_name == f"{socket.gethostname()}:{os.getpid()}:{instance}"
    started = datetime.fromisoformat(details["process_started_at"].replace("Z", "+00:00"))
    assert started.tzinfo is not None and started <= event.produced_at
    assert details["reference_ready"] in {"true", "false"}
    assert int(details["last_snapshot_symbols"]) >= 0
    assert float(details["snapshot_progress_bound_seconds"]) > 0
    if payload.status == "healthy":
        assert details["reference_ready"] == "true"
        completed = datetime.fromisoformat(details["last_snapshot_completed_at"].replace("Z", "+00:00"))
        assert completed.tzinfo is not None and started <= completed <= event.produced_at
        assert 0 <= float(details["last_snapshot_age_seconds"]) <= float(details["snapshot_progress_bound_seconds"])
    assert kwargs["maxlen"] >= 10 and kwargs["approximate"] is True
    return {"kind": "heartbeat", "pid": os.getpid(), "thread_id": threading.get_ident(),
            "runtime_pid": int(details["runtime_pid"]), "runtime_instance_id": str(instance),
            "source_service": event.source_service, "service_name": payload.service_name,
            "status": payload.status, "observed_ns": time.monotonic_ns(),
            "produced_at": event.produced_at.isoformat(), "details": dict(details),
            "sha256": hashlib.sha256(encoded.encode()).hexdigest()}


def retain_worker_receipt(metrics, receipt):
    if receipt["kind"] == "heartbeat":
        metrics["worker_heartbeats"].append(receipt)
    elif receipt["kind"] in {"fetch", "publish"}:
        metrics["worker_receipts"].append(receipt)
    else:
        raise AssertionError("unexpected worker receipt is not silently ignored")


class SnapshotReceiptRedis:
    """Injected only in the snapshot child. Never decodes batches on trade loop."""

    def __init__(self, receipts):
        self.receipts = receipts

    async def xadd(self, stream, values, **kwargs):
        if stream == "l13-controlled:heartbeats":
            self.receipts.put_nowait(validate_worker_heartbeat(values["data"], kwargs))
            return "1-0"
        assert stream == "l13-controlled:snapshot-batches"
        encoded = values["data"]
        event = SnapshotBatchEvent.model_validate_json(encoded)
        assert event.source_service == "market-data-gateway"
        self.receipts.put_nowait({
            "kind": "publish", "pid": os.getpid(), "thread_id": threading.get_ident(),
            "start_ns": time.monotonic_ns(), "end_ns": time.monotonic_ns(),
            "count": len(event.payload.snapshots),
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "maxlen": kwargs["maxlen"], "approximate": kwargs["approximate"],
        })
        return "1-0"

    async def aclose(self):
        return None

    async def hset(self, name, *, mapping):
        assert name == "l13-controlled:snapshot-worker-health"
        assert set(mapping) == {"reference_tickers", "snapshots_without_reference", "last_snapshot_symbols"}
        assert all(int(value) >= 0 for value in mapping.values())


class CaptureSnapshotRedis:
    def __init__(self):
        self.encoded = None
        self.heartbeats = []
        self.snapshot_publications = 0

    async def xadd(self, stream, values, **kwargs):
        if stream == "l13-controlled:heartbeats":
            assert len(self.heartbeats) < 16, "one-batch capture heartbeat bound"
            self.heartbeats.append(validate_worker_heartbeat(values["data"], kwargs))
            return "1-0"
        assert stream == "l13-controlled:snapshot-batches"
        assert self.encoded is None, "one recorded input must publish exactly once"
        event = SnapshotBatchEvent.model_validate_json(values["data"])
        assert event.source_service == "market-data-gateway"
        self.encoded = values["data"]
        self.snapshot_publications += 1
        return "1-0"

    async def hset(self, name, *, mapping):
        assert name == "l13-controlled:snapshot-worker-health"
        assert set(mapping) == {"reference_tickers", "snapshots_without_reference", "last_snapshot_symbols"}

    async def aclose(self):
        return None


@contextmanager
def recorded_snapshot_completion(completed_at):
    """Opt-in replay constructor input, never rewrite already-encoded payloads."""
    if completed_at is None:
        yield
        return
    if completed_at.utcoffset() is None:
        raise ValueError("recorded completion must be timezone-aware")
    from project_mai_tai.market_data import publisher

    original = publisher.SnapshotBatchPayload

    def construct(*args, **kwargs):
        if "completed_at" in kwargs:
            raise AssertionError("publisher completion contract changed; review replay binding")
        return original(*args, completed_at=completed_at, **kwargs)

    with patch.object(publisher, "SnapshotBatchPayload", construct):
        yield


def _production_worker_child(settings, provider, references, redis, stop, sender, recorded,
                             recorded_completed_at=None):
    """Spawn target: actual G1 service; no test replica of its scheduling loop."""
    from project_mai_tai.market_data.snapshot_worker import SnapshotWorkerService
    from project_mai_tai.log import configure_logging

    configure_logging("market-data-snapshot-worker", settings.log_level)

    async def child_main():
        worker = SnapshotWorkerService(settings, redis, snapshot_provider=provider,
                                       reference_cache=references)
        if recorded is not None:
            try:
                with recorded_snapshot_completion(recorded_completed_at):
                    count = await worker.publish_snapshot_batch_once(recorded)
                sender.send({"kind": "batch", "pid": os.getpid(), "count": count,
                             "encoded": redis.encoded, "worker_heartbeats": redis.heartbeats,
                             "snapshot_publications": redis.snapshot_publications})
            finally:
                await redis.aclose()
            return
        local_stop = asyncio.Event()

        async def relay_stop():
            while not stop.is_set():
                await asyncio.sleep(0.02)
            local_stop.set()

        relay = asyncio.create_task(relay_stop())
        try:
            sender.send({"kind": "ready", "pid": os.getpid()})
            await worker.run(local_stop)
        finally:
            relay.cancel()
            await asyncio.gather(relay, return_exceptions=True)

    try:
        asyncio.run(child_main())
    except BaseException as exc:
        sender.send({"kind": "error", "error_type": type(exc).__name__, "pid": os.getpid()})
        raise
    finally:
        sender.close()


class RecordedReferences(ControlledReferences):
    def __init__(self, rows):
        self.rows = rows

    def ticker_count(self):
        return len(self.rows)

    def as_payloads(self, symbols):
        wanted = set(symbols)
        return [row for row in self.rows if row.symbol in wanted]


class ProductionWorkerAdapter:
    """Test binding to frozen G1 API, not a production replacement supervisor."""

    @staticmethod
    async def _receive(connection, process, timeout=10):
        deadline = asyncio.get_running_loop().time() + timeout
        while not connection.poll():
            if not process.is_alive():
                raise RuntimeError(f"controlled worker exited before receipt: {process.exitcode}")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("controlled worker receipt deadline")
            await asyncio.sleep(0.01)
        # A large P3 batch may be mid-send; do not block the consumer loop on recv.
        return await asyncio.wait_for(asyncio.to_thread(connection.recv), timeout)

    @staticmethod
    async def _join(process, stop):
        stop.set()
        deadline = asyncio.get_running_loop().time() + 5
        while process.is_alive() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        if process.is_alive():
            process.terminate()  # Only the test-owned child, on failed cleanup.
            await asyncio.to_thread(process.join, 2)
            raise RuntimeError("owned snapshot worker required forced cleanup")
        process.join(timeout=0)
        if process.exitcode != 0:
            raise RuntimeError(f"owned snapshot worker exit={process.exitcode}")

    @asynccontextmanager
    async def open_worker(self, *, settings, snapshot_provider, reference_cache, redis_client):
        context = multiprocessing.get_context("spawn")
        stop = context.Event()
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_production_worker_child,
                                  args=(settings, snapshot_provider, reference_cache, redis_client,
                                        stop, sender, None), name="l13-controlled-production-worker")
        process.start()
        sender.close()
        try:
            ready = await self._receive(receiver, process)
            assert ready["kind"] == "ready" and ready["pid"] == process.pid != os.getpid()
            yield process
        finally:
            try:
                await self._join(process, stop)
            finally:
                receiver.close()
                if not process.is_alive():
                    process.close()

    async def publish_recorded_batch(self, *, snapshots, reference_data, completed_at=None):
        context = multiprocessing.get_context("spawn")
        stop = context.Event()
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_production_worker_child,
                                  args=(offline_settings(), object(), RecordedReferences(reference_data),
                                        CaptureSnapshotRedis(), stop, sender, snapshots, completed_at),
                                  name="l13-recorded-production-worker")
        process.start()
        sender.close()
        try:
            result = await self._receive(receiver, process)
            assert result["kind"] == "batch" and result["pid"] == process.pid != os.getpid()
            assert result["count"] == len(snapshots)
            assert result["snapshot_publications"] == 1
            self.last_recorded_heartbeats = result["worker_heartbeats"]
            return result["encoded"]
        finally:
            try:
                await self._join(process, stop)
            finally:
                receiver.close()
                if not process.is_alive():
                    process.close()


def datetime_epoch_ns(value):
    if value.utcoffset() is None:
        raise ValueError("produced_at timezone required; never assume local time")
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000_000
            + delta.microseconds * 1_000)


class TradeSink:
    def __init__(self):
        self.sips = []
        self.lags_ms = []
        self.ack_times = []

    async def xadd(self, stream, values, **kwargs):
        assert stream == "l13-controlled:market-data"
        event = json.loads(values["data"])
        assert event["event_type"] == "trade_tick"
        sip_ns = event["payload"]["timestamp_ns"]
        produced = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
        self.sips.append(sip_ns)
        self.lags_ms.append((datetime_epoch_ns(produced) - sip_ns) / 1e6)
        self.ack_times.append(time.monotonic_ns())
        return f"{len(self.sips)}-0"


async def exercise_p1(adapter, context, metrics):
    """Main-only runtime. All worker startup/cleanup remains adapter-owned."""
    settings = offline_settings()
    receipts = context.Queue(maxsize=128)
    sink = TradeSink()
    stream = MassiveTradeStream("controlled-offline-not-a-key")
    gateway = MarketDataGatewayService(
        settings, sink, snapshot_provider=CpuSnapshotProvider(receipts),
        reference_cache=ControlledReferences(), trade_stream=stream,
    )
    stream._subscriptions = {"PONE"}
    loop = asyncio.get_running_loop()
    stream._on_trade = lambda record: loop.call_soon_threadsafe(gateway._enqueue_trade, record)
    stop = asyncio.Event()
    producer_stop = threading.Event()
    inbound = asyncio.Queue(maxsize=EVENTS)
    metrics.update(gateway_pid=os.getpid(), worker_receipts=[], worker_heartbeats=[], task_errors=[], sent=0)
    metrics["source_sha256"] = {
        name: hashlib.sha256(Path(importlib.import_module(
            "project_mai_tai.market_data." + name).__file__).read_bytes()).hexdigest()
        for name in ("gateway", "snapshot_worker", "publisher", "massive_provider")
    }
    tasks = []
    producer = None

    async def process_frames():
        while not stop.is_set():
            frame = await inbound.get()
            await stream._process_messages([frame])

    async def monitor():
        while not stop.is_set():
            due = time.monotonic_ns() + 1_000_000
            await asyncio.sleep(0.001)
            stalls.append(max(0, time.monotonic_ns() - due) / 1e6)

    def produce():
        try:
            for index in range(EVENTS):
                target = started_ns + index * 1_000_000_000 // RATE
                if producer_stop.wait(max(0, (target - time.monotonic_ns()) / 1e9)):
                    return
                frame = SimpleNamespace(event_type="T", symbol="PONE", price=2.5,
                                        size=1, sip_timestamp=wall_ns + target - started_ns,
                                        exchange=4, conditions=[])
                loop.call_soon_threadsafe(inbound.put_nowait, frame)
                metrics["sent"] += 1
        except BaseException as exc:
            metrics["task_errors"].append(f"producer:{type(exc).__name__}")

    stalls = []
    async def collect_receipts():
        while not stop.is_set():
            while True:
                try:
                    retain_worker_receipt(metrics, receipts.get_nowait())
                except queue.Empty:
                    break
            await asyncio.sleep(0.02)

    try:
        async with adapter.open_worker(
            settings=settings, snapshot_provider=CpuSnapshotProvider(receipts),
            reference_cache=ControlledReferences(), redis_client=SnapshotReceiptRedis(receipts),
        ):
            started_ns = time.monotonic_ns()
            wall_ns = time.time_ns()
            metrics["window_start_ns"] = started_ns
            metrics["sip_wall_anchor_ns"] = wall_ns
            tasks = [asyncio.create_task(process_frames()), asyncio.create_task(monitor()),
                     asyncio.create_task(gateway._stream_publish_loop(stop)),
                     asyncio.create_task(collect_receipts())]
            producer = threading.Thread(target=produce, name="l13-independent-sip", daemon=True)
            producer.start()
            await asyncio.sleep(SECONDS)
            deadline = loop.time() + 2
            while len(sink.sips) < EVENTS and loop.time() < deadline:
                await asyncio.sleep(0.001)
            metrics["elapsed_seconds"] = (time.monotonic_ns() - started_ns) / 1e9
            metrics["window_end_ns"] = time.monotonic_ns()
    finally:
        producer_stop.set()
        stop.set()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                metrics["task_errors"].append(type(result).__name__)
        if producer is not None:
            await asyncio.to_thread(producer.join, 2)
        metrics["producer_alive_after_cleanup"] = producer is not None and producer.is_alive()
        # The child has exited before draining its bounded, low-volume receipts.
        while True:
            try:
                receipt = receipts.get_nowait()
            except queue.Empty:
                break
            retain_worker_receipt(metrics, receipt)
        for receipt in metrics["worker_receipts"]:
            receipt["overlap_trades"] = sum(
                receipt["start_ns"] <= ack <= receipt["end_ns"] for ack in sink.ack_times)
        fetches = [row for row in metrics["worker_receipts"] if row["kind"] == "fetch"]
        cycles = []
        for publication in metrics["worker_receipts"]:
            if publication["kind"] != "publish":
                continue
            prior = [row for row in fetches if row["end_ns"] <= publication["start_ns"]]
            if not prior:
                continue
            fetch = max(prior, key=lambda row: row["end_ns"])
            if (metrics.get("window_start_ns", float("inf")) <= fetch["start_ns"]
                    and publication["end_ns"] <= metrics.get("window_end_ns", 0)):
                cycles.append({"start_ns": fetch["start_ns"], "end_ns": publication["end_ns"]})
        cycle_lags = []
        for sip, ack, lag in zip(sink.sips, sink.ack_times, sink.lags_ms):
            due = metrics["window_start_ns"] + sip - metrics["sip_wall_anchor_ns"]
            if any(row["start_ns"] <= due <= row["end_ns"]
                   or row["start_ns"] <= ack <= row["end_ns"] for row in cycles):
                cycle_lags.append(lag)
        metrics.update(snapshot_cycles=cycles, cycle_trade_count=len(cycle_lags),
                       cycle_lag_p99_ms=percentile_nearest_rank(cycle_lags, 99) if cycle_lags else None,
                       cycle_lag_samples_ms=cycle_lags)
        receipts.close()
        receipts.join_thread()
        metrics.update(received=len(sink.sips), unique_sip_count=len(set(sink.sips)),
                       negative_lags=sum(lag < 0 for lag in sink.lags_ms),
                       lag_p99_ms=percentile_nearest_rank(sink.lags_ms, 99) if sink.lags_ms else None,
                       loop_stall_max_ms=max(stalls, default=None),
                       pending_gateway_trades=gateway._trade_queue.qsize(),
                       pending_frames=inbound.qsize(), lag_samples_ms=sink.lags_ms,
                       loop_stall_samples_ms=stalls, sip_timestamps_ns=sink.sips,
                       publication_monotonic_ns=sink.ack_times)


def write_fresh_result(path, metrics):
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(metrics, output, indent=2, sort_keys=True, allow_nan=False)
        output.write("\n")


def load_recorded_packet(manifest_path):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("kind") != "recorded_snapshot_batches" or not manifest.get("provenance"):
        raise ValueError("recorded snapshot-batch provenance required; no synthetic fallback")
    data_path = manifest_path.parent / manifest["file"]
    if data_path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("recorded input exceeds 128 MiB bound")
    raw = data_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
        raise ValueError("recorded input SHA256 mismatch")
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        required = {"event_id", "event_type", "source_service", "produced_at", "payload"}
        payload_required = {"snapshots", "reference_data", "completed_at"}
        if not required <= item.keys() or not payload_required <= item["payload"].keys():
            raise ValueError("recorded envelope/header fields absent; no model-default timestamps")
        event = SnapshotBatchEvent.model_validate(item)
        if event.produced_at.tzinfo is None or event.payload.completed_at.tzinfo is None:
            raise ValueError("recorded timestamp timezone absent")
        rows.append(event)
    if not rows or len(rows) > 300:
        raise ValueError("recorded batch sequence requires 1..300 batches")
    times = [row.produced_at for row in rows]
    if any(after <= before for before, after in zip(times, times[1:])):
        raise ValueError("recorded batch times must increase; never backdate")
    return manifest, rows


def records_from_batch(event):
    names = {field.name for field in fields(SnapshotRecord)}
    return [SnapshotRecord(**{name: getattr(row, name) for name in names})
            for row in event.payload.snapshots]


def load_p3_evidence(manifest_path, manifest, rows):
    evidence = []
    for field, kind in (("prior_consumer_state", "recorded_scanner_prior_state"),
                        ("expected_consumer_results", "recorded_scanner_expected_results")):
        if field not in manifest:
            raise ValueError(f"P3 missing exact operand: {field}")
        binding = manifest[field]
        path = Path(manifest_path).parent / binding["file"]
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError(f"P3 {field} exceeds 16 MiB")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
            raise ValueError(f"P3 {field} SHA256 mismatch")
        item = json.loads(raw)
        if item.get("kind") != kind or not item.get("provenance"):
            raise ValueError(f"P3 {field} lacks recorded provenance")
        evidence.append(item)
    prior, expected = evidence
    asof = datetime.fromisoformat(prior["asof_utc"].replace("Z", "+00:00"))
    if asof.tzinfo is None or asof > rows[0].produced_at:
        raise ValueError("P3 prior consumer state is unknown or after the decision")
    if len(expected["batches"]) != len(rows):
        raise ValueError("P3 expected result coverage incomplete")
    for event, result in zip(rows, expected["batches"]):
        if result["source_event_id"] != str(event.event_id):
            raise ValueError("P3 expected result event identity mismatch")
        if not {"confirmed", "top_confirmed", "alerts", "emitted_alerts"} <= result.keys():
            raise ValueError("P3 expected confirmed/alert operands absent")
    return prior, expected


async def consume_recorded_batches(rows, prior=None):
    """Real service message dispatch and real scanners, controlled external I/O."""
    # Only historical scanner replay clocks/entrypoint logging setup are bound.
    # Neither perf-counter nor any P1 clock, GC, handler, or logger is changed.
    now = [rows[0].produced_at.astimezone(ZoneInfo("America/New_York"))]
    with ExitStack() as stack:
        for target in ("project_mai_tai.strategy_core.snapshot_utils.now_eastern",
                       "project_mai_tai.strategy_core.top_gainers.now_eastern",
                       "project_mai_tai.services.strategy_engine_app.now_eastern"):
            stack.enter_context(patch(target, lambda: now[0]))
        stack.enter_context(patch("project_mai_tai.services.strategy_engine_app.configure_logging",
                                  lambda name, level="INFO": logging.getLogger(name)))
        return await _consume_recorded_batches(rows, now, prior)


async def _consume_recorded_batches(rows, now, prior):
    from project_mai_tai.services.strategy_engine_app import (
        StrategyEngineService, current_scanner_session_start_utc,
    )
    from project_mai_tai.strategy_core import ReferenceData

    service = StrategyEngineService(settings=offline_settings(), redis_client=TradeSink(),
                                    now_provider=lambda: now[0])
    if prior is not None:
        required = {"alert_engine", "confirmed_tracking", "confirmed_candidates", "reference_data",
                    "recent_alerts", "today_alerts", "first_seen_by_ticker", "global_manual_stop_symbols",
                    "seeded_confirmed_pending_revalidation", "pending_recent_alert_replay"}
        if not required <= prior.keys():
            raise ValueError("P3 prior scanner operands incomplete; no empty/idle defaults")
        if not service.state.alert_engine.restore_state(prior["alert_engine"]):
            raise ValueError("P3 recorded alert warmup history absent/unreadable")
        service.state.confirmed_scanner._tracking = copy.deepcopy(prior["confirmed_tracking"])
        service.state.confirmed_scanner.seed_confirmed_candidates(prior["confirmed_candidates"])
        service.state.reference_data = {
            item["symbol"]: ReferenceData(shares_outstanding=item["shares_outstanding"],
                                          avg_daily_volume=float(item["avg_daily_volume"]))
            for item in prior["reference_data"]
        }
        service.state.recent_alerts = copy.deepcopy(prior["recent_alerts"])
        service.state.today_alerts = copy.deepcopy(prior["today_alerts"])
        service.state._first_seen_by_ticker = copy.deepcopy(prior["first_seen_by_ticker"])
        service.state.global_manual_stop_symbols = set(prior["global_manual_stop_symbols"])
        service.state._seeded_confirmed_pending_revalidation = prior["seeded_confirmed_pending_revalidation"]
        service.state._pending_recent_alert_replay = prior["pending_recent_alert_replay"]
        service.state._active_scanner_session_start = current_scanner_session_start_utc(
            datetime.fromisoformat(prior["asof_utc"].replace("Z", "+00:00")))
    service._preload_manual_stop_state = lambda: None
    service._load_schwab_ineligible_symbols_by_strategy = lambda: {}
    service._load_scanner_blacklist_symbols = lambda: set()
    subscriptions = []

    async def save_subscriptions(**targets):
        subscriptions.append(targets)

    async def no_persistence():
        return None

    service._sync_subscription_targets = save_subscriptions
    service._publish_strategy_state_snapshot = no_persistence
    emitted = []
    real_check_alerts = service.state.alert_engine.check_alerts

    def observed_check_alerts(*args, **kwargs):
        alerts = real_check_alerts(*args, **kwargs)
        emitted.append(copy.deepcopy(alerts))
        return alerts

    service.state.alert_engine.check_alerts = observed_check_alerts
    results = []
    for row in rows:
        now[0] = row.produced_at.astimezone(ZoneInfo("America/New_York"))
        await service._handle_stream_message("l13-controlled:snapshot-batches",
                                             {"data": row.model_dump_json()})
        results.append({
            "confirmed": service.state.all_confirmed,
            "top_confirmed": service.state.current_confirmed,
            "alerts": service.state.today_alerts,
            "emitted_alerts": emitted[-1],
            "recent_alerts": service.state.recent_alerts,
            "warmup": service.state.alert_warmup,
            "subscriptions": subscriptions[-1],
        })
        # State mutates across batches; preserve each decision, not final aliases.
        results[-1] = json.loads(json.dumps(results[-1], default=str))
        assert len(emitted) == len(results), "real alert engine must run exactly once per batch"
    return results


async def exercise_p3(adapter, manifest_path, metrics):
    manifest, recorded = load_recorded_packet(manifest_path)
    prior, expected = load_p3_evidence(manifest_path, manifest, recorded)
    import project_mai_tai.market_data.publisher as publisher_module

    if hashlib.sha256(Path(publisher_module.__file__).read_bytes()).hexdigest() != LEGACY_PUBLISHER_SHA256:
        raise ValueError("legacy publisher changed: need separately pinned690 publisher binding")
    legacy = []
    candidate = []
    for row in recorded:
        snapshots = records_from_batch(row)
        references = row.payload.reference_data
        encoded = MarketDataPublisher(None, "l13-controlled", "market-data-gateway")._encode_snapshot_batch(
            snapshots, references)
        before = SnapshotBatchEvent.model_validate_json(encoded)
        # Only the test consumer clock uses the original capture's decision time.
        before = before.model_copy(update={"produced_at": row.produced_at})
        after_raw = await adapter.publish_recorded_batch(snapshots=snapshots, reference_data=references)
        after = SnapshotBatchEvent.model_validate_json(after_raw)
        assert after.payload.snapshots == before.payload.snapshots
        assert after.payload.reference_data == before.payload.reference_data
        candidate.append(after.model_copy(update={"produced_at": row.produced_at}))
        legacy.append(before)
    old_results = await consume_recorded_batches(legacy, prior)
    new_results = await consume_recorded_batches(candidate, prior)
    metrics.update(input_sha256=manifest["sha256"], provenance=manifest["provenance"],
                   batches=len(recorded), source_event_ids=[str(row.event_id) for row in recorded],
                   before=old_results, after=new_results)
    assert old_results == new_results
    for actual, witness in zip(old_results, expected["batches"]):
        for key in ("confirmed", "top_confirmed", "alerts", "emitted_alerts"):
            assert actual[key] == witness[key], f"recorded scanner expectation differs: {key}"
    assert any(row["confirmed"] for row in old_results), "recorded confirmed witness missing"
    assert old_results[-1]["warmup"]["fully_ready"] is True, "recorded 10-minute warmup absent"
    rendered = json.dumps([row["emitted_alerts"] for row in old_results])
    assert "SQUEEZE_5MIN" in rendered, "recorded 5-minute alert witness missing"
    assert "SQUEEZE_10MIN" in rendered, "recorded 10-minute alert witness missing"
