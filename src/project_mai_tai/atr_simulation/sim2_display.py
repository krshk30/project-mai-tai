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
TITLES = ("FIRSTBAR", "FIRSTBAR+2min", "LIVE rules SIM", "LIVE+2min SIM")
# Operator 10-29 20:0x: the names confused him — each table says plainly how it BUYS and how it SELLS.
# The keys above stay as stored in the day rows; only the heading and description shown change.
_LIVE_BUY = ("BUY like the live bot: waiting buy at the line +0.5% (limit +0.5% more), following the line every "
             "minute while short and kept after the flip until filled.")
_FB_BUY = "BUY at the first trade at or above the flip bar's high + 1 cent (skips the flip bar itself)."
_SELL = "SELL at target +5%, stop -8%, or the next sell flip"
_TWO_MIN = " — PLUS the 2-minute cut: a trade not up 1% within 2 minutes of the buy is sold."
SHOWN = {
    "FIRSTBAR": ("1 · FIRSTBAR buy", _FB_BUY + " " + _SELL + "."),
    "FIRSTBAR+2min": ("2 · FIRSTBAR buy + 2-minute cut", _FB_BUY + " " + _SELL + _TWO_MIN),
    "LIVE rules SIM": ("3 · LIVE bot buy", _LIVE_BUY + " " + _SELL
                       + ", or the confirmation exit (the bar after the buy does not close long)."),
    "LIVE+2min SIM": ("4 · LIVE bot buy + 2-minute cut", _LIVE_BUY + " " + _SELL
                      + ", or the confirmation exit" + _TWO_MIN),
}
NOTE = ("Simulations, not real trades: every momentum-scanner-confirmed buy flip today, $600 per trade. "
        "Tables 1–2 buy the FIRSTBAR way, tables 3–4 buy the LIVE bot way; tables 2 and 4 add the 2-minute cut.")
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
                '<h3>ATR simulation (4 tables)</h3><p>' + escape(note) + '</p></section></div>')
    generated = datetime.fromisoformat(doc["generated_at"]).astimezone(ET).strftime("%H:%M:%S")
    panels = []
    for title in TITLES:
        rows = doc["tables"].get(title, [])
        traded = [r for r in rows if r.get("status") in ("CLOSED", "OPEN")]
        skipped = len(rows) - len(traded)
        shown = traded
        total = sum(r.get("pnl", 0.0) for r in traded)
        # Operator 10-30: pre-market vs regular hours, every table, every day.
        pre = [r for r in traded if (r.get("entry_time") or "99") < "09:30:00"]
        pre_total = sum(r.get("pnl", 0.0) for r in pre)
        updated = (f"{doc['trading_day']} · updated {generated} ET · {len(traded)} trades, {skipped} signals without a trade"
                   f" · total ${total:+.2f} on ${doc.get('notional_usd', 600):.0f} per trade"
                   f" · pre-market {len(pre)} trades ${pre_total:+.2f} · regular hours {len(traded) - len(pre)} trades"
                   f" ${total - pre_total:+.2f}")
        heading, description = SHOWN[title]
        panels.append(panel(heading, [_display(r) for r in shown], render_row=render_row, marker=title,
                            updated_text=updated, description=description))
    return ('<div id="atr-sim2" class="full" data-atrsim2="day" data-firstbar-mount="true" data-atrsim-firstbar="true">'
            + STYLE + '<p class="sim-desc">' + escape(NOTE) + "</p>" + "".join(panels) + h2_panel(doc) + "</div>")


def _pct(value):
    return "—" if value is None else f"{value * 100:+.2f}%"


def h2_panel(doc):
    """Operator 10-30: the H2 inputs recorded at every LIVE+2min buy, so H2 can be checked every day.
    H2 (coach, frozen 10-30): skip when the other confirmed stocks' median 5-minute move is negative and this
    stock is doing no better. Shown only — the live bot does not use it."""
    rows = [r for r in doc["tables"].get("LIVE+2min SIM", []) if r.get("status") in ("CLOSED", "OPEN")]
    body, kept, vetoed, unknown = "", 0.0, 0.0, 0
    for r in rows:
        h2 = r.get("h2") or {"verdict": "UNKNOWN", "reason": "not recorded"}
        verdict = h2.get("verdict", "UNKNOWN")
        if verdict == "VETO":
            vetoed += r.get("pnl", 0.0)
        elif verdict == "UNKNOWN":
            unknown += 1
        if verdict != "VETO":
            kept += r.get("pnl", 0.0)
        body += ("<tr><td>" + escape(r.get("symbol", "")) + "</td><td>" + escape(str(r.get("entry_time", "")))
                 + "</td><td>" + f"${r.get('pnl', 0.0):+.2f}" + "</td><td>" + _pct(h2.get("target_ret"))
                 + "</td><td>" + _pct(h2.get("peer_median")) + "</td><td>" + escape(str(h2.get("peers", "—")))
                 + "</td><td>" + escape(verdict) + "</td><td>" + escape(h2.get("reason", "")) + "</td></tr>")
    if not body:
        body = '<tr><td colspan="8">No LIVE+2min trades yet today.</td></tr>'
    summary = (f"{len(rows)} trades · H2 would skip trades worth ${vetoed:+.2f} · trades H2 keeps ${kept:+.2f}"
               f" · {unknown} could not be checked")
    return ('<section class="panel full" data-sim-mode="H2 check"><div class="panel-header"><div><h3>'
            "5 · H2 check on the LIVE bot buy + 2-minute cut trades</h3>"
            '<div class="sim-desc">Simulation, not real trades. At each buy: this stock\'s last 5 minutes vs the median of'
            " the other scanner-confirmed stocks (confirmed 60 s or more). H2 would SKIP the buy when the others are"
            " falling and this stock is doing no better. Recorded every day; not used by the live bot.</div>"
            '<div class="sim-updated">' + escape(summary) + "</div></div></div>"
            '<div class="table-wrap"><table><thead><tr><th>Stock</th><th>Buy time</th><th>P&amp;L</th>'
            "<th>Stock 5-min</th><th>Others 5-min (median)</th><th>Others</th><th>H2</th><th>Note</th></tr></thead>"
            "<tbody>" + body + "</tbody></table></div></section>")
