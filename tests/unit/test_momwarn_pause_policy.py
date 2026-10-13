"""MOMLATCH1 (a)-(c) controlled reason inputs, NOT full historical native tapes."""
import asyncio
from datetime import datetime
import json
import time

import pytest

from project_mai_tai.market_data.momwarn_pause_policy import (
    HARM_REASONS, MONITOR_REASONS, PAUSE_HISTORY_LIMIT, RECONNECT_EVENT_REASONS,
    ReasonClass, WarningPausePolicy, classify_reason,
)
from project_mai_tai.market_data.momentum_warning_watchdog import WarningHealth, WarningWatchdog
from project_mai_tai.momwarn_cohort_lateness import (
    CohortObservation, CompleteCohortPolicy, LIMITS_NS, MINUTE_NS, NS,
)
from project_mai_tai.momwarn_log_cursor import LogReadRace


class Clock:
    def __init__(self):
        self.ns = 0

    def __call__(self):
        return self.ns

    def advance(self, seconds=1):
        self.ns += int(seconds*NS)


def active():
    clock = Clock()
    policy = WarningPausePolicy(clock_ns=clock)
    for _ in range(3):
        clock.advance()
        decision = policy.observe(ready=True)
    assert decision.action == 'resume' and policy.state == 'active'
    return policy, clock


def three_clean(policy, clock):
    actions = []
    for _ in range(3):
        clock.advance()
        actions.append(policy.observe(ready=True).action)
    return actions


@pytest.mark.parametrize('reason', sorted(MONITOR_REASONS))
def test_fixed_noise_list_classifies_as_monitor_then_three_clean_resume(reason):
    assert classify_reason(reason) == ReasonClass.MONITOR
    assert classify_reason(reason) == 'monitor'
    policy, clock = active()
    clock.advance()
    assert policy.observe(reason, ready=True).action == 'pause'
    assert policy.state == 'paused'
    assert three_clean(policy, clock) == ['none', 'none', 'resume']
    assert policy.state == 'active'


# Six reviewer-provided historical classes, using explicit controlled inputs.
TODAY_CLASSES = [
    ('04:00-PermissionError', None, PermissionError('controlled permission'), 'monitor_unreadable'),
    ('06:12-log-read-race', None, LogReadRace('controlled appended log'), 'monitor_unreadable'),
    ('08:45-uncalibrated-restart-gap', 'v2_bar_late', None, 'v2_bar_late'),
    ('14:18-A-own-publish-lag', 'publish_lag', None, 'publish_lag'),
    ('15:39-1s-read-timeout', None, TimeoutError(), 'monitor_unreadable'),
    ('16:08-readiness-timeout', 'v2_startup_timeout', None, 'v2_startup_timeout'),
]


@pytest.mark.parametrize('label,reason,exception,expected', TODAY_CLASSES,
                         ids=[row[0] for row in TODAY_CLASSES])
def test_a_today_six_monitor_noise_classes_pause_and_resume_never_sticky(label, reason, exception, expected):
    policy, clock = active()
    clock.advance()
    result = policy.observe(reason, exception=exception, ready=False)
    assert result.action == 'pause' and result.reason == expected
    assert result.reason_class == ReasonClass.MONITOR
    assert three_clean(policy, clock)[-1] == 'resume'
    assert policy.state == 'active'
    assert policy.metrics()['wire_pause_count'] == 1


@pytest.mark.parametrize('exception', [PermissionError(), TimeoutError(), ValueError(),
                                       OSError(), RuntimeError(), LogReadRace('controlled')])
def test_any_read_exception_is_pause_not_a_provider_or_harm_reclassification(exception):
    policy, clock = active()
    clock.advance()
    assert policy.observe(exception=exception, ready=True).reason == 'monitor_unreadable'
    assert policy.state == 'paused'


@pytest.mark.parametrize('reason', sorted(HARM_REASONS) + [
    'watched_tq_lag', 'tq_lag', 'trade_lag', 'quote_lag', 'provider_disconnect', '1008',
    'publish_error', 'metrics_error', 'metrics_publish_error', 'unsubscribe_error',
    'new_unclassified_reason', 'monitor_unreadable:PermissionError', '',
])
def test_b_harm_and_every_unclassified_reason_stick_and_never_clean_recover(reason):
    assert classify_reason(reason) != ReasonClass.MONITOR
    policy, clock = active()
    clock.advance()
    decision = policy.observe(reason, ready=True)
    assert decision.action == 'sticky_off' and policy.state == 'off'
    original_reason = policy.reason
    for _ in range(5):
        clock.advance()
        assert policy.observe(ready=True).action == 'none'
        assert not policy.clean()
    clock.advance()
    assert policy.observe('monitor_unreadable').action == 'none'
    assert policy.reason == original_reason and policy.state == 'off'


