from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from project_mai_tai.db.models import BrokerAccount, Strategy
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    IsolatedBotStateEvent,
    StrategyBotStatePayload,
)
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_control_plane import FakeRedis, build_test_session_factory


NOW = datetime(2026, 10, 9, 13, 45, tzinfo=UTC)


def _heartbeat(name="orb-schwab", *, at=NOW, status="healthy", phase="entry_window"):
    return HeartbeatEvent(
        source_service=name, produced_at=at,
        payload=HeartbeatPayload(
            service_name=name, instance_name=name, status=status,
            details={
                "mode": "LIVE", "phase": phase,
                "subscribed": '["LIVE_ONLY"]', "universe": '["LIVE_ONLY"]',
                "last_bar_at": (NOW - timedelta(seconds=10)).isoformat(),
                "last_decision_at": (NOW - timedelta(seconds=5)).isoformat(),
            },
        ),
    )


def _app(monkeypatch, heartbeats, *, bar_counts=None):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    factory = build_test_session_factory()
    with factory() as session:
        session.add_all([
            BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="test", is_active=True),
            Strategy(code="orb_schwab", name="ORB live", is_enabled=True, metadata_json={}),
        ])
        session.commit()
    # Synthetic contradictory snapshot: it must never establish live runtime activity.
    snapshot = IsolatedBotStateEvent(
        source_service="strategy-engine", produced_at=NOW - timedelta(days=1),
        payload=StrategyBotStatePayload(
            strategy_code="orb_schwab", account_name="live:schwab_1m_v2",
            watchlist=["SNAPSHOT_ONLY"], bar_counts=bar_counts or {},
            last_tick_at={"SNAPSHOT_ONLY": "2026-10-08 09:45:00 AM ET"},
            data_health={"status": "stalled-rth", "loop_health": "degraded"},
        ),
    )
    redis = FakeRedis({
        "test:heartbeats": [
            (str(index), {"data": event.model_dump_json()})
            for index, event in enumerate(heartbeats)
        ],
        "test:strategy-state-isolated": [("old", {"data": snapshot.model_dump_json()})],
    })
    return cp.build_app(
        settings=Settings(
            redis_stream_prefix="test", orb_enabled=True, orb_live_schwab_orders_enabled=True,
            trade_coach_enabled=False, schwab_token_refresher_enabled=False,
        ),
        session_factory=factory, redis_client=redis,
    )


@pytest.mark.parametrize("phase, state", [
    ("entry_window", "EVALUATING"),
    ("waiting_for_open", "WAITING FOR 09:27"),
    ("session_complete", "SESSION COMPLETE"),
])
def test_home_and_orb_page_use_live_heartbeat_over_stale_snapshot(monkeypatch, phase, state):
    with TestClient(_app(monkeypatch, [_heartbeat(phase=phase)], bar_counts={"SNAPSHOT_ONLY": 987})) as client:
        home = client.get("/").text
        card = home.split('<a href="/bot/orb">', 1)[1].split("\n    </div>", 1)[0]
        assert cp._friendly_data_flow(state) in card
        assert 'm-label">Live symbols</div><div class="m-val">1</div>' in card
        assert 'm-label">Bar age</div><div class="m-val good">10s</div>' in card
        assert f"orb-schwab: {state.lower()}" in card
        assert "subscribed: 1" in card
        assert "stalled" not in card.lower() and "degraded" not in card
        assert "exceptions: not reported" in card
        live = client.get("/api/bot/orb-schwab").json()
        assert live["watched_tickers"] == ["LIVE_ONLY"]
        assert live["bar_counts"] == {}
        assert live["last_tick_at"] == {}
        assert live["data_health"] == {"status": "not reported"}
        listening = live["listening_status"]
        assert listening["state"] == state
        assert listening["latest_bot_tick_at"] == ""
        assert listening["data_health"] == {}
        assert listening["last_bar_at"] == (NOW - timedelta(seconds=10)).isoformat()
        assert listening["latest_decision_at"] == (NOW - timedelta(seconds=5)).isoformat()
        assert listening["tracked_bar_count"] is None
        page = client.get("/bot/orb").text
        assert f"<strong>Status:</strong> {state}" in page
        assert "Bars cached: not reported" in page
        assert "Bars cached: 987" not in page
        assert "NOT REPORTED" in page
        assert "ORB-Schwab heartbeat does not report data-path health." in page


