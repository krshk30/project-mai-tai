"""CONTROLLED log/proc/Redis-source wiring, not a production readiness receipt."""

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import pytest

from project_mai_tai.market_data import gateway, massive_provider
from project_mai_tai.market_data.momentum_warning_watchdog import WarningHealthReader, WarningWatchdog
from project_mai_tai.momwarn_live_health import LiveWarningHealthSource, newest_v2_state
from project_mai_tai.momwarn_health_contract import proposed_health_v2
from tests.unit.test_momwarn1_gateway import aggregate, event, service

NOW = int(datetime(2026, 10, 13, 12, 51, 4, tzinfo=UTC).timestamp()) * 1_000_000_000
NS = 1_000_000_000


def line(at, message):
    return datetime.fromtimestamp(at / NS, UTC).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3] + " INFO " + message + "\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def build(*, pending=False, now=NOW):
        clock = SimpleNamespace(ns=now)
        monkeypatch.setattr(time, "time_ns", lambda: clock.ns)
        pm = SimpleNamespace(now=lambda _: datetime.fromtimestamp(clock.ns / NS, UTC))
        monkeypatch.setattr(gateway, "datetime", pm)
        monkeypatch.setattr(massive_provider, "datetime", pm)
        proc = tmp_path / "proc"
        for pid, ticks in ((12, 100), (13, 101)):
            path = proc / str(pid)
            path.mkdir(parents=True, exist_ok=True)
            fields = ["0"] * 20
            fields[0], fields[19] = "S", str(ticks)
            (path / "stat").write_text(f"{pid} (controlled) " + " ".join(fields))
        cfg = dict(source_config_version=1, oms_pid=12, v2_pid=13,
                   oms_instance_id="12:100", v2_instance_id="13:101",
                   oms_started_ns=now-300*NS, v2_started_ns=now-300*NS,
                   verified_timezone="UTC", source_timezone_ref="CONTROLLED-UTC",
                   oms_sync_interval_seconds=15, oms_cadence_source="CONTROLLED-proc15",
                   v2_probe_symbols=["*"], v2_probe_source_ref="CONTROLLED-proc-probe-all",
                   v2_expectation_ref="CONTROLLED-same-day-first0700",
                   v2_expectation_kind="recorded_first_same_day_0700",
                   v2_first_bar_start_et="07:00", v2_bar_interval_seconds=60,
                   v2_first_bar_source_ref="CONTROLLED-reviewed-first-bar-source",
                   v2_expected_start_et="07:00", v2_source_lane="schwab_pricehistory_same_day")
        config = tmp_path / "health.json"
        config.write_text(json.dumps(cfg))
        (tmp_path / "oms.log").write_text(line(now-NS, "[OMS-BROKER-SYNC-PASS] id=10 "
            "phase=end outcome=ok duration_ms=2300.000 exceeds_note_age=False"))
        due = now // (60*NS) * (60*NS)
        bar_ms = due // 1_000_000 - 60_000
        probes = line(now-90*NS, "[schwab-1m-v2] controlled startup")
        probes += line(due+NS, f"[V2-MACD-PROBE] sym=AAA ts_ms={bar_ms} close=10.000000 ")
        if not pending:
            probes += line(now, f"[V2-MACD-PROBE] sym=BBB ts_ms={bar_ms} close=10.000000 ")
        (tmp_path / "schwab-1m-v2.log").write_text(probes)
        for name in ("strategy.log", "orb-schwab.log"):
            (tmp_path / name).write_text("old baseline\n")
        mem = tmp_path / "meminfo"
        mem.write_text("MemAvailable: 4194304 kB\n")
        reader = WarningHealthReader(str(config), str(tmp_path))
        original_open = reader._open_regular
        reader._open_regular = lambda path: original_open(mem if path == Path("/proc/meminfo") else path)
        monkeypatch.setattr("os.getloadavg", lambda: (1, 1, 1))
        source = LiveWarningHealthSource(cfg, tmp_path, proc=proc, wall_ns=lambda: clock.ns)
        reader._source, reader._source_config = source, dict(cfg)

        def state(**changes):
            payload = dict(watchlist=["AAA", "BBB"], restoration_complete=True)
            payload.update(changes)
            return dict(source_service="schwab-1m-v2",
                        produced_at=datetime.fromtimestamp(clock.ns / NS, UTC).isoformat(), payload=payload)

        client = AsyncMock()
        client.xrevrange.side_effect = lambda *args, **kwargs: [("1-0", {"data": json.dumps(state())})]
        return SimpleNamespace(clock=clock, cfg=cfg, config=config, reader=reader, source=source,
                               state=state, client=client, directory=tmp_path, proc=proc, mem=mem)
    return build


