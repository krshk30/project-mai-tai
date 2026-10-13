"""Gateway-only component controls; main's actual helper is a stack dependency.

Settings env validation and the provider A callback gate belong to composition,
not these controlled clock/provider hooks. No live/source timing evidence.
"""

import asyncio
from datetime import date, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from project_mai_tai.market_data import gateway, massive_provider
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.momwarn_validation_window import MomentumValidationWindow
from project_mai_tai.settings import Settings
from tests.unit.test_momwarn1_gateway import aggregate, event, healthy, stream
from test_market_data_gateway import FakeRedis, FakeSnapshotProvider, FakeReferenceCache


def build(monkeypatch, stamp, *, override=True):
    observed = datetime.fromisoformat(stamp)
    monkeypatch.setattr(gateway, "datetime", SimpleNamespace(now=lambda _: observed))
    settings = Settings(_env_file=None, redis_stream_prefix="test",
                        market_data_warmup_enabled=False, market_data_momentum_warning_enabled=True)
    if override:
        # Controlled cold settings object; actual env/schema validation is main-owned.
        settings = settings.model_copy(update={
            "momentum_validation_session_date": date(2026, 10, 13),
            "market_data_momentum_warning_end_et": time(18),
            "momentum_paper_detection_end_et": time(18),
            "momentum_paper_guard_end_et": time(18, 10),
        })
    provider = stream()
    provider.set_momentum_warning_aggregates = AsyncMock(return_value=True)
    svc = MarketDataGatewayService(settings, FakeRedis(), trade_stream=provider,
        snapshot_provider=FakeSnapshotProvider(), reference_cache=FakeReferenceCache(),
        warning_redis_client=FakeRedis(), warning_health_reader=SimpleNamespace(read=healthy))
    return svc, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("end,active", [(time(18), True), (time(9, 30), False)])
async def test_native_provider_shares_daily_clock_or_explicit_override(monkeypatch, end, active):
    observed = datetime.fromisoformat("2026-10-14T10:00:00-04:00")
    fixed = SimpleNamespace(now=lambda _: observed)
    monkeypatch.setattr(gateway, "datetime", fixed)
    monkeypatch.setattr(massive_provider, "datetime", fixed)
    settings = Settings(_env_file=None, market_data_momentum_warning_enabled=True,
                        market_data_momentum_warning_end_et=end,
                        momentum_paper_detection_end_et=end,
                        momentum_paper_guard_end_et=time(18, 10))
    provider = stream()
    svc = MarketDataGatewayService(settings, FakeRedis(), trade_stream=provider,
        snapshot_provider=FakeSnapshotProvider(), reference_cache=FakeReferenceCache(),
        warning_redis_client=FakeRedis(), warning_health_reader=SimpleNamespace(read=healthy))
    await svc._warning_monitor_once()
    assert svc._warning_active is active
    assert provider._warning_aggregates_enabled is active
    assert provider._momentum_warning_window.gateway_end_for(observed.date()) == end
    assert svc._warning_revert_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp,override,active", [
    ("2026-10-13T09:29:59-04:00", False, True),
    ("2026-10-13T09:30:00-04:00", False, True),
    ("2026-10-13T09:30:00-04:00", True, True),
    ("2026-10-13T17:59:59-04:00", True, True),
    ("2026-10-13T18:00:00-04:00", True, False),
    ("2026-10-14T09:29:59-04:00", True, True),
    ("2026-10-14T09:30:00-04:00", True, True),
    ("2026-10-13T03:59:59-04:00", True, False),
    ("2026-10-17T08:30:00-04:00", True, False),
    ("2026-12-25T08:30:00-05:00", True, False),
])
async def test_monitor_exact_today_end_and_tomorrow_defaults_preserve_eligibility(
        monkeypatch, stamp, override, active):
    svc, provider = build(monkeypatch, stamp, override=override)
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    owners, saved = dict(svc._desired_symbols_by_consumer), dict(svc.redis.hashes)
    await svc._warning_monitor_once()
    assert svc._warning_active is active and svc._warning_revert_reason is None
    provider.set_momentum_warning_aggregates.assert_awaited_once_with(active)
    assert svc._desired_symbols_by_consumer == owners and svc.redis.hashes == saved
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp,override,published", [
    ("2026-10-13T09:30:00-04:00", False, True),
    ("2026-10-13T09:30:00-04:00", True, True),
    ("2026-10-13T17:59:59-04:00", True, True),
    ("2026-10-13T18:00:00-04:00", True, False),
    ("2026-10-14T09:30:00-04:00", True, True),
])
async def test_publish_loop_uses_same_date_scoped_boundary_and_quiet_close(
        monkeypatch, stamp, override, published):
    svc, provider = build(monkeypatch, stamp, override=override)
    svc._warning_active = True
    record = provider._normalize_warning_aggregate(aggregate(), "WFF")
    assert record is not None
    svc._enqueue_warning(record)
    stop, seen = asyncio.Event(), []

    async def publish(*args, **kwargs):
        seen.append(args[0])
        stop.set()

    async def close(enabled):
        assert enabled is False
        stop.set()
        return False

    svc._warning_publisher.publish_momentum_warning_aggregate = publish
    provider.set_momentum_warning_aggregates.side_effect = close
    await asyncio.wait_for(svc._warning_publish_loop(stop), timeout=1)
    assert bool(seen) is published
    assert svc._warning_drop_count == int(not published)
    assert svc._warning_queue.empty() and svc._warning_revert_reason is None
    assert svc._warning_active is published


@pytest.mark.asyncio
async def test_inside_validation_window_does_not_clear_safety_latch(monkeypatch):
    svc, provider = build(monkeypatch, "2026-10-13T17:00:00-04:00")
    svc._warning_revert_reason = "load"
    await svc._warning_monitor_once()
    assert svc._warning_revert_reason == "load" and not svc._warning_active
    provider.set_momentum_warning_aggregates.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_helper_constructed_once_not_reloaded_on_monitor_tick(monkeypatch):
    constructor = Mock(wraps=MomentumValidationWindow.from_settings)
    monkeypatch.setattr(MomentumValidationWindow, "from_settings", constructor)
    svc, _ = build(monkeypatch, "2026-10-13T17:00:00-04:00")
    captured = svc._momentum_validation_window
    svc.settings = svc.settings.model_copy(update={"market_data_momentum_warning_end_et": time(19)})
    await svc._warning_monitor_once()
    await svc._warning_monitor_once()
    constructor.assert_called_once()
    assert captured is svc._momentum_validation_window
    assert captured.gateway_end_for(date(2026, 10, 13)) == time(18)


@pytest.mark.asyncio
async def test_helper_day_is_ET_not_UTC(monkeypatch):
    original = MomentumValidationWindow.gateway_end_for
    days = []

    def end(window, day):
        days.append(day)
        return original(window, day)

    monkeypatch.setattr(MomentumValidationWindow, "gateway_end_for", end)
    svc, _ = build(monkeypatch, "2026-10-14T00:30:00+00:00")
    await svc._warning_monitor_once()
    assert days == [date(2026, 10, 13)]
    assert not svc._warning_active and svc._warning_revert_reason is None
