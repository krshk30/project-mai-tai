"""Operator 07:2x: other-service log observations do not control A."""

from dataclasses import replace
import inspect
import textwrap
import time

import pytest

from project_mai_tai.market_data import momentum_warning_watchdog as runtime
from project_mai_tai.market_data.momwarn_pause_policy import WarningPausePolicy
from project_mai_tai.momwarn_cohort_lateness import CohortObservation, MINUTE_NS, NS
from tests.unit.test_momwarn1_live_health import setup as native_setup, start_native, line
from tests.unit.test_momwarn1_gateway import health_reader as component_reader, healthy, service, aggregate

setup = native_setup
health_reader = component_reader


@pytest.mark.asyncio
@pytest.mark.parametrize("name", runtime.WarningHealthReader.LOG_NAMES)
@pytest.mark.parametrize("kind", ["ERROR", "Traceback"])
async def test_each_log_observes_errors_and_tracebacks_without_pause(setup, name, kind):
    env = setup()
    svc, provider, redis = await start_native(env)
    owners, saved = dict(svc._desired_symbols_by_consumer), dict(redis.hashes)
    raw = (line(env.clock.ns, "[ERRCLASS-CONTROL] failure").replace(" INFO ", " ERROR ")
           if kind == "ERROR" else "Traceback (most recent call last):\n  CONTROLLED other-bot failure\n")
    with (env.directory / name).open("a") as stream:
        stream.write(raw)
    await svc._warning_monitor_once()
    await svc._warning_monitor_once()
    metrics = svc.warning_metrics()
    assert svc._warning_active and svc._warning_revert_reason is None
    assert metrics["pause_state"]["wire_pause_count"] == 0
    assert metrics["error_lines_by_log"][name] == (1 if kind == "ERROR" else 0)
    assert metrics["traceback_lines_by_log"][name] == (1 if kind == "Traceback" else 0)
    if kind == "ERROR":
        assert metrics["last_error_tag"] == "ERRCLASS-CONTROL"
    assert provider._ws.removed == [] and provider._ws.added.count("A.*") == 1
    assert svc._desired_symbols_by_consumer == owners and redis.hashes == saved
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}


def test_retained_error_boolean_is_truthful_but_never_trips_health():
    health = replace(healthy(), new_live_error=True)
    watchdog = runtime.WarningWatchdog()
    assert watchdog.check(health, publish_lag_seconds=0) is None
    for _ in range(4):
        assert watchdog.check_recoverable(health, publish_lag_seconds=0) is None


def test_restore_error_trip_mutation_kills_operator_positive():
    watchdog = runtime.WarningWatchdog()
    health = replace(healthy(), new_live_error=True)

    def operator_positive():
        assert watchdog.check_recoverable(health, publish_lag_seconds=0) is None
    operator_positive()
    watchdog.check_recoverable = lambda *args, **kwargs: "live_error"
    with pytest.raises(AssertionError):
        operator_positive()


def test_count_split_tokens_once_and_reset_counters_only_with_new_reader(health_reader):
    reader, _, directory = health_reader
    reader.read()
    path = directory / "strategy.log"
    for fragment in (b"[SPLIT-TAG] ERR", b"OR failed ERROR again\nTraceback (most recent ", b"call last):\n"):
        with path.open("ab") as stream:
            stream.write(fragment)
        reader.read()
        reader.read()
    assert reader.error_observations()["error_lines_by_log"][path.name] == 1
    assert reader.error_observations()["traceback_lines_by_log"][path.name] == 1
    assert reader.error_observations()["last_error_tag"] == "SPLIT-TAG"
    reader.reset_window()
    reader.read()
    assert reader.error_observations()["error_lines_by_log"][path.name] == 1
    assert runtime.WarningHealthReader(str(reader.health_path), str(directory)).error_observations()["error_lines_by_log"][path.name] == 0


def test_huge_line_overlap_error_and_traceback_count_once(health_reader):
    reader, _, directory = health_reader
    reader.read()
    path = directory / "strategy.log"
    raw = (b"[HUGE-TAG] " + b" " * (reader.MAX_LOG_PASS_BYTES - 15)
           + b"ERROR " + b" " * 80000 + b"Traceback (most recent call last):" + b" " * 80000 + b"\n")
    with path.open("ab") as stream:
        stream.write(raw)
    with pytest.raises(ValueError, match="catch-up incomplete"):
        reader.read()
    reader.read()
    reader.read()
    assert reader.error_observations()["error_lines_by_log"][path.name] == 1
    assert reader.error_observations()["traceback_lines_by_log"][path.name] == 1
    assert reader.error_observations()["last_error_tag"] == "HUGE-TAG"


def test_short_anchor_failure_never_commits_counts_without_cursor(health_reader, monkeypatch):
    reader, _, directory = health_reader
    reader.read()
    path = directory / "strategy.log"
    with path.open("ab") as stream:
        stream.write(b"ERROR [ANCHOR-TAG] observed\n")
    original = runtime.cursor_anchor
    calls = []

    def fail_once(fd, offset):
        import os
        if os.fstat(fd).st_ino == path.stat().st_ino and offset == path.stat().st_size and not calls:
            calls.append(True)
            raise ValueError("CONTROLLED anchor failure")
        return original(fd, offset)
    monkeypatch.setattr(runtime, "cursor_anchor", fail_once)
    with pytest.raises(ValueError):
        reader.read()
    assert reader.error_observations()["error_lines_by_log"][path.name] == 0
    reader.read()
    assert reader.error_observations()["error_lines_by_log"][path.name] == 1


