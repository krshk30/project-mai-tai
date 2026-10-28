"""GET-only legacy SIM display; caller owns mounting and reader-pool lifetime.

No engine execution, repository writer, live dashboard, or provider is reachable
from this module. FIRSTBAR routes and their seed/month scope remain separate.
"""

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from html import escape
import json
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from project_mai_tai.atr_simulation.completed_ui import completed_comparison_view, render_completed_section
from project_mai_tai.atr_simulation.engine_binding import bound_simulation_view
from project_mai_tai.atr_simulation.service_contract import ENGINE_CONTRACT_VERSION
from project_mai_tai.atr_simulation.ui import render_simulation_section
from project_mai_tai.atr_simulation.ui_reader import (
    ROW_LIMIT, SOURCE_ID_PATTERN, SimulationUIReader, unavailable_storage, validate_read_scope,
)
from project_mai_tai.atr_simulation.ui_schema import detached_simulation_snapshot
from project_mai_tai.atr_simulation.legacy_html_projection import _legacy_html_projection


LEGACY_MODELS = {"LIVE": {"ACTUAL"}, "TODAY": {"SUPPLIED"},
    "VOLBODY": {"QUEUE_AWARE", "OPTIMISTIC"}}
DATA_ERRORS = (ValueError, TypeError, KeyError, ArithmeticError, RecursionError, AttributeError)


class _EncodedLegacyView(dict):
    """Private per-request encoding, checked inside the UNKNOWN boundary."""

    def __init__(self, value):
        super().__init__(value)
        self.body = json.dumps(value, ensure_ascii=False, allow_nan=False,
            indent=None, separators=(",", ":")).encode("utf-8")


class LegacySimulationReadPort:
    """Borrow an existing UI pool, never own/close it or widen its base read.

Main may pass its FirstBarSeedUIReader owner. Calling the base method explicitly
keeps the old seven-day query instead of its subclass's seed-only override.
The existing owner closes the one shared pool at application shutdown.
"""

    def __init__(self, owner):
        if not isinstance(owner, SimulationUIReader):
            raise TypeError("existing_simulation_ui_pool_owner_required")
        self._owner = owner

    def read(self, source_id, start_day, end_day, *, limit=ROW_LIMIT):
        return SimulationUIReader.read(self._owner, source_id, start_day, end_day, limit=limit)


def _scope(source_id, day, days):
    start = day if day is not None else datetime.now(ZoneInfo("America/New_York")).date()
    if type(days) is not int or not 1 <= days <= 7:
        raise ValueError("bounded_legacy_simulation_scope_required")
    end = start + timedelta(days=days - 1)
    validate_read_scope(source_id, start, end, ROW_LIMIT)
    return start, end


def _project(stored, source_id, start, end, *, render_html=True):
    detached = detached_simulation_snapshot(stored)
    snapshot = detached.snapshot
    if len(snapshot["rows"]) > ROW_LIMIT or len(snapshot["consumers"]) > 100:
        raise ValueError("bounded_legacy_simulation_page_required")
    run = snapshot["run"]
    if run is None:
        if snapshot["rows"]:
            raise ValueError("orphan_legacy_rows_without_run")
    elif (run.get("source_id") != source_id
            or not isinstance(run.get("policy_version"), str) or not run["policy_version"]):
        raise ValueError("legacy_source_policy_required")
    elif run["coverage"].get("engine_contract_version") not in (None, ENGINE_CONTRACT_VERSION):
        raise ValueError("legacy_source_contains_other_contract")
    for row in snapshot["rows"]:
        if row["mode"] not in LEGACY_MODELS or row["model"] not in LEGACY_MODELS[row["mode"]]:
            raise ValueError("legacy_source_contains_other_world")
        if not start <= date.fromisoformat(row["identity"]["trading_day"]) <= end:
            raise ValueError("legacy_row_outside_requested_window")
        if any(key in row and row[key] != run[key] for key in ("source_id", "policy_version")):
            raise ValueError("legacy_row_conflicts_with_run")
        if not isinstance(row.get("issues", []), (list, tuple)) or any(
                not isinstance(issue, str) for issue in row.get("issues", [])):
            raise ValueError("legacy_receipt_issues_required")
        if row.get("execution_policy") is not None and not isinstance(row["execution_policy"], Mapping):
            raise ValueError("legacy_execution_policy_shape_required")
    view = bound_simulation_view(snapshot, _detached_snapshot=detached)
    view.update(read_status=snapshot.get("read_status", "UNKNOWN_SOURCE"),
        read_issues=snapshot.get("read_issues", []),
        workers_truncated=snapshot.get("workers_truncated", False))
    if render_html is False:
        # API preserves every raw field and its original encoding/check order.
        view = _EncodedLegacyView(view)
        completed_comparison_view(view, copy_receipts=False)
        return view, None
    # Full stored-tree/source validation has already run. Keep UTF-8 refusal
    # for omitted metadata, without constructing an unused API response body.
    render_view = _legacy_html_projection(view)
    completed = completed_comparison_view(render_view, copy_receipts=False)
    if render_html is None:
        # CP-only compact output; retain full validation without encoding an unused API body.
        return render_view, None
    html = render_completed_section(completed) + render_simulation_section(render_view)
    return view, html


