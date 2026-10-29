"""Compact CP-only presentation of validated SIM views; no model or storage IO."""

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from html import escape, unescape
import json
import re
from zoneinfo import ZoneInfo

from .completed_ui import LEGS, completed_comparison_view
from .firstbar_display import firstbar_tables
from .realized_display import render_realized_display, unknown_realized_view


# Accepted run identities from the retained installed three-run census, not paths.
FIRST_SOURCE = "firstbar-study:fac9c0fc0efd00f15a00776f869eafb28b4010f47c57dfc86e0a105357c93e2d"
AGGREGATE_SOURCE = "aggregate-entry:265b847b615df1efbb85cbf3b053bd9d597198309bdebd89d270ca458b0698e8"
LEGACY_SOURCE = "real4-retained-v3-main-io-20261023"
FIRST_DAY = date(2026, 9, 22)
LEGACY_DAYS = (date(2026, 10, 12), date(2026, 10, 21))
COLUMNS = ("Ticker", "Path", "Qty", "Entry Time", "Entry", "Exit Time", "Exit", "P&L", "Exit reason")
HEADER = '<tr>' + ''.join('<th' + (' style="text-align:right"' if name in {"Qty", "Entry", "Exit"} else '')
    + '>' + escape(name) + '</th>' for name in COLUMNS) + '</tr>'
CAPTION = "Simulation - not real trades; never in live P&L."
# The one table of REAL recorded trades must never wear the simulation badge (operator 10-27).
REAL_MARKER = "LIVE:ACTUAL"
REAL_CAPTION = "Real recorded trades - comparison only; never added to live P&L."
REAL_BADGE = "REAL RECORDED TRADES"
SIM_BADGE = "SIMULATION - NOT REAL TRADES"
COLUMN_WIDTHS = (6, 10, 5, 11, 8, 11, 8, 17, 24)
COLGROUP = '<colgroup>' + ''.join('<col style="width:' + str(width) + '%">'
    for width in COLUMN_WIDTHS) + '</colgroup>'
# Operator-approved 10-27 plain-English table descriptions, checked against the SIM code.
DESCRIPTIONS = {
    "FIRSTBAR": "Simulation, not real trades. After a buy flip, buys at the first trade at or above the flip"
        " bar's high + 1 cent. Exits: target +5%, stop -8%, sell flip or session end.",
    "FIRSTBAR+2min": "The same simulation plus the 2-minute rule: a trade that is not up 1% within"
        " 2 minutes is sold.",
    "Live replay": "Our real live buys (recorded order fill price), with the exits simulated by the same"
        " rules as FIRSTBAR. Shows what our real entries would make with those exits. Not real P&L.",
    "Recorded LIVE comparison": "Our real recorded live trades on the stored comparison days, shown next"
        " to the three SIM tables below. Never added to live P&L; UNKNOWN means not matched to a broker fill.",
    "SIM TODAY": "Simulation of the same signals with today's entry (the real fill where the bot bought)"
        " and simulated exits; the Exit reason column shows which rule closed each trade.",
    "SIM VOLBODY / Queue aware": "Simulation of the VOLBODY rule: buy only when the price is inside the"
        " bar's volume body. Fills assume we wait in line behind other buyers (the realistic case).",
    "SIM VOLBODY / Optimistic": "The same VOLBODY rule, but fills assume we are first in line (the best case).",
}
OVERRIDES_DESCRIPTION = "Extra data sources the replays can read; for checking only, they never change trading."
DAY_FILTER_STYLE = ('#atr-sim-compact .sim-desc{padding:2px 0 0;color:#98a6c8;font-size:11px;'
    'line-height:1.35;max-width:900px}'
    '#atr-sim-compact .sim-dayfilter{display:flex;flex-wrap:wrap;align-items:center;gap:12px;'
    'margin:0 0 12px;font-size:12px}'
    '#atr-sim-compact .sim-dayfilter small{color:#98a6c8}')
