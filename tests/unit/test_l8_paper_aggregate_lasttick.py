"""L8 synthetic shape regressions; not deployed-byte or production-health proof."""

import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
import json

from fastapi.testclient import TestClient
import pytest

from project_mai_tai.db.models import MomentumPaperEvent
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.services import control_plane as cp
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_mompage4_live_first import app_fixture, owner


DAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 13, 31, 42, 123000, tzinfo=UTC)
SIP_MS = int(NOW.timestamp() * 1000)
CODES = ("momentum_30s_warn", "momentum_60s_warn")


def completed_aggregate(code, *, symbol="RUMBW"):
    # Faithful missing-SIP aggregate shape, not a copy of unavailable box rows.
    return {"logical_id": f"SYNTHETIC-L8:{code}:{symbol}", "strategy_code": code,
        "session_date": DAY.isoformat(), "symbol": symbol, "status": "UNANSWERABLE",
        "reason": "feed_gap", "detect": {"symbol": symbol, "price": "1.25",
            "start_ms": SIP_MS - 1000, "end_ms": SIP_MS,
            "observed_decision_ns": SIP_MS * 1_000_000, "lineage": "observed_aggregate",
            "exact_signal_sip_timestamp": None, "signal_field": "close",
            "price_provenance": "qualifying_bucket_last_close"},
        "fill": None, "exit": None, "pnl": None, "pnl_pct": None}


class RecordingRedis:
    def __init__(self):
        self.rows = []

    async def xadd(self, stream, fields, **kwargs):
        self.rows.append((stream, fields))


def service():
    redis = RecordingRedis()
    settings = Settings(_env_file=None, momentum_paper_enabled=True,
        market_data_momentum_warning_enabled=True, momentum_live_enabled=False,
        redis_stream_prefix="test")
    result = MomentumPaperService(settings, redis_client=redis, clock=lambda: NOW)
    result._session_date = DAY
    result._engine = MomentumPaperEngine(prior_closes={"RUMBW": Decimal(1)},
        condition_version="synthetic-l8", coverage_started_ms=SIP_MS - 60_000,
        strategy_windows={})
    result._warning = MomentumWarningConsumer(prior_closes={"RUMBW": Decimal(1)},
        condition_version="synthetic-l8", coverage_started_ms=SIP_MS - 60_000)
    result._connected = result._condition_feed_verified = True
    return result


@pytest.mark.parametrize("code", CODES)
def test_completed_unanswerable_aggregate_without_sip_builds_real_state(code):
    svc = service()
    row = completed_aggregate(code)
    before = deepcopy(row)
    state = svc._build_bot_state(code, [row])
    assert "sip_ts_ms" not in row["detect"]
    assert state.last_tick_at == {}
    assert state.positions == state.closed_today == state.pending_open_symbols == []
    assert state.daily_pnl == 0 and state.watchlist == []
    assert state.recent_decisions[0]["symbol"] == "RUMBW"
    assert state.recent_decisions[0]["status"] == "UNANSWERABLE"
    assert state.recent_decisions[0]["reason"] == "feed_gap"
    assert state.data_health["momentum_paper"]["unanswerable"] == 1
    assert state.data_health["momentum_paper"]["events"] == 1
    assert row == before  # Never synthesize a SIP clock into the source evidence.


@pytest.mark.parametrize("code", (*CODES, "momentum_30s", "momentum_60s"))
@pytest.mark.parametrize("encoded", [False, True], ids=["integer", "integer-string"])
def test_valid_sip_timestamp_is_unchanged(code, encoded):
    row = completed_aggregate(code, symbol="XLAP")
    row["detect"] = {"symbol": "XLAP", "price": "1.25",
                     "sip_ts_ms": str(SIP_MS) if encoded else SIP_MS}
    before = deepcopy(row)
    state = service()._build_bot_state(code, [row])
    assert state.last_tick_at == {"XLAP": NOW.isoformat()}
    assert state.recent_decisions[0]["last_tick_at"] == NOW.isoformat()
    assert row == before


