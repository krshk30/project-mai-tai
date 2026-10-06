"""Replay the measured20:37 off-hours heartbeat; flip one guard at a time."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("standing_baseline", Path(__file__).with_name("test_standing_allowance.py"))
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
gate = baseline.gate
NOW = datetime(2026, 10, 6, 0, 37, tzinfo=timezone.utc)


@pytest.fixture
def measured(monkeypatch):
    monkeypatch.setattr(baseline, "NOW", NOW)
    args = baseline.evidence()
    row = {"service_name": "schwab-1m-v2", "observed_at_raw": (NOW - timedelta(seconds=2)).isoformat()}
    args[1]["services"].append(row)
    row.update(status="degraded", raw_status="degraded", effective_status="degraded", details={
        "data_flow": "stalled_offhours_rest_dry", "market_session": "closed",
        "secs_since_last_bar": "2260", "secs_since_last_quote": "2",
        "loop_health": "healthy", "loop_exceptions_total": "0",
        "streamer_connected": "true", "enabled": "true", "warmed_size": "5", "watchlist_size": "5"})
    return args, row


def remaining(args, now=NOW):
    adjusted, audit = gate.standing_allowance(*args, now)
    return adjusted, audit, gate.evaluate_live_deploy_preflight(adjusted, service_target="oms", now=now)


def test_recorded_closed_market_shape_admitted_only_in_memory(measured):
    args, row = measured
    original = copy.deepcopy(args)
    adjusted, audit, failures = remaining(args)
    assert not failures
    assert args == original and row["effective_status"] == "degraded"
    assert next(r for r in adjusted["services"] if r["service_name"] == "schwab-1m-v2")["effective_status"] == "healthy"
    line = next(line for line in audit if "service=schwab-1m-v2" in line)
    for key in ("data_flow", "market_session", "loop_health", "loop_exceptions_total",
                "streamer_connected", "enabled", "warmed_size", "watchlist_size", "secs_since_last_bar"):
        assert f"{key}={row['details'][key]}" in line
    assert "heartbeat_max_age_seconds=120" in line
    assert "bar_age_limit_seconds=2520.0" in line
    assert "admission=in_memory_only" in line


@pytest.mark.parametrize("key,value", (
    ("data_flow", "stalled_rth"), ("market_session", "regular"),
    ("market_session", "unknown"), ("loop_health", "degraded-persistent"),
    ("loop_exceptions_total", "1"), ("streamer_connected", "false"),
    ("enabled", "false"), ("warmed_size", "4"), ("warmed_size", "5.5"),
    ("secs_since_last_bar", "2521"), ("secs_since_last_bar", "-1"),
    ("secs_since_last_bar", "NaN"), ("loop_exceptions_total", None),
))
def test_each_changed_detail_leaves_original_general_block(measured, key, value):
    args, row = measured
    row["details"][key] = value
    _, audit, failures = remaining(args)
    assert any("service schwab-1m-v2 is not healthy" in f for f in failures)
    assert not any("service=schwab-1m-v2" in line for line in audit)


def test_zero_watchlist_and_zero_warmed_do_not_admit(measured):
    args, row = measured
    row["details"].update(warmed_size="0", watchlist_size="0")
    assert remaining(args)[2]


@pytest.mark.parametrize("age", (121, -1))
def test_stale_or_future_v2_heartbeat_never_admitted(measured, age):
    args, row = measured
    row["observed_at_raw"] = (NOW - timedelta(seconds=age)).isoformat()
    _, audit, failures = remaining(args)
    assert failures
    assert not any("service=schwab-1m-v2" in line for line in audit)


def test_bar_stall_bound_is_derived_from_actual_20_et_end(measured):
    args, row = measured
    row["details"]["secs_since_last_bar"] = "2520"
    assert not remaining(args)[2]
    row["details"]["secs_since_last_bar"] = "2520.001"
    assert remaining(args)[2]


def test_third_degraded_service_is_not_admitted(measured):
    args, _ = measured
    other = next(row for row in args[1]["services"] if row["service_name"] == "market-data-gateway")
    other.update(status="degraded", effective_status="degraded")
    _, _, failures = remaining(args)
    assert any("service market-data-gateway is not healthy" in failure for failure in failures)


def test_raw_status_must_match_degraded_effective_status(measured):
    args, row = measured
    row["raw_status"] = "starting"
    assert remaining(args)[2]


def test_pre_20_clock_cannot_admit_even_short_stall(measured):
    args, row = measured
    row["details"]["secs_since_last_bar"] = "0"
    adjusted = copy.deepcopy(args[1])
    assert gate.admit_v2_offhours(adjusted, args[1], datetime(2026, 10, 5, 23, 59, tzinfo=timezone.utc)) == []


@pytest.mark.parametrize("key", ("enabled", "streamer_connected", "loop_health", "data_flow",
                                 "market_session", "warmed_size", "watchlist_size", "secs_since_last_bar"))
def test_missing_required_detail_never_admitted(measured, key):
    args, row = measured
    del row["details"][key]
    assert remaining(args)[2]
