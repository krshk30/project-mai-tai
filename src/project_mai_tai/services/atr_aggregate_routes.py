"""Explicitly bound, GET-only modeled LIVE baseline; no default DB or source."""

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from project_mai_tai.atr_simulation.aggregate_entry_display import render_aggregate_entry_table
from project_mai_tai.atr_simulation.aggregate_entry_read_model import (
    AggregateReadBinding, FIRST_DAY, LAST_DAY, aggregate_entry_display_view,
)
from project_mai_tai.atr_simulation.firstbar_display import COLUMN_HEADER
from project_mai_tai.atr_simulation.ui_reader import unavailable_storage
from project_mai_tai.atr_simulation.realized_display import (
    BoundRealizedDisplay, render_realized_display, unknown_realized_view,
)


TITLE = "Live entries, modeled exits (same engine) · partial timing UNKNOWN"
SOURCE_PATTERN = r"^aggregate-entry:[0-9a-f]{64}$"


@dataclass(frozen=True)
class AggregateSimulationReadPort:
    reader: object
    binding: AggregateReadBinding
    realized: BoundRealizedDisplay | None = None

    def __post_init__(self):
        if not isinstance(self.binding, AggregateReadBinding) or not callable(getattr(self.reader, "read", None)):
            raise ValueError("explicit_aggregate_reader_and_projection_binding_required")
        if self.realized is not None and not isinstance(self.realized, BoundRealizedDisplay):
            raise ValueError("explicit_bound_realized_display_required")


async def load_aggregate_display(port, *, source_id=None, day=None, days=32):
    day = FIRST_DAY if day is None else day
    if (type(day) is not date or type(days) is not int or not 1 <= days <= 32
            or not FIRST_DAY <= day <= day + timedelta(days=days - 1) <= LAST_DAY):
        raise HTTPException(400, "Bounded aggregate seed scope required")
    if source_id is None or port is None:
        realized = (port.realized.view() if isinstance(port, AggregateSimulationReadPort)
                    and port.realized is not None else unknown_realized_view())
        return {"view": None, "read_status": "UNAVAILABLE", "source_id": source_id,
                "realized": realized}
    if not isinstance(port, AggregateSimulationReadPort) or source_id != port.binding.source_id:
        raise HTTPException(400, "Reviewed aggregate source binding required")
    end = day + timedelta(days=days - 1)
    try:
        stored = await asyncio.to_thread(port.reader.read, source_id, day, end, limit=10000)
    except (OSError, TimeoutError, RuntimeError, ValueError, TypeError, KeyError):
        stored = unavailable_storage("AGGREGATE_STORAGE_UNAVAILABLE")
    try:
        view = await asyncio.to_thread(aggregate_entry_display_view, stored,
            binding=port.binding, start=day.isoformat(), end=end.isoformat())
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        view = await asyncio.to_thread(aggregate_entry_display_view,
            unavailable_storage("AGGREGATE_INVALID_STORED_EVIDENCE"), binding=port.binding,
            start=day.isoformat(), end=end.isoformat())
    return {"view": view, "read_status": view["read_status"], "source_id": source_id,
            "realized": port.realized.view() if port.realized is not None else unknown_realized_view()}


def render_aggregate_page(context, *, render_row):
    realized = render_realized_display(context.get("realized"))
    if context["view"] is None:
        return ('<section data-aggregate-entry-unavailable="true"><h3>' + TITLE
            + '</h3><p>UNKNOWN: no reviewed baseline source selected. '
            'Modeled replay is never added to realized LIVE P&amp;L.</p></section>') + realized
    return render_aggregate_entry_table(context["view"], render_row=render_row,
        column_header=COLUMN_HEADER) + realized


def build_aggregate_router(port, *, render_row):
    router = APIRouter(prefix="/atr-aggregate", tags=["READ-ONLY MODELED LIVE BASELINE"])

    @router.get("/api")
    async def aggregate_api(source_id: str | None = Query(default=None, pattern=SOURCE_PATTERN),
                            day: date | None = None, days: int = Query(default=32, ge=1, le=32)):
        context = await load_aggregate_display(port, source_id=source_id, day=day, days=days)
        return await asyncio.to_thread(JSONResponse, context, headers={"Cache-Control": "no-store"})

    @router.get("", response_class=HTMLResponse)
    async def aggregate_page(source_id: str | None = Query(default=None, pattern=SOURCE_PATTERN),
                             day: date | None = None, days: int = Query(default=32, ge=1, le=32)):
        context = await load_aggregate_display(port, source_id=source_id, day=day, days=days)
        rendered = await asyncio.to_thread(render_aggregate_page, context, render_row=render_row)
        return HTMLResponse(rendered, headers={"Cache-Control": "no-store"})

    return router