@pytest.mark.asyncio
async def test_actual_bounded_sources_retain_completion_and_complete_due_coverage(setup):
    env = setup()
    first = await env.reader.read_live(env.client, "test")
    assert first.oms_sync_seconds == 2.3 and first.v2_bar_lateness_seconds == 4
    assert not first.v2_not_applicable
    end = env.source.oms.latest["oms_measured_wall_ns"]
    env.clock.ns += 6*NS
    second = await env.reader.read_live(env.client, "test")
    assert second.observed_wall_ns == env.clock.ns
    assert env.source.oms.latest["oms_measured_wall_ns"] == end
    assert end == NOW-NS and second.oms_sync_seq == first.oms_sync_seq
    assert WarningWatchdog().check(second, publish_lag_seconds=0) is None
    env.client.xrevrange.assert_called_with("test:strategy-state-isolated", count=32)


@pytest.mark.parametrize("case", ["missing_watchlist", "empty_startup", "subset_probe",
                                  "missing_expectation", "pid_reused", "stale_state"])
def test_missing_or_partial_source_evidence_is_unknown_not_zero(setup, case):
    env = setup()
    state = env.state()
    if case == "missing_watchlist":
        state["payload"].pop("watchlist")
    elif case == "empty_startup":
        state["payload"].update(watchlist=[], restoration_complete=False)
    elif case == "subset_probe":
        env.source.config["v2_probe_symbols"] = ["AAA"]
    elif case == "missing_expectation":
        cfg = dict(env.cfg, v2_expectation_ref="")
        with pytest.raises(ValueError):
            LiveWarningHealthSource(cfg, env.directory, proc=env.proc)
        return
    elif case == "pid_reused":
        path = env.proc / "13" / "stat"
        path.write_text(path.read_text().rsplit(" ", 1)[0] + " 999")
    else:
        state["produced_at"] = datetime.fromtimestamp((NOW-11*NS)/NS, UTC).isoformat()
    with pytest.raises(ValueError):
        env.source.read(state)


def test_positive_empty_watchlist_not_applicable_lateness_is_null(setup):
    env = setup()
    payload = env.source.read(env.state(watchlist=[]))
    assert payload["v2_applicability"] == "NOT_APPLICABLE"
    assert payload["v2_bar_lateness_seconds"] is None
    health = env.reader.read(measured=payload)
    assert health.v2_not_applicable and health.v2_bar_lateness_seconds is None
    assert WarningWatchdog().check(health, publish_lag_seconds=0) is None


def test_sourced_pre07_vs_missing_expectation_never_invents_zero(setup):
    early = int(datetime(2026, 10, 13, 8, 11, 4, tzinfo=UTC).timestamp())*NS
    env = setup(now=early)
    payload = env.source.read(env.state())
    assert payload["v2_applicability"] == "NOT_APPLICABLE"
    assert payload["v2_bar_lateness_seconds"] is None
    env.source.config["v2_expectation_kind"] = "absent_bars_only"
    with pytest.raises(ValueError):
        env.source.read(env.state())


