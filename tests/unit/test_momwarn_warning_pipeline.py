import asyncio
from dataclasses import replace
import time

import pytest

from project_mai_tai.market_data.publisher import MarketDataPublisher, WarningBatchWriteError
from tests.unit.test_momwarn1_gateway import aggregate, service


@pytest.mark.asyncio
async def test_bounded_pipeline_preserves_stream_order_fields_and_one_roundtrip():
    svc, provider, _, redis = service()
    template = provider._normalize_warning_aggregate(aggregate(), 'WFF')
    assert template is not None
    records = tuple((index+1, replace(template, symbol=f'X{index}')) for index in range(32))
    publisher = MarketDataPublisher(redis, 'test', 'market-data-gateway')
    assert await publisher.publish_momentum_warning_batch(records, dropped_total=3,
        gateway_instance_id='controlled', generation=5) == 32
    assert [item[1]['sequence'] for item in redis.entries] == list(range(1, 33))
    assert all(item[0] == 'test:momentum-warning-bars' and item[2] == {'maxlen': 20000, 'approximate': False}
               for item in redis.entries)
    assert all(item[1]['dropped_total'] == 3 and item[1]['generation'] == 5
               and item[1]['gateway_instance_id'] == 'controlled' for item in redis.entries)
    with pytest.raises(ValueError, match='bound'):
        await publisher.publish_momentum_warning_batch(records+records[:1], dropped_total=3,
            gateway_instance_id='controlled', generation=5)


@pytest.mark.asyncio
async def test_partial_reply_never_retries_or_counts_failed_rows(monkeypatch):
    svc, provider, _, redis = service()
    template = provider._normalize_warning_aggregate(aggregate(), 'WFF')
    original = redis.xadd
    calls = 0
    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError('controlled failed command')
        return await original(*args, **kwargs)
    monkeypatch.setattr(redis, 'xadd', fail_second)
    publisher = MarketDataPublisher(redis, 'test', 'market-data-gateway')
    with pytest.raises(WarningBatchWriteError) as failure:
        await publisher.publish_momentum_warning_batch(tuple((i+1, template) for i in range(3)),
            dropped_total=0, gateway_instance_id='controlled', generation=1)
    assert failure.value.acknowledged == 2 and calls == 3
    assert [item[1]['sequence'] for item in redis.entries] == [1, 3]


@pytest.mark.asyncio
async def test_pipeline_keeps_old_age_gate_and_exact_drop_accounting():
    svc, provider, _, redis = service()
    template = provider._normalize_warning_aggregate(aggregate(), 'WFF')
    svc._warning_active = True
    for _ in range(35):
        svc._enqueue_warning(replace(template, received_monotonic_ns=time.monotonic_ns()-2_100_000_000))
    await svc._warning_publish_loop(asyncio.Event())
    assert svc._warning_revert_reason == 'publish_lag'
    assert svc._warning_drop_count == 35 and svc._warning_published == 0
    assert not redis.entries and svc._warning_queue.empty()


@pytest.mark.asyncio
async def test_pipeline_partial_revert_counts_acknowledged_and_retired_once(monkeypatch):
    svc, provider, _, redis = service()
    template = provider._normalize_warning_aggregate(aggregate(), 'WFF')
    svc._warning_active = True
    for _ in range(3):
        svc._enqueue_warning(template)
    calls = 0
    original = redis.xadd
    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError('controlled failed command')
        return await original(*args, **kwargs)
    redis.xadd = fail_second
    await svc._warning_publish_loop(asyncio.Event())
    assert svc._warning_revert_reason == 'publish_error'
    assert calls == 3 and svc._warning_published == 2 and svc._warning_drop_count == 1
    assert svc._warning_ack_unknown == 1
    assert [item[1]['sequence'] for item in redis.entries] == [1, 3]
