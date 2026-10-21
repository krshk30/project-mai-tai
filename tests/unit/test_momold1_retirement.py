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
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import WarningEvaluation
from project_mai_tai.runtime_registry import configured_broker_account_registrations, configured_strategy_registrations
from project_mai_tai.services.control_plane import (
    CONTROL_PLANE_ACTIVE_BOT_CODES, _build_bot_listening_status, _compact_bot_card, _render_compact_dashboard,
    MOMENTUM_LIVE_CODE, _visible_bot_page_meta,
)
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_paper_service import _condition_snapshot, _store
from tests.unit.test_momentum_warning import START, bar


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
    assert not set(MOMENTUM_WARNING_STRATEGIES) & set(CONTROL_PLANE_ACTIVE_BOT_CODES)
    assert MOMENTUM_LIVE_CODE in CONTROL_PLANE_ACTIVE_BOT_CODES
    assert [meta["path"] for _, meta in _visible_bot_page_meta(list(MOMENTUM_WARNING_STRATEGIES))] == ["/bot/momentum"]


def test_offline_engine_default_still_supports_legacy_history():
    engine = MomentumPaperEngine(prior_closes={"ABCD": Decimal("1.25")},
                                 condition_version="controlled", coverage_started_ms=1)
    assert engine.strategy_windows == MOMENTUM_STRATEGIES


@pytest.mark.parametrize("code", MOMENTUM_WARNING_STRATEGIES)
def test_warning_compact_card_is_paper_reference_and_exposes_no_pnl(code):
    html = _compact_bot_card(
        {"strategy_code": code, "account_name": "paper:momentum", "daily_pnl": 987654.32},
        service_by_name={}, latest_strategy_bars={},
    )
    assert 'href="/bot/momentum"' in html
    assert ">PAPER</span>" in html and ">LIVE</span>" not in html
    assert "paper reference" in html and "Reference symbols" in html
    assert "987654" not in html and "P&L" not in html
    assert "Webull + Schwab" not in html


def test_dashboard_excludes_all_reference_cards_positions_and_pnl():
    html = _render_compact_dashboard({"bots": [
        {"strategy_code": "schwab_1m_v2", "position_count": 2},
        {"strategy_code": "momentum_30s_warn", "account_name": "paper:momentum",
         "position_count": 7, "daily_pnl": 987654.32},
        {"strategy_code": "momentum_60s_warn", "account_name": "paper:momentum",
         "position_count": 9, "daily_pnl": 876543.21},
        {"strategy_code": "momentum_30s", "position_count": 99},
    ]})
    assert 'href="/bot/momentum"' not in html
    assert '<div class="label">Open positions</div><div class="val">2</div>' in html
    assert "987654" not in html and "876543" not in html
    assert "paper reference" not in html


@pytest.mark.parametrize("kind", ["healthy", "missing", "stale", "future", "conditions_unknown"])
def test_warning_api_health_uses_paper_heartbeat_not_strategy_engine(monkeypatch, kind):
    from project_mai_tai.services import control_plane as cp

    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    stamp = NOW if kind not in {"stale", "future"} else datetime.fromtimestamp(
        NOW.timestamp() + (-91 if kind == "stale" else 1), UTC)
    service = {"service_name": "momentum-paper", "status": "healthy",
        "observed_at": cp._datetime_str(stamp),
        "details": {"streamer_connected": "true",
                    "trade_conditions_verified": "false" if kind == "conditions_unknown" else "true"}}
    data = {"services": [{"service_name": "strategy-engine", "status": "stopped"},
                         *([] if kind == "missing" else [service])]}
    status = _build_bot_listening_status(data, {"strategy_code": "momentum_30s_warn"}, [])
    assert status["state"] == ("PAPER REFERENCE" if kind == "healthy" else
                               "DEGRADED" if kind == "conditions_unknown" else "UNKNOWN")
    assert status["tracked_bar_count"] is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize("live_mirroring", [False, True])
