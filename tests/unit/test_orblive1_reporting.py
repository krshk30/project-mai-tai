import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from project_mai_tai.events import HeartbeatEvent
from project_mai_tai.services import control_plane as cp
from project_mai_tai.services import orb_app
from project_mai_tai.services.orb_schwab_app import OrbSchwabService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from tests.unit.test_control_plane import FakeRedis, _orbpage_app, _orbpage_service
from tests.unit.test_fleet_health import _load


NOW = datetime(2026, 10, 8, 10, 27, tzinfo=UTC)
OPEN = NOW.replace(hour=13, minute=30)


def service(monkeypatch, *, observe=False):
    redis = FakeRedis({})
    svc = OrbSchwabService(
        settings=Settings(redis_stream_prefix="test", orb_enabled=True,
                          orb_live_schwab_orders_enabled=not observe,
                          orb_schwab_observe_enabled=observe),
        redis_client=redis, session_factory=lambda: None,
    )
    monkeypatch.setattr(svc, "_processing_time", lambda: NOW)
    monkeypatch.setattr(svc, "_session_open_utc", lambda: OPEN)
    return svc, redis


@pytest.mark.parametrize("minute,phase", [
    (13 * 60 + 24, "waiting_for_open"), (13 * 60 + 25, "opening_range"),
    (13 * 60 + 26, "opening_range"), (13 * 60 + 27, "entry_window"),
    (13 * 60 + 59, "entry_window"), (14 * 60, "session_complete"),
])
def test_live_heartbeat_phase_boundaries_do_not_move_trading_window(monkeypatch, minute, phase):
    svc, _ = service(monkeypatch)
    at = NOW.replace(hour=minute // 60, minute=minute % 60)
    assert svc._live_phase(at) == phase


def test_live_heartbeat_wire_owns_only_successfully_announced_symbols_and_no_paper_state(monkeypatch):
    svc, redis = service(monkeypatch)
    svc._universe = {"AIXI", "DKI"}
    svc._last_gateway_symbols = ["AIXI", "DKI", "HELD"]
    svc._gateway_subscription_announced = True
    svc._on_bar("AIXI", OrbBar(NOW, 1, 1, 1, 1, 100))
    svc._live_last_decision_at = NOW - timedelta(seconds=1)
    asyncio.run(svc._publish_live_heartbeat())
    assert list(redis.streams) == ["test:heartbeats"]
    event = HeartbeatEvent.model_validate_json(redis.streams["test:heartbeats"][0][1]["data"])
    assert event.source_service == event.payload.service_name == "orb-schwab"
    assert event.payload.status == "healthy"
    assert event.payload.details["mode"] == "LIVE"
    assert event.payload.details["phase"] == "waiting_for_open"
    assert json.loads(event.payload.details["universe"]) == ["AIXI", "DKI"]
    assert json.loads(event.payload.details["subscribed"]) == ["AIXI", "DKI", "HELD"]
    assert event.payload.details["last_bar_at"] == NOW.isoformat()
    assert event.payload.details["last_decision_at"] == (NOW - timedelta(seconds=1)).isoformat()
    assert event.payload.details["healthy_since"] == NOW.isoformat()
    svc._gateway_subscription_announced = False
    asyncio.run(svc._publish_live_heartbeat())
    assert json.loads(json.loads(redis.streams["test:heartbeats"][0][1]["data"])["payload"]["details"]["subscribed"]) == []


def test_observer_heartbeat_never_claims_live_subscriptions(monkeypatch):
    svc, redis = service(monkeypatch, observe=True)
    svc._last_gateway_symbols = ["AIXI"]
    svc._gateway_subscription_announced = True
    asyncio.run(svc._publish_live_heartbeat())
    details = json.loads(redis.streams["test:heartbeats"][0][1]["data"])["payload"]["details"]
    assert details["mode"] == "OBSERVE_ONLY"
    assert json.loads(details["subscribed"]) == []


def test_heartbeat_loop_retries_publish_failure_at_15_seconds_without_db(monkeypatch):
    svc, redis = service(monkeypatch)
    pauses = []
    calls = []
    original = redis.xadd

    async def publish(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("unavailable")
        return await original(*args, **kwargs)

    async def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(redis, "xadd", publish)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    svc.session_factory = lambda: pytest.fail("heartbeat attempted SQL")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc._live_heartbeat_loop())
    assert pauses == [15, 15]
    assert len(calls) == 2
    assert svc._live_healthy_since == NOW


def test_real_run_heartbeat_task_cannot_block_bar_or_exit_processing(monkeypatch):
    svc, redis = service(monkeypatch)
    svc.settings.market_data_subscription_startup_enabled = False
    processed = []

    async def exercise():
        publishing = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_publish(*args, **kwargs):
            publishing.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def drain():
            await asyncio.wait_for(publishing.wait(), 1)
            return 1

        async def bars():
            processed.append("bars")

        async def exits(*args):
            processed.append("exits")
            raise RuntimeError("end test turn")

        async def noop(*args):
            pass

        monkeypatch.setattr(redis, "xadd", slow_publish)
        monkeypatch.setattr(svc, "_refresh_universe", lambda: None)
        monkeypatch.setattr(svc, "_sync_gateway_subscription", noop)
        monkeypatch.setattr(svc, "_drain_market_data", drain)
        monkeypatch.setattr(svc, "_process_closed_bars", bars)
        monkeypatch.setattr(svc, "_observe_working_plans", noop)
        monkeypatch.setattr(svc, "_process_strategy_exits", exits)
        with pytest.raises(RuntimeError, match="end test turn"):
            await asyncio.wait_for(svc.run(), 2)
        assert cancelled.is_set()

    asyncio.run(exercise())
    assert processed == ["bars", "exits"]


@pytest.mark.parametrize("phase,expected", [
    ("waiting_for_open", "WAITING FOR 09:27"), ("opening_range", "EVALUATING"),
    ("entry_window", "EVALUATING"), ("session_complete", "SESSION COMPLETE"),
    ("unknown", "UNKNOWN"),
])
def test_listening_phase_is_live_report_not_inferred_from_clock_or_decisions(monkeypatch, phase, expected):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    reported = _orbpage_service(NOW, phase)
    reported["details"].update(universe='["AIXI", "DKI"]', subscribed='["DKI"]', healthy_since=NOW.isoformat())
    bot = {"strategy_code": "orb_schwab", "provider": "schwab", "watchlist": ["OLD"], "positions": []}
    result = cp._build_bot_listening_status({"services": [reported]}, bot, [])
    assert result["state"] == expected
    assert result["subscribed"] == ["DKI"]
    assert result["universe"] == ["AIXI", "DKI"]
    if phase == "waiting_for_open":
        assert "AIXI, DKI" in result["detail"] and NOW.isoformat() in result["detail"]


@pytest.mark.parametrize("age", [None, 60.001, -1])
@pytest.mark.parametrize("holding", [False, True])
def test_missing_or_stale_heartbeat_is_stopped_even_with_ticks_or_held_book(monkeypatch, age, holding):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    services = [] if age is None else [_orbpage_service(NOW - timedelta(seconds=age), "entry_window")]
    bot = {"strategy_code": "orb_schwab", "provider": "schwab", "positions": [{"ticker": "DKI"}] if holding else [],
           "last_tick_at": {"DKI": cp._datetime_str(NOW)}, "watchlist": ["DKI"]}
    assert cp._build_bot_listening_status({"services": services}, bot, [])["state"] == "STOPPED"


@pytest.mark.parametrize("kind,expected", [("order", "WORKING"), ("position", "IN TRADE"), ("closed_order", "SESSION COMPLETE")])
def test_live_book_overrides_complete_phase_but_terminal_orders_do_not(monkeypatch, kind, expected):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    bot = {"strategy_code": "orb_schwab", "provider": "schwab", "positions": [], "watchlist": []}
    if kind == "position":
        bot["positions"] = [{"ticker": "DKI"}]
    else:
        bot["recent_orders"] = [{"status": "accepted" if kind == "order" else "cancelled"}]
    assert cp._build_bot_listening_status({"services": [_orbpage_service(NOW, "session_complete")]}, bot, [])["state"] == expected


def test_live_heartbeat_reaches_health_and_symbols_without_any_decision_rows(monkeypatch):
    app, _ = _orbpage_app(monkeypatch, heartbeat=False)
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    event = HeartbeatEvent(source_service="orb-schwab", produced_at=NOW,
                           payload={"service_name": "orb-schwab", "instance_name": "orb-schwab", "status": "healthy",
                                    "details": {"mode": "LIVE", "phase": "waiting_for_open", "universe": '["AIXI", "DKI"]',
                                                "subscribed": '["DKI"]', "healthy_since": NOW.isoformat()}})
    with TestClient(app) as client:
        app.state.repository.redis.streams["test:heartbeats"] = [("1", {"data": event.model_dump_json()})]
        health = client.get("/health").json()
        assert next(row for row in health["services"] if row["service_name"] == "orb-schwab")["effective_status"] == "healthy"
        live = client.get("/api/bot/orb-schwab").json()
        assert live["watched_tickers"] == ["DKI"]
        assert live["recent_decisions"] == []
        assert live["listening_status"]["state"] == "WAITING FOR 09:27"
        page = client.get("/bot/orb").text
        assert "<title>ORB Live</title>" in page
        assert '<span class="pill-chip">DKI</span>' in page
        assert '<span class="pill-chip">AIXI</span>' not in page
        assert "observer" not in page.lower() and "paper" not in page.lower()
        monkeypatch.setattr(cp, "utcnow", lambda: NOW + timedelta(seconds=61))
        health = client.get("/health").json()
        assert next(row for row in health["services"] if row["service_name"] == "orb-schwab")["effective_status"] == "stopped"
        assert client.get("/api/bot/orb-schwab").json()["listening_status"]["state"] == "STOPPED"


@pytest.mark.parametrize("age,status,level", [(0, "healthy", "GREEN"), (60, "healthy", "GREEN"),
                                             (61, "healthy", "RED"), (-1, "healthy", "RED"),
                                             (0, "degraded", "RED"), (None, "healthy", "RED")])
def test_fleet_heartbeat_freshness_uses_live_service_not_paper(monkeypatch, age, status, level):
    fhc = _load()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(fhc, "datetime", Clock)
    calls = []
    event = {"produced_at": NOW.isoformat(), "payload": {"service_name": "orb", "status": "healthy"}}
    rows = [["2", ["data", json.dumps(event)]]]
    if age is not None:
        event = {"produced_at": (NOW - timedelta(seconds=age)).isoformat(), "payload": {"service_name": "orb-schwab", "status": status}}
        rows.append(["1", ["data", json.dumps(event)]])

    def read(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows))

    monkeypatch.setattr(fhc.subprocess, "run", read)
    assert fhc.check_orb_schwab_heartbeat()[0] == level
    assert len(calls) == 1 and calls[0][1]["timeout"] == 5
    assert "200" in calls[0][0]


