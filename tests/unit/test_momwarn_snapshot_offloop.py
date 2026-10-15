"""Snapshot encoding isolation; no provider or broker connections."""
import asyncio
from dataclasses import fields, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import threading
import time

import pytest

from project_mai_tai.market_data.models import QuoteTickRecord, SnapshotRecord, TradeTickRecord
from project_mai_tai.events import ReferenceDataPayload, SnapshotBatchEvent, SnapshotBatchPayload
from project_mai_tai.market_data import gateway
from tests.unit.test_momwarn1_gateway import aggregate, service
# Keep A publication inside its real window while testing snapshot isolation.
from tests.unit.test_momwarn1_gateway import pm_clock as pm_clock


@pytest.mark.asyncio
async def test_whole_snapshot_encoding_runs_offloop_and_keeps_payload(monkeypatch):
    svc, _, redis, _ = service()
    main_thread = threading.get_ident()
    original = svc.publisher._encode_snapshot_batch
    threads = []

    def encode(*args):
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(svc.publisher, '_encode_snapshot_batch', encode)
    row = SnapshotRecord(symbol='UGRO', previous_close=2.10, day_close=2.35,
                         day_volume=900_000, last_trade_price=2.36)
    assert await svc.publish_snapshot_batch_once([row]) == 1
    assert threads and all(identity != main_thread for identity in threads)
    stream, event, options = redis.entries[-1]
    assert stream == 'test:snapshot-batches'
    assert event['payload']['snapshots'] == [row.to_payload().model_dump(mode='json')]
    assert event['payload']['reference_data'][0]['symbol'] == 'UGRO'
    assert options['maxlen'] == 120 and options['approximate'] is True


@pytest.mark.asyncio
async def test_reference_payload_build_runs_offloop(monkeypatch):
    svc, _, _, _ = service()
    main_thread = threading.get_ident()
    original = svc.reference_cache.as_payloads
    threads = []

    def build(symbols):
        threads.append(threading.get_ident())
        return original(symbols)

    monkeypatch.setattr(svc.reference_cache, 'as_payloads', build)
    await svc.publish_snapshot_batch_once([SnapshotRecord(symbol='UGRO')])
    assert threads and threads[0] != main_thread


@pytest.mark.asyncio
async def test_slow_snapshot_build_does_not_hold_A_or_watched_trade(monkeypatch):
    svc, provider, redis, warning_redis = service()
    started, release = threading.Event(), threading.Event()
    original = svc.publisher._encode_snapshot_batch

    def held_encode(*args):
        started.set()
        if not release.wait(2):
            raise RuntimeError('controlled worker barrier not released')
        return original(*args)

    monkeypatch.setattr(svc.publisher, '_encode_snapshot_batch', held_encode)
    svc._warning_active = True
    stop = asyncio.Event()
    writer = asyncio.create_task(svc._warning_publish_loop(stop))
    snapshot = asyncio.create_task(svc.publish_snapshot_batch_once([SnapshotRecord(symbol='UGRO')]))
    try:
        assert await asyncio.to_thread(started.wait, .5)
        record = provider._normalize_warning_aggregate(aggregate(), 'WFF')
        assert record is not None
        svc._enqueue_warning(record)
        trade = TradeTickRecord(symbol='UGRO', price=2.36, size=10, timestamp_ns=time.time_ns())
        await asyncio.wait_for(svc.publisher.publish_trade_tick(trade), timeout=.2)
        deadline = time.monotonic() + .2
        while not warning_redis.entries and time.monotonic() < deadline:
            await asyncio.sleep(.001)
        assert warning_redis.entries and svc._warning_published == 1
        assert svc._warning_revert_reason is None and svc._warning_queue_drop_count == 0
        assert any(row[1]['event_type'] == 'trade_tick' for row in redis.entries)
        assert not snapshot.done()
    finally:
        release.set()
        await snapshot
        stop.set()
        await writer


@pytest.mark.asyncio
async def test_encoder_failure_does_not_publish_partial_batch_or_hold_quote_path(monkeypatch):
    svc, _, redis, _ = service()

    def fail(*args):
        raise ValueError('controlled invalid snapshot')

    monkeypatch.setattr(svc.publisher, '_encode_snapshot_batch', fail)
    with pytest.raises(ValueError, match='invalid snapshot'):
        await svc.publish_snapshot_batch_once([SnapshotRecord(symbol='UGRO')])
    assert not redis.entries
    await asyncio.wait_for(svc.publisher.publish_quote_tick(
        QuoteTickRecord(symbol='UGRO', bid_price=2.35, ask_price=2.36)), timeout=.2)
    assert len(redis.entries) == 1 and redis.entries[0][1]['event_type'] == 'quote_tick'