def _project_or_unknown(stored, source_id, start, end, *, render_html=True):
    try:
        return _project(stored, source_id, start, end, render_html=render_html)
    except DATA_ERRORS:
        return _project(unavailable_storage("LEGACY_SIMULATION_MALFORMED_VIEW"), source_id, start, end,
            render_html=render_html)


async def load_legacy_display(reader, *, source_id=None, day=None, days=1, render_html=True):
    selected = source_id if source_id is not None else "unconfigured"
    try:
        start, end = _scope(selected, day, days)
    except (ValueError, TypeError, OverflowError) as error:
        raise HTTPException(400, "Bounded legacy SIM source/day scope required") from error
    if source_id is None:
        stored = unavailable_storage("LEGACY_SIMULATION_SOURCE_NOT_SELECTED")
    elif reader is None:
        stored = unavailable_storage("LEGACY_SIMULATION_READ_PORT_NOT_CONFIGURED")
    else:
        try:
            stored = await asyncio.to_thread(reader.read, selected, start, end, limit=ROW_LIMIT)
        except (OSError, TimeoutError, RuntimeError, ValueError, TypeError, KeyError, ArithmeticError):
            stored = unavailable_storage("LEGACY_SIMULATION_STORAGE_UNAVAILABLE")
    if source_id is None:
        # Only the locally constructed empty UNKNOWN view is projected inline.
        view, html = _project_or_unknown(stored, selected, start, end, render_html=render_html)
    else:
        view, html = await asyncio.to_thread(_project_or_unknown, stored, selected, start, end,
            render_html=render_html)
    return {"view": view, "html": html, "source_id": source_id, "day": start, "days": days}


def render_legacy_page(context, *, form_action="/atr-simulation", field_prefix=""):
    source = context["source_id"] or ""
    return ('<section data-legacy-sim-mount="true"><h2>Recorded legacy SIM</h2>'
        '<p>Read-only; separate from FIRSTBAR and never included in live P&amp;L.</p>'
        '<form method="get" action="' + escape(form_action, quote=True) + '">'
        '<label>Legacy source <input name="' + escape(field_prefix, quote=True) + 'source_id" required maxlength="128" value="'
        + escape(source, quote=True) + '"></label> '
        '<label>From <input name="' + escape(field_prefix, quote=True) + 'day" type="date" required value="'
        + context["day"].isoformat() + '"></label> '
        '<label>Days <input name="' + escape(field_prefix, quote=True) + 'days" type="number" min="1" max="7" value="'
        + str(context["days"]) + '"></label> <button type="submit">View legacy replay</button>'
        '</form><p>Storage: ' + escape(context["view"]["read_status"]) + '</p>'
        + context["html"] + '</section>')


def build_legacy_router(reader):
    """Inert router factory. Reader construction/disposal stays with the caller."""
    router = APIRouter(prefix="/atr-simulation", tags=["READ-ONLY LEGACY SIM"])

    @router.get("/api")
    async def legacy_api(
        source_id: str | None = Query(default=None, pattern=SOURCE_ID_PATTERN),
        day: date | None = None, days: int = Query(default=1, ge=1, le=7),
    ):
        context = await load_legacy_display(reader, source_id=source_id, day=day, days=days,
            render_html=False)
        return Response(context["view"].body, media_type="application/json",
            headers={"Cache-Control": "no-store"})

    @router.get("", response_class=HTMLResponse)
    async def legacy_page(
        source_id: str | None = Query(default=None, pattern=SOURCE_ID_PATTERN),
        day: date | None = None, days: int = Query(default=1, ge=1, le=7),
    ):
        context = await load_legacy_display(reader, source_id=source_id, day=day, days=days)
        return HTMLResponse(render_legacy_page(context), headers={"Cache-Control": "no-store"})

    return router
