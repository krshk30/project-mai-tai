"""No provider health writes; the native freshness and SLA gates still decide."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import health_view
from project_mai_tai.post_restart_health_gate import GateOutcome, inspect_health_payload, wait_for_post_restart_health


NOW = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)


def payload():
    return dict(services=[dict(service_name='schwab-1m-v2', status='degraded', raw_status='degraded',
        observed_at_raw=NOW.isoformat(), details=dict(data_flow='stalled_offhours_rest_dry',
            market_session='closed', loop_health='healthy', loop_exceptions_total=0,
            streamer_connected=True, enabled=True, warmed_size=5, watchlist_size=5,
            secs_since_last_bar=3600)), dict(service_name='oms', status='degraded', raw_status='degraded')])


def test_exact_shape_copy_admitted_native_gate_and_other_service_unchanged():
    actual = payload()
    original = deepcopy(actual)
    reports = []
    view = health_view.admitted_view(actual, NOW, reports.append)
    assert actual == original and view['services'][1] == actual['services'][1]
    assert len(reports) == 1 and reports[0]['details'] == actual['services'][0]['details']
    assert reports[0]['allowance'] == 'STANDING-ALLOWANCE'
    start = NOW - timedelta(seconds=10)
    assert inspect_health_payload(view, service_name='schwab-1m-v2', process_started_at=start).outcome == GateOutcome.HEALTHY
    assert inspect_health_payload(actual, service_name='schwab-1m-v2', process_started_at=start).outcome == GateOutcome.NOT_HEALTHY
    assert inspect_health_payload(view, service_name='schwab-1m-v2', process_started_at=NOW).outcome == GateOutcome.NOT_HEALTHY


@pytest.mark.parametrize('field,bad', [
    ('data_flow', 'stalled_rth'), ('market_session', 'regular'), ('loop_health', 'unhealthy'),
    ('loop_exceptions_total', 1), ('streamer_connected', False), ('enabled', False),
    ('warmed_size', 4), ('watchlist_size', 0), ('secs_since_last_bar', 3900.01),
    ('secs_since_last_bar', -1), ('secs_since_last_bar', float('inf')),
    ('secs_since_last_bar', float('nan')), ('secs_since_last_bar', '3600seconds'),
    ('loop_exceptions_total', False), ('watchlist_size', True), ('streamer_connected', 'TRUE'),
])
def test_each_shape_guard_blocks_without_allowance(field, bad):
    actual = payload()
    actual['services'][0]['details'][field] = bad
    reports = []
    view = health_view.admitted_view(actual, NOW, reports.append)
    assert view['services'][0]['raw_status'] == 'degraded' and reports == []


@pytest.mark.parametrize('stamp', [None, 'invalid', '2026-10-09T01:00:00',
                                  (NOW - timedelta(seconds=121)).isoformat(),
                                  (NOW + timedelta(seconds=1)).isoformat()])
def test_unreadable_stale_future_heartbeat_not_admitted(stamp):
    actual = payload()
    actual['services'][0]['observed_at_raw'] = stamp
    assert health_view.admitted_view(actual, NOW, lambda event: pytest.fail('allowance')) == actual


def test_after_hours_anchor_and_regular_hours_refusal():
    now = NOW - timedelta(hours=3)
    actual = payload()
    actual['services'][0]['observed_at_raw'] = now.isoformat()
    detail = actual['services'][0]['details']
    detail.update(market_session='after_hours', secs_since_last_bar=7200)
    assert health_view.admitted_view(actual, now, lambda event: None)['services'][0]['raw_status'] == 'healthy'
    detail['secs_since_last_bar'] = 7501
    assert health_view.admitted_view(actual, now, lambda event: pytest.fail('allowance')) == actual
    now -= timedelta(hours=3)
    actual['services'][0]['observed_at_raw'] = now.isoformat()
    assert health_view.admitted_view(actual, now, lambda event: pytest.fail('allowance')) == actual


def test_duplicate_identity_and_healthy_passthrough():
    actual = payload()
    actual['services'].append(deepcopy(actual['services'][0]))
    assert health_view.admitted_view(actual, NOW, lambda event: pytest.fail('allowance')) == actual
    actual = payload()
    actual['services'][0]['raw_status'] = 'healthy'
    assert health_view.admitted_view(actual, NOW, lambda event: pytest.fail('allowance')) == actual


def test_actual_bot_serialized_details_admitted_without_coercing_unknown_values():
    actual = payload()
    for key in ('loop_exceptions_total', 'watchlist_size', 'warmed_size', 'secs_since_last_bar'):
        actual['services'][0]['details'][key] = str(actual['services'][0]['details'][key])
    for key in ('streamer_connected', 'enabled'):
        actual['services'][0]['details'][key] = 'true'
    reports = []
    assert health_view.admitted_view(actual, NOW, reports.append)['services'][0]['raw_status'] == 'healthy'
    assert reports[0]['details'] == actual['services'][0]['details']
    actual['services'][0]['details']['secs_since_last_bar'] = 'none'
    assert health_view.admitted_view(actual, NOW, lambda event: pytest.fail('allowance')) == actual


def test_local_adapter_fetches_each_time_no_cached_or_forged_response():
    calls, reports = [], []
    def load():
        calls.append(1)
        return payload()
    with health_view.health_view(reports.append, loader=load, now=lambda: NOW) as url:
        for _ in range(2):
            with urlopen(url, timeout=2) as response:
                assert json.load(response)['services'][0]['raw_status'] == 'healthy'
    assert len(calls) == len(reports) == 2


def test_unreadable_upstream_is_502_not_healthy():
    def load():
        raise TimeoutError('private provider detail never logged')
    reports = []
    with health_view.health_view(reports.append, loader=load) as url:
        with pytest.raises(HTTPError) as error:
            urlopen(url, timeout=2)
        assert error.value.code == 502
    assert reports == [dict(health_read='UNREADABLE', error_type='TimeoutError')]


def test_native_sixty_second_sla_still_rejects_late_allowance():
    start = NOW - timedelta(seconds=61)
    view = health_view.admitted_view(payload(), NOW, lambda event: None)
    result = wait_for_post_restart_health(health_url='isolated', service_name='schwab-1m-v2',
        process_started_at=start, sla_seconds=60, load_health=lambda url: view,
        now=lambda: NOW, sleep=lambda seconds: pytest.fail('late heartbeat must decide immediately'),
        report=lambda message: None)
    assert result.outcome == GateOutcome.NOT_HEALTHY and 'after the SLA deadline' in result.summary
