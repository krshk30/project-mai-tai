"""Controlled read-only warning counter page; no real Redis/provider connection."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from project_mai_tai.market_data import gateway, massive_provider
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_mompage1 import RedisSnapshot, make_app
from tests.unit.test_momwarn1_gateway import aggregate, service

NOW = 1791850016000000000


def sample(**changes):
    result = dict(version=1, source="market-data-gateway", event_type="gateway_sample",
                  observed_ns=NOW, gateway_instance_id="controlled-gateway", generation=2,
                  queue_depth=0, queue_drops=3, dropped_total=5, A_received=1811,
                  A_published=1806, lag_ms=0, max_publish_lag_ms=134.5,
                  watchdog_revert_reason=None)
    return {**result, **changes}


def entry(value):
    return ("1791850016000-0", {"data": json.dumps(value)})


class MetricsRedis(RedisSnapshot):
    def __init__(self, entries=(), *, failure=None):
        super().__init__()
        self.entries, self.failure, self.metric_calls = list(entries), failure, []

    async def xrevrange(self, stream, **kwargs):
        if not stream.endswith(":momentum-warning-metrics"):
            return await super().xrevrange(stream, **kwargs)
        self.metric_calls.append((stream, kwargs))
        if self.failure == "timeout":
            await asyncio.sleep(1)
        if self.failure:
            raise OSError("controlled read failure")
        return self.entries


@pytest.mark.asyncio
async def test_fresh_actual_counters_epoch_and_zero_are_not_inferred():
    original = sample(queue_drops=0)
    client = MetricsRedis([entry(original)])
    result = await cp._read_momentum_warning_gateway_metrics(client, "test", now_ns=NOW)
    assert result["status"] == "READABLE" and result["queue_drops"] == 0
    assert result["dropped_total"] == 5 and result["generation"] == 2
    assert result["gateway_instance_id"] == "controlled-gateway" and result["observed_ns"] == NOW
    assert client.metric_calls == [("test:momentum-warning-metrics", {"count": 64})]
    original.pop("queue_drops")
    client.entries = [entry(original)]
    result = await cp._read_momentum_warning_gateway_metrics(client, "test", now_ns=NOW)
    assert result["queue_drops"] == "UNKNOWN" and result["dropped_total"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"observed_ns": NOW-20_000_000_001}, {"observed_ns": NOW+1},
    {"observed_ns": True}, {"generation": None}, {"generation": True},
    {"gateway_instance_id": ""}, {"version": 2}, {"version": True},
])
async def test_stale_future_bad_epoch_never_falls_back_to_older_sample(changes):
    client = MetricsRedis([entry(sample(**changes)), entry(sample(generation=1))])
    result = await cp._read_momentum_warning_gateway_metrics(client, "test", now_ns=NOW)
    assert result == {"status": "UNKNOWN"}
    panel = cp._render_momentum_warning_gateway_metrics(result)
    assert "Queue drops: UNKNOWN" in panel and "total discarded: UNKNOWN" in panel


@pytest.mark.asyncio
async def test_exact20s_boundary_and_consumer_epoch_never_borrowed():
    client = MetricsRedis([
        entry(sample(source="momentum-paper", event_type="consumer_sample", generation=99)),
        entry(sample(observed_ns=NOW-20_000_000_000)),
    ])
    result = await cp._read_momentum_warning_gateway_metrics(client, "test", now_ns=NOW)
    assert result["status"] == "READABLE" and result["age_ms"] == 20000
    assert result["generation"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("older", [
    {"generation": 3, "observed_ns": NOW-1},
    {"gateway_instance_id": "new-gateway", "observed_ns": NOW-1},
    {"observed_ns": NOW},
])
async def test_fresh_epoch_or_queued_old_order_conflict_is_unknown(older):
    client = MetricsRedis([entry(sample(observed_ns=NOW-2)), entry(sample(**older))])
    result = await cp._read_momentum_warning_gateway_metrics(client, "test", now_ns=NOW)
    assert result == {"status": "UNKNOWN"}


@pytest.mark.asyncio
async def test_stale_previous_epoch_does_not_replace_current_sample():
    client = MetricsRedis([
        entry(sample()), entry(sample(generation=1, observed_ns=NOW-20_000_000_001)),
    ])
    result = await cp._read_momentum_warning_gateway_metrics(client, "test", now_ns=NOW)
    assert result["status"] == "READABLE" and result["generation"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, -1, 1.5, None, "0", 2**63])
async def test_invalid_count_is_unknown_not_zero(value):
    result = await cp._read_momentum_warning_gateway_metrics(
        MetricsRedis([entry(sample(queue_drops=value))]), "test", now_ns=NOW,
    )
    assert result["queue_drops"] == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("entries,failure", [
    ([], None), ([entry(sample(source="other"))], None),
    ([("1-0", {"data": "bad JSON"})], None),
    ([("1-0", {"data": "x"*16385})], None),
    ([("1-0", {})], None), ([entry(sample())]*65, None),
    ([entry(sample(lag_ms=float("nan")))], None), ([], "read"), ([], "timeout"),
])
async def test_bounded_bad_missing_or_unreadable_read_is_unknown(entries, failure):
    client = MetricsRedis(entries, failure=failure)
    result = await cp._read_momentum_warning_gateway_metrics(client, "test", now_ns=NOW)
    assert result == {"status": "UNKNOWN"}
    assert len(client.metric_calls) == 1  # No reconnect, retry, extra connection or write.


@pytest.mark.asyncio
async def test_bytes_fields_revert_sample_and_html_escape():
    payload = sample(event_type="revert", watchdog_revert_reason="<unsafe>",
                     gateway_instance_id="<gateway>")
    client = MetricsRedis([("1-0", {b"data": json.dumps(payload).encode()})])
    result = await cp._read_momentum_warning_gateway_metrics(client, "test", now_ns=NOW)
    before = deepcopy(result)
    html = cp._render_momentum_warning_gateway_metrics(result)
    assert "&lt;unsafe&gt;" in html and "&lt;gateway&gt;" in html
    assert "Feed details" in html and "existing safety auto-revert: YES" not in html
    assert result == before


@pytest.mark.asyncio
async def test_actual_gateway_overflow_counter_through_page_preserves_snapshot(monkeypatch):
    # Actual producer counters from controlled SDK events, not manually seeded counts.
    clock = SimpleNamespace(now=lambda _: datetime(2026, 10, 12, 12, 51, 21, tzinfo=UTC))
    monkeypatch.setattr(gateway, "datetime", clock)
    monkeypatch.setattr(massive_provider, "datetime", clock)
    svc, provider, _, _ = service(market_data_momentum_warning_queue_maxsize=2)
    for _ in range(3):
        await svc._warning_monitor_once()
    for _ in range(5):
        provider._handle_messages([aggregate()])
    produced = svc.warning_metrics()
    assert produced["queue_drops"] == 3 and produced["dropped_total"] == 3
    monkeypatch.setattr(Settings, "market_data_momentum_warning_enabled", property(lambda self: True), raising=False)
    app, _ = make_app(monkeypatch)
    client = MetricsRedis([entry(produced)])
    with TestClient(app) as http:
        app.state.repository.redis = client
        page = http.get("/bot/momentum-30")
    assert page.status_code == 200
    assert "Queue drops: 3" in page.text and "total discarded: 3" in page.text
    assert "queue depth: 2" in page.text and produced["gateway_instance_id"] in page.text
    assert "$+31.80" not in page.text  # Snapshot total is no longer page P&L.
    assert 'LIVE (real orders)' in page.text
    assert "Feed details" in page.text and "existing safety auto-revert: YES" not in page.text
    assert len(client.metric_calls) == 1


@pytest.mark.parametrize("enabled,route", [(False, "/bot/momentum-30"), (True, "/bot/orb")])
def test_default_off_and_other_page_do_not_read_warning_metrics(monkeypatch, enabled, route):
    monkeypatch.setattr(Settings, "market_data_momentum_warning_enabled", property(lambda self: enabled), raising=False)
    app, _ = make_app(monkeypatch)
    client = MetricsRedis([entry(sample())])
    with TestClient(app) as http:
        app.state.repository.redis = client
        page = http.get(route)
    assert page.status_code == 200 and not client.metric_calls
    assert "A gateway counters" not in page.text


@pytest.mark.parametrize("entries", [[], [entry(sample(observed_ns=1))]])
def test_page_missing_or_stale_never_renders_fabricated_zero(monkeypatch, entries):
    monkeypatch.setattr(Settings, "market_data_momentum_warning_enabled", property(lambda self: True), raising=False)
    app, _ = make_app(monkeypatch)
    with TestClient(app) as http:
        app.state.repository.redis = MetricsRedis(entries)
        page = http.get("/bot/momentum-60")
    assert "Queue drops: UNKNOWN" in page.text and "total discarded: UNKNOWN" in page.text
    assert "Queue drops: 0" not in page.text and "$+176.00" not in page.text
    assert 'LIVE (real orders)' in page.text


def test_fresh_counter_remains_separate_from_unreadable_ledger(monkeypatch):
    monkeypatch.setattr(Settings, "market_data_momentum_warning_enabled", property(lambda self: True), raising=False)
    app, _ = make_app(monkeypatch)
    def unavailable(*args, **kwargs):
        raise OSError("controlled warning ledger unavailable")
    monkeypatch.setattr(cp, "load_warning_completed_today", unavailable)
    live_sample = sample(observed_ns=cp.time.time_ns())
    with TestClient(app) as http:
        app.state.repository.redis = MetricsRedis([entry(live_sample)])
        page = http.get("/bot/momentum-30")
    assert "Signal reference ledger unreadable: UNKNOWN" in page.text
    assert "Queue drops: 3" in page.text and "$+31.80" not in page.text