@pytest.mark.parametrize("code", CODES)
def test_missing_sip_cannot_overwrite_valid_same_symbol_timestamp(code):
    missing = completed_aggregate(code)
    valid = deepcopy(missing)
    valid["logical_id"] += ":raw"
    valid["detect"] = {"symbol": "RUMBW", "price": "1.25", "sip_ts_ms": SIP_MS}
    state = service()._build_bot_state(code, [missing, valid])
    assert state.last_tick_at == {"RUMBW": NOW.isoformat()}
    assert len(state.recent_decisions) == 2
    assert state.data_health["momentum_paper"]["unanswerable"] == 2


@pytest.mark.asyncio
async def test_actual_state_publication_keeps_both_warning_ledgers_and_heartbeat():
    svc = service()
    svc._completed_from_store = [completed_aggregate(code) for code in CODES]
    before = deepcopy(svc._completed_from_store)
    await svc._publish_state()
    assert len(svc.redis.rows) == 3
    assert svc.redis.rows[0][0] == "test:heartbeats"
    states = [json.loads(fields["data"])["payload"] for stream, fields in svc.redis.rows
              if stream == "test:strategy-state-isolated"]
    assert {state["strategy_code"] for state in states} == set(CODES)
    assert all(state["last_tick_at"] == {} and state["recent_decisions"][0]["status"] == "UNANSWERABLE"
               and state["data_health"]["momentum_paper"]["unanswerable"] == 1 for state in states)
    assert svc._completed_from_store == before


@pytest.mark.parametrize("route", ["/bot/momentum", "/api/bots", "/api/overview"])
def test_real_page_and_api_render_after_missing_sip_state_publication(monkeypatch, route):
    svc = service()
    svc._completed_from_store = [completed_aggregate(code) for code in CODES]
    asyncio.run(svc._publish_state())  # The pre-fix source raises here, before HTTP.
    isolated = [(str(index), fields) for index, (stream, fields) in enumerate(svc.redis.rows)
                if stream == "test:strategy-state-isolated"]
    app, factory, redis = app_fixture(monkeypatch, with_fill=False,
        proof=owner(observed_ns=SIP_MS * 1_000_000))
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    monkeypatch.setattr(cp.time, "time_ns", lambda: SIP_MS * 1_000_000)
    original_read = redis.xrevrange
    isolated_reads = []

    async def actual_published_state(stream, **kwargs):
        if stream == "test:strategy-state-isolated":
            isolated_reads.append(stream)
            return list(reversed(isolated))
        return await original_read(stream, **kwargs)

    monkeypatch.setattr(redis, "xrevrange", actual_published_state)
    with factory.begin() as session:
        for row in svc._completed_from_store:
            session.add(MomentumPaperEvent(event_key=row["logical_id"] + ":UNANSWERABLE",
                logical_id=row["logical_id"], strategy_code=row["strategy_code"],
                event_type="UNANSWERABLE", session_date=DAY, symbol=row["symbol"],
                observed_at=NOW, payload=deepcopy(row)))
    with TestClient(app) as client:
        app.state.repository.redis = redis
        response = client.get(route)
    assert response.status_code == 200
    assert isolated_reads  # Actual repository ingests the emitted event contracts.
    if route == "/bot/momentum":
        assert "LIVE (real orders)" in response.text
        assert "Warning method 30 s completed trades" in response.text
        assert "Warning method 60 s completed trades" in response.text
        assert "Traceback" not in response.text
    else:
        live = [bot for bot in response.json()["bots"] if bot["strategy_code"] == "momentum_warn_live"]
        assert len(live) == 1 and live[0]["positions"] == []
        assert Decimal(str(live[0]["daily_pnl"])) == 0
        assert not any(bot["strategy_code"] in CODES for bot in response.json()["bots"])