@pytest.mark.parametrize("stdout,returncode", [("not-json", 0), ("[]", 1)])
def test_fleet_heartbeat_source_failure_is_unknown_not_healthy(monkeypatch, stdout, returncode):
    fhc = _load()
    monkeypatch.setattr(fhc.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=stdout, returncode=returncode))
    assert fhc.check_orb_schwab_heartbeat()[0] == "AMBER"


def test_preopen_missing_heartbeat_uses_exact_live_unit_and_own_gateway_owner_only(monkeypatch):
    app, _ = _orbpage_app(monkeypatch, heartbeat=False)
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    calls = []

    def read_unit(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=(
            "ActiveState=active\nSubState=running\nNRestarts=1\n"
            "ActiveEnterTimestamp=Thu 2026-10-08 06:31:14 UTC\n"
        ))

    async def own_symbols(key, owner):
        assert key == "test:market-data-subscription-owners"
        assert owner == "orb-schwab"
        return '["AIXI", "DKI"]'

    monkeypatch.setattr(cp.subprocess, "run", read_unit)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.repository.redis, "hget", own_symbols, raising=False)
        live = client.get("/api/bot/orb-schwab").json()
        assert live["listening_status"]["state"] == "WAITING FOR 09:27"
        assert live["watched_tickers"] == ["AIXI", "DKI"]
        detail = live["listening_status"]["detail"]
        assert "06:31:14 UTC" in detail and "NRestarts=1" in detail
        assert live["listening_status"]["latest_heartbeat_at"] == ""
        assert "Heartbeat not yet reported" in detail
        assert len(calls) == 1 and calls[0][1]["timeout"] == 2
        assert calls[0][0][2] == "project-mai-tai-orb-schwab.service"