async def test_dormant_engine_cap_expiry_rewarning_and_subscription_replacement(live_mirroring):
    from dataclasses import replace

    service, _ = await prepared()
    service.settings.momentum_live_enabled = live_mirroring
    symbols = [f"CAP{chr(65 + index)}" for index in range(17)]
    warning = MomentumWarningConsumer(
        prior_closes={symbol: Decimal("10") for symbol in symbols},
        condition_version="SYNTH", coverage_started_ms=START,
    )
    service._warning = warning
    service._clock = lambda: datetime.fromtimestamp((START + 1000) / 1000, UTC)
    for index, symbol in enumerate(symbols):
        item = replace(bar(symbol=symbol, high="11"), close=Decimal("10"), sequence=index)
        outcome, records = warning.observe_aggregate(
            item, observed_ns=item.published_utc_ns, legacy_active=service._engine.active_symbols,
        )
        assert not records  # Warning at +10%, below the +20% detection boundary.
        assert outcome.status == ("ADMITTED" if index < 16 else "REFUSED")
        if index == 16:
            assert outcome.reason == "warning_cap_full"
    await service._sync_gateway_subscriptions()
    assert service._warning_subscribed_symbols == set(symbols[:16])
    assert service._engine.active_symbols == set()
    # One fresh eligible callback protects its lease; silence releases the other 15.
    warning.candidates.observe_eligible_trade(symbols[0], observed_ms=START + 60_000)
    dropped = warning.expire_quiet(observed_ms=START + 61_000, legacy_active=set())
    assert {row.symbol for row in dropped if row.status == "DROPPED"} == set(symbols[1:16])
    await service._sync_gateway_subscriptions()
    assert service._warning_subscribed_symbols == {symbols[0]}
    item = replace(bar(61, symbol=symbols[16], high="11"), close=Decimal("10"), sequence=17)
    outcome, _ = warning.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert outcome.status == "ADMITTED"
    await service._sync_gateway_subscriptions()
    assert service._warning_subscribed_symbols == {symbols[0], symbols[16]}
    requests = [json.loads(call.args[1]["data"])["payload"]
                for call in service.redis.xadd.await_args_list
                if call.args[0].endswith(":market-data-subscriptions")]
    warning_requests = [row for row in requests if row["consumer_name"] == "momentum-warning"]
    assert [set(row["symbols"]) for row in warning_requests] == [
        set(symbols[:16]), {symbols[0]}, {symbols[0], symbols[16]},
    ]
    assert all(row["mode"] == "replace" and len(row["symbols"]) <= 16 for row in requests)
    if live_mirroring:
        owners = {}
        for row in requests:
            owners[row["consumer_name"]] = set(row["symbols"])
            assert len(set().union(*owners.values())) <= 16
        assert service._subscribed_symbols == service._warning_subscribed_symbols


@pytest.mark.parametrize("state", ["WAITING_FILL", "OPEN"])
def test_dormant_legacy_engine_does_not_release_actual_active_warning_path(state):
    from tests.unit.test_momwarn1_no_data_expiry import admitted, ledger

    warning = admitted()
    warning.engine = ledger("momentum_30s_warn", state=state)
    dormant = MomentumPaperEngine(prior_closes={"TEST": Decimal("10")},
        condition_version="SYNTH", coverage_started_ms=START, strategy_windows={})
    outcomes = warning.expire_quiet(observed_ms=START + 61_000, legacy_active=dormant.active_symbols)
    assert warning.engine.active_events[0]["status"] == state
    assert warning.desired_symbols(dormant.active_symbols) == {"TEST"}
    assert not any(row.status == "DROPPED" for row in outcomes)


def test_synth_window_eviction_keeps_warning_and_active_path_evidence():
    from dataclasses import replace

    symbols = [f"MEM{chr(65 + index)}" for index in range(18)]
    warning = MomentumWarningConsumer(
        prior_closes={symbol: Decimal("10") for symbol in symbols},
        condition_version="SYNTH", coverage_started_ms=START, max_window_symbols=16,
    )
    for index, symbol in enumerate(symbols[:16]):
        item = replace(bar(symbol=symbol, high="11" if index == 0 else "10"),
                       close=Decimal("10"), sequence=index)
        warning.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    protected = warning.windows[symbols[0]]
    item = replace(bar(1, symbol=symbols[16]), sequence=16)
    warning.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert len(warning.windows) == 16
    assert warning.windows[symbols[0]] is protected
    assert symbols[1] not in warning.windows
    assert warning.desired_symbols(set()) == {symbols[0]}
    # Cap full with active paths: no eviction may discard their window evidence.
    active = set(warning.windows)
    item = replace(bar(2, symbol=symbols[17]), sequence=17)
    outcome, records = warning.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=active)
    assert (outcome.status, outcome.reason, records) == ("REFUSED", "window_capacity", ())
    assert set(warning.windows) == active
    assert warning.windows[symbols[0]] is protected


def test_synth_removal_of_legacy_ownership_can_change_warning_admission():
    # This is a counterexample to blanket full-universe parity, not a waiver.
    from project_mai_tai.momentum_paper.warning_window import WarningCandidates

    before, after = WarningCandidates(), WarningCandidates()
    active = {f"OLD{chr(65 + index)}" for index in range(16)}
    evaluation = WarningEvaluation("WARN", START, (), (30,))
    assert before.observe(evaluation, active_path_symbols=active).reason == "warning_cap_full"
    assert after.observe(evaluation, active_path_symbols=set()).status == "ADMITTED"
