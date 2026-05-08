from __future__ import annotations

from datetime import UTC, datetime

from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.settings import Settings


def fixed_now() -> datetime:
    return datetime(2026, 4, 24, 10, 0, tzinfo=UTC)


def test_restore_confirmed_runtime_view_seeds_polygon_when_snapshot_has_empty_polygon_handoff() -> None:
    state = StrategyEngineState(
        settings=Settings(
            strategy_macd_30s_enabled=True,
            strategy_polygon_30s_enabled=True,
            scanner_feed_retention_enabled=False,
        ),
        now_provider=fixed_now,
    )

    confirmed = [
        {"ticker": "AUUD", "confirmed_at": "06:18:23 AM ET"},
        {"ticker": "CAST", "confirmed_at": "04:06:56 AM ET"},
    ]
    state.restore_confirmed_runtime_view(
        confirmed,
        all_confirmed=confirmed,
        bot_handoff_symbols_by_strategy={
            "macd_30s": ["AUUD", "CAST"],
            "polygon_30s": [],
        },
        bot_handoff_history_by_strategy={
            "macd_30s": ["AUUD", "CAST"],
            "polygon_30s": [],
        },
    )

    assert state.bots["macd_30s"].watchlist == {"AUUD", "CAST"}
    assert state.bots["polygon_30s"].watchlist == {"AUUD", "CAST"}
