from dataclasses import replace
from datetime import UTC, datetime
import json
import logging

import pytest

from project_mai_tai.momwarn_cohort_lateness import (
    CohortObservation, CohortTracker, CompleteCohortPolicy, LIMITS_NS, MINUTE_NS, NS,
    POLICY, first_eligible_bar_ns, replay_events,
)
from project_mai_tai.momwarn_live_health import LiveWarningHealthSource
from project_mai_tai.momwarn_health_contract import proposed_health_v2
from project_mai_tai.market_data.momentum_warning_watchdog import WarningWatchdog
from tests.unit.test_momwarn1_live_health import setup as setup, NOW, line
from tests.unit.test_momwarn1_d2_startup import advance
from tests.unit.test_momwarn1_gateway import service, event

DUE = NOW//MINUTE_NS*MINUTE_NS


def complete(due=DUE, late_ns=5*NS):
    return CohortObservation(due, due+late_ns, "strategy-state-isolated", True, ("AAA",), (), (),
                             (("AAA", due+late_ns),), "first_native_receipts")


@pytest.mark.parametrize("offset,extra", [(0,1), (1,2), (MINUTE_NS-1,2)])
def test_first_full_bucket_exact_integer_semantics(offset, extra):
    assert first_eligible_bar_ns(DUE+offset) == DUE+extra*MINUTE_NS


def test_incomplete_neither_trips_nor_resets_and_two_late_complete_fire_across_unknown_clock_gap():
    policy = CompleteCohortPolicy()
    assert policy.observe(complete()) is None and policy.streak == 1
    missing = CohortObservation(DUE+MINUTE_NS, DUE+MINUTE_NS+8*NS, "strategy-state-isolated",
                                True, ("AAA",), (), ("AAA",), (), "completion_missing_UNKNOWN")
    for _ in range(10):
        assert policy.observe(missing) is None
    assert policy.streak == 1 and policy.incomplete_minutes == 1
    assert policy.metrics()["lateness_seconds"] is None
    assert policy.observe(complete(DUE+4*MINUTE_NS)) == "v2_cohort_late_twice"
    assert policy.gap_minutes == 2


def test_on_time_complete_resets_only_once_and_threshold_boundary_is_not_late():
    policy = CompleteCohortPolicy()
    assert policy.observe(complete(late_ns=LIMITS_NS["PM"]+1)) is None
    same = complete(late_ns=LIMITS_NS["PM"]+1)
    for _ in range(10):
        assert policy.observe(same) is None
    assert policy.complete_minutes == policy.streak == 1
    assert policy.observe(complete(DUE+MINUTE_NS, LIMITS_NS["PM"])) is None
    assert policy.streak == 0


@pytest.mark.parametrize("a,b", [
    ("2026-10-13T13:29:00+00:00", "2026-10-13T13:30:00+00:00"),
    ("2026-10-13T19:59:00+00:00", "2026-10-13T20:00:00+00:00"),
    ("2026-10-12T12:00:00+00:00", "2026-10-13T12:00:00+00:00"),
])
def test_date_and_window_boundaries_reset_streak_not_unknown_inside_same_window(a,b):
    first = int(datetime.fromisoformat(a).timestamp())*NS
    second = int(datetime.fromisoformat(b).timestamp())*NS
    policy = CompleteCohortPolicy()
    assert policy.observe(complete(first, 6*NS)) is None and policy.streak == 1
    assert policy.observe(complete(second, 6*NS)) is None and policy.streak == 1
    assert policy.observe(complete(second+MINUTE_NS, 6*NS)) == "v2_cohort_late_twice"


