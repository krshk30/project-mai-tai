"""Native receipt/covered-interval controls; no box/provider/performance evidence."""

from datetime import datetime

import pytest

from project_mai_tai.momwarn_health_contract import proposed_health_v2
from tests.unit.test_momwarn1_live_health import (
    start_native as start, three_clean_native_reads, complete_next_native_minute,
)
from tests.unit.test_momwarn1_live_health import NS, line, setup as live_setup

setup = live_setup
BASE = int(datetime.fromisoformat("2026-10-13T12:20:04+00:00").timestamp()) * NS


@pytest.mark.asyncio
async def test_native_OMS_receipt_qualifies_sequence_vs_lost_bytes_through_validation(setup):
    env = setup()
    await env.reader.read_live(env.client, "test")
    first = env.source.read(env.state())
    original_end = first["oms_measured_wall_ns"]
    assert first["oms_monitor_coverage"] == "consecutive_observed_end_sequence"
    assert first["oms_log_byte_coverage"] == "observed_cursor_reads_only"
    assert first["oms_log_copytruncate_count"] == 0
    (env.directory / "oms.log").write_text("")
    raw = env.source.oms.read()
    assert raw["oms_monitor_coverage"] != "complete_since_baseline"
    assert raw["oms_monitor_coverage"] == "consecutive_observed_end_sequence"
    assert raw["oms_log_byte_coverage"] == "gap_unobserved_copytruncate"
    assert raw["oms_log_copytruncate_count"] == 1
    receipt = env.source.read(env.state())
    assert receipt["oms_measured_wall_ns"] == original_end
    normalized = proposed_health_v2(receipt, now_ns=env.clock.ns)
    for key in ("oms_monitor_coverage", "oms_log_byte_coverage", "oms_log_copytruncate_count"):
        assert normalized[key] == raw[key]
    measured = await env.reader.read_live(env.client, "test")
    assert measured.oms_sync_seq == 10
    assert env.reader._last_oms_evidence[-1] == original_end


@pytest.mark.parametrize("changes", [
    {"oms_monitor_coverage": "complete_since_baseline"},
    {"oms_log_byte_coverage": "UNKNOWN"},
    {"oms_log_copytruncate_count": -1},
    {"oms_log_copytruncate_count": True},
    {"oms_log_copytruncate_count": 0, "oms_log_byte_coverage": "gap_unobserved_copytruncate"},
    {"oms_monitor_coverage": "consecutive_observed_end_sequence", "oms_log_byte_coverage": None},
    {"oms_measured_wall_ns": 1},
    {"oms_source": "unknown-origin"},
])
def test_new_coverage_fields_cannot_upgrade_unknown_or_false_continuity(setup, changes):
    env = setup()
    receipt = env.source.read(env.state())
    with pytest.raises(ValueError, match="UNKNOWN:OMS"):
        proposed_health_v2(dict(receipt, **changes), now_ns=env.clock.ns)


async def known_gap(env):
    svc, provider, _ = await start(env)
    source = env.reader._source
    env.clock.ns = BASE + 26 * NS  # 08:20:30 ET; 08:20 due already completed/observed.
    (env.directory / "oms.log").write_text(line(env.clock.ns,
        "[OMS-BROKER-SYNC-PASS] id=11 phase=end outcome=ok duration_ms=1000.000 exceeds_note_age=False"))
    (env.directory / "schwab-1m-v2.log").write_text("")
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_revert_reason is None
    assert source.truncations == 1 and source.covered_from_ns == env.clock.ns
    return svc, provider, source


def next_due(env):
    env.clock.ns = BASE + 56 * NS  # 08:21:00 ET.
    (env.directory / "oms.log").write_text(line(env.clock.ns,
        "[OMS-BROKER-SYNC-PASS] id=12 phase=end outcome=ok duration_ms=1000.000 exceeds_note_age=False"))


