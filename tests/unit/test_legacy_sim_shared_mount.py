"""Controlled shared-page wiring, not actual PG or native UI qualification."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from project_mai_tai.services import control_plane as cp
from project_mai_tai.atr_simulation.firstbar_ui_reader import SEED_START
from project_mai_tai.settings import Settings
from tests.unit.test_atrpnl1 import fill, page_data
from tests.unit.test_atr_simulation_legacy_visibility import Reader, SOURCE
from tests.unit.test_firstbar_readonly_mount import Recorder


@contextmanager
def mounted(*, legacy=True):
    firstbar, old = Recorder(), Reader()
    data = page_data([fill("buy", 10, 4, "CONTROLLED-entry"),
        fill("sell", 10, 5, "CONTROLLED-entry-ocoexit-native")])
    before = deepcopy(data)
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        app = cp.build_app(Settings(_env_file=None, trade_coach_enabled=False,
            bot_control_enabled=False), session_factory=sessionmaker(engine),
            firstbar_read_port=firstbar, legacy_simulation_read_port=old if legacy else None)

        async def dashboard():
            return deepcopy(data)

        app.state.repository = SimpleNamespace(load_bot_dashboard_data=dashboard)
        yield app, firstbar, old, data, before
    finally:
        engine.dispose()


def request(app, path, method="GET"):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                base_url="http://controlled") as client:
            return await client.request(method, path)
    return asyncio.run(run())


def test_default_legacy_port_borrows_inert_existing_reader_without_new_pool():
    owner = cp.app.state.firstbar_read_port
    borrowed = cp.app.state.legacy_simulation_read_port
    assert borrowed._owner is owner and not hasattr(borrowed, "close")
    assert owner._engine is None
    response = request(cp.app, "/atr-simulation/api")
    assert response.status_code == 200 and owner._engine is None
    assert "position_api_binding_head" not in response.json()


def test_custom_firstbar_port_without_legacy_di_does_not_create_another_reader():
    with mounted(legacy=False) as (app, firstbar, old, _, _):
        assert app.state.legacy_simulation_read_port is None
        response = request(app, "/atr-simulation/api?source_id=" + SOURCE)
        assert response.status_code == 200
        assert response.json()["read_issues"] == ["LEGACY_SIMULATION_READ_PORT_NOT_CONFIGURED"]
        assert firstbar.calls == old.calls == []


def test_shared_atr_uses_sim2_and_existing_live_bytes_stay_unchanged(monkeypatch):
    monkeypatch.setattr(cp, "utcnow", lambda: datetime(2026, 10, 23, 20, tzinfo=UTC))
    actual = cp._render_bot_detail_page
    captures = []

    def tracked(data, strategy, **kwargs):
        captures.append((deepcopy(data), strategy, kwargs["firstbar_simulation_html"]))
        return actual(data, strategy, **kwargs)

    monkeypatch.setattr(cp, "_render_bot_detail_page", tracked)
    with mounted() as (app, firstbar, old, data, before):
        response = request(app, "/bot/1m-schwab-v2")
        assert response.status_code == 200 and firstbar.calls == old.calls == []
        assert "Simulation unavailable right now" in response.text
        assert 'name="legacy_source_id"' not in response.text
        assert 'data-firstbar-mount="true"' in response.text  # Reused ATRSIM2 mount marker.
        assert "Recorded FIRSTBAR SIM" not in response.text
        assert 'data-legacy-sim-mount="true"' not in response.text
        snapshot, strategy, panel = captures[0]
        baseline = actual(snapshot, strategy)
        assert response.text.replace(panel, "", 1) == baseline
        assert data == before


def test_legacy_source_uses_only_legacy_reader_and_seven_day_scope():
    with mounted() as (app, firstbar, old, data, before):
        response = request(app, "/atr-simulation?source_id=" + SOURCE
            + "&day=2026-10-23&days=7")
        assert response.status_code == 200 and firstbar.calls == [] and len(old.calls) == 1
        args, kwargs, _ = old.calls[0]
        assert args[0] == SOURCE and str(args[1]) == "2026-10-23" and str(args[2]) == "2026-10-29"
        assert kwargs == {"limit": 10000} and data == before


def test_firstbar_source_does_not_select_legacy_source():
    with mounted() as (app, firstbar, old, _, _):
        response = request(app, "/atr-firstbar?source_id=CONTROLLED-seed&day="
            + SEED_START.isoformat() + "&days=32")
        assert response.status_code == 200 and len(firstbar.calls) == 1 and old.calls == []


def test_out_of_seed_firstbar_window_keeps_original_guard_and_never_reads_legacy():
    with mounted() as (app, firstbar, old, _, _):
        response = request(app, "/atr-firstbar?source_id=CONTROLLED-seed&day=2026-10-12&days=32")
        assert response.status_code == 400 and firstbar.calls == old.calls == []


@pytest.mark.parametrize("query", ["days=8", "source_id=bad/path", "day=bad"])
def test_invalid_legacy_query_refuses_before_any_page_read(query):
    with mounted() as (app, firstbar, old, _, _):
        assert request(app, "/atr-simulation?" + query).status_code == 422
        assert firstbar.calls == old.calls == []


@pytest.mark.parametrize("path", ["/atr-simulation", "/atr-simulation/api"])
def test_mounted_legacy_routes_cannot_write(path):
    with mounted() as (app, firstbar, old, _, _):
        assert request(app, path, "POST").status_code == 405
        assert firstbar.calls == old.calls == []