@pytest.mark.parametrize("gate,token,reason", [
    ("load", "health.load_1m > 6", "load"),
    ("ram", "health.ram_available_bytes < 3 * 1024**3", "ram"),
    ("publish", "publish_lag_seconds > 2", "publish_lag"),
    ("v2_late", "not calibrated and lateness is not None and lateness > 5", "v2_bar_late"),
    ("oms", "self._slow_syncs >= 2", "oms_sync_slow_twice"),
])
def test_measured_health_gate_and_per_gate_mutation(monkeypatch, gate, token, reason):
    def positive():
        watchdog = runtime.WarningWatchdog()
        health = healthy()
        changes = {"load": dict(load_1m=7), "ram": dict(ram_available_bytes=1),
                   "v2_late": dict(v2_bar_lateness_seconds=5.001), "oms": dict(oms_sync_seconds=6)}
        lag = 2.001 if gate == "publish" else 0
        health = replace(health, **changes.get(gate, {}))
        if gate == "oms":
            assert watchdog.check(health, publish_lag_seconds=lag) is None
            health = replace(health, oms_sync_seq=2)
        assert watchdog.check(health, publish_lag_seconds=lag) == reason
    positive()
    source = textwrap.dedent(inspect.getsource(runtime.WarningWatchdog.check))
    assert token in source
    namespace = dict(runtime.__dict__)
    exec(compile(source.replace(token, "False"), "<controlled_gate_mutation>", "exec"), namespace)
    monkeypatch.setattr(runtime.WarningWatchdog, "check", namespace["check"])
    with pytest.raises(AssertionError):
        positive()


def test_calibrated_two_cohort_harm_and_mutation(monkeypatch):
    due = 1791890100 * NS  # Oct13 07:15 ET, full minute.
    clock = [due + 6 * NS]
    monkeypatch.setattr(time, "time_ns", lambda: clock[0])

    def positive():
        watchdog = runtime.WarningWatchdog()
        for index in range(2):
            clock[0] = due + index * MINUTE_NS + 6 * NS
            observation = CohortObservation(due + index * MINUTE_NS, clock[0],
                "strategy-state-isolated", True, ("AAA",), (), (),
                (("AAA", due + index * MINUTE_NS + 5 * NS),), "CONTROLLED")
            health = replace(healthy(), v2_bar_lateness_seconds=5, v2_cohort=observation.payload())
            reason = watchdog.check_recoverable(health, publish_lag_seconds=0)
        assert reason == "v2_cohort_late_twice"
    positive()
    for name, token in (("check", "elif cohort_reason:"),
                        ("check_recoverable", "if self._cohort.streak >= 2:")):
        source = textwrap.dedent(inspect.getsource(getattr(runtime.WarningWatchdog, name)))
        namespace = dict(runtime.__dict__)
        exec(compile(source.replace(token, token.split(" ")[0] + " False:"),
                     "<controlled_cohort_mutation>", "exec"), namespace)
        monkeypatch.setattr(runtime.WarningWatchdog, name, namespace[name])
    with pytest.raises(AssertionError):
        positive()


def test_reconnect_storm_harm_and_threshold_mutation(monkeypatch):
    from project_mai_tai.market_data import momwarn_pause_policy as policy

    def positive():
        pause = WarningPausePolicy()
        for index in range(4):
            pause.trip("reconnect_1012", now_ns=index * NS)
        assert pause.state == "off" and pause.reason == "reconnect_storm"
    positive()
    monkeypatch.setattr(policy, "RECONNECT_THRESHOLD", 100)
    with pytest.raises(AssertionError):
        positive()


@pytest.mark.asyncio
async def test_A_queue_drop_policy_and_mutation_unchanged(setup, monkeypatch):
    env = setup()
    svc, provider, _, _ = service(market_data_momentum_warning_queue_maxsize=1)
    svc._warning_health_reader, svc._warning_health_redis = env.reader, env.client
    from tests.unit.test_momwarn1_live_health import three_clean_native_reads
    await three_clean_native_reads(svc, env)
    record = provider._normalize_warning_aggregate(aggregate(), "WFF")

    def positive():
        svc._enqueue_warning(record)
        svc._enqueue_warning(record)
        assert svc._warning_queue.qsize() == 1 and svc._warning_queue_drop_count == 1
        assert svc._warning_revert_reason is None  # Baseline drops oldest; it has no overflow latch.
    positive()
    svc._warning_queue.get_nowait()
    svc._warning_queue_drop_count = 0
    source = textwrap.dedent(inspect.getsource(type(svc)._enqueue_warning))
    namespace = dict(__import__(type(svc).__module__, fromlist=["*"]).__dict__)
    exec(compile(source.replace("self._warning_queue_drop_count += 1", "pass"),
                 "<controlled_queue_counter_mutation>", "exec"), namespace)
    monkeypatch.setattr(type(svc), "_enqueue_warning", namespace["_enqueue_warning"])
    with pytest.raises(AssertionError):
        positive()
