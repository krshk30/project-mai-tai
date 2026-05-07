from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.services.strategy_engine_app import StrategyEngineService, StrategyEngineState
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


def fixed_now() -> datetime:
    return datetime(2026, 4, 24, 10, 0, tzinfo=UTC)


def test_expired_schwab_prewarm_symbol_is_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_macd_30s_broker_provider="schwab",
            scanner_feed_retention_enabled=False,
            schwab_prewarm_symbol_ttl_seconds=60.0,
        ),
        now_provider=fixed_now,
    )

    first_now = datetime(2026, 4, 24, 10, 0, tzinfo=UTC)
    second_now = first_now + timedelta(minutes=2)
    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.utcnow", lambda: first_now)
    state._add_schwab_prewarm_symbols(["UGRO"])

    assert state.schwab_stream_symbols() == ["UGRO"]

    monkeypatch.setattr("project_mai_tai.services.strategy_engine_app.utcnow", lambda: second_now)
    assert state.schwab_stream_symbols() == []
    assert state.schwab_prewarm_symbols == []
    assert state.bots["macd_30s"].prewarm_symbols == set()


@pytest.mark.asyncio
async def test_service_surfaces_schwab_auth_failure_reason_without_fake_resubscribe() -> None:
    service = StrategyEngineService(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            scanner_feed_retention_enabled=False,
            strategy_history_persistence_enabled=False,
            schwab_stream_symbol_stale_after_seconds=1.0,
            schwab_stream_symbol_resubscribe_interval_seconds=1.0,
        ),
        redis_client=FakeRedis(),
    )
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["AUUD"])
    old = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_symbol_active_first_seen_at["AUUD"] = old
    service._schwab_stream_disconnected_since = old

    class FakeDisconnectedStreamClient:
        connected = False
        connection_failures = 3
        last_error = "RuntimeError: failed refreshing Schwab token: refresh_token_authentication_error: unsupported_token_type"

        def __init__(self) -> None:
            self.force_resubscribe_calls = 0

        async def force_resubscribe(self) -> None:
            self.force_resubscribe_calls += 1

    fake_stream_client = FakeDisconnectedStreamClient()
    service._schwab_stream_client = fake_stream_client

    intent_count = await service._monitor_schwab_symbol_health()

    assert intent_count == 1
    assert fake_stream_client.force_resubscribe_calls == 0
    assert runtime.data_health_summary()["reasons"]["AUUD"] == (
        "Schwab OAuth refresh failed on the VPS; reauthorize Schwab tokens before trading"
    )


@pytest.mark.asyncio
async def test_service_surfaces_quote_poll_auth_failure_immediately_for_active_symbol() -> None:
    service = StrategyEngineService(
        settings=Settings(
            strategy_macd_30s_broker_provider="schwab",
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            scanner_feed_retention_enabled=False,
            strategy_history_persistence_enabled=False,
            schwab_stream_symbol_stale_after_seconds=300.0,
            schwab_stream_symbol_resubscribe_interval_seconds=60.0,
        ),
        redis_client=FakeRedis(),
    )
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["LABT"])
    service._schwab_symbol_active_first_seen_at["LABT"] = datetime.now(UTC)
    service._schwab_stream_disconnected_since = None

    class FakeHealthyStreamClient:
        connected = True
        connection_failures = 0
        last_error = ""

        async def force_resubscribe(self) -> None:
            raise AssertionError("force_resubscribe should not be called during auth failure")

    class FakeAuthAdapter:
        last_error = "RuntimeError: failed refreshing Schwab token: refresh_token_authentication_error: unsupported_token_type"

    service._schwab_stream_client = FakeHealthyStreamClient()
    service._schwab_quote_poll_adapter = FakeAuthAdapter()

    intent_count = await service._monitor_schwab_symbol_health()

    assert intent_count == 1
    assert runtime.data_health_summary()["reasons"]["LABT"] == (
        "Schwab OAuth refresh failed on the VPS; reauthorize Schwab tokens before trading"
    )
