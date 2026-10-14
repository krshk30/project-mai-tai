"""CONTROLLED native read chain; no live, provider or deployment proof."""

from dataclasses import replace
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.market_data.momentum_warning_watchdog import WarningWatchdog
from project_mai_tai.momwarn_cohort_lateness import CohortObservation, MINUTE_NS, NS, POLICY, window
from project_mai_tai.momwarn_health_contract import proposed_health_v2
from project_mai_tai.momwarn_live_health import LiveWarningHealthSource
from tests.unit.test_momwarn1_d2_startup import advance
from tests.unit.test_momwarn1_live_health import setup as setup, line, start_native


def et(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()) * NS


@pytest.fixture
def cohort_setup(setup):
    def build(value='2026-10-14T04:30:04'):
        env = setup(now=et(value))
        env.cfg['v2_lateness_policy'] = POLICY
        env.config.write_text(json.dumps(env.cfg))
        # No completed v2 bars are fabricated while v2 is not trading.
        (env.directory / 'schwab-1m-v2.log').write_text(line(env.clock.ns - 90 * NS, 'controlled baseline'))
        env.source = LiveWarningHealthSource(env.cfg, env.directory, proc=env.proc, wall_ns=lambda: env.clock.ns)
        env.reader._source, env.reader._source_config = env.source, dict(env.cfg)
        return env
    return build


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [
    '2026-10-14T04:30:04', '2026-10-14T07:00:59', '2026-10-13T20:30:04',
    '2026-10-17T08:30:04', '2026-10-18T08:30:04', '2026-11-04T04:30:04',
])
async def test_actual_read_chain_outside_cal_window_is_NA_not_exception_or_zero(cohort_setup, value):
    env = cohort_setup(value)
    measured = env.source.read(env.state())
    assert measured['v2_applicability'] == 'NOT_APPLICABLE'
    assert measured['v2_applicability_reason'] == 'sourced_cal_outside_trading_window'
    assert measured['v2_due_wall_ns'] == env.clock.ns // MINUTE_NS * MINUTE_NS
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_not_applicable and health.v2_cohort is None
    assert health.v2_bar_lateness_seconds is None and not health.v2_startup_pending
    assert health.oms_sync_seconds == 2.3 and health.oms_sync_seq == 10
    assert WarningWatchdog().check_recoverable(health, publish_lag_seconds=0) is None
    env.client.xrevrange.assert_called_with('test:strategy-state-isolated', count=32)


@pytest.mark.asyncio
async def test_0430_three_actual_native_clean_reads_arm_A_without_v2_readiness_fabrication(cohort_setup):
    env = cohort_setup()
    env.reader.bind_gateway_start('controlled-0430-gateway', env.clock.ns)
    svc, provider, redis = await start_native(env)
    assert env.client.xrevrange.call_count == 3
    assert svc._warning_active and svc._warning_revert_reason is None
    assert svc.warning_metrics()['pause_state']['clean_streak'] == 3
    assert provider._ws.added.count('A.*') == 1
    assert provider._ws.removed == []
    assert provider._subscriptions == {'WFF'} and provider._warning_trade_symbols == {'NEW'}
    assert env.reader._gateway_startup['ready'] is None
    assert env.reader._gateway_startup['activation_ns'] is None
    assert not env.reader._source.bars


@pytest.mark.asyncio
async def test_0701_exact_due_is_enforced_and_two_actual_complete_late_cohorts_trip(cohort_setup):
    env = cohort_setup('2026-10-14T06:57:04')
    assert (await env.reader.read_live(env.client, 'test')).v2_not_applicable
    due = et('2026-10-14T07:01:00')
    advance(env, due)
    first = await env.reader.read_live(env.client, 'test')
    assert not first.v2_not_applicable and first.v2_cohort['due_ns'] == due
    assert first.v2_cohort['window'] == 'PM' and first.v2_cohort['status'] == 'INCOMPLETE'
    watchdog = WarningWatchdog()
    advance(env, due + 6 * NS, symbols=('AAA', 'BBB'), late_ns=5 * NS)
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_cohort['status'] == 'COMPLETE' and health.v2_bar_lateness_seconds == 5
    assert watchdog.check_recoverable(health, publish_lag_seconds=0) is None
    advance(env, due + MINUTE_NS + 6 * NS, symbols=('AAA', 'BBB'), late_ns=5 * NS)
    assert watchdog.check_recoverable(await env.reader.read_live(env.client, 'test'),
                                      publish_lag_seconds=0) == 'v2_cohort_late_twice'


@pytest.mark.asyncio
async def test_premarket_NA_does_not_forge_startup_ready_at_0701(cohort_setup):
    env = cohort_setup()
    env.reader.bind_gateway_start('controlled-pre07-gateway', env.clock.ns)
    assert (await env.reader.read_live(env.client, 'test')).v2_not_applicable
    advance(env, et('2026-10-14T07:01:04'))
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_startup_pending and not health.v2_not_applicable
    assert env.reader._gateway_startup['ready'] is None


@pytest.mark.parametrize('value,expected', [
    ('2026-10-14T07:00:00', None), ('2026-10-14T07:01:00', 'PM'),
    ('2026-10-14T09:30:00', 'RTH'), ('2026-10-14T16:00:00', 'AH'),
    ('2026-10-14T20:00:00', 'AH'), ('2026-10-14T20:01:00', None),
    ('2026-10-17T07:01:00', None), ('2026-11-04T07:01:00', 'PM'),
])
def test_due_window_weekday_boundaries_and_DST(value, expected):
    assert window(et(value)) == expected


