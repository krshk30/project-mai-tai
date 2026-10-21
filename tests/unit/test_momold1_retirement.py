from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from project_mai_tai.events import (
    MarketSnapshotPayload, SnapshotBatchEvent, SnapshotBatchPayload,
    TradeTickEvent, TradeTickPayload,
)
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import MOMENTUM_STRATEGIES, MOMENTUM_WARNING_STRATEGIES, MomentumTapeRecord
from project_mai_tai.momentum_paper.store import session_record
from project_mai_tai.runtime_registry import configured_broker_account_registrations, configured_strategy_registrations
from project_mai_tai.services.control_plane import CONTROL_PLANE_ACTIVE_BOT_CODES, _visible_bot_page_meta
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_paper_service import _condition_snapshot, _store


DAY = date(2026, 9, 16)
NOW = datetime(2026, 9, 16, 8, 12, tzinfo=UTC)


def ready():
    return session_record(
        session_date=DAY, observed_at=NOW, prior_close_date=date(2026, 9, 15),
        prior_closes={"ABCD": "1.25"}, condition_snapshot=_condition_snapshot().payload(),
    )


def detected(code):
    return MomentumTapeRecord(
        event_key=f"{code}:ABCD:retained:DETECTED", logical_id=f"{code}:ABCD:retained",
        strategy_code=code, event_type="DETECTED", session_date=DAY, symbol="ABCD",
        observed_at=NOW, payload={"symbol": "ABCD", "strategy_code": code,
                                  "session_date": DAY.isoformat(), "detect": {"sip_ts_ms": 1}},
    )


async def prepared(*, records=()):
    store = _store()
    store.append_many([ready(), *records])
    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True, market_data_momentum_warning_enabled=True),
        store=store, redis_client=AsyncMock(), clock=lambda: NOW,
        rest_client_factory=Mock(side_effect=AssertionError("no reference backfill")),
    )
    service._session_date = DAY
    await service._prepare_session(DAY)
    return service, store


@pytest.mark.parametrize("warning_enabled", [False, True])
def test_active_registry_never_advertises_legacy_or_a_paper_broker(warning_enabled):
    settings = Settings(momentum_paper_enabled=True, market_data_momentum_warning_enabled=warning_enabled)
    codes = {row.code for row in configured_strategy_registrations(settings)}
    assert not set(MOMENTUM_STRATEGIES) & codes
    assert (set(MOMENTUM_WARNING_STRATEGIES) <= codes) is warning_enabled
    assert "paper:momentum" not in {row.name for row in configured_broker_account_registrations(settings)}
    for code in MOMENTUM_WARNING_STRATEGIES:
        assert settings.provider_for_strategy(code) == "none"
        assert settings.market_data_provider_for_strategy(code) == "massive"
    assert not set(MOMENTUM_STRATEGIES) & set(CONTROL_PLANE_ACTIVE_BOT_CODES)
    assert [meta["path"] for _, meta in _visible_bot_page_meta(list(MOMENTUM_WARNING_STRATEGIES))] == ["/bot/momentum"]


def test_offline_engine_default_still_supports_legacy_history():
    engine = MomentumPaperEngine(prior_closes={"ABCD": Decimal("1.25")},
                                 condition_version="controlled", coverage_started_ms=1)
    assert engine.strategy_windows == MOMENTUM_STRATEGIES


@pytest.mark.asyncio
async def test_production_preparation_has_no_legacy_evaluator_or_history_mutation():
    old = [detected(code) for code in MOMENTUM_STRATEGIES]
    service, store = await prepared(records=old)
    assert service._engine.strategy_windows == {}
    assert service._engine.active_symbols == set()
    assert service._warning.engine.strategy_windows == MOMENTUM_WARNING_STRATEGIES
    assert service._warning.engine.prior_closes == {"ABCD": Decimal("1.25")}
    retained = [row for row in store.load_session(DAY) if row.strategy_code in MOMENTUM_STRATEGIES]
    assert retained == old
    assert not any(row.event_type == "UNANSWERABLE" for row in retained)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", MOMENTUM_WARNING_STRATEGIES)
