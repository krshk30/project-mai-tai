"""New source-only controls. UNRUN; no native/store/installation acceptance."""

import asyncio
from copy import deepcopy
from datetime import date, datetime, timezone
from threading import get_ident

from fastapi import FastAPI
import httpx
import pytest

from project_mai_tai.atr_simulation import engine_binding, read_model
from project_mai_tai.atr_simulation.firstbar_ui_reader import FirstBarSeedUIReader
from project_mai_tai.atr_simulation.ui_reader import SimulationUIReader, unavailable_storage
from project_mai_tai.services import atr_simulation_legacy_routes as routes


DAY = date(2026, 10, 23)
SOURCE = "CONTROLLED-legacy"
POLICY = "CONTROLLED-legacy-policy"


def stored_page(*, engine=None):
    coverage = {"complete": False, "gaps": ["CONTROLLED_UNKNOWN_CAPTURE"]}
    if engine is not None:
        coverage["engine_dependency_head"] = engine
    return {"run": {"source_id": SOURCE, "policy_version": POLICY, "coverage": coverage},
        "rows": [], "consumers": [], "truncated": False, "workers_truncated": False,
        "read_status": "AVAILABLE", "read_issues": []}


def live_row():
    return {"identity": {"trading_day": DAY.isoformat(), "symbol": "CONTROLLED",
        "broker": "CONTROLLED", "account_id": "CONTROLLED-account", "strategy": "CONTROLLED-ATR",
        "flip_id": "CONTROLLED-flip", "segment_id": "CONTROLLED-segment"},
        "mode": "LIVE", "model": "ACTUAL", "decision": "BOUGHT", "gross_pnl": "10",
        "entries": [{"fill_id": "CONTROLLED-entry", "quantity": 100, "price": "1",
                     "at": {"event_ns": 1}}],
        "exits": [{"fill_id": "CONTROLLED-exit", "quantity": 100, "price": "1.1",
                   "at": {"event_ns": 2}, "reason": "TARGET", "raw_reason": "CONTROLLED<raw>",
                   "mapped_reason": "TARGET"}],
        "coverage": {"complete": True, "gaps": [], "sources": ["CONTROLLED-fill-receipts"]},
        "issues": [], "comparison": "LIVE_FACTS_ONLY"}


class Reader:
    def __init__(self, stored=None, error=None):
        self.stored = stored_page() if stored is None else stored
        self.error = error
        self.calls = []

    def read(self, *args, **kwargs):
        self.calls.append((args, kwargs, get_ident()))
        if self.error is not None:
            raise self.error
        return self.stored


def load(reader, **kwargs):
    return asyncio.run(routes.load_legacy_display(reader, day=DAY, **kwargs))


def request(reader, path, method="GET"):
    app = FastAPI()
    app.include_router(routes.build_legacy_router(reader))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
            return await client.request(method, path)

    return asyncio.run(run())


@pytest.mark.parametrize("projector", [read_model.simulation_view, engine_binding.bound_simulation_view])
@pytest.mark.parametrize("kind", ["no_run", "missing", "mismatch"])
def test_unknown_stored_provenance_never_claims_imported_engine(projector, kind):
    page = stored_page(engine="CONTROLLED-other-engine" if kind == "mismatch" else None)
    if kind == "no_run":
        page["run"] = None
    before = deepcopy(page)
    assert "position_api_binding_head" not in projector(page)
    assert page == before


@pytest.mark.parametrize("projector", [read_model.simulation_view, engine_binding.bound_simulation_view])
def test_exact_stored_engine_declaration_is_retained_without_coverage_upgrade(projector):
    page = stored_page(engine=engine_binding.ENGINE_HEAD)
    view = projector(page)
    assert view["position_api_binding_head"] == engine_binding.ENGINE_HEAD
    assert view["source"]["coverage"]["complete"] is False
    assert view["coverage_status"]["gaps"] == ["CONTROLLED_UNKNOWN_CAPTURE"]


def test_binding_wrapper_removes_a_stale_upstream_header(monkeypatch):
    monkeypatch.setattr(read_model, "simulation_view", lambda *args, **kwargs:
        {"position_api_binding_head": "CONTROLLED-stale"})
    assert engine_binding.bound_simulation_view(stored_page()) == {}