def test_orb_listening_helper_does_not_borrow_snapshot_telemetry(monkeypatch):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    event = _heartbeat()
    data = {"services": [{
        "service_name": "orb-schwab", "status": "healthy", "observed_at_raw": NOW,
        "details": event.payload.details,
    }]}
    bot = {
        "strategy_code": "orb_schwab", "positions": [],
        "bar_counts": {"SNAPSHOT_ONLY": 987},
        "last_tick_at": {"SNAPSHOT_ONLY": "2026-10-08 09:45:00 AM ET"},
        "data_health": {"status": "degraded"},
    }
    status = cp._build_bot_listening_status(data, bot, [])
    assert status["state"] == "EVALUATING"
    assert status["tracked_bar_count"] is None
    assert status["latest_bot_tick_at"] == ""
    assert status["data_health"] == {}


@pytest.mark.parametrize("bar_counts", [{}, {"SNAPSHOT_ONLY": 0}, {"SNAPSHOT_ONLY": 987}])
def test_unreported_cached_bars_are_not_snapshot_count_or_fabricated_zero(monkeypatch, bar_counts):
    with TestClient(_app(monkeypatch, [_heartbeat()], bar_counts=bar_counts)) as client:
        assert client.get("/api/bot/orb-schwab").json()["listening_status"]["tracked_bar_count"] is None
        page = client.get("/bot/orb").text
        assert "Bars cached: not reported" in page
        assert "Bars cached: 0" not in page


@pytest.mark.parametrize("heartbeats", [[], [_heartbeat(at=NOW - timedelta(seconds=61))]])
def test_home_cannot_claim_live_activity_without_fresh_live_heartbeat(monkeypatch, heartbeats):
    with TestClient(_app(monkeypatch, heartbeats)) as client:
        card = client.get("/").text.split('<a href="/bot/orb">', 1)[1].split("\n    </div>", 1)[0]
        assert "Stopped" in card
        assert "orb-schwab: stopped" in card
        assert client.get("/api/bot/orb-schwab").json()["listening_status"]["state"] == "STOPPED"


@pytest.mark.parametrize("live_status, health_status", [("healthy", "healthy"), ("degraded", "degraded")])
def test_health_excludes_retired_paper_orb_but_preserves_live_and_other_services(monkeypatch, live_status, health_status):
    heartbeats = [
        _heartbeat(status=live_status),
        _heartbeat("orb", at=NOW - timedelta(days=1), status="degraded"),
        _heartbeat("oms-risk"),
    ]
    with TestClient(_app(monkeypatch, heartbeats)) as client:
        health = client.get("/health").json()
        services = {service["service_name"]: service for service in health["services"]}
        assert "orb" not in services
        assert services["orb-schwab"]["raw_status"] == live_status
        assert services["orb-schwab"]["details"]["subscribed"] == '["LIVE_ONLY"]'
        assert services["oms-risk"]["status"] == "healthy"
        assert health["status"] == health_status


def test_non_orb_snapshot_count_and_tick_still_render(monkeypatch):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    bot = {
        "strategy_code": "schwab_1m_v2", "provider": "schwab",
        "watchlist": ["ATR_ONLY"], "positions": [], "bar_counts": {"ATR_ONLY": 0},
        "last_tick_at": {"ATR_ONLY": "2026-10-09 09:45:00 AM ET"},
        "data_health": {"status": "healthy"},
    }
    status = cp._build_bot_listening_status({"services": [], "market_data": {}}, bot, [])
    assert status["tracked_bar_count"] == 0
    assert status["latest_bot_tick_at"] == "2026-10-09 09:45:00 AM ET"
    assert status["data_health"] == {"status": "healthy"}