async def test_active_warning_restart_contract_still_marks_interrupted_once(code):
    service, store = await prepared(records=[detected(code)])
    rows = store.load_session(DAY, strategy_codes=MOMENTUM_WARNING_STRATEGIES)
    interrupted = [row for row in rows if row.event_type == "UNANSWERABLE"]
    assert len(interrupted) == 1
    assert interrupted[0].strategy_code == code
    assert interrupted[0].payload["reason"] == "service_restart_interrupted_forward_path"
    assert interrupted[0].payload in service._completed_from_store
    await service._prepare_session(DAY)
    assert len([row for row in store.load_session(DAY, strategy_codes=MOMENTUM_WARNING_STRATEGIES)
                if row.event_type == "UNANSWERABLE"]) == 1


@pytest.mark.asyncio
async def test_snapshot_still_updates_live_volume_without_legacy_detection(monkeypatch):
    service, store = await prepared()
    service._live_handoff = SimpleNamespace()
    legacy = Mock(side_effect=AssertionError("retired detector called"))
    monkeypatch.setattr(service._engine, "detect_from_snapshots", legacy)
    event = SnapshotBatchEvent(source_service="market-data-gateway", payload=SnapshotBatchPayload(
        snapshots=[MarketSnapshotPayload(symbol="ABCD", last_trade_price=Decimal("9"),
                   day_volume=100_000, minute_accumulated_volume=90_000,
                   last_trade_timestamp_ns=int(NOW.timestamp() * 1_000_000_000))],
        completed_at=NOW,
    ))
    await service._handle_gateway_event(event.model_dump_json())
    legacy.assert_not_called()
    assert service._live_day_volumes["ABCD"] == (100_000, NOW)
    assert service._connected is True
    assert service._last_snapshot_at == NOW
    assert not [row for row in store.load_session(DAY) if row.event_type == "DETECTED"]


@pytest.mark.asyncio
async def test_classified_tick_reaches_warning_once_not_retired_history(monkeypatch):
    service, _ = await prepared()
    service._warning_subscribed_symbols = {"ABCD"}
    legacy = Mock(side_effect=AssertionError("retired tick evaluator called"))
    warning = Mock(return_value=[])
    monkeypatch.setattr(service._engine, "ingest", legacy)
    monkeypatch.setattr(service._warning, "observe_trade", warning)
    event = TradeTickEvent(source_service="market-data-gateway", payload=TradeTickPayload(
        symbol="ABCD", price=Decimal("1.30"), size=100,
        timestamp_ns=int(NOW.timestamp() * 1_000_000_000),
        conditions=["1"], conditions_present=True,
    ))
    await service._handle_gateway_event(event.model_dump_json())
    legacy.assert_not_called()
    warning.assert_called_once()
    trade = warning.call_args.args[0]
    assert trade.eligible is True and trade.price == Decimal("1.30")
    assert service._condition_feed_verified is True


@pytest.mark.asyncio
@pytest.mark.parametrize("connected,conditions", [(False, False), (True, False), (True, True)])
async def test_warning_state_keeps_heartbeat_health_contract_and_no_legacy_bots(connected, conditions):
    service, _ = await prepared()
    service._connected = connected
    service._condition_feed_verified = conditions
    await service._publish_state()
    rows = [json.loads(call.args[1]["data"]) for call in service.redis.xadd.await_args_list]
    heartbeat = next(row["payload"] for row in rows if row["event_type"] == "service_heartbeat")
    assert heartbeat["status"] == ("healthy" if connected and conditions else "degraded")
    assert heartbeat["details"]["subscription_mode"] == "warning_candidates"
    assert heartbeat["details"]["legacy_snapshot_enabled"] == "false"
    assert heartbeat["details"]["subscription_cap"] == "16"
    codes = {row["payload"]["strategy_code"] for row in rows if row["event_type"] == "isolated_bot_state"}
    assert codes == set(MOMENTUM_WARNING_STRATEGIES)
