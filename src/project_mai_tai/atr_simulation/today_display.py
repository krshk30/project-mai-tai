"""TODAY-only SIM page, over detached canonical rows; no history/model reads."""

from datetime import datetime
from html import escape

from .cp_sim_display import DESCRIPTIONS, panel
from .today_file import ET, ROW_FIELDS, TABLES


def _clock(value, pattern):
    if value is None:
        return "UNKNOWN"
    return datetime.fromisoformat(value).astimezone(ET).strftime(pattern)


def render_today_page(context, *, render_row):
    panels = []
    for key, title, marker in TABLES:
        table = context["tables"][key]
        updated, source = table.get("updated_at"), table.get("source_asof")
        clocks = [datetime.fromisoformat(value) for value in (updated, source) if value is not None]
        oldest = min(clocks).isoformat() if len(clocks) == 2 else None
        warning = None
        if table["display_status"] != "AVAILABLE":
            if table["display_status"] == table["read_status"] == "UNKNOWN" and len(clocks) == 2:
                warning = "Simulation coverage UNKNOWN"
            else:
                warning = ("simulation not updated since " + _clock(oldest, "%H:%M")
                           + " ET · " + table["display_status"])
        if table.get("issues"):
            warning = (warning + ": " if warning else "Source / parity notes: ") + "; ".join(table["issues"])
        counts = ("Skipped: " + str(table.get("skipped_count") if table.get("skipped_count") is not None else "UNKNOWN")
                  + "; unresolved: " + str(table.get("unknown_count") if table.get("unknown_count") is not None else "UNKNOWN"))
        if table.get("truncated"):
            counts += "; truncated source — not complete economics"
        description = DESCRIPTIONS.get(title, "")
        if key == "LIVE:ACTUAL":
            description = ("Our real recorded live trades today, shown next to the SIM tables. "
                           "Never added to live P&L; UNKNOWN means not matched to a broker fill.")
        tooltip = (table.get("source_id", "UNKNOWN") + "; " + counts
                   + "; known rows only; native execution qualification UNKNOWN")
        if table.get("issues"):
            tooltip += "; " + "; ".join(table["issues"])
        # Only the existing visible row contract enters the escaped HTML row
        # renderer. Optional raw receipt metadata stays intact in file/context,
        # without deep-copying unused model trees on every page request.
        render_rows = []
        for row in table["rows"]:
            display = {field: row[field] for field in (*ROW_FIELDS, "pnl", "pnl_label_known", "coverage_note")
                       if field in row}
            if key != "LIVE:ACTUAL":
                # Labels are a detached view; canonical USD and REAL rows stay
                # unchanged. Missing percent evidence is never recalculated.
                display["pnl_label"] = row["pnl_pct_label"] if row.get("pnl_pct_label_known") is True else "UNKNOWN"
                display["pnl_pct_label"] = "USD " + row["pnl_label"]
                display["summary"] += ("; " + row["sizing_status"] + "; " + row["percent_basis"]
                                       if "sizing_status" in row else "; percent evidence UNKNOWN (legacy row)")
                display["coverage_note"] += ("; line recomputed from stored bars" if "pnl_pct_label_known" in row
                                             else "; line provenance UNKNOWN (legacy row)")
                display["coverage_note"] += "; actual live flip UNKNOWN unless matched by actual v2 log evidence"
                if "run_receipt" in context and "signal_time" in row:
                    display["summary"] += ("; signal " + row["signal_time"] + "; status " + row["status"]
                                           + "; " + row["label"])
            render_rows.append(display)
        panels.append(panel(title, render_rows, render_row=render_row, marker=marker,
            description=description, tooltip=tooltip,
            updated_text=("Last updated " + _clock(updated, "%H:%M:%S") + " ET; inputs as of "
                          + _clock(source, "%H:%M:%S") + " ET · " + counts
                          + "; known-row P&L: " + table.get("known_pnl_label", "UNKNOWN")),
            warning_text=warning))
    content = ('<div data-firstbar-mount="true" data-atrsim-firstbar="true">'
               + ''.join(panels[:2]) + '</div>' + panels[2]
               + '<div data-legacy-sim-mount="true" id="atr-simulation">'
               + '<div id="atr-simulation-completed">' + ''.join(panels[3:]) + '</div></div>')
    receipt = context.get("run_receipt")
    run_notes = ""
    if receipt is not None:
        facts = ["C1-reported " + fact["symbol"] + " " + fact["reference"] + " "
                 + fact["reference_direction"] + ": " + fact["status"] for fact in receipt["comparisons"]]
        run_notes = ('<section class="sim-run-receipt"><h3>Run summary</h3><p>'
            + escape(str(receipt["counts"]["symbols"]) + " symbols; UNKNOWN counts by cause: "
                     + "; ".join(cause + "=" + str(count) for cause, count in receipt["unknown_counts_by_cause"].items()))
            + '</p><ul>' + ''.join('<li>' + escape(fact) + '</li>' for fact in facts)
            + ('<li>Parity facts UNKNOWN: no supplied comparisons; not a 0/0 PASS.</li>' if not facts else '')
            + '</ul></section>')
    return ('<div id="atr-sim-compact" class="full" data-l14-ui="compact" data-atrsimlive1="today-only">'
        '<style>#atr-sim-compact{min-width:0;max-width:100%;grid-column:1/-1}'
        '#atr-sim-compact .panel{min-width:0;max-width:100%;margin-bottom:16px}'
        '#atr-sim-compact .table-wrap{max-width:100%;overflow:auto}'
        '#atr-sim-compact table{min-width:760px;width:100%;table-layout:fixed}'
        '#atr-sim-compact th,#atr-sim-compact td{box-sizing:border-box;overflow:hidden;text-overflow:ellipsis}'
        '#atr-sim-compact .panel-header{flex-wrap:wrap}'
        '#atr-sim-compact .sim-updated{font-size:11px;color:#98a6c8;margin-top:6px}'
        '#atr-sim-compact .sim-stale{color:#ffb74d;font-size:12px;margin-top:6px}'
        '#atr-sim-compact .sim-row-coverage{display:block;white-space:normal;overflow-wrap:anywhere;'
        'line-height:1.35;font-size:10px;color:#98a6c8;margin-top:4px}'
        '#atr-sim-compact .count{white-space:normal}</style>'
        '<h2>Today · ' + escape(context["trading_day"]) + '</h2>'
        '<p>Automatic refresh; simulations are not real trades or live P&amp;L.</p>'
        '<p>SIM: percent per trade first, USD secondary. Line recomputed from stored bars; '
        'actual live flip UNKNOWN unless supported by actual v2 log evidence. '
        'Parity references are C1-reported BUY flips, not native verification; '
        'all comparisons and mismatches appear in source / parity notes.</p>'
        + content + run_notes + '</div>')
