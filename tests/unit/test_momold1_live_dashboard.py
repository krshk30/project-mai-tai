"""MOMOLD1 actual-LIVE dashboard contract, using controlled local evidence.

These are authored unit/HTTP fixtures, not installed-box or native order proof.
No warning/reference quantity or P&L may become a mainboard LIVE result.
"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import html
import re
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import pytest

from project_mai_tai.runtime_registry import configured_broker_account_registrations
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings


NOW = datetime(2026, 10, 21, 14, tzinfo=UTC)
NS = int(NOW.timestamp() * 1e9)


def evidence():
    data = {"bots": [], "services": [
        {"service_name": "momentum-paper", "status": "healthy", "effective_status": "healthy",
         "observed_at_raw": NOW, "details": {"streamer_connected": "true", "trade_conditions_verified": "true"}}],
        "market_data": {"latest_snapshot_batch": {"completed_at": NOW.isoformat()}},
        "recent_orders": [], "recent_fills": [], "recent_intents": [],
        "account_positions": [], "virtual_positions": []}
    owner = {"status": "READABLE", "version": 1, "source_service": "oms",
        "strategy_code": "momentum_warn_live", "observed_ns": NS,
        "producer_instance_id": "123:456", "restore_ready": True, "entry_admission_enabled": True,
        "account_names": ["live:orb", "live:schwab_1m_v2"], "episodes": [], "quote_symbols": []}
    metrics = {"status": "READABLE", "observed_ns": NS, "coverage_status": "provider_event_observed",
        "gateway_instance_id": "SYNTH-gateway", "generation": 1,
        "A_subscription_requested_ns": NS - 1_000_000_000, "owned_warning_symbols": [],
        "consumer": {"status": "READABLE", "observed_ns": NS, "transport_status": "observed_transport",
                     "owned_warning_symbols": 0}}
    return data, owner, metrics


def ledger(**changes):
    return {"strategy_code": "momentum_warn_live", "source": "strategy_owned_broker_fills",
        "status": "READABLE", "positions": [], "closed_today": [], "recent_decisions": [],
        "daily_pnl": "0", "daily_pnl_partial": False, "unknown_episodes": [], **changes}


def position(**changes):
    return {"episode_id": "00000000-0000-0000-0000-000000000001", "symbol": "AMOD", "ticker": "AMOD",
        "account_name": "live:orb", "quantity": "10", "remaining_quantity": "7", "entry_price": "2.125",
        "entry_time": "2026-10-21T09:50:00-04:00", **changes}


def closed(**changes):
    return {**position(), "remaining_quantity": "0", "exit_price": "2.625", "pnl": "5.00",
        "pnl_pct": "23.5294", "exit_time": "2026-10-21T09:51:00-04:00", "exit_reason": "broker_fill", **changes}


def text(value):
    return html.unescape(re.sub(r"<[^>]+>", " ", value))


@pytest.fixture(autouse=True)
def controlled_clock(monkeypatch):
    monkeypatch.setattr(cp, "utcnow", lambda: NOW)
    monkeypatch.setattr(cp.time, "time_ns", lambda: NS)


def test_readable_empty_owned_live_ledger_is_known_zero_without_fake_account():
    data, owner, metrics = evidence()
    view = cp._momentum_live_dashboard_view(data, ledger(), owner, metrics)
    assert view["strategy_code"] == cp.MOMENTUM_LIVE_CODE
    assert view["execution_mode"] == "live" and view["runtime_kind"] == "momentum_live"
    assert view["positions"] == [] and type(view["position_count"]) is int and view["position_count"] == 0
    assert Decimal(str(view["daily_pnl"])) == 0 and view["daily_pnl_partial"] is False
    assert view["account_name"] == "" and view["broker_account_names"] == []
    assert "paper:momentum" not in {row.name for row in configured_broker_account_registrations(
        Settings(_env_file=None, momentum_paper_enabled=True, market_data_momentum_warning_enabled=True))}
    card = cp._compact_momentum_live_card(view)
    assert ">LIVE</span>" in card and 'href="/bot/momentum"' in card
    assert "+0.00" in text(card) and "No open LIVE positions" in text(card)
    assert "paper:momentum" not in card


def test_owned_ledger_retains_remaining_quantity_actual_pnl_and_account_identity():
    data, owner, metrics = evidence()
    data["account_positions"] = [{"broker_account_name": "live:orb", "symbol": "AMOD", "quantity": "9999"}]
    data["recent_orders"] = [
        {"strategy_code": "momentum_warn_live", "symbol": "AMOD"},
        {"strategy_code": "momentum_30s_warn", "symbol": "REFERENCEONLY"}]
    source = ledger(positions=[position()], closed_today=[closed(account_name="live:schwab_1m_v2")], daily_pnl="5.00")
    before = deepcopy(source)
    view = cp._momentum_live_dashboard_view(data, source, owner, metrics)
    assert view["position_count"] == 1 and Decimal(str(view["positions"][0]["quantity"])) == 7
    assert Decimal(str(view["daily_pnl"])) == Decimal("5.00")
    assert view["broker_account_names"] == ["live:orb", "live:schwab_1m_v2"]
    assert view["recent_orders"] == [data["recent_orders"][0]]
    assert source == before
    card = text(cp._compact_momentum_live_card(view))
    assert "AMOD 7 @ 2.125 (live:orb)" in card and "+5.00" in card
    assert "9999" not in card and "REFERENCEONLY" not in card


@pytest.mark.parametrize("change", [
    {"status": "PARTIAL", "daily_pnl_partial": True}, {"status": "UNKNOWN"},
    {"positions": None}, {"positions": ["malformed"]}, {"closed_today": [None]},
])
def test_partial_unreadable_or_malformed_collections_never_look_known_flat(change):
    data, owner, metrics = evidence()
    view = cp._momentum_live_dashboard_view(data, ledger(daily_pnl="5", **change), owner, metrics)
    assert view["position_count"] == "UNKNOWN" and view["daily_pnl"] is None
    assert view["daily_pnl_partial"] is True
    card = text(cp._compact_momentum_live_card(view))
    assert "UNKNOWN" in card and "+0.00" not in card and "No open LIVE positions" not in card


def test_partial_owned_rows_keep_account_evidence_without_claiming_complete_exposure():
    data, owner, metrics = evidence()
    view = cp._momentum_live_dashboard_view(data,
        ledger(status="PARTIAL", positions=[position()], daily_pnl="5.00", daily_pnl_partial=True), owner, metrics)
    assert view["position_count"] == "UNKNOWN" and view["daily_pnl"] is None
    assert view["broker_account_names"] == ["live:orb"]
    assert view["positions"][0]["account_name"] == "live:orb"
    card = text(cp._compact_momentum_live_card(view))
    assert "UNKNOWN" in card and "AMOD 7" not in card and "+5.00" not in card


@pytest.mark.parametrize("change", [
    {"strategy_code": "momentum_30s_warn"}, {"strategy_code": "momentum_30s"},
    {"source": "paper_warning_records"}, {"source": None},
])
def test_wrong_ledger_identity_or_provenance_cannot_be_relabelled_live(change):
    data, owner, metrics = evidence()
    source = ledger(positions=[position()], daily_pnl="987654.32", **change)
    view = cp._momentum_live_dashboard_view(data, source, owner, metrics)
    assert view["position_count"] == "UNKNOWN" and view["daily_pnl"] is None
    assert view["positions"] == []
    card = text(cp._compact_momentum_live_card(view))
    assert "987654" not in card and "AMOD 7" not in card and "UNKNOWN" in card


@pytest.mark.parametrize("bad_row", [{}, position(remaining_quantity="UNKNOWN"),
    position(remaining_quantity="NaN"), position(account_name="paper:momentum")])
def test_malformed_position_fields_are_unknown_not_known_live_exposure(bad_row):
    data, owner, metrics = evidence()
    view = cp._momentum_live_dashboard_view(data, ledger(positions=[bad_row]), owner, metrics)
    assert view["position_count"] == "UNKNOWN" and view["daily_pnl"] is None


@pytest.mark.parametrize("bad_pnl", ["NaN", "Infinity", "not-a-number", None])
def test_malformed_pnl_never_leaves_a_complete_known_flat_view(bad_pnl):
    data, owner, metrics = evidence()
    view = cp._momentum_live_dashboard_view(data, ledger(daily_pnl=bad_pnl), owner, metrics)
    assert view["daily_pnl"] is None and view["position_count"] == "UNKNOWN"


def test_api_listening_status_rechecks_owner_clock_instead_of_cached_state(monkeypatch):
    data, owner, metrics = evidence()
    view = cp._momentum_live_dashboard_view(data, ledger(), owner, metrics)
    assert cp._build_bot_listening_status(data, view, [])["state"] == "LISTENING"
    before = deepcopy(view)
    monkeypatch.setattr(cp, "utcnow", lambda: NOW + timedelta(seconds=4))
    assert cp._build_bot_listening_status(data, view, [])["state"] == "UNKNOWN"
    assert view == before
    assert "UNKNOWN" in text(cp._compact_momentum_live_card(view))


def test_api_listening_status_rechecks_feed_evidence_independently_of_fresh_owner(monkeypatch):
    data, owner, metrics = evidence()
    owner["observed_ns"] = NS + 21_000_000_000
    monkeypatch.setattr(cp, "utcnow", lambda: NOW + timedelta(seconds=21))
    view = cp._momentum_live_dashboard_view(data, ledger(), owner, metrics)
    assert cp._build_bot_listening_status(data, view, [])["state"] == "UNKNOWN"


def test_mainboard_contains_only_actual_live_momentum_and_excludes_reference_totals():
    data, owner, metrics = evidence()
    view = cp._momentum_live_dashboard_view(data,
        ledger(positions=[position()], closed_today=[closed()], daily_pnl="5.00"), owner, metrics)
    data["bots"] = [view, *[{"strategy_code": code, "display_name": "REFERENCEONLY",
        "account_name": "paper:momentum", "daily_pnl": 987654.32, "position_count": 99}
        for code in cp.MOMENTUM_PAGE_CODES]]
    rendered = cp._render_compact_dashboard(data)
    assert rendered.count('data-strategy="momentum_warn_live"') == 1
    assert rendered.count('href="/bot/momentum"') == 1
    assert '<div class="label">Open positions</div><div class="val">1</div>' in rendered
    assert "REFERENCEONLY" not in rendered and "987654" not in rendered
    assert "paper reference" not in rendered and ">PAPER</span>" not in rendered
    assert "+5.00" in text(rendered)


def test_unknown_live_positions_propagate_to_dashboard_header_instead_of_zero():
    data, owner, metrics = evidence()
    data["bots"] = [cp._momentum_live_dashboard_view(data, ledger(status="UNKNOWN"), owner, metrics)]
    rendered = cp._render_compact_dashboard(data)
    assert '<div class="label">Open positions</div><div class="val">UNKNOWN</div>' in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "ledger", "owner", "metrics"])
async def test_repository_attachment_reads_owned_ledger_bounded_and_removes_all_retired_rows(monkeypatch, failure):
    from project_mai_tai.momentum_live import ledger as ledger_module

    data, owner, metrics = evidence()
    data["bots"] = [{"strategy_code": "schwab_1m_v2"},
        {"strategy_code": cp.MOMENTUM_LIVE_CODE, "daily_pnl": "stale"},
        *[{"strategy_code": code} for code in cp.MOMENTUM_PAGE_CODES]]
    retained = deepcopy(data)
    load = Mock(side_effect=RuntimeError("owned ledger unreadable") if failure == "ledger" else None,
                return_value=ledger(positions=[position()], daily_pnl="5.00"))
    monkeypatch.setattr(ledger_module, "load_live_ledger", load)
    read_owner, read_metrics = AsyncMock(return_value=owner), AsyncMock(return_value=metrics)
    if failure == "owner":
        read_owner.side_effect = RuntimeError("owner proof unreadable")
    elif failure == "metrics":
        read_metrics.side_effect = RuntimeError("feed metrics unreadable")
    read_consumer = AsyncMock(return_value=metrics["consumer"])
    monkeypatch.setattr(cp, "_read_momentum_page_owner", read_owner)
    monkeypatch.setattr(cp, "_read_momentum_warning_gateway_metrics", read_metrics)
    monkeypatch.setattr(cp, "_read_momentum_page_consumer", read_consumer)
    waits, original_wait = [], asyncio.wait_for

    async def bounded(awaitable, *, timeout):
        waits.append(timeout)
        return await original_wait(awaitable, timeout=timeout)

    monkeypatch.setattr(cp.asyncio, "wait_for", bounded)
    repository = cp.ControlPlaneRepository.__new__(cp.ControlPlaneRepository)
    repository.settings = Settings(_env_file=None, momentum_paper_enabled=True,
                                   market_data_momentum_warning_enabled=True, redis_stream_prefix="test")
    repository.session_factory, repository.redis = object(), object()
    result = await repository._attach_momentum_live_dashboard(data)
    assert data == retained
    assert [row["strategy_code"] for row in result["bots"]] == ["schwab_1m_v2", cp.MOMENTUM_LIVE_CODE]
    load.assert_called_once_with(repository.session_factory, NOW.astimezone(cp.EASTERN_TZ).date())
    assert 3 in waits
    read_owner.assert_awaited_once_with(repository.redis, "test", now_ns=NS)
    read_metrics.assert_awaited_once_with(repository.redis, "test", now_ns=NS)
    read_consumer.assert_awaited_once()
    live = result["bots"][-1]
    assert live["position_count"] == ("UNKNOWN" if failure == "ledger" else 1)
    if failure == "ledger":
        assert live["daily_pnl"] is None
    else:
        assert Decimal(str(live["daily_pnl"])) == 5
    if failure in {"owner", "metrics"}:
        assert cp._build_bot_listening_status(data, live, [])["state"] == "UNKNOWN"


def test_canonical_page_keeps_actual_live_pnl_and_two_separate_paper_warning_sections(monkeypatch):
    from tests.unit.test_mompage4_live_first import app_fixture

    app, _, redis = app_fixture(monkeypatch)

    def warning_loader(factory, code, day):
        return {"strategy_code": code, "status": "READABLE", "positions": [],
            "closed_today": [closed(ticker="REFERENCEONLY", symbol="REFERENCEONLY", pnl="9999",
                entry_time="2026-10-15T08:00:00-04:00", exit_time="2026-10-15T08:01:00-04:00")],
            "daily_pnl": "9999", "recent_decisions": []}

    monkeypatch.setattr(cp, "_load_warning_page_ledger", warning_loader)
    with TestClient(app) as client:
        app.state.repository.redis = redis
        response = client.get("/bot/momentum")
    assert response.status_code == 200
    rendered = response.text
    assert 'Day total: $+4.96' in rendered and rendered.count('Day total:') == 1
    assert 'Warning method 60 s completed trades' in rendered and 'Warning method 30 s completed trades' in rendered
    assert rendered.count('>REFERENCE</span>') == 2
    assert "REFERENCEONLY" in rendered and 'Day total: $+9999' not in rendered
    assert '<strong>Mode:</strong> LIVE' in rendered and 'LIVE (real orders)' in rendered
