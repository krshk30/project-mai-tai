"""Native read_live controls; synthetic local files, not box rotation evidence."""

import json
import logging

import pytest

from tests.unit.test_momwarn1_gateway import event, service
from tests.unit.test_momwarn1_live_health import NS, line, setup as live_setup

setup = live_setup
LOGS = ("oms.log", "strategy.log", "schwab-1m-v2.log", "orb-schwab.log")


async def start(env):
    svc, provider, redis, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    svc._warning_health_reader, svc._warning_redis = env.reader, env.client
    await svc._warning_monitor_once()
    assert svc._warning_active
    return svc, provider, redis


@pytest.mark.asyncio
@pytest.mark.parametrize("name", LOGS)
@pytest.mark.parametrize("regrown", [False, True])
async def test_native_in_window_rebaseline_preserves_real_facts_TQ_and_gap(setup, caplog, name, regrown):
    env = setup()
    svc, provider, redis = await start(env)
    source = env.reader._source
    end = source.oms.latest["oms_measured_wall_ns"]
    owners, saved = dict(svc._desired_symbols_by_consumer), dict(redis.hashes)
    path = env.directory / name
    inode, size = path.stat().st_ino, path.stat().st_size
    path.write_text("INFO post-copy retained\n" * (size // 10 + 1) if regrown else "")
    assert path.stat().st_ino == inode
    assert (path.stat().st_size >= size) is regrown
    caplog.clear()  # Subscription setup is outside the two monitor calls under test.
    with caplog.at_level(logging.WARNING, logger="market-data-gateway"):
        await svc._warning_monitor_once()
        await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_revert_reason is None
    assert env.reader._source is source and source.oms.latest["oms_measured_wall_ns"] == end
    assert env.reader.log_observability() == {
        "copytruncate_counts": {name: 1}, "coverage_status": "GAP_UNOBSERVED_COPYTRUNCATE"}
    assert len(caplog.records) == 1 and "unobserved_interval=UNKNOWN" in caplog.records[0].message
    assert "AUTO-REVERT" not in caplog.records[0].message
    assert provider._ws.removed == [] and provider._ws.added.count("A.*") == 1
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}
    assert svc._desired_symbols_by_consumer == owners and redis.hashes == saved
    assert env.client.xrevrange.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("name", LOGS)
async def test_native_retained_ERROR_after_copytruncate_still_reverts_A_only(setup, name):
    env = setup()
    svc, provider, _ = await start(env)
    (env.directory / name).write_text("ERROR retained live failure\n")
    await svc._warning_monitor_once()
    assert svc._warning_revert_reason == "live_error"
    assert provider._ws.removed == ["A.*"]
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}


@pytest.mark.asyncio
async def test_native_two_actual_slow_completions_across_truncations_still_revert(setup):
    env = setup()
    svc, provider, _ = await start(env)
    for seq in (11, 12):
        env.clock.ns += 6 * NS
        (env.directory / "oms.log").write_text(line(env.clock.ns,
            f"[OMS-BROKER-SYNC-PASS] id={seq} phase=end outcome=ok "
            "duration_ms=6000.000 exceeds_note_age=False"))
        await svc._warning_monitor_once()
        if seq == 11:
            assert svc._warning_active and svc._warning_revert_reason is None
    assert svc._warning_revert_reason == "oms_sync_slow_twice"
    assert provider._ws.removed == ["A.*"]


@pytest.mark.asyncio
async def test_removed_unobserved_ERROR_is_gap_not_fabricated_clean_interval(setup):
    env = setup()
    svc, provider, _ = await start(env)
    path = env.directory / "strategy.log"
    with path.open("a") as out:
        out.write("ERROR CONTROLLED lost before monitor could read\n")
    archive = env.directory / "strategy.log.1"
    archive.write_bytes(path.read_bytes())
    archive.chmod(0)
    path.write_text("")
    try:
        await svc._warning_monitor_once()
    finally:
        archive.chmod(0o600)
    assert svc._warning_active and svc._warning_revert_reason is None
    assert env.reader.log_observability()["coverage_status"] == "GAP_UNOBSERVED_COPYTRUNCATE"
    assert provider._ws.removed == []


@pytest.mark.asyncio
async def test_partial_ERROR_cannot_be_fabricated_across_truncation(setup):
    env = setup()
    svc, _, _ = await start(env)
    path = env.directory / "strategy.log"
    with path.open("a") as out:
        out.write("ERR")
    await svc._warning_monitor_once()
    assert env.reader._logs["strategy.log"][2] == b"ERR"
    path.write_text("OR\n")
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_revert_reason is None
    assert env.reader.log_observability()["coverage_status"] == "GAP_UNOBSERVED_COPYTRUNCATE"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["inode_oms", "inode_v2", "inode_error_log", "backlog_oms",
    "backlog_v2", "backlog_error_log", "sequence_gap", "v2_missing_due", "OMS_dead", "v2_dead",
    "config_changed", "log_missing", "restamped_end"])