# Presentation only: every row stays in the HTML; the browser hides rows not on the chosen ET day.
DAY_FILTER = ('<div class="sim-dayfilter" data-sim-dayfilter="true">'
    '<label>Show trades from <input type="date" id="atr-sim-day"></label>'
    '<label><input type="checkbox" id="atr-sim-all"> All dates</label>'
    '<small>Default is today (ET). The boxes above total every date in the window.</small></div>')
# Placed after every table: it must run once the rows exist.
DAY_FILTER_SCRIPT = ('<script type="text/javascript">(function(){'
    'var root=document.getElementById("atr-sim-compact");if(!root){return;}'
    'var day=document.getElementById("atr-sim-day"),all=document.getElementById("atr-sim-all");'
    'var today=new Intl.DateTimeFormat("en-CA",{timeZone:"America/New_York",year:"numeric",'
    'month:"2-digit",day:"2-digit"}).format(new Date());day.value=today;'
    'function rowDay(row){var cells=[row.querySelector(\'td[data-sim-time="entry"]\'),'
    'row.querySelector(\'td[data-sim-time="exit"]\')];for(var i=0;i<cells.length;i++){'
    'var m=cells[i]&&/^(\\d{4}-\\d{2}-\\d{2})/.exec(cells[i].getAttribute("title")||"");'
    'if(m){return m[1];}}return "";}'
    'function apply(){var want=all.checked?"":(day.value||today);'
    'root.querySelectorAll("section[data-sim-mode] tbody").forEach(function(body){'
    'var old=body.querySelector("tr[data-sim-day-empty]");if(old){old.remove();}'
    'var shown=0,hidden=0;body.querySelectorAll("tr").forEach(function(row){'
    'if(!row.querySelector("td[data-sim-time]")){return;}'
    'var keep=!want||rowDay(row)===want;row.style.display=keep?"":"none";'
    'if(keep){shown++;}else{hidden++;}});'
    'if(!shown&&hidden){var tr=document.createElement("tr");tr.setAttribute("data-sim-day-empty","true");'
    'var td=document.createElement("td");td.colSpan=9;td.textContent="No trades on "+want'
    '+" in this table ("+hidden+" on other dates; tick All dates to see them).";'
    'tr.appendChild(td);body.appendChild(tr);}});}'
    'day.addEventListener("change",function(){all.checked=false;apply();});'
    'all.addEventListener("change",apply);if(document.readyState==="loading"){'
    'document.addEventListener("DOMContentLoaded",apply);}else{apply();}})();</script>')


def page_scopes(port, firstbar=None, aggregate=None, legacy=None):
    """Only an accepted configured port enables defaults; overrides stay explicit.

    Standalone API routes and an unconfigured CP retain their no-source behavior.
    Two separate one-day legacy reads preserve the existing seven-day read cap.
    """
    firstbar, aggregate, legacy = (dict(value or {}) for value in (firstbar, aggregate, legacy))
    configured = getattr(getattr(port, "binding", None), "source_id", None) == AGGREGATE_SOURCE
    if not configured:
        return firstbar, aggregate, [legacy]
    for scope, source in ((firstbar, FIRST_SOURCE), (aggregate, AGGREGATE_SOURCE)):
        if scope.get("source_id") is None:
            scope["source_id"] = source
        if scope.get("day") is None:
            scope["day"] = FIRST_DAY
        scope.setdefault("days", 32)
    if legacy.get("source_id") is not None:
        return firstbar, aggregate, [legacy]
    legacy["source_id"] = LEGACY_SOURCE
    if legacy.get("day") is None and legacy.get("days", 1) == 1:
        return firstbar, aggregate, [{**legacy, "day": day, "days": 1} for day in LEGACY_DAYS]
    if legacy.get("day") is None:
        legacy["day"] = LEGACY_DAYS[0]
    return firstbar, aggregate, [legacy]


