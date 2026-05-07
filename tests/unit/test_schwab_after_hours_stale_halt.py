from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.services.strategy_engine_app import StrategyEngineService
from project_mai_tai.settings import Settings


class FakeRedis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"

    async def xread(self, *_args, **_kwargs):
        return []

    async def ping(self):
        return True

    async def aclose(self):
        return None


def make_settings(**kwargs) -> Settings:
    return Settings(
        scanner_feed_retention_enabled=False,
        strategy_macd_30s_enabled=True,
        strategy_webull_30s_enabled=False,
        strategy_macd_1m_enabled=False,
        strategy_schwab_1m_enabled=False,
        strategy_macd_30s_broker_provider="schwab",
        redis_stream_prefix="test",
        dashboard_snapshot_persistence_enabled=False,
        strategy_history_persistence_enabled=False,
        schwab_stream_symbol_stale_after_seconds=8.0,
        schwab_stream_symbol_stale_after_seconds_without_position=90.0,
        schwab_stream_symbol_resubscribe_interval_seconds=1.0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_flat_schwab_symbol_becomes_warning_not_halt_during_trading_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StrategyEngineService(settings=make_settings(), redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["APLZ"])
    fixed_now = datetime(2026, 4, 24, 20, 0, 0, tzinfo=UTC)
    old = fixed_now - timedelta(seconds=122)
    service._schwab_symbol_last_stream_trade_at["APLZ"] = old
    service._schwab_symbol_last_stream_quote_at["APLZ"] = old

    class FakeStreamClient:
        connected = True

        async def force_resubscribe(self) -> None:
            return None

    service._schwab_stream_client = FakeStreamClient()
    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.utcnow", lambda: fixed_now)

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 1
    assert service._schwab_stale_symbols == set()
    assert service._schwab_warning_symbols == {"APLZ"}
    assert runtime.data_health_summary()["status"] == "degraded"
    assert runtime.data_health_summary()["halted_symbols"] == []
    assert runtime.data_health_summary()["warning_symbols"] == ["APLZ"]


@pytest.mark.asyncio
async def test_flat_schwab_symbol_does_not_halt_after_trading_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StrategyEngineService(settings=make_settings(), redis_client=FakeRedis())
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["APLZ"])
    fixed_now = datetime(2026, 4, 24, 22, 20, 43, tzinfo=UTC)
    old = fixed_now - timedelta(seconds=122)
    service._schwab_symbol_last_stream_trade_at["APLZ"] = old
    service._schwab_symbol_last_stream_quote_at["APLZ"] = old

    class FakeStreamClient:
        connected = True

        async def force_resubscribe(self) -> None:
            return None

    service._schwab_stream_client = FakeStreamClient()
    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.utcnow", lambda: fixed_now)

    activity_count = await service._monitor_schwab_symbol_health()

    assert activity_count == 0
    assert service._schwab_stale_symbols == set()
    assert runtime.data_health_summary()["status"] == "healthy"
    assert runtime.data_health_summary()["halted_symbols"] == []
