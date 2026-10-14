"""Controlled native restart readiness, not live restart or performance evidence."""
from dataclasses import replace
from datetime import datetime, UTC
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.market_data import gateway
from project_mai_tai.market_data.momentum_warning_watchdog import WarningWatchdog
from project_mai_tai.momwarn_health_contract import proposed_health_v2
from tests.unit.test_momwarn1_gateway import arm_warning_with_three_clean_reads, event, service
from tests.unit import test_momwarn1_live_health as native_controls
from tests.unit.test_momwarn1_live_health import NS, NOW, line

setup = native_controls.setup


def first_due(started):
    return (started // (60 * NS) + 1) * (60 * NS)


def advance(env, now, *, symbols=(), outcome="published", late_ns=None):
    env.clock.ns = now
    env.seq = getattr(env, "seq", 10) + 1
    with (env.directory / 'oms.log').open('a') as stream:
        stream.write(line(now - NS, f'[OMS-BROKER-SYNC-PASS] id={env.seq} '
            'phase=end outcome=ok duration_ms=2300.000 exceeds_note_age=False'))
    due = now // (60 * NS) * (60 * NS)
    bar_ms = due // 1_000_000 - 60_000
    with (env.directory / 'schwab-1m-v2.log').open('a') as stream:
        for symbol in symbols:
            stamp = now if late_ns is None else due + late_ns
            stream.write(line(stamp, f'[V2-LINE-RESTORE] sym={symbol} lane=controlled '
                f'current_bar_ms={bar_ms} outcome={outcome}'))


@pytest.mark.asyncio
async def test_native_restart_pending_unknown_then_first_ontime_arms_and_following_late_reverts(setup):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled-new-gateway', NOW)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending and not health.v2_not_applicable
    assert health.v2_bar_lateness_seconds is None
    assert health.v2_startup_deadline_ns == first_due(NOW) + 5 * NS
    watchdog = WarningWatchdog()
    assert watchdog.check(health, publish_lag_seconds=0) is None
    advance(env, first_due(NOW) + 2 * NS, symbols=('AAA', 'BBB'))
    health = await env.reader.read_live(env.client, 'test')
    assert not health.v2_startup_pending and health.v2_bar_lateness_seconds == 2
    assert env.reader._gateway_startup['ready']['completed_ns'] == env.clock.ns
    assert watchdog.check(health, publish_lag_seconds=0) is None
    advance(env, first_due(NOW) + 60 * NS + 5 * NS)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_bar_lateness_seconds == 5
    assert watchdog.check(health, publish_lag_seconds=0) is None
    advance(env, env.clock.ns + 1_000_000)
    health = await env.reader.read_live(env.client, 'test')
    assert watchdog.check(health, publish_lag_seconds=0) == 'v2_bar_late'
    assert watchdog.reason == 'v2_bar_late'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'partial', 'warmup', 'failure', 'unclosed'])
async def test_native_firstbar_exact_deadline_and_missing_policy_no_healthy_upgrade(setup, case):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled-new-gateway', NOW)
    await env.reader.read_live(env.client, 'test')
    due = first_due(NOW)
    symbols = () if case == 'missing' else (('AAA',) if case == 'partial' else ('AAA', 'BBB'))
    advance(env, due + 5 * NS, symbols=symbols,
            outcome=case if case in {'warmup', 'failure'} else 'published',
            late_ns=-NS if case == 'unclosed' else None)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending and health.v2_bar_lateness_seconds is None
    watchdog = WarningWatchdog()
    assert watchdog.check(health, publish_lag_seconds=0) is None
    advance(env, due + 5 * NS + 1_000_000)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending
    assert env.reader._gateway_startup['ready'] is None
    assert watchdog.check(health, publish_lag_seconds=0) == 'v2_startup_timeout'


@pytest.mark.asyncio
async def test_first_ontime_exact_five_boundary_arms_not_elapsed_grace(setup):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled-new-gateway', NOW)
    await env.reader.read_live(env.client, 'test')
    advance(env, first_due(NOW) + 5 * NS, symbols=('AAA', 'BBB'))
    health = await env.reader.read_live(env.client, 'test')
    assert not health.v2_startup_pending
    assert health.v2_bar_lateness_seconds == 5
    assert WarningWatchdog().check(health, publish_lag_seconds=0) is None


