"""MOMLATCH1 d/e controls. Policy adapter is NOT completed gateway integration."""

import asyncio
from dataclasses import fields
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from project_mai_tai.events import ReferenceDataPayload
from project_mai_tai.market_data import gateway, massive_provider
from project_mai_tai.market_data.models import QuoteTickRecord, SnapshotRecord, TradeTickRecord
from tests.unit.test_momwarn1_gateway import aggregate, event, healthy, service


@pytest.fixture
def clock(monkeypatch):
    fixed = SimpleNamespace(now=lambda _: datetime(2026, 10, 13, 20, 1, tzinfo=UTC))
    monkeypatch.setattr(gateway, 'datetime', fixed)
    monkeypatch.setattr(massive_provider, 'datetime', fixed)


@pytest.fixture
def policy():
    source = os.environ.get('MOMLATCH_POLICY_SOURCE')
    if source:
        spec = importlib.util.spec_from_file_location('controlled_external_pause_policy', source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        module = pytest.importorskip('project_mai_tai.market_data.momwarn_pause_policy',
                                    reason='policy not composed; no replacement implementation')
    return module.WarningPausePolicy(clock_ns=lambda: 1)


async def apply_policy(svc, policy, **observation):
    """Test-only proposed action adapter. Main must also pass monitor integration controls."""
    reason = observation.get('reason')
    if reason is not None:
        classification = policy.trip(reason)
        action = classification if svc._warning_active else 'none'
        svc._warning_active = False
        await svc.trade_stream.set_momentum_warning_aggregates(False)
    elif observation.get('ready') and policy.clean() and not svc._warning_active:
        action = 'resume'
        svc._warning_active = await svc.trade_stream.set_momentum_warning_aggregates(True)
    else:
        action = 'none'
    return SimpleNamespace(action=action, state=policy.state)


@pytest.mark.asyncio
async def test_policy_actions_real_provider_FIFO_no_double_SUB_and_TQ_owners(clock, policy):
    svc, provider, _, warning_redis = service()
    await svc.apply_subscription_event(event('v2', ['WFF']))
    await svc.apply_subscription_event(event('momentum-warning', ['WFF', 'LGHL']))
    owners = svc.active_symbols(), set(provider._subscriptions), set(provider._warning_trade_symbols)
    for _ in range(3):
        await apply_policy(svc, policy, ready=True)
    assert svc._warning_active and provider._ws.added.count('A.*') == 1
    stop = asyncio.Event()
    writer = asyncio.create_task(svc._warning_publish_loop(stop))
    try:
        for cycle in range(2):
            provider._handle_messages([aggregate(sym='LGHL', c=(3.8, 3.9)[cycle], h=4, l=3.3, o=3.4)])
            for _ in range(200):
                if len(warning_redis.entries) == cycle + 1:
                    break
                await asyncio.sleep(0)
            assert len(warning_redis.entries) == cycle + 1
            assert (await apply_policy(svc, policy, reason='monitor_unreadable')).action == 'pause'
            assert not provider._warning_aggregates_enabled and svc._warning_revert_reason is None
            for _ in range(2):
                assert (await apply_policy(svc, policy, reason='monitor_unreadable')).action == 'none'
            for index in range(3):
                decision = await apply_policy(svc, policy, ready=True)
                assert decision.action == ('resume' if index == 2 else 'none')
            for _ in range(2):
                await apply_policy(svc, policy, ready=True)
        bars = [row[1] for row in warning_redis.entries]
        assert [row['sequence'] for row in bars] == [1, 2]
        assert [row['close'] for row in bars] == [3.8, 3.9]
        assert provider._ws.added.count('A.*') == 3 and provider._ws.removed.count('A.*') == 2
        assert owners == (svc.active_symbols(), set(provider._subscriptions), set(provider._warning_trade_symbols))
        assert 'T.*' not in provider._ws.added and 'Q.*' not in provider._ws.added
        assert not any(name.startswith(('T.', 'Q.')) for name in provider._ws.removed)
    finally:
        stop.set()
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


@pytest.mark.asyncio
async def test_main_monitor_contract_noise_three_clean_no_duplicate_SUB(clock):
    svc, provider, _, redis = service()
    await svc.apply_subscription_event(event('v2', ['WFF']))
    for _ in range(3):
        await svc._warning_monitor_once()
    assert svc._warning_active
    stop = asyncio.Event()
    writer = asyncio.create_task(svc._warning_publish_loop(stop))
    try:
        provider._handle_messages([aggregate(c=13.35)])
        for _ in range(200):
            if redis.entries:
                break
            await asyncio.sleep(0)
        assert len(redis.entries) == 1
        def unavailable():
            raise PermissionError('CONTROLLED original first-trip class; not a native successor read')
        svc._warning_health_reader = SimpleNamespace(read=unavailable)
        await svc._warning_monitor_once()
        assert svc._warning_active and svc._warning_revert_reason is None
        assert provider._ws.removed.count('A.*') == 0
        svc._warning_health_reader = SimpleNamespace(read=healthy)
        for index in range(3):
            await svc._warning_monitor_once()
            assert svc._warning_active
        for _ in range(3):
            await svc._warning_monitor_once()
        provider._handle_messages([aggregate(c=13.4)])
        for _ in range(200):
            if len(redis.entries) == 2:
                break
            await asyncio.sleep(0)
        assert [row[1]['sequence'] for row in redis.entries] == [1, 2]
        assert [row[1]['close'] for row in redis.entries] == [13.35, 13.4]
        assert provider._ws.added.count('A.*') == 1 and provider._ws.removed.count('A.*') == 0
        assert provider._subscriptions == {'WFF'} and 'T.WFF' not in provider._ws.removed
    finally:
        stop.set()
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


@pytest.mark.asyncio
async def test_pause_does_not_remove_watched_trade_quote_feed(clock, policy):
    svc, provider, redis, _ = service()
    await svc.apply_subscription_event(event('v2', ['WFF']))
    for _ in range(3):
        await apply_policy(svc, policy, ready=True)
    await apply_policy(svc, policy, reason='monitor_unreadable')
    await svc.publisher.publish_trade_tick(TradeTickRecord(symbol='WFF', price=13.35, size=100))
    await svc.publisher.publish_quote_tick(QuoteTickRecord(symbol='WFF', bid_price=13.34, ask_price=13.35))
    assert [row[1]['event_type'] for row in redis.entries[-2:]] == ['trade_tick', 'quote_tick']
    assert provider._subscriptions == {'WFF'}
    assert provider._ws.removed == ['A.*']  # Explicit pure-policy adapter, not gateway monitor.


@pytest.mark.asyncio
async def test_retained_13269_snapshot_payload_equality_without_rate_or_timing(clock, monkeypatch):
    selected = os.environ.get('MOMWARN_RETAINED_SNAPSHOT')
    if not selected:
        pytest.skip('retained full snapshot not supplied; never fabricate native content')
    batch = json.loads(Path(selected).read_bytes())['event']['payload']
    names = {field.name for field in fields(SnapshotRecord)}
    snapshots = [SnapshotRecord(**{key: value for key, value in row.items() if key in names})
                 for row in batch['snapshots']]
    refs = [ReferenceDataPayload.model_validate(row) for row in batch['reference_data']]
    assert len(snapshots) == 13269 and len(refs) == 3184
    svc, _, redis, _ = service()
    monkeypatch.setattr(svc.reference_cache, 'as_payloads', lambda _: refs)
    assert await svc.publish_snapshot_batch_once(snapshots) == 13269
    stream, published, options = redis.entries[-1]
    assert stream == 'test:snapshot-batches'
    assert published['payload']['snapshots'] == [row.to_payload().model_dump(mode='json') for row in snapshots]
    assert published['payload']['reference_data'] == [row.model_dump(mode='json') for row in refs]
    assert options == {'maxlen': 120, 'approximate': True}
