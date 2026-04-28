from __future__ import annotations

from datetime import UTC, datetime, timedelta

from project_mai_tai.services.control_plane import _build_bot_listening_status
from project_mai_tai.services.strategy_engine_app import EASTERN_TZ


def test_listening_status_prefers_fresh_bot_activity_over_stale_service_state() -> None:
    now = datetime.now(UTC).astimezone(EASTERN_TZ)
    heartbeat_at = (now - timedelta(seconds=15)).strftime("%Y-%m-%d %I:%M:%S %p ET")
    market_data_at = (now - timedelta(seconds=10)).strftime("%Y-%m-%d %I:%M:%S %p ET")
    tick_at = (now - timedelta(seconds=12)).strftime("%Y-%m-%d %I:%M:%S %p ET")
    decision_at = (now - timedelta(seconds=30)).strftime("%Y-%m-%d %I:%M:%S %p ET")

    data = {
        "services": [
            {
                "service_name": "strategy-engine",
                "status": "stopping",
                "effective_status": "stopping",
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
        "watchlist": ["YAAS"],
        "positions": [],
        "last_tick_at": {
            "YAAS": tick_at,
        },
        "indicator_snapshots": [],
        "data_health": {
            "status": "healthy",
            "halted_symbols": [],
            "reasons": {},
            "since": {},
        },
        "bar_counts": {"YAAS": 200},
    }
    recent_decisions = [
        {
            "last_bar_at": decision_at,
        }
    ]

    listening_status = _build_bot_listening_status(data, bot, recent_decisions)

    assert listening_status["state"] == "LISTENING"
    assert listening_status["detail"] == "Bot activity is fresh; strategy service status snapshot is lagging."


def test_listening_status_keeps_bot_listening_for_flat_symbol_warning() -> None:
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
            "halted_symbols": [],
            "warning_symbols": ["ENVB"],
            "reasons": {},
            "warning_reasons": {
                "ENVB": "Schwab symbol is quiet on a flat positionless name; synthetic 30s bars can continue, but live Schwab ticks are temporarily sparse.",
            },
            "since": {},
            "warning_since": {
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

    assert listening_status["state"] == "LISTENING"
    assert "temporarily sparse" in listening_status["detail"]
