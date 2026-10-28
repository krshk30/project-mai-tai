"""NEW changed-path controls, UNRUN. CONTROLLED receipts, not model/PG/timing proof."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from decimal import Decimal
from html import escape, unescape
import re
import sys
from threading import Event, get_ident
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from project_mai_tai.atr_simulation import engine_binding
from project_mai_tai.atr_simulation import cp_sim_display as compact
from project_mai_tai.atr_simulation.completed_ui import completed_comparison_view, render_completed_section
from project_mai_tai.atr_simulation.firstbar_binding import (
    FIRSTBAR_ENGINE_HEAD, FIRSTBAR_FILL_POLICY, FIRSTBAR_RESULT_VERSION,
)
from project_mai_tai.atr_simulation.firstbar_ui_reader import SEED_BINDING
from project_mai_tai.atr_simulation.ui import render_simulation_section
from project_mai_tai.services import atr_firstbar_routes as firstbar
from project_mai_tai.services import atr_simulation_legacy_routes as legacy
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_atrpnl1 import fill, page_data
from tests.unit.test_atr_simulation_legacy_visibility import DAY, SOURCE, live_row, stored_page
from tests.unit.test_l14_ui_compact_times import assert_only_sim_time_cells_changed


FIRST_SOURCE = "CONTROLLED-offloop"
ENTRY_NS = 1792764061_000000000


def firstbar_page():
    """Literal display receipts only: no engine calls, reconstructed tape or PnL oracle."""
    identity = {"trading_day": DAY.isoformat(), "symbol": "SIMONLYCONTROLLED", "broker": "schwab",
        "account_id": "CONTROLLED-account", "strategy": "ATR", "flip_id": "CONTROLLED-flip",
        "segment_id": "CONTROLLED-segment"}
    entry = {"fill_id": "CONTROLLED-fill", "quantity": 100, "price": "1.00",
        "at": {"event_ns": ENTRY_NS}}
    exit_fact = {"quantity": 100, "price": "1.10", "at": {"event_ns": ENTRY_NS + 1_000000000},
        "reason": "TARGET_5", "issues": [], "live_exit": {"raw_reason": "CONTROLLED<raw>"}}
    rows = []
    for mode in ("FIRSTBAR", "FIRSTBAR_X3"):
        position = {"fills": [deepcopy(entry)], "first_fill_ts": ENTRY_NS, "vwap": "1.00",
            "filled_qty": 100, "remainder_cancel_reason": None}
        rows.append({"identity": deepcopy(identity), "mode": mode, "model": FIRSTBAR_FILL_POLICY,
            "version": FIRSTBAR_RESULT_VERSION, "engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
            "outcome_id": "CONTROLLED-outcome-" + mode, "trade_id": "CONTROLLED-trade-" + mode,
            "decision": "TRADE_CLOSED", "issues": [], "gross_pnl": "10.00",
            "entries": [], "exits": [deepcopy(exit_fact)], "position": deepcopy(position),
            **deepcopy(position), "body_observations": [{"raw_unused": "x" * 128} for _ in range(100)]})
    return {"run": {"source_id": FIRST_SOURCE, "policy_version": FIRSTBAR_RESULT_VERSION,
        "source_refs": [{"sha256": SEED_BINDING[field]} for field in
            ("csv_sha256", "split_sha256", "selector_sha256")],
        "coverage": {"engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
            "engine_contract_version": FIRSTBAR_RESULT_VERSION, "complete": False,
            "gaps": ["CONTROLLED_NOT_CAPTURE_PROOF"], "reference_seed_binding": deepcopy(SEED_BINDING)}},
        "rows": rows, "consumers": [], "truncated": False, "workers_truncated": False,
        "read_status": "AVAILABLE", "read_issues": []}


class Reader:
    def __init__(self, stored):
        self.stored, self.calls = stored, []

    def read(self, *args, **kwargs):
        self.calls.append((args, kwargs, get_ident()))
        return deepcopy(self.stored)


@contextmanager
def application(style, reader, *, formatter=None):
    engine = None
    data = page_data([fill("buy", 10, 4, "CONTROLLED-live-entry"),
        fill("sell", 10, 5, "CONTROLLED-live-entry-ocoexit-native")])
    before = deepcopy(data)
    try:
        if style == "standalone":
            app = FastAPI()
            app.include_router(firstbar.build_firstbar_router(reader,
                render_row=formatter or cp._render_completed_position_row))
        else:
            engine = sa.create_engine("sqlite:///:memory:")
            app = cp.build_app(Settings(_env_file=None, database_url="sqlite+pysqlite:///:memory:",
                trade_coach_enabled=False, bot_control_enabled=False,
                atr_simulation_aggregate_receipt_path=None), session_factory=sessionmaker(engine),
                firstbar_read_port=reader)

            async def dashboard():
                return deepcopy(data)

            app.state.repository = SimpleNamespace(load_bot_dashboard_data=dashboard)
        @app.get("/CONTROLLED-independent")
        async def independent():
            return {"status": "readable"}
        yield app, data, before
    finally:
        if engine is not None:
            engine.dispose()


def selected_path(style):
    prefix = "/atr-firstbar" if style == "standalone" else "/bot/1m-schwab-v2"
    return prefix + "?source_id=" + FIRST_SOURCE + "&day=" + DAY.isoformat() + "&days=1"


def _assert_shared_canonical_rows_except_approved_time_cells(panel, tables):
    matches = re.findall(r'<section\b[^>]*data-sim-mode="([^"]+)"[^>]*>(.*?)</section>', panel, re.S)
    sections = dict(matches)
    assert len(sections) == len(matches), "duplicate_mode_section"
    for table in tables:
        body = re.search(r'<tbody>(.*?)</tbody>', sections[table["mode"]], re.S)[1]
        actual = re.findall(r'<tr>.*?</tr>', body, re.S)
        assert len(actual) == len(table["rows"]), "canonical_trade_row_missing_or_duplicated"
        for html, item in zip(actual, table["rows"]):
            canonical = cp._render_completed_position_row(item["row"])
            assert_only_sim_time_cells_changed(canonical, html)
            old_cells = re.findall(r'(<td\b[^>]*>)(.*?)(</td>)', canonical, re.S)
            new_cells = re.findall(r'(<td\b[^>]*>)(.*?)(</td>)', html, re.S)
            # Permit exactly the two required title/marker additions, not arbitrary HTML changes.
            for index, field in ((3, "entry"), (5, "exit")):
                full = unescape(old_cells[index][1])
                expected_open = (old_cells[index][0][:-1] + ' data-sim-time="' + field
                    + '" title="' + escape(full, quote=True) + '">')
                assert new_cells[index][0] == expected_open


@pytest.mark.parametrize("style", ["standalone", "shared"])
def test_selected_actual_route_holds_formatter_offloop_while_unrelated_get_progresses(monkeypatch, style):
    reader = Reader(firstbar_page())
    started, release, threads = Event(), Event(), []
    original = cp._render_completed_position_row

    def held(row):
        if row.get("ticker") == "SIMONLYCONTROLLED":
            threads.append(get_ident())
            started.set()
            assert release.wait(5), "CONTROLLED formatter must be released"
        return original(row)

    monkeypatch.setattr(cp, "_render_completed_position_row", held)

    async def scenario(app):
        loop_thread = get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
            task = asyncio.create_task(client.get(selected_path(style)))
            try:
                assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3)
                assert not task.done()
                response = await asyncio.wait_for(client.get("/CONTROLLED-independent"), 2)
                assert response.status_code == 200 and response.json() == {"status": "readable"}
                assert not task.done()
            finally:
                release.set()
                # Return exceptions so cleanup never masks a progress assertion.
                results = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            assert not isinstance(results[0], BaseException)
            assert results[0].status_code == 200 and "SIMONLYCONTROLLED" in results[0].text
            assert threads and all(thread != loop_thread for thread in threads)
            assert len(reader.calls) == 1 and reader.calls[0][2] != loop_thread

    with application(style, reader, formatter=held) as (app, data, before):
        asyncio.run(scenario(app))
        assert data == before


@pytest.mark.parametrize("style", ["standalone", "shared"])
def test_selected_html_exact_existing_formatter_bytes_and_controlled_live_panel_unchanged(monkeypatch, style):
    reader = Reader(firstbar_page())
    captured = []
    original = cp._render_bot_detail_page

    def observe(data, strategy, **kwargs):
        captured.append((deepcopy(data), strategy, kwargs["firstbar_simulation_html"]))
        return original(data, strategy, **kwargs)

    monkeypatch.setattr(cp, "_render_bot_detail_page", observe)

    async def scenario(app):
        context = await firstbar.load_firstbar_display(Reader(firstbar_page()), source_id=FIRST_SOURCE,
            day=DAY, days=1)
        action = "/atr-firstbar" if style == "standalone" else "/bot/1m-schwab-v2"
        expected = firstbar.render_firstbar_page(context, render_row=cp._render_completed_position_row,
            form_action=action)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
            response = await client.get(selected_path(style))
        assert response.status_code == 200 and "CONTROLLED&lt;raw&gt;" in response.text
        assert "$+10.00" in response.text
        if style == "standalone":
            assert response.content == expected.encode("utf-8")
        else:
            snapshot, strategy, panel = captured[-1]
            _assert_shared_canonical_rows_except_approved_time_cells(
                panel, compact.firstbar_tables(context["view"]))
            assert 'data-l14-ui="compact"' in panel and 'Exit reason' in panel
            assert response.text.replace(panel, "", 1) == original(snapshot, strategy)
        assert len(reader.calls) == 1

    with application(style, reader) as (app, data, before):
        asyncio.run(scenario(app))
        assert data == before


@pytest.mark.parametrize("fault", ["pnl", "raw_reason", "escape", "full_time_title", "missing_row", "extra_row"])
def test_shared_compact_contract_still_rejects_field_or_row_loss(fault):
    view = firstbar.firstbar_display_view(firstbar_page(), source_id=FIRST_SOURCE)
    table = compact.firstbar_tables(view)[0]
    panel = compact.panel("FIRSTBAR", [item["row"] for item in table["rows"]],
        render_row=cp._render_completed_position_row, marker=table["mode"])
    _assert_shared_canonical_rows_except_approved_time_cells(panel, [table])
    body = re.search(r'<tbody>(.*?)</tbody>', panel, re.S)[1]
    row = re.search(r'<tr>.*?</tr>', body, re.S)[0]
    cells = list(re.finditer(r'<td\b[^>]*>.*?</td>', row, re.S))
    if fault in {"pnl", "raw_reason"}:
        cell = cells[7 if fault == "pnl" else 8]
        damaged = row[:cell.start()] + '<td>CONTROLLED_LOSS</td>' + row[cell.end():]
    elif fault == "escape":
        damaged = row.replace('CONTROLLED&lt;raw&gt;', 'CONTROLLED<raw>')
        assert damaged != row
    elif fault == "full_time_title":
        damaged = re.sub(r' title="[^"]*"', ' title="CONTROLLED_LOSS"', row, count=1)
        assert damaged != row
    elif fault == "missing_row":
        damaged = ""
    else:
        damaged = row + row
    with pytest.raises(AssertionError):
        _assert_shared_canonical_rows_except_approved_time_cells(panel.replace(row, damaged, 1), [table])


def test_firstbar_api_bytes_and_all_outcomes_remain_exact_without_html_render(monkeypatch):
    reader = Reader(firstbar_page())

    def forbidden(*args, **kwargs):
        raise AssertionError("API must not render HTML")

    async def scenario():
        expected = firstbar.firstbar_display_view(firstbar_page(), source_id=FIRST_SOURCE)
        monkeypatch.setattr(firstbar, "render_firstbar_page", forbidden)
        with application("standalone", reader) as (app, _, _):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
                response = await client.get("/atr-firstbar/api?source_id=" + FIRST_SOURCE
                    + "&day=" + DAY.isoformat() + "&days=1")
        assert response.status_code == 200 and response.content == JSONResponse(expected).body
        assert response.headers["cache-control"] == "no-store"
        assert len(response.json()["outcomes"]) == 2 and len(reader.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("style", ["standalone", "shared"])
def test_selected_invalid_full_stored_tree_still_refuses_and_renders_unknown_offloop(monkeypatch, style):
    stored = firstbar_page()
    stored["rows"][0]["body_observations"][0]["invalid"] = float("inf")
    reader = Reader(stored)
    target = firstbar if style == "standalone" else compact
    renderer = "render_firstbar_page" if style == "standalone" else "render_page"
    original, threads = getattr(target, renderer), []

    def observe(*args, **kwargs):
        threads.append(get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(target, renderer, observe)

    async def scenario(app):
        loop_thread = get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
            response = await client.get(selected_path(style))
        assert response.status_code == 200
        if style == "standalone":
            assert "Storage: UNAVAILABLE" in response.text and "First-bar-high" in response.text
        else:
            assert "read status UNAVAILABLE" in response.text and "FIRSTBAR" in response.text
            assert "No recorded rows in this view; not evidence of zero trades." in response.text
        assert "UNKNOWN" in response.text
        assert "SIMONLYCONTROLLED" not in response.text
        assert threads and all(thread != loop_thread for thread in threads)
        assert len(reader.calls) == 1

    with application(style, reader) as (app, _, _):
        asyncio.run(scenario(app))


@pytest.mark.parametrize("style", ["standalone", "shared"])
def test_unselected_actual_route_keeps_no_executor_no_read_contract(monkeypatch, style):
    async def forbidden(*args, **kwargs):
        raise AssertionError("unselected empty local view must not use executor")

    reader = Reader(firstbar_page())
    path = "/atr-firstbar" if style == "standalone" else "/bot/1m-schwab-v2"
    with application(style, reader) as (app, _, _):
        monkeypatch.setattr(asyncio, "to_thread", forbidden)

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
                response = await client.get(path)
            assert response.status_code == 200 and "UNKNOWN" in response.text

        asyncio.run(scenario())
        assert reader.calls == []


def legacy_page():
    stored = stored_page()
    row = live_row()
    row["body_observations"] = [{"unused": "CONTROLLED" * 100} for _ in range(200)]
    row["extra"] = {"decimal": Decimal("1.2300"), "day": date(2026, 10, 23),
        "keys": {None: "none", "None": "literal None", True: "bool", "True": "literal True"}}
    stored["rows"] = [row]
    return stored


def test_legacy_html_skips_api_encoder_but_keeps_full_context_and_exact_rendered_bytes(monkeypatch):
    source = legacy_page()
    before = deepcopy(source)
    expected = engine_binding.bound_simulation_view(source)
    expected.update(read_status="AVAILABLE", read_issues=[], workers_truncated=False)
    expected_html = (render_completed_section(completed_comparison_view(expected, copy_receipts=False))
        + render_simulation_section(expected))

    def forbidden(*args, **kwargs):
        raise AssertionError("HTML must not allocate the unused full API body")

    monkeypatch.setattr(legacy, "_EncodedLegacyView", forbidden)
    view, html = legacy._project(source, SOURCE, DAY, DAY)
    assert html == expected_html and view == expected and source == before
    assert len(view["rows"][0]["body_observations"]) == 200
    assert "CONTROLLED&lt;raw&gt;" in html and "COMPLETED" in html


def test_legacy_api_still_encodes_every_raw_field_and_exact_old_key_normalization():
    source = legacy_page()
    before = deepcopy(source)
    expected = engine_binding.bound_simulation_view(source)
    expected.update(read_status="AVAILABLE", read_issues=[], workers_truncated=False)
    view, html = legacy._project(source, SOURCE, DAY, DAY, render_html=False)
    assert html is None and view.body == JSONResponse(expected).body and source == before
    assert len(view["rows"][0]["body_observations"]) == 200
    assert view["rows"][0]["extra"]["keys"] == {"None": "literal None", "True": "literal True"}
    reader = Reader(source)
    app = FastAPI()
    app.include_router(legacy.build_legacy_router(reader))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://controlled") as client:
            response = await client.get("/atr-simulation/api?source_id=" + SOURCE
                + "&day=" + DAY.isoformat() + "&days=1")
        assert response.status_code == 200 and response.content == view.body
        assert response.headers["cache-control"] == "no-store"
        assert len(reader.calls) == 1 and source == before

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), Decimal("NaN"), object(), "\ud800", "\udfff"])
def test_omitted_legacy_metadata_still_refuses_in_both_html_and_raw_api(bad):
    source = legacy_page()
    source["rows"][0]["body_observations"][0]["bad"] = bad
    for render_html in (False, True):
        view, html = legacy._project_or_unknown(source, SOURCE, DAY, DAY, render_html=render_html)
        assert view["read_status"] == "UNAVAILABLE" and view["rows"] == []
        assert view["read_issues"] == ["LEGACY_SIMULATION_MALFORMED_VIEW"]
        if render_html:
            assert "UNKNOWN" in html


@pytest.mark.parametrize("kind", ["surrogate_key", "cycle", "deep"])
def test_omitted_metadata_key_or_recursive_tree_is_not_lost_before_full_guard(kind):
    source = legacy_page()
    if kind == "surrogate_key":
        source["rows"][0]["body_observations"][0]["\ud800"] = "bad-key"
    elif kind == "cycle":
        value = []
        value.append(value)
        source["rows"][0]["extra"] = value
    else:
        value = []
        for _ in range(sys.getrecursionlimit() + 100):
            value = [value]
        source["rows"][0]["extra"] = value
    view, html = legacy._project_or_unknown(source, SOURCE, DAY, DAY)
    assert view["read_issues"] == ["LEGACY_SIMULATION_MALFORMED_VIEW"]
    assert view["rows"] == [] and "UNKNOWN" in html


def test_valid_nonascii_metadata_retains_full_api_bytes_without_new_html_rejection():
    source = legacy_page()
    source["rows"][0]["extra"]["valid_unicode"] = "\u00e9\U0001f642"
    api, _ = legacy._project(source, SOURCE, DAY, DAY, render_html=False)
    view, html = legacy._project(source, SOURCE, DAY, DAY)
    assert view == api and view["read_status"] == "AVAILABLE" and "COMPLETED" in html
    assert "\u00e9\U0001f642".encode() in api.body


def test_unused_firstbar_surrogate_is_not_subject_to_legacy_only_utf8_guard():
    source = firstbar_page()
    source["rows"][0]["body_observations"][0]["unused"] = "\ud800"
    view = firstbar.firstbar_display_view(source, source_id=FIRST_SOURCE)
    assert len(view["outcomes"]) == 2 and view["outcomes"][0]["kind"] == "TRADE"


def test_nonvisible_fill_property_cannot_escape_canonical_position_equality_after_projection():
    source = legacy_page()
    row = source["rows"][0]
    row.update(mode="TODAY", model="SUPPLIED", decision="TRADE_CLOSED", fills=deepcopy(row["entries"]),
        first_fill_ts=row["entries"][0]["at"]["event_ns"], vwap="1", filled_qty=100,
        remainder_cancel_reason=None, position_contract="ONE_POSITION_CURRENT_VWAP_FIRST_FILL_120S")
    row["exits"][0].update(reason="TARGET_5", issues=[])
    row["position"] = {name: deepcopy(row[name]) for name in
        ("first_fill_ts", "fills", "vwap", "filled_qty", "remainder_cancel_reason")}
    source["run"]["coverage"]["engine_dependency_head"] = engine_binding.ENGINE_HEAD
    positive, _ = legacy._project(source, SOURCE, DAY, DAY)
    positive_completed = completed_comparison_view(positive, copy_receipts=False)
    assert positive_completed["rows"][0]["legs"][1]["completed"] is True
    row["position"]["fills"][0]["hidden_allocation"] = "CONTROLLED-conflict"
    view, html = legacy._project(source, SOURCE, DAY, DAY)
    assert view["rows"][0]["position"] != {name: view["rows"][0][name] for name in
        ("first_fill_ts", "fills", "vwap", "filled_qty", "remainder_cancel_reason")}
    assert "No proven completed anchors" in html
    completed = completed_comparison_view(view, copy_receipts=False)
    assert completed["rows"] == []
    assert completed["known_page_counts"]["leg_states"]["SIM TODAY:INCOMPLETE"] == 1