def test_incomplete_can_become_complete_later_no_fabricated_zero_or_duplicate_count():
    tracker = CohortTracker()
    tracker.observe_membership(["AAA"], DUE-5*MINUTE_NS)
    policy = CompleteCohortPolicy()
    missing = tracker.observation(DUE, DUE+NS, {}, source="strategy-state-isolated")
    assert missing.status == "INCOMPLETE" and missing.lateness_ns is None
    assert policy.observe(missing) is None
    done = tracker.observation(DUE, DUE+6*NS, {"AAA":DUE+5*NS}, source="strategy-state-isolated")
    assert policy.observe(done) is None
    assert policy.complete_minutes == policy.incomplete_minutes == policy.streak == 1
    assert policy.metrics()["incomplete_sampled_minutes"] == 1
    assert "MAY_LATER_COMPLETE" in policy.metrics()["incomplete_count_semantics"]
    changed = replace(done, first_receipts=(("AAA",DUE+6*NS),))
    with pytest.raises(ValueError, match="altered"):
        policy.observe(changed)


def test_initial_monitor_join_and_named_seed_exclude_first_full_bucket_not_stale_bar():
    tracker = CohortTracker()
    tracker.observe_membership(["AAA"], DUE+NS)
    assert not tracker.observation(DUE, DUE+2*NS, {}, source="strategy-state-isolated").known
    for delta in (1,2):
        item = tracker.observation(DUE+delta*MINUTE_NS, DUE+delta*MINUTE_NS+NS,
                                   {}, source="strategy-state-isolated")
        assert item.status == "EXCLUDED"
    due = DUE+3*MINUTE_NS
    assert tracker.observation(due, due+NS, {"AAA":due+NS}, source="strategy-state-isolated").status == "COMPLETE"
    tracker.observe_seed("AAA", due+NS, "V2-WARMUP-LINE")
    assert tracker.observation(due+2*MINUTE_NS, due+2*MINUTE_NS+NS, {}, source="strategy-state-isolated").status == "EXCLUDED"
    with pytest.raises(ValueError):
        tracker.observe_seed("AAA", due, "initial_cached_bar")


@pytest.mark.parametrize("change", [
    {"observed_ns":DUE-1}, {"due_ns":DUE+1}, {"known":True, "missing":("BOGUS",)},
    {"first_receipts":(("AAA",DUE-1),)}, {"first_receipts":(("AAA",DUE+6*NS),)},
    {"source":"invented_source"},
])
def test_future_or_malformed_observation_is_invalid_not_unknown_permission(change):
    with pytest.raises(ValueError):
        replace(complete(), **change).validate()


def cal_env(setup):
    env = setup(now=NOW-4*MINUTE_NS)
    cfg = dict(env.cfg, v2_lateness_policy=POLICY)
    env.config.write_text(json.dumps(cfg))
    env.source = LiveWarningHealthSource(cfg, env.directory, proc=env.proc, wall_ns=lambda:env.clock.ns)
    env.reader._source, env.reader._source_config = env.source, dict(cfg)
    return env


@pytest.mark.asyncio
async def test_native_unknown_receipt_and_health_line_counted_without_revert(setup, caplog):
    env = cal_env(setup)
    svc, provider, _, _ = service()
    svc._warning_health_reader, svc._warning_redis = env.reader, env.client
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    caplog.set_level(logging.INFO)
    for index in range(3):
        await svc._warning_monitor_once()
        assert not svc._warning_revert_reason
        assert svc._warning_active is (index == 2)
    assert svc._warning_active and not svc._warning_revert_reason
    health = await env.reader.read_live(env.client, "test")
    assert health.v2_bar_lateness_seconds is None
    assert health.v2_cohort["status"] == "INCOMPLETE"
    assert svc.warning_metrics()["v2_cohort"]["incomplete_minutes"] == 1
    assert "status=INCOMPLETE" in caplog.text and "lateness=UNKNOWN" in caplog.text
    assert provider._subscriptions == {"WFF"}


