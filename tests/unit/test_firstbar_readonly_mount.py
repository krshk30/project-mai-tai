"""Controlled routing/scope evidence, not actual PG/month-load qualification."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Event, get_ident
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from project_mai_tai.atr_simulation.firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION
from project_mai_tai.atr_simulation.firstbar_ui_reader import (
    CSV_SHA256, SELECTOR_SHA256, SPLIT_SHA256, SEED_BINDING, SEED_END, SEED_START,
    FirstBarSeedUIReader, reference_seed_binding, seed_run_is_bound, validate_firstbar_scope,
)
from project_mai_tai.atr_simulation.ui_reader import unavailable_storage, validate_read_scope
from project_mai_tai.services import control_plane as cp
from project_mai_tai.services.atr_firstbar_routes import (
    build_firstbar_router, load_firstbar_display, render_firstbar_page,
)
from project_mai_tai.settings import Settings
from tests.unit.test_atrpnl1 import fill, page_data


class Recorder:
    def __init__(self):
        self.calls = []

    def read(self, source, start, end, *, limit):
        self.calls.append((source, start, end, limit, get_ident()))
        return unavailable_storage("CONTROLLED_NO_PUBLISHED_SOURCE")


def router_app(reader):
    app = FastAPI()
    app.include_router(build_firstbar_router(reader, render_row=cp._render_completed_position_row))
    return app


def test_module_default_app_can_be_built_before_live_formatter_definition():
    assert isinstance(cp.app.state.firstbar_read_port, FirstBarSeedUIReader)
    assert cp.app.state.firstbar_read_port._engine is None
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(cp.app), base_url="http://controlled") as client:
            return await client.get("/atr-firstbar")
    response = asyncio.run(request())
    assert response.status_code == 200
    assert "First-bar-high + 2-min" in response.text
    assert cp.app.state.firstbar_read_port._engine is None


def test_unselected_source_renders_two_unknown_tables_without_a_read():
    reader = Recorder()
    with TestClient(router_app(reader)) as client:
        response = client.get("/atr-firstbar")
    assert response.status_code == 200
    assert "First-bar-high + 2-min" in response.text
    assert "First-bar-high" in response.text and "UNKNOWN" in response.text
    assert cp._COMPLETED_POSITION_COLUMNS in response.text
    assert reader.calls == []


def test_actual_router_month_request_passes_exact_bounds_off_event_loop():
    async def scenario():
        reader = Recorder()
        loop_thread = get_ident()
        context = await load_firstbar_display(reader, source_id="CONTROLLED-seed", day=SEED_START, days=32)
        assert reader.calls[0][:4] == ("CONTROLLED-seed", SEED_START, SEED_END, 10000)
        assert reader.calls[0][4] != loop_thread
        assert context["view"]["coverage_status"] == "UNKNOWN"
    asyncio.run(scenario())


@pytest.mark.parametrize("query", ["days=33", "days=0", "day=not-a-date", "source_id=bad/path"])
def test_malformed_route_scope_never_reaches_reader(query):
    reader = Recorder()
    with TestClient(router_app(reader)) as client:
        assert client.get("/atr-firstbar/api?" + query).status_code == 422
    assert reader.calls == []


@pytest.mark.parametrize("start,end,limit", [
    (SEED_START - timedelta(days=1), SEED_END, 10000),
    (SEED_START, SEED_END + timedelta(days=1), 10000),
    (SEED_END, SEED_START, 10000), (SEED_START, SEED_END, 10001),
    (SEED_START, SEED_END, True),
])
def test_month_scope_cannot_expand_seed_or_existing_row_budget(start, end, limit):
    with pytest.raises(ValueError):
        validate_firstbar_scope("CONTROLLED-seed", start, end, limit)


def test_old_volbody_seven_day_scope_is_unchanged():
    validate_firstbar_scope("CONTROLLED-seed", SEED_START, SEED_END, 10000)
    with pytest.raises(ValueError, match="bounded_simulation_source_day_scope_required"):
        validate_read_scope("CONTROLLED-volbody", SEED_START, SEED_END, 10000)


def bound_run():
    return {"policy_version": FIRSTBAR_RESULT_VERSION,
        "coverage": {"engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
            "reference_seed_binding": deepcopy(SEED_BINDING)},
        "source_refs": [{"sha256": value} for value in (CSV_SHA256, SPLIT_SHA256, SELECTOR_SHA256)]}


@pytest.mark.parametrize("fault", ["policy", "engine", "seed", "source", "malformed"])
def test_run_requires_full_seed_source_binding_not_complete_boolean(fault):
    run = bound_run()
    assert seed_run_is_bound(run)
    if fault == "policy":
        run["policy_version"] = "wrong"
    elif fault == "engine":
        run["coverage"]["engine_dependency_head"] = "wrong"
    elif fault == "seed":
        run["coverage"]["reference_seed_binding"]["selected_signals"] = 174
    elif fault == "source":
        run["source_refs"].pop()
    else:
        run["coverage"] = None
    assert not seed_run_is_bound(run)


def test_seed_helper_does_not_accept_a_flag_or_caller_dictionary():
    with pytest.raises(ValueError, match="actual_reference_seed_required"):
        reference_seed_binding({"complete": True})


def test_reader_app_router_factories_open_no_connection():
    attempted = []
    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("constructor must not connect")
    reader = FirstBarSeedUIReader("postgresql://CONTROLLED/unused", engine_factory=forbidden)
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        app = cp.build_app(Settings(_env_file=None, trade_coach_enabled=False, bot_control_enabled=False),
            session_factory=sessionmaker(engine), firstbar_read_port=reader)
        assert app.state.firstbar_read_port is reader
        assert reader._engine is None and attempted == []
    finally:
        engine.dispose()


def test_full_atr_route_mount_preserves_live_table_and_does_not_mutate_data(monkeypatch):
    monkeypatch.setattr(cp, "utcnow", lambda: datetime(2026, 10, 23, 20, tzinfo=UTC))
    data = page_data([fill("buy", 10, 4, "CONTROLLED-entry"),
        fill("sell", 10, 5, "CONTROLLED-entry-ocoexit-native")])
    before = deepcopy(data)
    reader = Recorder()
    context = asyncio.run(load_firstbar_display(reader))
    panel = render_firstbar_page(context, render_row=cp._render_completed_position_row,
        form_action="/bot/1m-schwab-v2")
    baseline = cp._render_bot_detail_page(data, "schwab_1m_v2")
    mounted = cp._render_bot_detail_page(data, "schwab_1m_v2", firstbar_simulation_html=panel)
    assert mounted.replace(panel, "", 1) == baseline
    assert data == before and reader.calls == []
    engine = sa.create_engine("sqlite:///:memory:")
    app = cp.build_app(Settings(_env_file=None, trade_coach_enabled=False, bot_control_enabled=False),
        session_factory=sessionmaker(engine), firstbar_read_port=reader)
    async def dashboard():
        return deepcopy(data)
    app.state.repository = SimpleNamespace(load_bot_dashboard_data=dashboard)
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
            return await client.get("/bot/1m-schwab-v2")
    try:
        response = asyncio.run(request())
        assert response.status_code == 200
        assert 'data-firstbar-mount="true"' in response.text
        assert reader.calls == [] and data == before
    finally:
        engine.dispose()


def test_router_has_no_write_method():
    reader = Recorder()
    with TestClient(router_app(reader)) as client:
        assert client.post("/atr-firstbar").status_code == 405
        assert client.post("/atr-firstbar/api").status_code == 405
    assert reader.calls == []


def test_slow_seed_read_does_not_block_unrelated_page_read():
    class Blocking(Recorder):
        def __init__(self):
            super().__init__()
            self.started, self.release = Event(), Event()
        def read(self, *args, **kwargs):
            self.started.set()
            assert self.release.wait(3), "controlled read was not released"
            return super().read(*args, **kwargs)
    async def scenario():
        reader = Blocking()
        app = router_app(reader)
        @app.get("/unrelated")
        async def unrelated():
            return {"status": "readable"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
            task = asyncio.create_task(client.get("/atr-firstbar/api?source_id=CONTROLLED-seed"))
            try:
                assert await asyncio.to_thread(reader.started.wait, 2)
                response = await asyncio.wait_for(client.get("/unrelated"), 2)
                assert response.status_code == 200 and not task.done()
            finally:
                reader.release.set()
                await task
    asyncio.run(scenario())
