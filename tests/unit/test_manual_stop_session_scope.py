from __future__ import annotations

from datetime import UTC, datetime, timedelta

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.services.strategy_engine_app import StrategyEngineService, current_scanner_session_start_utc
from tests.unit.test_strategy_engine_service import (
    FakeRedis,
    build_test_session_factory,
    make_test_settings,
)


def test_service_ignores_bot_manual_stops_from_wrong_session_marker() -> None:
    session_factory = build_test_session_factory()
    current_session_start = current_scanner_session_start_utc()
    with session_factory() as session:
        session.add(
            DashboardSnapshot(
                snapshot_type="bot_manual_stop_symbols",
                payload={
                    "bots": {"polygon_30s": ["AUUD", "CAST"]},
                    "scanner_session_start_utc": (current_session_start - timedelta(days=1)).isoformat(),
                },
                created_at=datetime.now(UTC),
            )
        )
        session.commit()

    service = StrategyEngineService(
        settings=make_test_settings(
            redis_url="redis://localhost:6379/0",
            strategy_polygon_30s_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    service._preload_manual_stop_state()

    assert service.state.manual_stop_symbols_by_strategy == {}
    assert service.state.bots["polygon_30s"].manual_stop_symbols == set()


def test_service_surfaces_schwab_auth_failure_reason_and_skips_resubscribe() -> None:
    service = StrategyEngineService(
        settings=make_test_settings(
            strategy_macd_30s_broker_provider="schwab",
            redis_stream_prefix="test",
            dashboard_snapshot_persistence_enabled=False,
            strategy_history_persistence_enabled=False,
            schwab_stream_symbol_stale_after_seconds=1.0,
            schwab_stream_symbol_resubscribe_interval_seconds=1.0,
        ),
        redis_client=FakeRedis(),
    )
    runtime = service.state.bots["macd_30s"]
    runtime.set_watchlist(["AUUD"])
    service._schwab_symbol_active_first_seen_at["AUUD"] = datetime.now(UTC) - timedelta(seconds=40)
    service._schwab_stream_disconnected_since = datetime.now(UTC) - timedelta(seconds=40)

    class FakeStreamClient:
        connected = False
        last_error = (
            'failed refreshing Schwab token: unsupported_token_type: 400 Bad Request: '
            '{"error":"refresh_token_authentication_error"}'
        )

        def __init__(self) -> None:
            self.force_resubscribe_calls = 0

        async def force_resubscribe(self) -> None:
            self.force_resubscribe_calls += 1

    fake_stream_client = FakeStreamClient()
    service._schwab_stream_client = fake_stream_client

    import asyncio

    activity_count = asyncio.run(service._monitor_schwab_symbol_health())

    assert activity_count == 1
    assert fake_stream_client.force_resubscribe_calls == 0
    assert service._schwab_stale_symbols == {"AUUD"}
    assert runtime.data_health_summary()["reasons"]["AUUD"] == (
        "Schwab OAuth refresh failed on the VPS; reauthorize Schwab tokens before trading"
    )