def test_fact_payload_and_known_page_pnl_are_not_rewritten_by_metadata_fix():
    page = stored_page()
    page["rows"] = [live_row()]
    before = deepcopy(page)
    view = read_model.simulation_view(page)
    assert view["rows"] == before["rows"]
    assert view["known_row_pnl_subtotals"] == {"LIVE:ACTUAL": "10"}
    assert view["live_totals_included"] is False and view["subtotals_are_not_full_denominator"] is True
    assert page == before


def test_unselected_source_does_not_read_and_preserves_unknown_not_no_buy():
    reader = Reader()
    context = load(reader)
    assert reader.calls == []
    assert context["view"]["read_status"] == "UNAVAILABLE"
    assert context["view"]["coverage_status"] == "UNKNOWN"
    assert "position_api_binding_head" not in context["view"]
    assert "UNKNOWN, not NO_BUY" in context["html"]


def test_borrowed_firstbar_owner_uses_legacy_base_method_without_factory_or_close(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not invoke seed read, connect, or dispose")

    owner = FirstBarSeedUIReader("postgresql://CONTROLLED/unused", engine_factory=forbidden)
    monkeypatch.setattr(owner, "read", forbidden)
    monkeypatch.setattr(owner, "close", forbidden)
    calls = []

    def legacy_read(self, source, start, end, *, limit):
        calls.append((self, source, start, end, limit))
        return unavailable_storage("CONTROLLED-no-database")

    monkeypatch.setattr(SimulationUIReader, "read", legacy_read)
    borrowed = routes.LegacySimulationReadPort(owner)
    routes.build_legacy_router(borrowed)
    assert calls == [] and owner._engine is None and not hasattr(borrowed, "close")
    load(borrowed, source_id=SOURCE, days=7)
    assert calls == [(owner, SOURCE, DAY, date(2026, 10, 29), 10000)]
    assert owner._engine is None


def test_borrowed_pool_requires_actual_ui_owner_not_repository_writer():
    with pytest.raises(TypeError, match="existing_simulation_ui_pool_owner_required"):
        routes.LegacySimulationReadPort(object())


def test_missing_legacy_di_stays_unavailable_without_opening_another_reader():
    response = request(None, "/atr-simulation/api?source_id=" + SOURCE + "&day=" + DAY.isoformat())
    assert response.status_code == 200
    assert response.json()["read_status"] == "UNAVAILABLE"
    assert response.json()["read_issues"] == ["LEGACY_SIMULATION_READ_PORT_NOT_CONFIGURED"]
    assert "position_api_binding_head" not in response.json()


@pytest.mark.parametrize("days", [0, 8, True, 1.5])
def test_direct_loader_cannot_widen_legacy_window(days):
    reader = Reader()
    with pytest.raises(routes.HTTPException):
        load(reader, source_id=SOURCE, days=days)
    assert reader.calls == []


@pytest.mark.parametrize("query", ["days=8", "days=0", "day=bad", "source_id=bad/path"])
def test_http_scope_refusal_never_reads(query):
    reader = Reader()
    assert request(reader, "/atr-simulation/api?" + query).status_code == 422
    assert reader.calls == []


@pytest.mark.parametrize("path", ["/atr-simulation", "/atr-simulation/api"])
def test_legacy_endpoints_are_get_only(path):
    reader = Reader()
    assert request(reader, path, "POST").status_code == 405
    assert reader.calls == []


def test_read_projection_and_rendering_are_off_loop_on_one_detached_snapshot(monkeypatch):
    page = stored_page()
    page["rows"] = [live_row()]
    reader = Reader(page)
    before, threads = deepcopy(page), []
    original = routes.render_simulation_section

    def tracked(view):
        threads.append(get_ident())
        return original(view)

    monkeypatch.setattr(routes, "render_simulation_section", tracked)

    async def run():
        loop_thread = get_ident()
        context = await routes.load_legacy_display(reader, source_id=SOURCE, day=DAY)
        assert len(reader.calls) == 1 and reader.calls[0][2] != loop_thread
        assert threads and all(thread != loop_thread for thread in threads)
        assert "CONTROLLED&lt;raw&gt;" in context["html"]
        assert context["view"]["rows"] == page["rows"]
        assert context["view"]["live_totals_included"] is False

    asyncio.run(run())
    assert page == before


@pytest.mark.parametrize("error", [OSError("CONTROLLED"), TimeoutError("CONTROLLED"),
    RecursionError("CONTROLLED"), TypeError("CONTROLLED")])
def test_read_failure_returns_unknown_not_empty_success(error):
    reader = Reader(error=error)
    view = load(reader, source_id=SOURCE)["view"]
    assert view["read_status"] == "UNAVAILABLE" and view["coverage_status"] == "UNKNOWN"
    assert view["rows"] == [] and "position_api_binding_head" not in view


@pytest.mark.parametrize("fault", ["source", "policy", "other_contract", "other_world", "future_row",
    "oversize", "consumer_clock", "render_operand", "deepcopy", "orphan"])
def test_invalid_materialization_and_renderer_operands_fail_closed(fault):
    page = stored_page()
    page["rows"] = [live_row()]
    if fault == "source":
        page["run"]["source_id"] = "CONTROLLED-other"
    elif fault == "policy":
        page["rows"][0]["policy_version"] = "CONTROLLED-conflict"
    elif fault == "other_contract":
        page["run"]["coverage"]["engine_contract_version"] = "CONTROLLED-FIRSTBAR-contract"
    elif fault == "other_world":
        page["rows"][0].update(mode="FIRSTBAR", model="MODELED_STOP_AT_OBSERVED_PRINT_VOLUME",
            first_fill_ts=None, filled_qty=0, vwap=None, remainder_cancel_reason=None)
    elif fault == "future_row":
        page["rows"][0]["identity"]["trading_day"] = "2026-10-24"
    elif fault == "oversize":
        page["rows"] *= 10001
    elif fault == "consumer_clock":
        page["consumers"] = [{"consumer_id": "CONTROLLED", "updated_at": "bad", "issues": [], "status": "UNKNOWN"}]
    elif fault == "render_operand":
        page["rows"][0]["execution_policy"] = True
    elif fault == "deepcopy":
        class RefuseCopy(dict):
            def __deepcopy__(self, memo):
                raise TypeError("CONTROLLED materialization refusal")
        page = RefuseCopy(page)
    else:
        page["run"] = None
    view = load(Reader(page), source_id=SOURCE)["view"]
    assert view["read_status"] == "UNAVAILABLE" and view["rows"] == []
    assert "position_api_binding_head" not in view


def test_worker_status_and_raw_issues_survive_one_valid_read():
    page = stored_page(engine=engine_binding.ENGINE_HEAD)
    row = live_row()
    row["issues"] = ["CONTROLLED_UNKNOWN_EXIT"]
    row["gross_pnl"] = None
    page["rows"] = [row]
    page["consumers"] = [{"consumer_id": "CONTROLLED-worker", "updated_at": datetime.now(timezone.utc),
        "status": "UNKNOWN", "issues": ["CONTROLLED-worker-issue"]}]
    reader = Reader(page)
    response = request(reader, "/atr-simulation/api?source_id=" + SOURCE + "&day=" + DAY.isoformat())
    assert response.status_code == 200 and len(reader.calls) == 1
    body = response.json()
    assert body["rows"] == page["rows"] and body["workers"][0]["status"] == "UNKNOWN"
    assert body["position_api_binding_head"] == engine_binding.ENGINE_HEAD
    assert body["known_row_pnl_subtotals"] == {}


def test_html_and_api_preserve_unbound_stored_facts_without_current_engine_claim():
    page = stored_page(engine="CONTROLLED-historical-unknown")
    page["rows"] = [live_row()]
    reader = Reader(page)
    html = request(reader, "/atr-simulation?source_id=" + SOURCE + "&day=" + DAY.isoformat())
    assert html.status_code == 200 and len(reader.calls) == 1
    assert "COMPLETED - SIMULATION / NOT REAL TRADES" in html.text
    assert "Entry facts" in html.text and "CONTROLLED&lt;raw&gt;" in html.text
    assert "Never included in live PnL" in html.text
    api = request(reader, "/atr-simulation/api?source_id=" + SOURCE + "&day=" + DAY.isoformat())
    assert api.status_code == 200 and len(reader.calls) == 2
    assert api.json()["rows"] == page["rows"]
    assert "position_api_binding_head" not in api.json()