@pytest.mark.asyncio
async def test_late_first_bar_does_not_arm_or_restamp(setup):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled-new-gateway', NOW)
    await env.reader.read_live(env.client, 'test')
    advance(env, first_due(NOW) + 6 * NS, symbols=('AAA', 'BBB'))
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending
    assert env.reader._gateway_startup['ready'] is None
    assert WarningWatchdog().check(health, publish_lag_seconds=0) == 'v2_startup_timeout'


@pytest.mark.asyncio
@pytest.mark.parametrize('change,lag,reason', [
    ({'load_1m': 6.01}, 0, 'load'),
    ({'ram_available_bytes': 3 * 1024**3 - 1}, 0, 'ram'),
    ({'new_live_error': True}, 0, None),
    ({}, 2.001, 'publish_lag'),
])
async def test_other_watchdog_gates_remain_live_while_native_startup_pending(setup, change, lag, reason):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled-new-gateway', NOW)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending
    assert WarningWatchdog().check(replace(health, **change), publish_lag_seconds=lag) == reason


@pytest.mark.asyncio
async def test_pending_does_not_hide_slow_oms_twice_or_sequence_error(setup):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled-new-gateway', NOW)
    health = await env.reader.read_live(env.client, 'test')
    watchdog = WarningWatchdog()
    assert watchdog.check(replace(health, oms_sync_seconds=5.1), publish_lag_seconds=0) is None
    assert watchdog.check(replace(health, oms_sync_seq=11, oms_sync_seconds=5.1), publish_lag_seconds=0) == 'oms_sync_slow_twice'
    watchdog = WarningWatchdog()
    assert watchdog.check(health, publish_lag_seconds=0) is None
    assert watchdog.check(replace(health, oms_sync_seq=12), publish_lag_seconds=0) == 'monitor_sync_gap'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['dead_proc', 'stale_oms', 'permission', 'config_changed'])
async def test_native_startup_does_not_swallow_other_source_failures(setup, monkeypatch, case):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled-new-gateway', NOW)
    await env.reader.read_live(env.client, 'test')
    if case == 'dead_proc':
        (env.proc / '13/stat').unlink()
    elif case == 'stale_oms':
        env.clock.ns += 21 * NS
    elif case == 'permission':
        monkeypatch.setattr(env.reader, '_read_file', lambda: (_ for _ in ()).throw(PermissionError('controlled')))
    else:
        env.config.write_text(env.config.read_text().replace('CONTROLLED-proc15', 'CHANGED'))
    with pytest.raises((ValueError, OSError)):
        await env.reader.read_live(env.client, 'test')


@pytest.mark.asyncio
async def test_native_pending_reads_real_new_error(setup):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled-new-gateway', NOW)
    await env.reader.read_live(env.client, 'test')
    with (env.directory / 'orb-schwab.log').open('a') as stream:
        stream.write('ERROR controlled new ORB failure\n')
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending and health.new_live_error
    assert WarningWatchdog().check(health, publish_lag_seconds=0) is None
    assert env.reader.error_observations()['error_lines_by_log']['orb-schwab.log'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['real_twelve', 'five_minutes_later', 'other_error'])
async def test_v2_boot_unanswerable_exact_sixty_second_exception(setup, monkeypatch, case):
    started = int(datetime(2026, 10, 13, 17, 50, 42, tzinfo=UTC).timestamp()) * NS
    now = started + (305 if case == 'five_minutes_later' else 13) * NS
    env = setup(pending=True, now=now)
    (env.proc / 'stat').write_text(f'btime {started // NS - 1}\n')
    monkeypatch.setattr('os.sysconf', lambda name: 101)
    env.reader.bind_gateway_start('controlled-new-gateway', now)
    await env.reader.read_live(env.client, 'test')
    fixture = Path(__file__).parents[1] / 'fixtures/momwarn-v2-boot-errors-20261013.log'
    records = fixture.read_text().splitlines(keepends=True)
    assert len(records) == 12
    if case == 'five_minutes_later':
        records = [records[0].replace('17:50:49,160', '17:55:42,000')]
    elif case == 'other_error':
        records = [records[0].replace('reason=line_unproven', 'reason=unexpected_failure')]
    with (env.directory / 'schwab-1m-v2.log').open('a') as stream:
        stream.writelines(records)
    health = await env.reader.read_live(env.client, 'test')
    assert health.new_live_error
    assert env.reader.error_observations()['error_lines_by_log']['schwab-1m-v2.log'] == len(records)
    assert WarningWatchdog().check(health, publish_lag_seconds=0) is None