@pytest.mark.asyncio
async def test_2000_cohort_still_enforced_then_2001_NA(cohort_setup):
    env = cohort_setup('2026-10-13T19:57:04')
    await env.reader.read_live(env.client, 'test')
    advance(env, et('2026-10-13T20:00:04'), symbols=('AAA', 'BBB'), late_ns=NS)
    health = await env.reader.read_live(env.client, 'test')
    assert not health.v2_not_applicable and health.v2_cohort['window'] == 'AH'
    advance(env, et('2026-10-13T20:01:04'))
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_not_applicable and health.v2_cohort is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,reason', [('load', 'load'), ('ram', 'ram'), ('error', 'live_error'),
                                       ('lag', 'publish_lag'), ('slow', 'oms_sync_slow_twice')])
async def test_independent_health_gates_still_enforced_on_real_outside_window_reads(cohort_setup, monkeypatch, fault, reason):
    env = cohort_setup()
    watchdog = WarningWatchdog()
    assert watchdog.check_recoverable(await env.reader.read_live(env.client, 'test'), publish_lag_seconds=0) is None
    if fault == 'load':
        monkeypatch.setattr('os.getloadavg', lambda: (6.1, 1, 1))
    elif fault == 'ram':
        env.mem.write_text('MemAvailable: 2097152 kB\n')
    elif fault == 'error':
        with (env.directory / 'strategy.log').open('a') as out:
            out.write('ERROR controlled new live error\n')
    elif fault == 'slow':
        for seq in (11, 12):
            env.clock.ns += NS
            with (env.directory / 'oms.log').open('a') as out:
                out.write(line(env.clock.ns, f'[OMS-BROKER-SYNC-PASS] id={seq} phase=end outcome=ok duration_ms=6000.000 exceeds_note_age=False'))
            health = await env.reader.read_live(env.client, 'test')
            result = watchdog.check_recoverable(health, publish_lag_seconds=0)
            assert result == (None if seq == 11 else reason)
        return
    health = await env.reader.read_live(env.client, 'test')
    assert health.v2_not_applicable
    assert watchdog.check_recoverable(health, publish_lag_seconds=2.01 if fault == 'lag' else 0) == reason


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['v2_pid', 'oms_pid', 'stale_state', 'watchlist', 'future_log', 'stale_oms'])
async def test_outside_window_never_swallows_independent_bounded_source_errors(cohort_setup, fault):
    env = cohort_setup()
    await env.reader.read_live(env.client, 'test')
    if fault in {'v2_pid', 'oms_pid'}:
        (env.proc / ('13' if fault == 'v2_pid' else '12') / 'stat').unlink()
    elif fault in {'stale_state', 'watchlist'}:
        state = env.state()
        if fault == 'stale_state':
            state['produced_at'] = datetime.fromtimestamp((env.clock.ns - 11 * NS) / NS, ZoneInfo('UTC')).isoformat()
        else:
            state['payload']['watchlist'] = None
        env.client.xrevrange.side_effect = lambda *_a, **_kw: [('1-0', {'data': json.dumps(state)})]
    elif fault == 'future_log':
        with (env.directory / 'schwab-1m-v2.log').open('a') as out:
            out.write(line(env.clock.ns + NS, '[V2-WARMUP-LINE] AAA invalid future'))
    else:
        env.clock.ns += 21 * NS
    with pytest.raises((OSError, ValueError)):
        await env.reader.read_live(env.client, 'test')


@pytest.mark.parametrize('field,value', [
    ('v2_due_wall_ns', 1), ('v2_lateness_policy', 'invented'), ('v2_watchlist_complete', False),
    ('v2_watchlist_snapshot_ns', 1), ('v2_instance_id', ''), ('v2_source', 'invented'),
])
def test_outside_window_contract_rejects_altered_provenance(cohort_setup, field, value):
    env = cohort_setup()
    receipt = env.source.read(env.state())
    receipt[field] = value
    with pytest.raises(ValueError, match='unsourced v2 inapplicability'):
        proposed_health_v2(receipt, now_ns=env.clock.ns)


def test_outside_window_receipt_cannot_cross_0701_boundary(cohort_setup):
    env = cohort_setup('2026-10-14T07:00:59')
    receipt = env.source.read(env.state())
    with pytest.raises(ValueError, match='unsourced v2 inapplicability'):
        proposed_health_v2(receipt, now_ns=env.clock.ns + NS)


def test_outside_window_is_not_a_valid_calibrated_observation():
    due = et('2026-10-14T04:30:00')
    observation = CohortObservation(due, due + NS, 'strategy-state-isolated', True,
                                    ('AAA',), (), (), (('AAA', due + NS),), 'first_native_receipts')
    with pytest.raises(ValueError, match='outside sourced'):
        observation.validate()


@pytest.mark.asyncio
async def test_NA_does_not_clear_existing_sticky_harm(cohort_setup):
    env = cohort_setup()
    health = await env.reader.read_live(env.client, 'test')
    watchdog = WarningWatchdog()
    assert watchdog.check_recoverable(replace(health, new_live_error=True), publish_lag_seconds=0) == 'live_error'
    for _ in range(3):
        assert watchdog.check_recoverable(await env.reader.read_live(env.client, 'test'),
                                          publish_lag_seconds=0) == 'live_error'