def _money(label):
    label = str(label)
    return label[1] + "$" + label[2:] if label.startswith(("$+", "$-")) else label


def stat_tile(title, amount, *, detail="", tooltip="", realized=False):
    # Retain the projector's canonical label alongside the compact visible format.
    return ('<div class="hero-card"' + (' data-realized-reconciliation="true"' if realized else '')
        + ' data-pnl-label="' + escape(str(amount), quote=True) + '" title="'
        + escape(tooltip, quote=True) + '"><span>'
        + escape(title) + '</span><strong>' + escape(_money(amount)) + '</strong>'
        + ('<small>' + escape(detail) + '</small>' if detail else '') + '</div>')


def _firstbar_outcome_audit(view, table):
    """Account for non-trades in a collapsed audit, never as execution rows."""
    trades = iter(table["rows"])
    seen, output = set(), []
    for outcome in view["outcomes"]:
        if outcome["mode"] != table["mode"]:
            continue
        outcome_id, kind = outcome["outcome_id"], outcome["kind"]
        if outcome_id in seen:
            raise ValueError("duplicate_firstbar_mode_outcome_id")
        seen.add(outcome_id)
        if kind == "TRADE":
            trade = next(trades, None)
            if trade is None:
                raise ValueError("firstbar_trade_projection_missing")
        output.append(dict(outcome_id=outcome_id, kind=kind, model=outcome["model"],
            identity=deepcopy(outcome["identity"]), reason=str(outcome.get("reason") or "UNKNOWN")))
    if next(trades, None) is not None:
        raise ValueError("firstbar_trade_projection_unconsumed")
    return output


def _compact_time(label):
    for pattern in ("%Y-%m-%d %I:%M:%S %p ET", "%Y-%m-%d %H:%M:%S ET"):
        try:
            return datetime.strptime(label, pattern).strftime("%m-%d %H:%M")
        except ValueError:
            pass
    return label


def _sim_row(row, render_row):
    """Keep the canonical row bytes except the two SIM-only time cells."""
    rendered = render_row(deepcopy(row))
    cells = list(re.finditer(r'(<td\b[^>]*>)(.*?)(</td>)', rendered, re.S))
    if len(cells) != len(COLUMNS):
        raise ValueError("nine_canonical_SIM_cells_required")
    for index, field in ((5, "exit"), (3, "entry")):
        cell = cells[index]
        full = unescape(cell[2])
        opening = cell[1][:-1] + ' data-sim-time="' + field + '" title="' + escape(full, quote=True) + '">'
        replacement = opening + escape(_compact_time(full)) + cell[3]
        rendered = rendered[:cell.start()] + replacement + rendered[cell.end():]
    if row.get("coverage_note") is not None:
        note = row["coverage_note"]
        if not isinstance(note, str) or not 0 < len(note) <= 2048:
            raise ValueError("bounded_plain_coverage_note_required")
        summary = list(re.finditer(r'(<td\b[^>]*>)(.*?)(</td>)', rendered, re.S))[8]
        replacement = (summary[1] + summary[2]
            + '<br><small class="sim-row-coverage">' + escape(note) + '</small>' + summary[3])
        rendered = rendered[:summary.start()] + replacement + rendered[summary.end():]
    return rendered


