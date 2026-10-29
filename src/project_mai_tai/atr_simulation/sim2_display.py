"""ATR page: the two operator tables (FIRSTBAR, FIRSTBAR+2min) for one trading day (10-29).

Reads the single 'atr_sim2_day' row the 5-minute job writes; earlier days stay in the table as the
archive. Rendering only — no simulation work happens on a page request.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .cp_sim_display import panel

ET = ZoneInfo("America/New_York")
SNAPSHOT_TYPE = "atr_sim2_day"
TITLES = ("FIRSTBAR", "FIRSTBAR+2min", "LIVE rules SIM")
LIVE_TITLE = "LIVE rules SIM"
LIVE_DESCRIPTION = ("Simulation, not real trades: the same scanner-confirmed signals traded with the live ATR bot's "
                    "own rules — waiting buy at the line +0.5% (limit +0.5% more), following the line every minute while short; "
                    "exits: target +5%, stop -8%, confirmation exit if the bar after the fill does not close long, "
                    "sell flip.")
# Operator 10-29 17:1x: full-width panels, all rows visible, readable columns.
STYLE = ('<style>#atr-sim2{grid-column:1/-1;min-width:0;max-width:100%;width:100%}'
         '#atr-sim2 .panel{max-width:100%;width:100%;margin-bottom:16px;box-sizing:border-box}'
         '#atr-sim2 .panel-header{flex-wrap:wrap}'
         '#atr-sim2 .table-wrap{max-width:100%;max-height:none;overflow-x:auto;overflow-y:visible}'
         '#atr-sim2 table{width:100%;min-width:900px;table-layout:auto}'
         '#atr-sim2 th,#atr-sim2 td{white-space:nowrap;padding:6px 10px}'
         '#atr-sim2 td:last-child{white-space:normal;max-width:none!important;overflow:visible!important}'
         '</style>')


def load_day(session_factory, day: str):
    """The page must never fail because of the simulation: any read error shows 'unavailable'."""
    try:
        with session_factory() as session:
            row = session.execute(text(
                "select payload from dashboard_snapshots where snapshot_type=:t and payload->>'trading_day'=:d "
                "order by created_at desc limit 1"), {"t": SNAPSHOT_TYPE, "d": day}).first()
    except Exception as error:
        return {"unavailable": type(error).__name__}
    if row is None:
        return None
    payload = row[0]
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)
    return payload if isinstance(payload, dict) else None


def _display(row):
    traded = row.get("status") in ("CLOSED", "OPEN")
    pnl = row.get("pnl", 0.0) if traded else 0.0
    summary = row.get("reason", "") + " · signal " + row.get("signal", "?") + " · buy at " + str(row.get("trigger", "?"))
    return {
        "ticker": row.get("symbol", ""),
        "path": row.get("status", ""),
        "quantity": row.get("quantity", "") if traded else "",
        "entry_time": row.get("entry_time", "") if traded else "",
        "entry_price": row.get("entry_price", "") if traded else "",
        "exit_time": row.get("exit_time", "") if traded else "",
        "exit_price": row.get("exit_price", "") if traded else "",
        "pnl": pnl,
        "pnl_pct_label": (f"{row['pnl_pct']:+.2f}%" if traded else "no trade"),
        "pnl_label": (f"${pnl:+.2f}" if traded else "—"),
        "pnl_label_known": traded,
        "summary": summary,
    }


def render_day(doc, *, render_row, day: str):
    if doc is None or "unavailable" in doc:
        note = ("No simulation for " + day + " yet — the job runs every 5 minutes from 04:00 ET."
                if doc is None else "Simulation unavailable right now (" + doc["unavailable"] + ").")
        return ('<div data-firstbar-mount="true" data-atrsim-firstbar="true"><section class="panel full">'
                '<h3>ATR simulation (FIRSTBAR / FIRSTBAR+2min)</h3><p>' + escape(note) + '</p></section></div>')
    generated = datetime.fromisoformat(doc["generated_at"]).astimezone(ET).strftime("%H:%M:%S")
    panels = []
    for title in TITLES:
        rows = doc["tables"].get(title, [])
        traded = [r for r in rows if r.get("status") in ("CLOSED", "OPEN")]
        skipped = len(rows) - len(traded)
        shown = traded
        total = sum(r.get("pnl", 0.0) for r in traded)
        updated = (f"{doc['trading_day']} · updated {generated} ET · {len(traded)} trades, {skipped} signals without a trade"
                   f" · total ${total:+.2f} on ${doc.get('notional_usd', 600):.0f} per trade")
        extra = {"description": LIVE_DESCRIPTION} if title == LIVE_TITLE else {}
        panels.append(panel(title, [_display(r) for r in shown], render_row=render_row, marker=title,
                            updated_text=updated, **extra))
    return ('<div id="atr-sim2" class="full" data-atrsim2="day" data-firstbar-mount="true" data-atrsim-firstbar="true">'
            + STYLE + "".join(panels) + "</div>")
