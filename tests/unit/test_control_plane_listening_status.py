from __future__ import annotations

from datetime import UTC, datetime, timedelta

from project_mai_tai.services.control_plane import _build_bot_listening_status
from project_mai_tai.services.strategy_engine_app import EASTERN_TZ


def test_listening_status_surfaces_flat_symbol_staleness_as_degraded_not_data_halt() -> None:
    now = datetime.now(UTC).astimezone(EASTERN_TZ)
    heartbeat_at = (now - timedelta(seconds=15)).strftime("%Y-%m-%d %I:%M:%S %p ET")
    market_data_at = (now - timedelta(seconds=10)).strftime("%Y-%m-%d %I:%M:%S %p ET")
    tick_at = (now - timedelta(seconds=20)).strftime("%Y-%m-%d %I:%M:%S %p ET")
    decision_at = (now - timedelta(seconds=30)).strftime("%Y-%m-%d %I:%M:%S %p ET")

    data = {
        "services": [
            {
                "service_name": "strategy-engine",
                "status": "healthy",
                "effective_status": "healthy",
                "observed_at": heartbeat_at,
            }
        ],
        "market_data": {
            "latest_snapshot_batch": {
                "completed_at": market_data_at,
            }
        },
    }
    bot = {
        "provider": "schwab",
        "watchlist": ["ENVB"],
        "positions": [],
        "last_tick_at": {
            "ENVB": tick_at,
        },
        "indicator_snapshots": [],
        "data_health": {
            "status": "degraded",
            "halted_symbols": ["ENVB"],
            "reasons": {
                "ENVB": "Schwab stream stale/disconnected; trading halted until live Schwab ticks recover",
            },
            "since": {
                "ENVB": tick_at,
            },
        },
        "bar_counts": {"ENVB": 200},
    }
    recent_decisions = [
        {
            "last_bar_at": decision_at,
        }
    ]

    listening_status = _build_bot_listening_status(data, bot, recent_decisions)

    assert listening_status["state"] == "DEGRADED"
    assert "trading halted until live Schwab ticks recover" in listening_status["detail"]
