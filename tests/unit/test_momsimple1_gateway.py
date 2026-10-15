"""Operator 07:3x fast path: measurements never control A subscription."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.unit.test_momwarn1_gateway import service, healthy, aggregate
# Register the legal-window clock without changing explicit clock overrides.
from tests.unit.test_momwarn1_gateway import pm_clock as pm_clock


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "load", "ram", "live_error", "oms_sync_slow_twice", "v2_cohort_late_twice",
    "v2_bar_late", "publish_lag", "monitor_unreadable", "monitor_stale",
    "monitor_invalid", "monitor_sync_regressed", "monitor_sync_gap",
    "v2_startup_pending", "v2_startup_timeout", "provider_disconnected",
    "disconnect", "policy_1008", "reconnect_1012", "reconnect_storm",
    "provider_error", "invalid_aggregate", "publish_error", "unsubscribe_error",
    "unknown_future_reason",
])
async def test_every_former_trip_is_observed_without_actions(reason, caplog):
    svc, provider, _, _ = service()
    await svc._warning_monitor_once()
    assert svc._warning_active
    provider._handle_messages([aggregate()])
    depth = svc._warning_queue.qsize()
    before = dict(svc._desired_symbols_by_consumer)
    await svc._revert_warning(reason, diagnostic="CONTROLLED")
    assert svc._warning_active and svc._warning_revert_reason is None
    assert svc._warning_queue.qsize() == depth
    assert provider._ws.removed == [] and not provider._warning_failed
    assert svc._desired_symbols_by_consumer == before
    metrics = svc.warning_metrics()
    assert metrics["watchdog_mode"] == "log_only"
    assert metrics["watchdog_observed_reason"] == reason
    assert metrics["pause_state"]["wire_pause_count"] == 0
    assert metrics["pause_state"]["state"] == "active"
    assert f"reason={reason} action=none" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("change,lag,expected", [
    ({"load_1m": 7}, 0, "load"),
    ({"ram_available_bytes": 1}, 0, "ram"),
    ({}, 3, "publish_lag"),
    ({"v2_bar_lateness_seconds": 10}, 0, "v2_bar_late"),
    ({"oms_sync_seconds": 6}, 0, "oms_sync_slow_twice"),
    ({"v2_startup_pending": True}, 0, "v2_startup_pending"),
])
async def test_actual_health_reasons_are_log_only(change, lag, expected):
    svc, provider, _, _ = service()
    svc._warning_publish_lag = lambda: lag
    seq = [0]

    def read():
        seq[0] += 1
        return replace(healthy(), oms_sync_seq=seq[0], **change)

    svc._warning_health_reader = SimpleNamespace(read=read)
    for _ in range(4):
        await svc._warning_monitor_once()
        assert svc._warning_active and svc._warning_revert_reason is None
    assert svc.warning_metrics()["watchdog_observed_reason"] == expected
    assert provider._ws.added.count("A.*") == 1 and provider._ws.removed == []


@pytest.mark.asyncio
async def test_unknown_measurement_does_not_block_initial_subscription():
    svc, provider, _, _ = service()

    def read():
        raise ValueError("CONTROLLED unreadable")

    svc._warning_health_reader = SimpleNamespace(read=read)
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_revert_reason is None
    assert svc.warning_metrics()["watchdog_observed_reason"] == "monitor_unreadable"
    assert provider._ws.added == ["A.*"] and provider._ws.removed == []


@pytest.mark.asyncio
async def test_manual_off_and_window_close_still_unsubscribe(monkeypatch):
    svc, provider, _, _ = service()
    await svc._warning_monitor_once()
    svc.settings.market_data_momentum_warning_enabled = False
    await svc._warning_monitor_once()
    assert not svc._warning_active and provider._ws.removed == ["A.*"]
    svc.settings.market_data_momentum_warning_enabled = True
    await svc._warning_monitor_once()
    await svc._end_warning_window()
    assert not svc._warning_active and provider._ws.removed == ["A.*", "A.*"]


@pytest.mark.asyncio
async def test_provider_errors_and_reconnect_storm_never_latch_or_unsubscribe():
    svc, provider, _, _ = service()
    await svc._warning_monitor_once()
    for reason in ["invalid_aggregate", "provider_error"] + ["disconnect"] * 4:
        provider._revert_warning(reason)
        await svc._warning_monitor_once()
        assert svc._warning_active and not provider._warning_failed
    assert svc.warning_metrics()["pause_state"]["reconnect_event_count"] == 4
    assert provider._ws.removed == [] and svc._warning_revert_reason is None


@pytest.mark.asyncio
async def test_metrics_preserve_measurements_and_actual_error_counts():
    svc, _, _, _ = service()
    svc._warning_health_reader = SimpleNamespace(
        read=healthy,
        error_observations=lambda: {"error_lines_by_log": {"oms.log": 2},
            "traceback_lines_by_log": {"oms.log": 1}, "last_error_tag": "CONTROLLED"},
    )
    await svc._warning_monitor_once()
    metrics = svc.warning_metrics()
    assert metrics["oms_sync_pass_seconds"] == 1
    assert metrics["v2_bar_lateness_seconds"] == 0
    assert metrics["error_lines_by_log"] == {"oms.log": 2}
    assert metrics["traceback_lines_by_log"] == {"oms.log": 1}
    assert metrics["last_error_tag"] == "CONTROLLED"
    assert all(key in metrics for key in ("A_received", "A_published", "queue_drops", "lag_ms"))
    assert metrics["last_successful_health_observed_wall_ns"] is not None
    assert metrics["health_measurement_age_seconds"] >= 0
    assert metrics["reconnect_count_scope"] == "LIFETIME_CALLBACK_COUNT_NOT_600S_WINDOW"


@pytest.mark.asyncio
async def test_retained_measurement_reports_original_timestamp_and_age(monkeypatch):
    import time
    clock = [time.time_ns()]
    monkeypatch.setattr(time, "time_ns", lambda: clock[0])
    svc, _, _, _ = service()
    assert svc.warning_metrics()["last_successful_health_observed_wall_ns"] is None
    assert svc.warning_metrics()["health_measurement_age_seconds"] is None
    await svc._warning_monitor_once()
    observed = svc.warning_metrics()["last_successful_health_observed_wall_ns"]
    clock[0] += 30_000_000_000

    def unreadable():
        raise ValueError("CONTROLLED unreadable")

    svc._warning_health_reader = SimpleNamespace(read=unreadable)
    await svc._warning_monitor_once()
    metrics = svc.warning_metrics()
    assert metrics["last_successful_health_observed_wall_ns"] == observed
    assert metrics["health_measurement_age_seconds"] == 30
    assert metrics["watchdog_observed_reason"] == "monitor_unreadable"
    assert svc._warning_active