def panel(title, rows, *, render_row, tooltip="", marker="", caption=CAPTION, outcomes=None,
          description=None, updated_text=None, warning_text=None):
    description = DESCRIPTIONS.get(title, "") if description is None else description
    real = marker == REAL_MARKER
    if real and caption == CAPTION:
        caption = REAL_CAPTION
    body = ''.join(_sim_row(row, render_row) for row in rows)
    metadata, audit = "", ""
    if outcomes is not None:
        metadata = ' data-firstbar-outcomes="' + escape(json.dumps([
            {key: item[key] for key in ("outcome_id", "kind", "model", "identity")} for item in outcomes],
            separators=(",", ":")), quote=True) + '"'
        skipped = [item for item in outcomes if item["kind"] == "SKIP"]
        unresolved = [item for item in outcomes if item["kind"] == "UNKNOWN"]
        if skipped or unresolved:
            line = str(len(skipped)) + " signals skipped (no trade)"
            if unresolved:
                line += "; " + str(len(unresolved)) + " unresolved (UNKNOWN)"
            audit = '<details class="sim-outcome-audit"><summary>' + escape(line) + '</summary><ul>'
            for item in (*skipped, *unresolved):
                identity = " / ".join(str(item["identity"][key]) for key in (
                    "trading_day", "symbol", "broker", "account_id", "strategy", "flip_id", "segment_id"))
                audit += '<li data-firstbar-kind="' + escape(item["kind"], quote=True)
                audit += '" data-firstbar-outcome-id="' + escape(item["outcome_id"], quote=True) + '">'
                audit += escape(identity + " / " + item["kind"] + ": " + item["reason"]) + '</li>'
            audit += '</ul></details>'
    if not body:
        body = '<tr><td colspan="9">No recorded rows in this view; not evidence of zero trades.</td></tr>'
    return ('<section class="panel full" data-sim-mode="' + escape(marker, quote=True)
        + '"' + metadata + (' data-aggregate-entry-sim="true"' if marker == "AGGREGATE_ENTRY" else '')
        + '><div class="panel-header"><div><h3>' + escape(title) + '</h3>'
        + '<div class="sub" title="' + escape(tooltip, quote=True) + '">' + escape(caption)
        + '</div>' + ('<div class="sim-desc">' + escape(description) + '</div>' if description else '')
        + ('<div class="sim-updated">' + escape(updated_text) + '</div>' if updated_text is not None else '')
        + ('<div class="sim-stale" role="status">' + escape(warning_text) + '</div>' if warning_text else '')
        + '</div><span class="count ' + ('green' if real else 'pink') + '" style="font-size:10px">'
        + (REAL_BADGE if real else SIM_BADGE) + '</span>'
        + '</div><div class="table-wrap"><table>' + COLGROUP + '<thead>' + HEADER + '</thead><tbody>'
        + body + '</tbody></table></div>' + audit + '</section>')


