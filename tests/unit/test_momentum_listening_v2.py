"""Page-only provenance controls; samples are not consumed-event timestamps."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json

import pytest

from project_mai_tai.services import control_plane as cp


NOW = datetime(2026, 10, 21, 11, 10, tzinfo=UTC)
NS = int(NOW.timestamp() * 1e9)


def evidence(monkeypatch):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    stamp = cp._datetime_str(NOW - timedelta(seconds=5))
    data = {"services": [{"service_name": "momentum-paper", "status": "healthy",
                         "effective_status": "healthy", "observed_at": stamp}],
            "market_data": {"latest_snapshot_batch": {"completed_at": stamp}}}
    live = {"status": "READABLE", "positions": [], "recent_decisions": []}
    warnings = [{"status": "READABLE", "recent_decisions": [
        {"symbol": "REAL", "status": "WATCH", "last_bar_at": stamp}]}]
    owner = {"status": "READABLE", "observed_ns": NS - 1_000_000_000,
             "entry_admission_enabled": True}
    metrics = {"status": "READABLE", "observed_ns": NS - 2_000_000_000,
               "gateway_instance_id": "current", "generation": 4,
               "coverage_status": "provider_event_observed", "owned_warning_symbols": ["REAL"],
               "A_subscription_requested_ns": NS - 60_000_000_000,
               "A_received": 100, "A_published": 90, "max_publish_lag_ms": 12.3456789,
               "consumer": {"status": "READABLE", "observed_ns": NS - 3_000_000_000,
                            "transport_status": "observed_transport", "owned_warning_symbols": 1}}
    return data, live, warnings, owner, metrics


def test_real_sources_and_no_sample_as_last_consumed_tick(monkeypatch):
    values = evidence(monkeypatch)
    before = deepcopy(values)
    status = cp._momentum_listening_status(*values)
    assert values == before
    assert status["state"] == "LISTENING"
    assert status["latest_decision_detail"] == "REAL / WATCH"
    assert status["latest_market_data_at"] == values[0]["market_data"]["latest_snapshot_batch"]["completed_at"]
    assert status["latest_heartbeat_at"] == values[0]["services"][0]["observed_at"]
    assert status["latest_bot_tick_at"] == "UNKNOWN"
    assert status["daily_bars_in"] == status["daily_bars_out"] == status["daily_max_delay_ms"] == "UNKNOWN"
    assert status["watchlist_count"] == "UNKNOWN" and status["position_count"] == 0
    assert status["owned_warning_symbol_count"] == 1
    html = cp._render_momentum_listening_status(status, values[4])
    for tile in ("State", "Last Decision", "Last Bot Tick", "Last Market Data", "Last Heartbeat", "Feed Health", "Tracked Symbols"):
        assert f"<span>{tile}</span>" in html
    assert 'id="momentum-listening"' in html
    assert "delay max 12 ms" in html
    assert "not today" in html and "no daily provenance" in html


@pytest.mark.parametrize("kind", ["owner_missing", "owner_stale", "owner_future", "feed_stale", "consumer_missing", "heartbeat_missing", "market_missing"])
def test_missing_or_stale_provenance_never_claims_listening(monkeypatch, kind):
    data, live, warnings, owner, metrics = evidence(monkeypatch)
    if kind == "owner_missing":
        owner.clear()
    elif kind == "owner_stale":
        owner["observed_ns"] = NS - 4_000_000_000
    elif kind == "owner_future":
        owner["observed_ns"] = NS + 1
    elif kind == "feed_stale":
        metrics["observed_ns"] = NS - 21_000_000_000
    elif kind == "consumer_missing":
        metrics.pop("consumer")
    elif kind == "heartbeat_missing":
        data["services"] = []
    else:
        data["market_data"] = {}
    assert cp._momentum_listening_status(data, live, warnings, owner, metrics)["state"] == "UNKNOWN"


@pytest.mark.parametrize("kind", ["admission", "feed"])
def test_explicit_off_is_distinct_from_unknown(monkeypatch, kind):
    values = evidence(monkeypatch)
    if kind == "admission":
        values[3]["entry_admission_enabled"] = False
    else:
        values[4]["coverage_status"] = "off_by_window"
    assert cp._momentum_listening_status(*values)["state"] == "OFF"


@pytest.mark.parametrize("kind", ["service", "transport", "requested", "market_stale"])
def test_degraded_real_evidence_does_not_get_green_badge(monkeypatch, kind):
    values = evidence(monkeypatch)
    if kind == "service":
        values[0]["services"][0]["effective_status"] = "degraded"
    elif kind == "transport":
        values[4]["consumer"]["transport_status"] = "UNKNOWN"
    elif kind == "requested":
        values[4]["coverage_status"] = "requested"
    else:
        values[0]["market_data"]["latest_snapshot_batch"]["completed_at"] = cp._datetime_str(NOW - timedelta(seconds=100))
    assert cp._momentum_listening_status(*values)["state"] == "DEGRADED"


def test_unreadable_live_positions_never_become_known_zero(monkeypatch):
    values = evidence(monkeypatch)
    values[1]["page_positions_incomplete"] = True
    values[4]["consumer"]["owned_warning_symbols"] = "UNKNOWN"
    status = cp._momentum_listening_status(*values)
    assert status["position_count"] == status["watchlist_count"] == "UNKNOWN"


class Redis:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def xrevrange(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return [("1-0", {"data": json.dumps(row)}) for row in self.rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "epoch", "stale", "future", "bool_generation", "bool_count"])
async def test_consumer_reader_requires_fresh_current_epoch_and_bounded_query(mutation):
    row = {"source": "momentum-paper", "event_type": "consumer_sample", "version": 1,
           "observed_ns": NS - 1_000_000_000, "gateway_instance_id": "current", "generation": 4,
           "transport_status": "observed_transport", "owned_warning_symbols": 2}
    if mutation == "epoch":
        row["gateway_instance_id"] = "old"
    elif mutation == "stale":
        row["observed_ns"] = NS - 21_000_000_000
    elif mutation == "future":
        row["observed_ns"] = NS + 1
    elif mutation == "bool_generation":
        row["generation"] = True
    elif mutation == "bool_count":
        row["owned_warning_symbols"] = True
    redis = Redis([row])
    result = await cp._read_momentum_page_consumer(redis, "test", now_ns=NS,
        gateway_metrics={"status": "READABLE", "gateway_instance_id": "current", "generation": 4})
    assert redis.calls == [(cp.stream_name("test", "momentum-warning-metrics"), {"count": 64})]
    if mutation is None:
        assert result["status"] == "READABLE" and result["owned_warning_symbols"] == 2
    elif mutation == "bool_count":
        assert result["owned_warning_symbols"] == "UNKNOWN"
    else:
        assert result == {"status": "UNKNOWN"}