@pytest.mark.parametrize("bad", ["inactive", "unreadable", "stale", "has_heartbeat", "after_open", "restarts_missing"])
def test_unit_fallback_cannot_adopt_bad_evidence_or_mask_stale_heartbeat(monkeypatch, bad):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    unit = {"ActiveState": "active", "SubState": "running", "NRestarts": "1",
            "ActiveEnterTimestamp": "Thu 2026-10-08 06:31:14 UTC", "checked_at": NOW.isoformat()}
    report = {"runtime_fallback": unit, "observed_at": ""}
    if bad == "inactive":
        unit["ActiveState"] = "inactive"
    elif bad == "unreadable":
        unit["ActiveEnterTimestamp"] = ""
    elif bad == "stale":
        unit["checked_at"] = (NOW - timedelta(seconds=61)).isoformat()
    elif bad == "has_heartbeat":
        report["observed_at_raw"] = NOW - timedelta(seconds=61)
    elif bad == "after_open":
        monkeypatch.setattr(cp, "utcnow", lambda: OPEN)
    else:
        unit.pop("NRestarts")
    assert not cp._orb_unit_fallback_fresh(report)


def test_paused_orb_tab_dates_every_data_section_and_new_day_drops_old_rows(monkeypatch):
    app, _ = _orbpage_app(monkeypatch)
    now = [datetime(2026, 10, 6, 17, tzinfo=UTC)]
    monkeypatch.setattr(cp, "utcnow", lambda: now[0])
    monkeypatch.setattr(cp, "current_eastern_day_start_utc", lambda now=None:
                        cp.utcnow().astimezone(cp.EASTERN_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC))
    with TestClient(app) as client:
        yesterday = client.get("/bot/orb").text
        assert 'http-equiv="refresh"' not in yesterday
        for name in ["Decision Tape", "Failed Actions", "Completed Positions", "Open Positions", "Order History", "Listening Status", "Live Symbols"]:
            assert f"{name} &#183; 2026-10-06" in yesterday
        assert "6.6700" in yesterday
        now[0] = datetime(2026, 10, 7, 4, 1, tzinfo=UTC)
        today = client.get("/bot/orb").text
        assert "Decision Tape &#183; 2026-10-07" in today
        assert "6.6700" not in today
        assert client.get("/api/bot/orb-schwab").json()["recent_fills"] == []