def test_actual_uncalibrated_0845_shape_is_pause_but_two_complete_cal_minutes_are_harm():
    wall = time.time_ns()
    health = WarningHealth(wall, 1, .1, 30, 1, 4*1024**3)
    uncalibrated = WarningWatchdog().check(health, publish_lag_seconds=0)
    assert uncalibrated == 'v2_bar_late' and classify_reason(uncalibrated) == ReasonClass.MONITOR
    due = int(datetime.fromisoformat('2026-10-13T08:45:00-04:00').timestamp())*NS
    cal = CompleteCohortPolicy()
    def complete(at):
        late = LIMITS_NS['PM']+1
        return CohortObservation(at, at+late, 'strategy-state-isolated', True, ('AAA',), (), (),
                                 (('AAA', at+late),), 'first_native_receipts')
    assert cal.observe(complete(due)) is None
    harm = cal.observe(complete(due+MINUTE_NS))
    assert harm == 'v2_cohort_late_twice' and classify_reason(harm) == ReasonClass.HARM
    policy, clock = active()
    assert policy.observe(harm).action == 'sticky_off'
    assert not policy.clean(clock.ns)


def test_initial_three_proven_clean_reads_no_constructor_health_or_pending_bypass():
    clock = Clock()
    policy = WarningPausePolicy(clock_ns=clock)
    assert policy.state == 'paused' and policy.metrics()['history'] == []
    assert policy.observe(ready=True).action == 'none'
    clock.advance()
    assert policy.observe(ready=True).action == 'none'
    clock.advance()
    assert policy.observe(ready=False).reason == 'v2_startup_pending'
    assert policy.clean_streak == 0
    assert three_clean(policy, clock) == ['none', 'none', 'resume']
    assert policy.metrics()['wire_pause_count'] == 0


def test_ready_flag_cannot_override_noise_and_bad_read_resets_cardinal_clean_streak():
    policy, clock = active()
    assert policy.observe('monitor_unreadable', ready=True).action == 'pause'
    clock.advance()
    assert policy.observe(ready=True).clean_reads == 1
    clock.advance()
    assert policy.observe(ready=True).clean_reads == 2
    clock.advance()
    assert policy.observe('v2_startup_pending', ready=True).clean_reads == 0
    assert three_clean(policy, clock) == ['none', 'none', 'resume']


def test_c_alternating_noise_clean_is_one_episode_alerts_on_seven_recovery_interruptions():
    policy, clock = active()
    clock.advance()
    assert policy.observe('monitor_unreadable').action == 'pause'
    started = policy.metrics()['history'][-1]['start_ns']
    for index in range(7):
        clock.advance()
        assert policy.observe(ready=True).action == 'none'
        clock.advance()
        result = policy.observe('monitor_unreadable')
        assert result.action == 'none' and policy.state == 'paused'
        assert ('pause_flapping' in result.alert_reasons) == (index == 6)
    metrics = policy.metrics()
    assert metrics['wire_pause_count'] == 1
    assert metrics['pause_episode_count'] == 2  # initial startup + actual interrupted episode
    assert metrics['recovery_interruptions'] == 7 and metrics['pause_events'] == 9
    current = metrics['history'][-1]
    assert current['start_ns'] == started and current['end_ns'] is None
    assert len(metrics['history']) == 2


def test_continuous_bad_reads_and_changing_reasons_are_not_fabricated_wire_episodes():
    policy, clock = active()
    clock.advance()
    policy.observe('monitor_unreadable')
    started = policy.metrics()['history'][-1]['start_ns']
    for index in range(30):
        clock.advance()
        policy.observe('monitor_stale' if index % 2 else 'monitor_unreadable')
    metrics = policy.metrics()
    assert metrics['wire_pause_count'] == 1 and metrics['recovery_interruptions'] == 0
    assert metrics['history'][-1]['start_ns'] == started
    assert metrics['pause_count'] > metrics['pause_episode_count']
    assert metrics['pause_count_semantics'] == 'reason_segments_not_wire_unsubscribes'
    assert metrics['alert_reasons'] == []
    clock.ns = started+600*NS
    assert 'pause_over_10min' not in policy.metrics()['alert_reasons']
    clock.advance(.000000001)
    assert 'pause_over_10min' in policy.metrics()['alert_reasons']


def test_seven_real_episodes_alert_and_hour_window_expires_without_resetting_lifetime_counts():
    policy, clock = active()
    for _ in range(7):
        clock.advance()
        policy.observe('monitor_invalid')
        three_clean(policy, clock)
    assert policy.metrics()['wire_pause_count'] == 7
    assert 'pause_flapping' in policy.metrics()['alert_reasons']
    clock.advance(3600)
    assert policy.metrics()['alert_reasons'] == []
    assert policy.metrics()['wire_pause_count'] == 7


def test_history_is_bounded_detached_and_episode_end_duration_is_real_clock_delta():
    policy, clock = active()
    for _ in range(PAUSE_HISTORY_LIMIT+5):
        clock.advance()
        policy.observe('publish_lag')
        three_clean(policy, clock)
    snapshot = policy.metrics()
    assert len(snapshot['history']) == PAUSE_HISTORY_LIMIT
    assert snapshot['history'][-1]['duration_ns'] == 3*NS
    assert snapshot['history'][-1]['end_ns']-snapshot['history'][-1]['start_ns'] == 3*NS
    assert snapshot['history'][-1]['outcome'] == 'resumed'
    snapshot['history'][-1]['reason'] = 'external mutation'
    snapshot['pause_reason_counts'].clear()
    assert policy.metrics()['history'][-1]['reason'] == 'publish_lag'
    assert policy.metrics()['pause_reason_counts']
    assert len(json.dumps(policy.metrics())) < 16_384