@pytest.mark.asyncio
async def test_gateway_native_pending_then_three_clean_admits_uncalibrated_late_pauses_and_recovers(setup, monkeypatch):
    env = setup(pending=True)
    svc, provider, redis, _ = service()
    svc._warning_health_reader = env.reader
    svc._warning_health_redis = env.client
    monkeypatch.setattr(gateway, '_install_signal_handlers', lambda stop: None)
    svc._ensure_reference_data = AsyncMock(side_effect=RuntimeError('controlled stop before provider or Redis'))
    with pytest.raises(RuntimeError, match='controlled stop'):
        await svc.run()
    assert env.reader._gateway_startup['gateway_instance_id'] == svc._warning_instance_id
    assert env.reader._gateway_startup['started_ns'] == NOW
    await svc.apply_subscription_event(event('v2', ['WFF']))
    await svc.apply_subscription_event(event('momentum-warning', ['WFF', 'NEW']))
    owners = {key: set(value) for key, value in svc._desired_symbols_by_consumer.items()}
    tq = set(provider._subscriptions), set(provider._warning_trade_symbols)
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_revert_reason is None
    assert 'A.*' in provider._ws.added
    advance(env, first_due(NOW) + 2 * NS, symbols=('AAA', 'BBB'))
    await arm_warning_with_three_clean_reads(svc)
    assert svc._warning_active and 'A.*' in provider._ws.added
    advance(env, first_due(NOW) + 66 * NS)
    await svc._warning_monitor_once()
    assert svc._warning_revert_reason is None and svc._warning_active
    assert svc.warning_metrics()['watchdog_observed_reason'] == 'v2_bar_late'
    assert provider._ws.removed == []
    assert svc._desired_symbols_by_consumer == owners
    assert (provider._subscriptions, provider._warning_trade_symbols) == tq
    advance(env, first_due(NOW) + 122 * NS, symbols=('AAA', 'BBB'))
    await arm_warning_with_three_clean_reads(svc)
    assert svc._warning_revert_reason is None and svc._warning_generation == 1
    assert provider._ws.added.count('A.*') == 1
    assert svc._desired_symbols_by_consumer == owners
    assert (provider._subscriptions, provider._warning_trade_symbols) == tq


@pytest.mark.asyncio
async def test_window_reset_requires_new_ontime_bar_then_keeps_following_late_gate(setup):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled-new-gateway', NOW)
    await env.reader.read_live(env.client, 'test')
    advance(env, first_due(NOW) + 2 * NS, symbols=('AAA', 'BBB'))
    await env.reader.read_live(env.client, 'test')
    proof = dict(env.reader._gateway_startup['ready'])
    env.reader.reset_window()
    assert env.reader._gateway_startup['ready'] is None
    assert env.reader._gateway_startup['baseline_ns'] == env.clock.ns
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending and health.v2_bar_lateness_seconds is None
    advance(env, first_due(NOW) + 62 * NS, symbols=('AAA', 'BBB'))
    health = await env.reader.read_live(env.client, 'test')
    assert not health.v2_startup_pending
    assert env.reader._gateway_startup['ready']['completed_ns'] > proof['completed_ns']
    advance(env, first_due(NOW) + 126 * NS)
    health = await env.reader.read_live(env.client, 'test')
    assert WarningWatchdog().check(health, publish_lag_seconds=0) == 'v2_bar_late'


@pytest.mark.asyncio
async def test_startup_contract_rejects_deadline_extension_and_fake_inapplicability(setup):
    env = setup(pending=True)
    startup = dict(gateway_instance_id='controlled', started_ns=NOW, baseline_ns=NOW,
                   activation_ns=None, empty_seen=False, ready=None)
    measured = env.source.read(env.state(), startup=startup)
    assert measured['v2_applicability'] == 'STARTUP_PENDING'
    source = {**measured, **measured['v2_applicability_provenance']}
    source['v2_startup_deadline_ns'] += NS
    with pytest.raises(ValueError, match='startup readiness'):
        proposed_health_v2(source, now_ns=NOW)
    source['v2_applicability'] = 'NOT_APPLICABLE'
    with pytest.raises(ValueError):
        proposed_health_v2(source, now_ns=NOW)