@pytest.mark.asyncio
async def test_native_first_receipt_not_later_duplicate_and_two_complete_late_minutes(setup):
    env = cal_env(setup)
    await env.reader.read_live(env.client, "test")
    advance(env, NOW, symbols=("AAA", "BBB"), late_ns=5*NS)
    # The log cannot contain a future receipt; the read happens at due+6 s.
    env.clock.ns = DUE+6*NS
    watchdog = WarningWatchdog()
    health = await env.reader.read_live(env.client, "test")
    assert health.v2_bar_lateness_seconds == 5
    assert watchdog.check(health, publish_lag_seconds=0) is None
    with (env.directory/"schwab-1m-v2.log").open("a") as out:
        out.write(line(env.clock.ns, f"[V2-MACD-PROBE] sym=BBB ts_ms={DUE//10**6-60000} "))
    assert (await env.reader.read_live(env.client, "test")).v2_bar_lateness_seconds == 5
    advance(env, DUE+MINUTE_NS+6*NS, symbols=("AAA",))
    health = await env.reader.read_live(env.client, "test")
    assert health.v2_cohort["status"] == "INCOMPLETE"
    assert watchdog.check(health, publish_lag_seconds=0) is None
    assert watchdog.cohort_metrics()["late_streak"] == 1
    advance(env, DUE+2*MINUTE_NS+6*NS, symbols=("AAA", "BBB"))
    health = await env.reader.read_live(env.client, "test")
    assert watchdog.check(health, publish_lag_seconds=0) == "v2_cohort_late_twice"


@pytest.mark.asyncio
@pytest.mark.parametrize("change,lag,reason", [
    ({"load_1m":6.01},0,"load"), ({"ram_available_bytes":3*1024**3-1},0,"ram"),
    ({"new_live_error":True},0,"live_error"), ({},2.01,"publish_lag"),
    ({"observed_wall_ns":NOW},0,"monitor_invalid"),
])
async def test_independent_revert_gates_still_strict_with_incomplete_cohort(setup, change, lag, reason):
    env = cal_env(setup)
    health = await env.reader.read_live(env.client, "test")
    assert health.v2_cohort["status"] == "INCOMPLETE"
    assert WarningWatchdog().check(replace(health, **change), publish_lag_seconds=lag) == reason


@pytest.mark.asyncio
async def test_cal_does_not_change_d2_startup_readiness_timeout(setup):
    env = cal_env(setup)
    env.reader.bind_gateway_start("controlled", env.clock.ns)
    health = await env.reader.read_live(env.client, "test")
    assert health.v2_startup_pending and health.v2_cohort is None
    deadline = health.v2_startup_deadline_ns
    advance(env, deadline+NS)
    health = await env.reader.read_live(env.client, "test")
    assert WarningWatchdog().check(health, publish_lag_seconds=0) == "v2_startup_timeout"


@pytest.mark.asyncio
async def test_offline_cohort_is_valid_only_for_replay_never_native_live_receipt(setup):
    env = cal_env(setup)
    live = env.source.read(env.state())
    offline = dict(live, v2_cohort=dict(live["v2_cohort"], source="retained_watchlist_log"))
    # Pure policy/replay accepts retained log evidence but the LIVE adapters do not.
    assert CohortObservation.read(offline["v2_cohort"]).source == "retained_watchlist_log"
    with pytest.raises(ValueError, match="current source/watchlist"):
        proposed_health_v2(offline, now_ns=env.clock.ns)
    health = await env.reader.read_live(env.client, "test")
    assert WarningWatchdog().check(replace(health, v2_cohort=offline["v2_cohort"]),
                                   publish_lag_seconds=0) == "monitor_invalid"


@pytest.mark.asyncio
async def test_actual_local_native_read_serializes_through_cal_f3_adapter_without_health_claim(setup):
    from dataclasses import asdict
    from tests.unit.test_momwarn1_install_receipt_adapter import adapter

    env = cal_env(setup)
    health = await env.reader.read_live(env.client, "test")
    assert health.v2_cohort["status"] == "INCOMPLETE" and health.v2_bar_lateness_seconds is None
    now = datetime.fromtimestamp(env.clock.ns / NS, UTC)
    assert adapter.native_health(asdict(health), final_observed_ns=env.clock.ns, now=now) == env.clock.ns