def test_sticky_harm_during_pause_closes_interval_and_never_resumes():
    policy, clock = active()
    policy.observe('monitor_stale')
    clock.advance(5)
    result = policy.observe('live_error', exception=TimeoutError(), ready=True)
    assert result.action == 'sticky_off' and result.reason == 'live_error'
    assert policy.metrics()['history'][-1]['outcome'] == 'sticky'
    assert policy.metrics()['history'][-1]['duration_ns'] == 5*NS
    assert three_clean(policy, clock) == ['none', 'none', 'none']


def test_cancellation_is_control_flow_not_monitor_noise():
    policy, _ = active()
    with pytest.raises(asyncio.CancelledError):
        policy.observe(exception=asyncio.CancelledError())
    assert policy.state == 'active'


@pytest.mark.parametrize('invalid', [-1, 1.5, True, float('nan')])
def test_invalid_clock_is_not_a_clean_permission(invalid):
    policy = WarningPausePolicy()
    with pytest.raises(ValueError):
        policy.clean(invalid)


def test_clock_regression_refuses_and_alert_delta_deduplicates():
    clock = Clock()
    policy = WarningPausePolicy(clock_ns=clock)
    policy.observe('monitor_unreadable')
    clock.advance(601)
    assert policy.observe('monitor_unreadable').new_alert_reasons == ('pause_over_10min',)
    assert policy.observe('monitor_unreadable').new_alert_reasons == ()
    with pytest.raises(ValueError, match='regressed'):
        policy.clean(0)


@pytest.mark.parametrize('reason', sorted(RECONNECT_EVENT_REASONS))
def test_one_actual_disconnect_event_pauses_then_three_ready_reads_resume(reason):
    policy, clock = active()
    clock.advance()
    assert policy.observe(reason).action == 'pause'
    for _ in range(30):
        clock.advance()
        assert policy.observe('provider_disconnected').action == 'none'
    metrics = policy.metrics()
    assert metrics['reconnect_event_count'] == 1
    assert metrics['reconnect_events_last_600s'] == 1
    assert metrics['wire_pause_count'] == 1 and metrics['recovery_interruptions'] == 0
    assert three_clean(policy, clock) == ['none', 'none', 'resume']


def test_four_distinct_callbacks_in_ten_minutes_latch_storm_even_between_clean_recoveries():
    policy, clock = active()
    for reason in ('disconnect', 'policy_1008', 'reconnect_1012'):
        clock.advance(100)
        assert policy.observe(reason).action == 'pause'
        assert three_clean(policy, clock)[-1] == 'resume'
    clock.advance(100)
    decision = policy.observe('disconnect')
    assert decision.action == 'sticky_off' and decision.reason == 'reconnect_storm'
    assert decision.reason_class == ReasonClass.HARM
    metrics = policy.metrics()
    assert metrics['reconnect_event_count'] == 4
    assert len(metrics['reconnect_event_times_ns']) == metrics['reconnect_history_limit'] == 4
    assert metrics['reconnect_count_semantics'] == 'distinct_callbacks_only_not_provider_disconnected_polls'
    clock.advance(601)
    assert three_clean(policy, clock) == ['none', 'none', 'none']
    assert policy.state == 'off' and policy.reason == 'reconnect_storm'
    assert policy.metrics()['reconnect_events_last_600s'] == 0


def test_four_callbacks_without_clean_reads_still_storm_not_four_wire_pauses():
    policy, clock = active()
    for index in range(4):
        clock.advance()
        decision = policy.observe('disconnect')
        assert decision.state == ('off' if index == 3 else 'paused')
    assert policy.metrics()['wire_pause_count'] == 1
    assert policy.metrics()['reconnect_event_count'] == 4


@pytest.mark.parametrize('elapsed,storm', [(599_999_999_999, True), (600_000_000_000, False)])
def test_reconnect_rolling_window_exact_boundary(elapsed, storm):
    policy, clock = active()
    start = clock.ns
    for _ in range(3):
        policy.observe('disconnect')
    clock.ns = start+elapsed
    decision = policy.observe('policy_1008')
    assert decision.state == ('off' if storm else 'paused')
    assert policy.metrics()['reconnect_events_last_600s'] == (4 if storm else 1)


def test_disconnected_polls_alone_never_count_or_storm_and_do_not_bypass_readiness():
    policy, clock = active()
    for _ in range(100):
        clock.advance()
        policy.observe('provider_disconnected', ready=True)
    metrics = policy.metrics()
    assert metrics['state'] == 'paused' and metrics['reconnect_event_count'] == 0
    assert metrics['reconnect_event_times_ns'] == []
    assert policy.observe(ready=False).clean_reads == 0
    assert three_clean(policy, clock)[-1] == 'resume'