@pytest.mark.asyncio
async def test_retained_whole_batch_content_and_A_TQ_progress(record_property, monkeypatch):
    selected = os.environ.get('MOMWARN_RETAINED_SNAPSHOT')
    if not selected:
        pytest.skip('retained full batch not selected; no invented native payload')
    batch = json.loads(Path(selected).read_bytes())['event']['payload']
    names = {item.name for item in fields(SnapshotRecord)}
    float_fields = {'previous_close', 'day_close', 'day_high', 'day_vwap', 'minute_close',
                    'minute_high', 'minute_vwap', 'last_trade_price', 'bid_price', 'ask_price',
                    'todays_change_percent'}
    snapshots = [SnapshotRecord(**{name: float(value) if name in float_fields and value is not None else value
                                   for name, value in row.items() if name in names})
                 for row in batch['snapshots']]
    refs = [ReferenceDataPayload.model_validate(row) for row in batch['reference_data']]
    assert len(snapshots) == 13269 and len(refs) == 3184
    expected = SnapshotBatchEvent(source_service='market-data-gateway', payload=SnapshotBatchPayload(
        snapshots=[row.to_payload() for row in snapshots], reference_data=refs)).model_dump(mode='json')
    svc, provider, redis, warning_redis = service()
    # Use the actual publication path but do not add fake six-MB JSON decoding
    # to the measured gateway loop. The real client sends the encoded string.
    original_xadd = redis.xadd
    captured = []
    async def capture_snapshot(stream, data, **options):
        if stream.endswith(':snapshot-batches'):
            captured.append((data['data'], options))
            await asyncio.sleep(0)
            return 'controlled-snapshot'
        return await original_xadd(stream, data, **options)
    monkeypatch.setattr(redis, 'xadd', capture_snapshot)
    monkeypatch.setattr(svc.reference_cache, 'as_payloads', lambda symbols: refs)
    monkeypatch.setattr(gateway, 'datetime', type('Clock', (), {'now': staticmethod(
        lambda zone: datetime(2026, 10, 13, 18, 18, tzinfo=UTC))}))
    svc._warning_active = True
    stop = asyncio.Event()
    writer = asyncio.create_task(svc._warning_publish_loop(stop))
    template = provider._normalize_warning_aggregate(aggregate(), 'WFF')
    assert template is not None
    A_lags = []
    original_A = warning_redis.xadd
    async def measured_A(*args, **kwargs):
        result = await original_A(*args, **kwargs)
        A_lags.append((time.monotonic_ns()-svc._warning_inflight.received_monotonic_ns)/1e6)
        return result
    warning_redis.xadd = measured_A
    trade_lags, quote_lags = [], []
    try:
        snapshot = asyncio.create_task(svc.publish_snapshot_batch_once(snapshots))
        for _ in range(240):
            svc._enqueue_warning(replace(template, received_utc_ns=time.time_ns(),
                                         received_monotonic_ns=time.monotonic_ns()))
            started = time.monotonic_ns()
            await svc.publisher.publish_trade_tick(TradeTickRecord(
                symbol='WFF', price=13.35, size=100, timestamp_ns=time.time_ns()))
            trade_lags.append((time.monotonic_ns()-started)/1e6)
            started = time.monotonic_ns()
            await svc.publisher.publish_quote_tick(QuoteTickRecord(
                symbol='WFF', bid_price=13.34, ask_price=13.35))
            quote_lags.append((time.monotonic_ns()-started)/1e6)
            await asyncio.sleep(1/240)
        await snapshot
        deadline = time.monotonic()+1
        while svc._warning_published < 240 and time.monotonic() < deadline:
            await asyncio.sleep(.001)
        assert svc._warning_published == 240 and len(A_lags) == 240
        assert svc._warning_revert_reason is None
        assert svc._warning_queue_drop_count == svc._warning_drop_count == 0
        assert max(A_lags) < 1000
        assert len(captured) == 1
        actual = json.loads(captured[0][0])
        assert actual['payload']['snapshots'] == expected['payload']['snapshots']
        assert actual['payload']['reference_data'] == expected['payload']['reference_data']
        assert actual['source_service'] == expected['source_service']
        assert captured[0][1] == {'maxlen': 120, 'approximate': True}
        record_property('snapshot_A_TQ', json.dumps(dict(
            scope='controlled local Redis writes; current retained payload, not historical frame or provider arrival',
            snapshots=len(snapshots), A=len(A_lags), A_max_completion_ms=max(A_lags),
            T=len(trade_lags), T_max_handler_ms=max(trade_lags), Q=len(quote_lags),
            Q_max_handler_ms=max(quote_lags), queue_drops=svc._warning_queue_drop_count)))
    finally:
        stop.set()
        await writer