@pytest.mark.asyncio
async def test_fully_observed_minute_after_gap_gets_0_to_5s_then_actual_2s_completion(setup):
    env = setup(now=BASE)
    svc, provider, source = await known_gap(env)
    next_due(env)
    due = env.clock.ns
    assert source.covered_from_ns < due
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_revert_reason is None
    assert source.read(env.state())["v2_bar_lateness_seconds"] == 0
    env.clock.ns = due + 2 * NS
    for symbol in ("AAA", "BBB"):
        with (env.directory / "schwab-1m-v2.log").open("a") as out:
            out.write(line(env.clock.ns,
                f"[V2-MACD-PROBE] sym={symbol} ts_ms={due//1_000_000-60_000} close=10.000000 "))
    await svc._warning_monitor_once()
    assert source.read(env.state())["v2_bar_lateness_seconds"] == 2
    assert svc._warning_active and svc._warning_revert_reason is None
    assert provider._ws.removed == []
    assert env.reader.log_observability()["coverage_status"] == "GAP_UNOBSERVED_COPYTRUNCATE"


@pytest.mark.asyncio
async def test_fully_observed_postgap_minute_missing_bar_exact5_allowed_gt5_pauses(setup):
    env = setup(now=BASE)
    svc, provider, source = await known_gap(env)
    next_due(env)
    due = env.clock.ns
    for delay in (0, 1, 5):
        env.clock.ns = due + delay * NS
        await svc._warning_monitor_once()
        assert svc._warning_active and svc._warning_revert_reason is None
        assert source.read(env.state())["v2_bar_lateness_seconds"] == delay
    env.clock.ns = due + 5 * NS + 1
    await svc._warning_monitor_once()
    assert not svc._warning_active and svc._warning_revert_reason is None
    assert svc.warning_metrics()["pause_state"]["state"] == "paused"
    assert svc.warning_metrics()["pause_state"]["reason"] == "v2_bar_late"
    assert provider._ws.removed == ["A.*"]
    complete_next_native_minute(env)
    await three_clean_native_reads(svc, env)
    assert provider._ws.added.count("A.*") == 2
    assert provider._ws.removed == ["A.*"]
    assert env.reader.log_observability()["coverage_status"] == "GAP_UNOBSERVED_COPYTRUNCATE"


@pytest.mark.asyncio
async def test_required_completion_inside_gap_is_unknown_not_grace_or_fake_coverage(setup):
    env = setup(now=BASE)
    svc, provider, _ = await start(env)
    # Last actual EOF 08:20:04; unread bytes removed across 08:21:00 due.
    env.clock.ns = BASE + 59 * NS  # 08:21:03 ET, not enough to treat the due as covered.
    (env.directory / "oms.log").write_text(line(env.clock.ns,
        "[OMS-BROKER-SYNC-PASS] id=11 phase=end outcome=ok duration_ms=1000.000 exceeds_note_age=False"))
    (env.directory / "schwab-1m-v2.log").write_text("")
    with pytest.raises(ValueError, match="completion unknown after copytruncate gap"):
        await env.reader.read_live(env.client, "test")
    assert env.reader._source.covered_from_ns == env.clock.ns
    await svc._warning_monitor_once()
    assert not svc._warning_active and svc._warning_revert_reason is None
    assert svc.warning_metrics()["pause_state"]["state"] == "paused"
    assert svc.warning_metrics()["pause_state"]["reason"] == "monitor_unreadable"
    assert provider._ws.removed == ["A.*"]
    complete_next_native_minute(env)
    # The invalid due read never delivered OMS 11 to the watchdog. Actual 12
    # first reports/rebaselines that sequence gap; it is not a clean read.
    before = env.client.xrevrange.call_count
    await svc._warning_monitor_once()
    assert env.client.xrevrange.call_count == before + 1
    assert not svc._warning_active and svc._warning_revert_reason is None
    assert svc.warning_metrics()["pause_state"]["reason"] == "monitor_sync_gap"
    assert svc.warning_metrics()["pause_state"]["clean_streak"] == 0
    await three_clean_native_reads(svc, env)
    assert provider._ws.added.count("A.*") == 2
    assert provider._ws.removed == ["A.*"]
    assert env.reader.log_observability()["coverage_status"] == "GAP_UNOBSERVED_COPYTRUNCATE"
