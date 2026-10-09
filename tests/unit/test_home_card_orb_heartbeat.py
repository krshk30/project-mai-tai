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
                "healthy_since": "2026-10-09T00:10:32Z",
            },
        ),
    )


def _app(monkeypatch, heartbeats, *, bar_counts=None, now=NOW):
    monkeypatch.setattr(cp, "utcnow", lambda: now)
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
        assert live["data_health"]["status"] == "healthy"
        listening = live["listening_status"]
        assert listening["state"] == state
        assert listening["latest_bot_tick_at"] == "2026-10-09 09:44:50 AM ET"
        assert listening["latest_market_data_at"] == "2026-10-09 09:44:50 AM ET"
        assert listening["data_health"]["status"] == "healthy"
        assert listening["last_bar_at"] == "2026-10-09 09:44:50 AM ET"
        assert listening["latest_decision_at"] == (NOW - timedelta(seconds=5)).isoformat()
        assert listening["tracked_bar_count"] is None
        page = client.get("/bot/orb").text
        assert f"<strong>Status:</strong> {state}" in page
        assert "Bars cached: not reported" in page
        assert "Bars cached: 987" not in page
        assert '<span>Schwab Data Health</span><strong style="color:#5fff8d">HEALTHY</strong>' in page
        assert "ORB-Schwab heartbeat is fresh and its last bar is within 3 minutes." in page
        assert "Healthy since 10-08 08:10:32 PM ET" in page
        assert "Data date: 2026-10-09 (TODAY ET)" in card


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
    assert status["latest_bot_tick_at"] == "2026-10-09 09:44:50 AM ET"
    assert status["latest_market_data_at"] == "2026-10-09 09:44:50 AM ET"
    assert status["data_health"]["status"] == "healthy"


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


def test_rendered_bar_rows_convert_zulu_to_eastern_and_healthy_since_is_compact(monkeypatch):
    now = datetime(2026, 10, 9, 10, 5, 10, tzinfo=UTC)
    event = _heartbeat(at=now, phase="waiting_for_open")
    event.payload.details["last_bar_at"] = "2026-10-09T10:05:00Z"
    with TestClient(_app(monkeypatch, [event], now=now)) as client:
        page = client.get("/bot/orb").text
        for label in ("Last Bot Tick", "Last Market Data"):
            assert f'<span>{label}</span><strong>2026-10-09 06:05:00 AM ET</strong>' in page
        assert "Healthy since 10-08 08:10:32 PM ET" in page
        assert "Healthy since 2026-10-09T00:10:32Z" not in page
        assert '<span>Schwab Data Health</span><strong style="color:#5fff8d">HEALTHY</strong>' in page
        assert "Bars cached: not reported" in page


@pytest.mark.parametrize("heartbeat_offset, bar_offset, expected", [
    (0, 0, "HEALTHY"), (60, 180, "HEALTHY"),
    (0, 180.001, "WARN"), (0, 600, "WARN"),
    (60.001, 10, "STOPPED"), (61, 600, "STOPPED"),
    (-1, 10, "STOPPED"), (0, -1, "WARN"),
])
def test_rendered_schwab_health_uses_heartbeat_and_three_minute_bar_bound(monkeypatch, heartbeat_offset, bar_offset, expected):
    event = _heartbeat(at=NOW - timedelta(seconds=heartbeat_offset))
    event.payload.details["last_bar_at"] = (NOW - timedelta(seconds=bar_offset)).isoformat()
    with TestClient(_app(monkeypatch, [event], bar_counts={"SNAPSHOT_ONLY": 987})) as client:
        page = client.get("/bot/orb").text
        color = {"HEALTHY": "#5fff8d", "STOPPED": "#ff6b6b", "WARN": "#ffcc5b"}[expected]
        assert f'<span>Schwab Data Health</span><strong style="color:{color}">{expected}</strong>' in page
        assert client.get("/api/bot/orb-schwab").json()["data_health"]["status"] == expected.lower()
        assert "Bars cached: not reported" in page


@pytest.mark.parametrize("stamp", ["", "unreadable", "2026-10-09T13:44:50", "2026-10-09T13:45:01Z"])
def test_invalid_bar_time_renders_warn_and_unreported_times_without_snapshot_fallback(monkeypatch, stamp):
    event = _heartbeat()
    event.payload.details["last_bar_at"] = stamp
    with TestClient(_app(monkeypatch, [event])) as client:
        page = client.get("/bot/orb").text
        for label in ("Last Bot Tick", "Last Market Data"):
            assert f'<span>{label}</span><strong>not reported</strong>' in page
        assert '<span>Schwab Data Health</span><strong style="color:#ffcc5b">WARN</strong>' in page
        assert "Bars cached: not reported" in page


@pytest.mark.parametrize("stamp", ["unreadable", "2026-10-09T13:44:50", "2026-10-09T13:45:01Z"])
def test_invalid_heartbeat_identity_is_stopped_not_healthy(monkeypatch, stamp):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    event = _heartbeat()
    data = {"services": [{"service_name": "orb-schwab", "status": "healthy", "observed_at_raw": stamp,
                           "details": event.payload.details}]}
    status = cp._build_bot_listening_status(data, {"strategy_code": "orb_schwab"}, [])
    assert status["state"] == "STOPPED"
    assert status["data_health"]["status"] == "stopped"


def test_home_data_date_is_today_eastern_not_utc_or_old_snapshot_date(monkeypatch):
    now = datetime(2026, 10, 9, 0, 5, tzinfo=UTC)
    event = _heartbeat(at=now)
    event.payload.details["last_bar_at"] = (now - timedelta(seconds=10)).isoformat()
    with TestClient(_app(monkeypatch, [event], now=now)) as client:
        card = client.get("/").text.split('<a href="/bot/orb">', 1)[1].split("\n    </div>", 1)[0]
        assert "Data date: 2026-10-08 (TODAY ET)" in card
        assert "Data date: 2026-10-09" not in card


@pytest.mark.parametrize("stamp", ["unreadable", "2026-10-09T13:44:50", "2026-10-09T13:45:01Z"])
def test_invalid_healthy_since_is_unreported_not_raw_or_future(monkeypatch, stamp):
    event = _heartbeat(phase="waiting_for_open")
    event.payload.details["healthy_since"] = stamp
    with TestClient(_app(monkeypatch, [event])) as client:
        page = client.get("/bot/orb").text
        assert "Healthy since not reported" in page
        assert f"Healthy since {stamp}" not in page
        assert "Bars cached: not reported" in page