def _clock(facts, *, last=False):
    clocks = [(item["at"].get("event_ns") if isinstance(item, dict) and isinstance(item.get("at"), dict)
        else None) for item in facts]
    if not clocks or any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in clocks):
        return "UNKNOWN"
    stamp = max(clocks) if last else min(clocks)
    return datetime.fromtimestamp(stamp // 1_000_000_000, timezone.utc).astimezone(
        ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S ET")


def _prices(facts):
    # Preserve individual receipt prices: never invent a VWAP or an execution.
    return "; ".join("$" + str(item["price"]) for item in facts
        if isinstance(item, dict) and item.get("price") is not None) or "UNKNOWN"


def _legacy_row(receipt, leg):
    entries = receipt.get("entries" if receipt["mode"] == "LIVE" else "fills", [])
    exits = receipt.get("exits", [])
    quantity = (sum(item["quantity"] for item in entries)
        if entries and all(isinstance(item, dict) and type(item.get("quantity")) is int
            and item["quantity"] > 0 for item in entries)
        else "UNKNOWN")
    known = leg is not None and leg["completed"]
    amount = leg["known_pnl"] if known else None
    reasons = [(item.get("mapped_reason") or item.get("reason") or item.get("raw_reason") or "UNKNOWN")
        if isinstance(item, dict) else "UNKNOWN"
        for item in exits]
    return dict(ticker=receipt["identity"]["symbol"], path=receipt["identity"]["broker"],
        quantity=str(quantity), entry_time=_clock(entries), entry_price=_prices(entries),
        exit_time=_clock(exits, last=True), exit_price=_prices(exits),
        pnl=amount, pnl_label_known=known,
        pnl_label=("$" + format(Decimal(str(amount)), "+.2f")) if known else "UNKNOWN",
        pnl_pct_label="UNKNOWN", summary="; ".join(reasons) or receipt.get("decision", "UNKNOWN"))


def legacy_panels(contexts, *, render_row):
    rows = {key: [] for key in LEGS}
    seen, sources = set(), []
    for context in contexts:
        view = context["view"]
        completed = completed_comparison_view(view, copy_receipts=False)
        indexed = {}
        for group in completed["rows"]:
            for key, leg in zip(LEGS, group["legs"]):
                indexed[(tuple(group["identity"].items()), key)] = leg
        sources.append(str((view.get("source") or {}).get("source_id", "unconfigured")))
        for receipt in view["rows"]:
            key = (receipt["mode"], receipt["model"])
            identity = tuple((name, receipt["identity"][name]) for name in (
                "trading_day", "symbol", "broker", "account_id", "strategy", "flip_id", "segment_id"))
            unique = (sources[-1], identity, key)
            if unique in seen:
                raise ValueError("duplicate_compact_legacy_identity")
            seen.add(unique)
            rows[key].append(_legacy_row(receipt, indexed.get((identity, key))))
    names = ("Recorded LIVE comparison", "SIM TODAY", "SIM VOLBODY / Queue aware", "SIM VOLBODY / Optimistic")
    return ('<div data-legacy-sim-mount="true" id="atr-simulation"><div id="atr-simulation-completed">'
        + ''.join(panel(title, rows[key], render_row=render_row, marker=":".join(key),
            tooltip="; ".join(sources) + ". Selected accounts, not a historical denominator; native proof UNKNOWN. "
            "Retained comparison, not live totals. Individual fill prices; no inferred VWAP.")
            for key, title in zip(LEGS, names)) + '</div></div>')


def _overrides(firstbar, aggregate, legacy):
    fields = []
    for label, prefix, scope, default_day, max_days in (
        ("FIRSTBAR", "", firstbar, FIRST_DAY, 32),
        ("Live replay", "aggregate_", aggregate, FIRST_DAY, 32),
        ("Legacy", "legacy_", legacy[0], LEGACY_DAYS[0], 7),
    ):
        fields.append('<label>' + label + ' source <input name="' + prefix + 'source_id" maxlength="128" value="'
            + escape(scope.get("source_id") or "", quote=True) + '"></label>'
            + '<label>From <input name="' + prefix + 'day" type="date" value="'
            + (scope.get("day") or default_day).isoformat() + '"></label>'
            + '<label>Days <input name="' + prefix + 'days" type="number" min="1" max="'
            + str(max_days) + '" value="' + str(scope.get("days", max_days if max_days == 32 else 1))
            + '"></label>')
    return ('<details class="panel full sim-overrides"><summary>Optional replay sources</summary>'
        '<div class="sim-desc">' + escape(OVERRIDES_DESCRIPTION) + '</div>'
        '<form method="get" action="/bot/1m-schwab-v2" '
        'onsubmit="for(const field of this.elements){if(field.name &amp;&amp; !field.value){field.disabled=true;}}">'
        + ''.join(fields)
        + '<button type="submit">Apply overrides</button> <a href="/bot/1m-schwab-v2">Reset to accepted</a>'
        '</form></details>')


def render_page(firstbar, aggregate, legacy, *, render_row, scopes):
    """Called off-loop with detached, validated display views only."""
    from .aggregate_entry_display import render_aggregate_entry_table

    tables = firstbar_tables(firstbar["view"])
    realized = aggregate.get("realized") or unknown_realized_view()
    render_realized_display(realized)  # Keep the existing display validation/UNKNOWN guard.
    realized_known = realized.get("status") == "BOUND_COMPLETE_SIGNAL_GROSS" and realized.get("receipt_sha256") is not None
    realized_amount = (format(Decimal(realized["complete_signal_gross_pnl"]), "+.2f")
        if realized_known else None)
    realized_label = realized_amount[0] + "$" + realized_amount[1:] if realized_known else "Under reconciliation"
    aggregate_view = aggregate["view"]
    tiles = [stat_tile(label, table["totals"]["known_pnl_label"], tooltip=table["source"]["source_id"])
        for table, label in zip(tables, ("FIRSTBAR", "FIRSTBAR+2min"))]
    tiles.append(stat_tile("Live replay", aggregate_view["known_pnl_label"] if aggregate_view else "UNKNOWN",
        tooltip=aggregate.get("source_id") or "No reviewed aggregate source"))
    tiles.append(stat_tile("Realized", realized_label,
        detail=(str(realized["matched_signals"]) + " complete / " + str(realized["unmatched_signals"])
            + " unreconciled") if realized_known else "",
        tooltip="Receipt SHA256: " + str(realized.get("receipt_sha256")) + "; gross, fees not included; never added to LIVE totals.",
        realized=True))
    first_panels = '<div data-firstbar-mount="true" data-atrsim-firstbar="true">' + ''.join(
        panel(label, [row["row"] for row in table["rows"]], render_row=render_row,
        outcomes=_firstbar_outcome_audit(firstbar["view"], table),
        marker=table["mode"], tooltip=table["source"]["source_id"]
        + "; read status " + str(firstbar.get("read_status", "UNKNOWN"))
        + "; in-sample replay; reference differences under review; known requested-window totals only. "
        "Skipped signals have no trade; unresolved signals remain UNKNOWN.")
        for table, label in zip(tables, ("FIRSTBAR", "FIRSTBAR+2min"))) + '</div>'
    aggregate_html = (render_aggregate_entry_table(aggregate_view, render_row=render_row,
        column_header=HEADER, compact=True) if aggregate_view is not None else
        panel("Live replay", [], render_row=render_row, marker="AGGREGATE_ENTRY",
            tooltip="No reviewed baseline selected; economics UNKNOWN."))
    return ('<div id="atr-sim-compact" class="full" data-l14-ui="compact">'
        '<style>#atr-sim-compact{min-width:0;max-width:100%;grid-column:1/-1}'
        '#atr-sim-compact .panel{min-width:0;max-width:100%}'
        '#atr-sim-compact .table-wrap{max-width:100%;overflow:auto}'
        '#atr-sim-compact table{min-width:760px;width:100%;table-layout:fixed}'
        '#atr-sim-compact th,#atr-sim-compact td{box-sizing:border-box;overflow:hidden;text-overflow:ellipsis}'
        '#atr-sim-compact .sim-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}'
        '#atr-sim-compact .panel-header{flex-wrap:wrap}'
        '#atr-sim-compact .sim-outcome-audit{padding:8px 16px;color:#98a6c8;font-size:11px}'
        '#atr-sim-compact .sim-outcome-audit ul{max-height:240px;overflow:auto;overflow-wrap:anywhere}'
        '#atr-sim-compact .sim-overrides{padding:12px 16px}'
        '#atr-sim-compact form{display:flex;flex-wrap:wrap;gap:8px;max-width:100%;padding-top:10px}'
        '#atr-sim-compact input{max-width:100%;box-sizing:border-box}'
        '@media(max-width:720px){#atr-sim-compact .sim-stats{grid-template-columns:repeat(2,minmax(0,1fr))}'
        '#atr-sim-compact .count{white-space:normal}}' + DAY_FILTER_STYLE + '</style>'
        '<div class="sim-stats">' + ''.join(tiles) + '</div>' + DAY_FILTER + first_panels + aggregate_html
        + legacy_panels(legacy, render_row=render_row) + _overrides(*scopes) + DAY_FILTER_SCRIPT + '</div>')