@pytest.mark.asyncio
async def test_sourced_pre_first_due_and_positive_empty_remain_not_applicable(setup):
    early = int(datetime(2026, 10, 13, 8, 11, 4, tzinfo=UTC).timestamp()) * NS
    env = setup(now=early)
    env.reader.bind_gateway_start('controlled-early-gateway', early)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_not_applicable and not health.v2_startup_pending
    env.client.xrevrange.side_effect = lambda *a, **kw: [('1-0', {'data': json.dumps(env.state(watchlist=[]))})]
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_not_applicable and not health.v2_startup_pending


@pytest.mark.asyncio
async def test_pre07_restored_empty_then_0710_watch_has_bounded_new_activation_not_expired_0701(setup):
    early = int(datetime(2026, 10, 13, 8, 11, 4, tzinfo=UTC).timestamp()) * NS
    watched = int(datetime(2026, 10, 13, 11, 10, 1, tzinfo=UTC).timestamp()) * NS
    env = setup(now=early)
    env.reader.bind_gateway_start('controlled-early', early)
    env.client.xrevrange.side_effect = lambda *a, **kw: [('1-0', {'data': json.dumps(env.state(watchlist=[]))})]
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_not_applicable and env.reader._gateway_startup['activation_ns'] is None
    env.client.xrevrange.side_effect = lambda *a, **kw: [('1-0', {'data': json.dumps(env.state())})]
    advance(env, watched)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending
    assert health.v2_startup_deadline_ns == first_due(watched) + 5 * NS
    assert WarningWatchdog().check(health, publish_lag_seconds=0) is None
    # Existing on-time current due is admitted immediately if newly observed after baseline.
    advance(env, watched + NS, symbols=('AAA', 'BBB'))
    health = await env.reader.read_live(env.client, 'test')
    assert not health.v2_startup_pending and health.v2_bar_lateness_seconds == 2


@pytest.mark.asyncio
async def test_positive_empty_after_ready_clears_baseline_but_not_every_nonempty_addition(setup):
    env = setup(pending=True)
    env.reader.bind_gateway_start('controlled', NOW)
    await env.reader.read_live(env.client, 'test')
    advance(env, first_due(NOW) + 2 * NS, symbols=('AAA', 'BBB'))
    await env.reader.read_live(env.client, 'test')
    assert env.reader._gateway_startup['ready'] is not None
    env.client.xrevrange.side_effect = lambda *a, **kw: [('1-0', {'data': json.dumps(env.state(watchlist=[]))})]
    advance(env, env.clock.ns + NS)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_not_applicable and env.reader._gateway_startup['ready'] is None
    baseline = env.reader._gateway_startup['baseline_ns']
    env.client.xrevrange.side_effect = lambda *a, **kw: [('1-0', {'data': json.dumps(env.state())})]
    advance(env, env.clock.ns + NS)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending
    assert env.reader._gateway_startup['baseline_ns'] == baseline
    advance(env, first_due(NOW) + 62 * NS, symbols=('AAA', 'BBB'))
    await env.reader.read_live(env.client, 'test')
    proof = dict(env.reader._gateway_startup['ready'])
    env.client.xrevrange.side_effect = lambda *a, **kw: [('1-0', {'data': json.dumps(env.state(watchlist=['AAA', 'BBB', 'CCC']))})]
    advance(env, env.clock.ns + 4 * NS)
    health = await env.reader.read_live(env.client, 'test')
    assert not health.v2_startup_pending and env.reader._gateway_startup['ready'] == proof
    assert WarningWatchdog().check(health, publish_lag_seconds=0) == 'v2_bar_late'


@pytest.mark.asyncio
async def test_restart_with_old_ontime_current_bars_cannot_reuse_them(setup):
    env = setup(pending=False)
    env.reader.bind_gateway_start('controlled-new', NOW)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending
    assert env.reader._gateway_startup['ready'] is None
    advance(env, first_due(NOW) + 2 * NS, symbols=('AAA', 'BBB'))
    health = await env.reader.read_live(env.client, 'test')
    assert not health.v2_startup_pending
    assert env.reader._gateway_startup['ready']['completed_ns'] > NOW
    env.client.xrevrange.side_effect = lambda *a, **kw: [('1-0', {'data': json.dumps(env.state(watchlist=[]))})]
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_not_applicable and not health.v2_startup_pending