async def test_native_independent_unknowns_never_hidden_by_rebaseline(setup, caplog, failure):
    env = setup()
    svc, provider, _ = await start(env)
    if failure.startswith("inode_"):
        name = {"inode_oms": "oms.log", "inode_v2": "schwab-1m-v2.log",
                "inode_error_log": "strategy.log"}[failure]
        path = env.directory / name
        raw = path.read_text()
        path.rename(env.directory / "old.log")
        path.write_text(raw)
    elif failure.startswith("backlog_"):
        name = {"backlog_oms": "oms.log", "backlog_v2": "schwab-1m-v2.log",
                "backlog_error_log": "strategy.log"}[failure]
        (env.directory / name).write_text("X" * 65537)
    elif failure == "sequence_gap":
        (env.directory / "oms.log").write_text(line(env.clock.ns,
            "[OMS-BROKER-SYNC-PASS] id=12 phase=end outcome=ok duration_ms=1000.000 exceeds_note_age=False"))
    elif failure == "v2_missing_due":
        assert svc._warning_active
        env.clock.ns += 60 * NS
        # Detection spans this due boundary: completion may be in removed bytes.
        (env.directory / "schwab-1m-v2.log").write_text("")
        (env.directory / "oms.log").write_text(line(env.clock.ns,
            "[OMS-BROKER-SYNC-PASS] id=11 phase=end outcome=ok duration_ms=1000.000 exceeds_note_age=False"))
    elif failure in {"OMS_dead", "v2_dead"}:
        path = env.proc / ("12" if failure == "OMS_dead" else "13") / "stat"
        path.write_text(path.read_text().replace(") S ", ") Z "))
    elif failure == "config_changed":
        config = dict(env.cfg, source_timezone_ref="changed")
        env.config.write_text(json.dumps(config))
    elif failure == "log_missing":
        (env.directory / "strategy.log").unlink()
    else:
        (env.directory / "oms.log").write_text(line(env.clock.ns,
            "[OMS-BROKER-SYNC-PASS] id=10 phase=end outcome=ok duration_ms=2000.000 exceeds_note_age=False"))
    with caplog.at_level(logging.WARNING, logger="market-data-gateway"):
        await svc._warning_monitor_once()
    assert svc._warning_revert_reason == "monitor_unreadable"
    assert provider._ws.removed == ["A.*"]
    assert any("exception_class" in row.message for row in caplog.records)
    if failure == "v2_missing_due":
        assert any("completion unknown after copytruncate" in row.message for row in caplog.records)
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_A_active", [True, False])
async def test_native_next_day_reset_after_evening_truncate_even_if_A_never_active(setup, previous_A_active):
    env = setup()
    svc, provider, redis, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    provider._connected = previous_A_active
    svc._warning_health_reader, svc._warning_redis = env.reader, env.client
    await svc._warning_monitor_once()
    old_source = env.reader._source
    assert bool(svc._warning_active) is previous_A_active and env.reader._logs
    owners, saved = dict(svc._desired_symbols_by_consumer), dict(redis.hashes)
    # 09:30 close then actual reviewed 20:00-ET copytruncate phase, CONTROLLED clocks.
    env.clock.ns = env.clock.ns // (86400 * NS) * (86400 * NS) + 13 * 3600 * NS + 30 * 60 * NS
    await svc._warning_monitor_once()
    env.clock.ns = env.clock.ns // (86400 * NS) * (86400 * NS) + 86400 * NS
    for name in LOGS:
        (env.directory / name).write_text("")
    env.clock.ns += 8 * 3600 * NS
    (env.directory / "oms.log").write_text(line(env.clock.ns - NS,
        "[OMS-BROKER-SYNC-PASS] id=100 phase=end outcome=ok duration_ms=1000.000 exceeds_note_age=False"))
    provider._connected = True
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_revert_reason is None
    assert env.reader._source is not old_source
    assert env.reader._source.oms.latest["oms_sync_seq"] == 100
    assert env.reader._source.offset is not None and env.reader._last_oms_evidence[1] == 100
    assert env.reader.log_observability()["copytruncate_counts"] == {}
    assert svc._desired_symbols_by_consumer == owners and redis.hashes == saved
    assert all(channel == "A.*" for channel in provider._ws.removed)