def test_live_feed_has_no_modelled_fill_or_paper_writer_even_with_legacy_env(monkeypatch):
    for key in ["PAPER_LIFECYCLE", "PAPER_ATR_EXIT", "RESTING_ENTRY"]:
        monkeypatch.setenv(f"MAI_TAI_ORB_{key}_ENABLED", "true")
    svc, _ = service(monkeypatch)
    for name in ["paper_store", "_paper_positions", "_record_pending_paper_entries", "_check_fixed_resting_fill"]:
        assert not hasattr(svc, name)
    source = Path(orb_app.__file__).read_text()
    assert "OrbPaperStore" not in source and "orb_paper_events" not in source
    assert "tests.support" not in source
    assert "orb" not in __import__("ops.health.expected_flags_check", fromlist=["SERVICE_UNITS"]).SERVICE_UNITS


def test_empty_orb_owner_replacement_preserves_live_owner_and_migration_marker(monkeypatch):
    from tests.unit.test_coldstart1_subscriptions import RedisReplay, drain, gateway, replay_recorded

    async def exercise():
        redis = RedisReplay()
        gw = gateway(redis)
        await replay_recorded(gw, redis, nonempty=True)
        before = await redis.hgetall("test:market-data-subscription-owners")
        feed = orb_app.OrbService(
            settings=Settings(redis_stream_prefix="test", market_data_subscription_startup_enabled=True),
            redis_client=redis,
        )
        offset = len(redis.entries)
        await feed._sync_gateway_subscription([])
        await drain(gw, redis, start=offset)
        after = await redis.hgetall("test:market-data-subscription-owners")
        assert json.loads(after["orb"]) == []
        assert after["orb-schwab"] == before["orb-schwab"]
        assert after["_last_applied_id"] != before["_last_applied_id"]
        assert {key: value for key, value in after.items() if key not in {"orb", "_last_applied_id"}} == {
            key: value for key, value in before.items() if key not in {"orb", "_last_applied_id"}
        }

    asyncio.run(exercise())