def test_shared_state_selects_newest_actual_v2_not_other_producer(setup):
    env = setup()
    foreign = {"source_service": "momentum-paper"}
    entries = [("2-0", {"data": json.dumps(foreign)}), ("1-0", {"data": json.dumps(env.state())})]
    assert newest_v2_state(entries) == env.state()
    with pytest.raises(ValueError):
        newest_v2_state(entries[:1])
    with pytest.raises(ValueError):
        newest_v2_state(entries*17)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,reason", [
    ("late", "v2_bar_late"), ("load", "load"), ("ram", "ram"),
    ("error", "live_error"), ("slow_twice", "oms_sync_slow_twice"),
    ("unreadable", "monitor_unreadable"), ("lag", "publish_lag"),
])
async def test_gateway_g3_live_source_wire_fires_A_only_every_health_trigger(setup, monkeypatch, failure, reason):
    env = setup(pending=failure == "late")
    svc, provider, _, _ = service()
    svc._warning_health_reader, svc._warning_redis = env.reader, env.client
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    await svc._warning_monitor_once()
    assert svc._warning_active
    provider._handle_messages([aggregate(s=NOW//1_000_000-1000, e=NOW//1_000_000)])
    if failure == "late":
        env.clock.ns += 2*NS
    elif failure == "load":
        monkeypatch.setattr("os.getloadavg", lambda: (6.1, 1, 1))
    elif failure == "ram":
        env.mem.write_text("MemAvailable: 2097152 kB\n")
    elif failure == "error":
        with (env.directory / "strategy.log").open("a") as out:
            out.write("ERROR new controlled error\n")
    elif failure == "unreadable":
        env.config.unlink()
    elif failure == "lag":
        seq, record = svc._warning_queue.get_nowait()
        svc._warning_queue.put_nowait((seq, replace(record, received_monotonic_ns=time.monotonic_ns()-3*NS)))
    else:
        for seq in (11, 12):
            env.clock.ns += 6*NS
            with (env.directory / "oms.log").open("a") as out:
                out.write(line(env.clock.ns, f"[OMS-BROKER-SYNC-PASS] id={seq} "
                               "phase=end outcome=ok duration_ms=6000.000 exceeds_note_age=False"))
            await svc._warning_monitor_once()
    if failure != "slow_twice":
        await svc._warning_monitor_once()
    assert svc._warning_revert_reason == reason
    assert provider._ws.removed == ["A.*"]
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}
    assert svc._warning_queue.empty()
    assert svc.warning_metrics()["dropped_total"] >= 1


@pytest.mark.asyncio
async def test_source_staleness_or_restamp_never_refreshed_as_current(setup):
    env = setup()
    await env.reader.read_live(env.client, "test")
    env.clock.ns += 20*NS
    with pytest.raises(ValueError, match="OMS cadence"):
        await env.reader.read_live(env.client, "test")
    env.clock.ns = NOW
    payload = env.source.read(env.state())
    payload["oms_measured_wall_ns"] += 1
    with pytest.raises(ValueError, match="restamped"):
        env.reader.read(measured=payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", [1, 2])
async def test_external_receipt_cannot_substitute_for_live_source_setup(setup, schema):
    env = setup()
    env.config.write_text(json.dumps(dict(schema_version=schema, observed_wall_ns=NOW,
                                         oms_sync_seq=10, oms_sync_seconds=0,
                                         v2_bar_lateness_seconds=0)))
    with pytest.raises(ValueError, match="external receipt is not live proof"):
        await env.reader.read_live(env.client, "test")
    env.client.xrevrange.assert_not_called()


def test_applicable_bar_expectation_cannot_come_from_missing_config(setup):
    env = setup()
    env.source.config.pop("v2_expectation_kind")
    with pytest.raises(ValueError, match="expectation"):
        env.source.read(env.state())


@pytest.mark.asyncio
async def test_changed_source_configuration_requires_review_not_automatic_rebaseline(setup):
    env = setup()
    await env.reader.read_live(env.client, "test")
    env.config.write_text(json.dumps(dict(env.cfg, v2_instance_id="13:999")))
    with pytest.raises(ValueError, match="configuration changed"):
        await env.reader.read_live(env.client, "test")


def test_future_probe_stamp_never_becomes_completed_bar_evidence(setup):
    env = setup()
    with (env.directory / "schwab-1m-v2.log").open("a") as out:
        out.write(line(NOW+NS, "[V2-MACD-PROBE] sym=BBB ts_ms=123 close=10 "))
    with pytest.raises(ValueError, match="future v2"):
        env.source.read(env.state())


def test_recorded_first0700_line_publication_is_completion_without_macd_probe(setup):
    # Recorded Oct12 v2 publication times; OMS/proc/Redis wiring stays CONTROLLED.
    due = int(datetime(2026, 10, 12, 11, 1, tzinfo=UTC).timestamp()) * NS
    env = setup(now=due + 6 * NS)
    bar_ms = due // 1_000_000 - 60_000
    messages = line(due - NS, "[schwab-1m-v2] controlled startup")
    for symbol, milliseconds in (("LNKS", 3478), ("MEDS", 3736), ("YMAT", 4004)):
        messages += line(due + milliseconds * 1_000_000,
            f"[V2-LINE-RESTORE] sym={symbol} epoch=1 revision=1 "
            f"current_bar_ms={bar_ms} bars=181 outcome=published live_append=0 entry_allowed=0 ")
    (env.directory / "schwab-1m-v2.log").write_text(messages)
    state = env.state(watchlist=["LNKS", "MEDS", "YMAT"])
    health = env.reader.read(measured=env.source.read(state))
    assert health.v2_bar_lateness_seconds == pytest.approx(4.004)
    assert WarningWatchdog().check(health, publish_lag_seconds=0) is None
    env.clock.ns += NS
    repeated = env.reader.read(measured=env.source.read(state))
    assert repeated.v2_bar_lateness_seconds == health.v2_bar_lateness_seconds


@pytest.mark.parametrize("outcome,minute_offset", [
    ("waiting", 0), ("failed", 0), ("superseded", 0), ("published", -60_000),
])
def test_unpublished_or_stale_line_does_not_prove_current_bar(setup, outcome, minute_offset):
    env = setup(pending=True)
    due = NOW // (60 * NS) * (60 * NS)
    bar_ms = due // 1_000_000 - 60_000 + minute_offset
    with (env.directory / "schwab-1m-v2.log").open("a") as output:
        output.write(line(NOW, f"[V2-LINE-RESTORE] sym=BBB current_bar_ms={bar_ms} "
            f"bars=181 outcome={outcome} live_append=0 entry_allowed=0 "))
    env.clock.ns += 2 * NS
    health = env.reader.read(measured=env.source.read(env.state()))
    assert health.v2_bar_lateness_seconds == 6
    assert WarningWatchdog().check(health, publish_lag_seconds=0) == "v2_bar_late"


def test_future_line_publication_is_not_completion_evidence(setup):
    env = setup(pending=True)
    bar_ms = NOW // (60 * NS) * 60_000 - 60_000
    with (env.directory / "schwab-1m-v2.log").open("a") as output:
        output.write(line(NOW + NS, f"[V2-LINE-RESTORE] sym=BBB current_bar_ms={bar_ms} "
            "bars=181 outcome=published live_append=0 entry_allowed=0 "))
    with pytest.raises(ValueError, match="future v2"):
        env.source.read(env.state())


@pytest.mark.parametrize("field,value", [
    ("v2_first_bar_start_et", None), ("v2_bar_interval_seconds", 61),
    ("v2_bar_interval_seconds", True), ("v2_first_bar_source_ref", ""),
    ("v2_source_lane", "schwab_anchored_current_day"),
])
def test_missing_or_uncalled_lane_first_due_proof_is_unknown(setup, field, value):
    at = int(datetime(2026, 10, 13, 11, 0, 59, tzinfo=UTC).timestamp())*NS
    env = setup(now=at)
    env.source.config[field] = value
    with pytest.raises(ValueError, match="first-bar"):
        env.source.read(env.state())


@pytest.mark.asyncio
async def test_reviewed_first_due_boundaries_and_missing_bar_revert_A_only(setup):
    before = int(datetime(2026, 10, 13, 11, 0, 59, tzinfo=UTC).timestamp())*NS
    due = before+NS
    env = setup(now=before)
    # No future or partial06:59 bar in the source. Baseline still covers first due.
    (env.directory / "schwab-1m-v2.log").write_text(line(before-90*NS, "controlled baseline"))
    payload = env.source.read(env.state())
    assert payload["v2_applicability"] == "NOT_APPLICABLE"
    assert payload["v2_applicability_reason"] == "sourced_before_first_due"
    assert payload["v2_first_due_wall_ns"] == due
    assert payload["v2_bar_lateness_seconds"] is None
    svc, provider, _, _ = service()
    svc._warning_health_reader, svc._warning_redis = env.reader, env.client
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    await svc._warning_monitor_once()
    assert svc._warning_active
    for at, expected_lag in ((due, 0), (due+5*NS, 5)):
        env.clock.ns = at
        payload = env.source.read(env.state())
        assert payload["v2_applicability"] == "APPLICABLE"
        assert payload["v2_completed_wall_ns"] is None
        assert payload["v2_due_wall_ns"] == due
        assert payload["v2_bar_lateness_seconds"] == expected_lag
        await svc._warning_monitor_once()
        assert svc._warning_active and svc._warning_revert_reason is None
    env.clock.ns = due+5*NS+1
    await svc._warning_monitor_once()
    assert svc._warning_revert_reason == "v2_bar_late" and not svc._warning_active
    assert provider._ws.removed == ["A.*"]
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}


def test_actual_first_closed_probe_at_due_is_applicable_not_extended_NA(setup):
    due = int(datetime(2026, 10, 13, 11, 1, tzinfo=UTC).timestamp())*NS
    env = setup(now=due)
    bar_ms = due // 1_000_000-60_000
    (env.directory / "schwab-1m-v2.log").write_text(
        line(due-90*NS, "controlled baseline")
        + line(due, f"[V2-MACD-PROBE] sym=AAA ts_ms={bar_ms} close=10 ")
        + line(due, f"[V2-MACD-PROBE] sym=BBB ts_ms={bar_ms} close=10 ")
    )
    payload = env.source.read(env.state())
    assert payload["v2_applicability"] == "APPLICABLE"
    assert payload["v2_completed_wall_ns"] == due and payload["v2_bar_lateness_seconds"] == 0


def test_first_due_NA_cannot_remain_applicable_exception_after_boundary(setup):
    before = int(datetime(2026, 10, 13, 11, 0, 59, tzinfo=UTC).timestamp())*NS
    env = setup(now=before)
    payload = env.source.read(env.state())
    source = {**payload, **payload["v2_applicability_provenance"]}
    with pytest.raises(ValueError, match="unsourced v2 inapplicability"):
        proposed_health_v2(source, now_ns=before+NS)
