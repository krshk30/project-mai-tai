"""GET-only FIRSTBAR views; no source selection means no storage connection."""

import asyncio
from collections.abc import Mapping
from datetime import date, timedelta
from html import escape

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from project_mai_tai.atr_simulation.firstbar_display import render_firstbar_tables
from project_mai_tai.atr_simulation.firstbar_case_index import (
    historical_study_case_index, render_historical_study_case_index,
)
from project_mai_tai.atr_simulation.firstbar_projection import FIRSTBAR_SEED_LABEL
from project_mai_tai.atr_simulation.firstbar_read_model import firstbar_display_view
from project_mai_tai.atr_simulation.firstbar_ui_reader import SEED_START, validate_firstbar_scope
from project_mai_tai.atr_simulation.ui_reader import SOURCE_ID_PATTERN, unavailable_storage


def _reference_footer(stored, source_id, start, end):
    summary = historical_study_case_index(stored, source_id=source_id,
        requested_start=start, requested_end=end)
    return render_historical_study_case_index(summary), summary.status == "BOUND_REFERENCE_INDEX"


async def load_firstbar_display(reader, *, source_id=None, day=None, days=32):
    day = day if day is not None else SEED_START
    selected = source_id if source_id is not None else "unconfigured"
    try:
        validate_firstbar_scope(selected, day, day + timedelta(days=days - 1), 10000)
    except (ValueError, TypeError, OverflowError) as error:
        raise HTTPException(400, "Bounded SIM source/day scope required") from error
    if source_id is None:
        stored = unavailable_storage("FIRSTBAR_SOURCE_NOT_SELECTED")
        view = firstbar_display_view(stored, source_id=selected)
        footer, index_bound = _reference_footer(stored, selected, day,
            day + timedelta(days=days - 1))
        return {"view": view, "source_id": source_id, "day": day, "days": days,
            "historical_reference_footer": footer, "historical_index_bound": index_bound,
            "read_status": stored.get("read_status", "UNKNOWN_SOURCE")}
    else:
        try:
            stored = await asyncio.to_thread(reader.read, selected, day,
                day + timedelta(days=days - 1), limit=10000)
        except (OSError, TimeoutError, RuntimeError, ValueError, TypeError, KeyError):
            stored = unavailable_storage("FIRSTBAR_STORAGE_UNAVAILABLE")
    index_stored = stored
    try:
        view = await asyncio.to_thread(firstbar_display_view, stored, source_id=selected)
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        # Retain the independent signal index, but never certify an invalid page.
        index_stored = {**stored, "read_status": "UNAVAILABLE"} if isinstance(stored, Mapping) else stored
        stored = unavailable_storage("FIRSTBAR_INVALID_STORED_EVIDENCE")
        view = await asyncio.to_thread(firstbar_display_view, stored, source_id=selected)
    footer, index_bound = await asyncio.to_thread(_reference_footer, index_stored,
        selected, day, day + timedelta(days=days - 1))
    return {"view": view, "source_id": source_id, "day": day, "days": days,
        "historical_reference_footer": footer, "historical_index_bound": index_bound,
        "read_status": stored.get("read_status", "UNKNOWN_SOURCE")}


def _totals(total):
    return ('<div class="panel-header"><span class="count pink">'
        + str(total["known_closed_trades"]) + ' known closed trades</span><strong>'
        + escape(total["known_pnl_label"]) + '</strong></div>')


def render_firstbar_page(context, *, render_row, form_action="/atr-firstbar"):
    source = "" if context["source_id"] is None else context["source_id"]
    form = ('<section class="panel full" data-firstbar-mount="true">'
        '<div class="panel-header"><h2>Recorded FIRSTBAR SIM</h2></div>'
        '<p>Read-only replay, in-sample. Never included in live P&amp;L.</p>'
        '<form method="get" action="' + escape(form_action, quote=True) + '">'
        '<label>Recorded source <input name="source_id" required maxlength="128" value="'
        + escape(source, quote=True) + '"></label> '
        '<label>From <input name="day" type="date" required value="'
        + context["day"].isoformat() + '"></label> '
        '<label>Days <input name="days" type="number" min="1" max="32" value="'
        + str(context["days"]) + '"></label> <button type="submit">View replay</button>'
        '</form><p>Storage: ' + escape(context["read_status"]) + '</p>')
    review = ('<p data-firstbar-reference-review="true">'
        'in-sample replay; reference differences under review</p>'
        if (context["view"]["source"].get("population_label") == FIRSTBAR_SEED_LABEL
            or context.get("historical_index_bound") is True) else '')
    return form + review + render_firstbar_tables(context["view"], render_row=render_row,
        render_totals=_totals) + context.get("historical_reference_footer", '') + '</section>'


def build_firstbar_router(reader, *, render_row):
    router = APIRouter(prefix="/atr-firstbar", tags=["READ-ONLY FIRSTBAR SIM"])

    @router.get("/api")
    async def firstbar_api(
        source_id: str | None = Query(default=None, pattern=SOURCE_ID_PATTERN),
        day: date | None = None, days: int = Query(default=32, ge=1, le=32),
    ):
        context = await load_firstbar_display(reader, source_id=source_id, day=day, days=days)
        return await asyncio.to_thread(JSONResponse, context["view"], headers={"Cache-Control": "no-store"})

    @router.get("", response_class=HTMLResponse)
    async def firstbar_page(
        source_id: str | None = Query(default=None, pattern=SOURCE_ID_PATTERN),
        day: date | None = None, days: int = Query(default=32, ge=1, le=32),
    ):
        context = await load_firstbar_display(reader, source_id=source_id, day=day, days=days)
        return HTMLResponse(await render_firstbar_display(context, render_row=render_row),
            headers={"Cache-Control": "no-store"})

    return router


async def render_firstbar_display(context, *, render_row, form_action="/atr-firstbar"):
    """Keep the empty local fast path; selected pages render off-loop."""
    if context["source_id"] is None:
        return render_firstbar_page(context, render_row=render_row, form_action=form_action)
    return await asyncio.to_thread(render_firstbar_page, context,
        render_row=render_row, form_action=form_action)