@pytest.mark.asyncio
async def test_cal_unclosed_first_bar_is_still_d2_pending_not_calibrated_health(setup):
    env = cal_env(setup)
    env.reader.bind_gateway_start("controlled", env.clock.ns)
    initial = await env.reader.read_live(env.client, "test")
    due = initial.v2_startup_deadline_ns - 5*NS
    advance(env, due+5*NS, symbols=("AAA", "BBB"), late_ns=-NS)
    pending = await env.reader.read_live(env.client, "test")
    assert pending.v2_startup_pending and pending.v2_cohort is None
    assert WarningWatchdog().check(pending, publish_lag_seconds=0) is None


@pytest.mark.asyncio
async def test_native_cal_current_unclosed_receipt_is_invalid_not_incomplete_healthy(setup):
    env = cal_env(setup)
    due = env.clock.ns//MINUTE_NS*MINUTE_NS
    with (env.directory/"schwab-1m-v2.log").open("a") as out:
        out.write(line(due-NS, f"[V2-MACD-PROBE] sym=BAD ts_ms={due//10**6-60000} "))
    with pytest.raises(ValueError, match="precedes bar close"):
        await env.reader.read_live(env.client, "test")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["permission", "pid", "stale_watch", "partial_watch", "future_log", "config"])
async def test_cal_does_not_swallow_independent_native_source_refusals(setup, monkeypatch, fault):
    env = cal_env(setup)
    await env.reader.read_live(env.client, "test")
    if fault == "permission":
        monkeypatch.setattr(env.reader, "_read_file", lambda: (_ for _ in ()).throw(PermissionError("controlled")))
    elif fault == "pid":
        (env.proc/"13/stat").unlink()
    elif fault == "future_log":
        with (env.directory/"schwab-1m-v2.log").open("a") as out:
            out.write(line(env.clock.ns+NS, "[V2-WARMUP-LINE] AAA invalid future"))
    elif fault == "config":
        env.config.write_text(env.config.read_text().replace("CONTROLLED-proc15", "CHANGED"))
    else:
        state = env.state()
        if fault == "stale_watch":
            state["produced_at"] = datetime.fromtimestamp((env.clock.ns-11*NS)/NS, UTC).isoformat()
        else:
            state["payload"]["watchlist"] = None
        env.client.xrevrange.side_effect = lambda *_args, **_kwargs:[("1-0", {"data":json.dumps(state)})]
    with pytest.raises((OSError, ValueError)):
        await env.reader.read_live(env.client, "test")


def test_replay_preserves_first_capture_receipt_excludes_seed_and_does_not_reset_unknown_gap():
    start = DUE-4*MINUTE_NS
    events = [{"kind":"membership", "observed_ns":start, "symbols":["AAA"], "count":1, "complete":True}]
    for due, lateness in ((DUE,5*NS), (DUE,8*NS), (DUE+2*MINUTE_NS,5*NS)):
        events.append({"kind":"completion", "observed_ns":due+lateness,
                       "symbol":"AAA", "bar_ms":due//10**6-60000})
    capture = {"rows":events, "as_of_ns":DUE+3*MINUTE_NS}
    result = replay_events(capture, first_due_ns=start, last_due_ns=DUE+2*MINUTE_NS)
    assert result["duplicate_receipts_ignored"] == 1
    first = next(row for row in result["rows"] if row["due_ns"] == DUE)
    assert first["lateness_seconds"] == 5
    assert len(result["trips"]) == 1 and result["trips"][0]["due_ns"] == DUE+2*MINUTE_NS
    missing = next(row for row in result["rows"] if row["due_ns"] == DUE+MINUTE_NS)
    assert missing["status"] == "INCOMPLETE" and missing["lateness_seconds"] is None
