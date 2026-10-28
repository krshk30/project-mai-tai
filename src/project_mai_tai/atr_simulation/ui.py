"""Additive read-only ATR section, never patches a live row or its totals."""

from html import escape


def render_simulation_section(view):
    def text(value):
        return escape("UNKNOWN" if value is None else str(value), quote=True)

    rendered = []
    def ordering(row):
        identity = row["identity"]
        return (identity["trading_day"], identity["symbol"], identity["broker"], identity["account_id"],
            identity["flip_id"], {"LIVE": 0, "TODAY": 1, "VOLBODY": 2}[row["mode"]], row["model"])

    for row in sorted(view["rows"], key=ordering):
        identity = row["identity"]
        exits = row.get("exits", [])
        reasons = [item.get("reason", item.get("raw_reason")) for item in exits]
        reason_text = ", ".join("UNKNOWN" if value is None else str(value) for value in reasons) or "OPEN / UNKNOWN"
        if row["mode"] != "LIVE":
            reason_text += " / LIVE raw: " + str(row.get("live_raw_reasons", "UNKNOWN"))
            reason_text += " / LIVE mapped: " + str(row.get("live_mapped_reasons", "UNKNOWN"))
        entries = row.get("fills", row.get("entries", []))
        entry_facts = "; ".join(str(item.get("quantity")) + " @ " + str(item.get("price"))
            + " / event_ns=" + str(item.get("print_key", item.get("at", {})).get("event_ns"))
            for item in entries) or "UNKNOWN / no entry"
        exit_facts = "; ".join(str(item.get("quantity")) + " @ " + str(item.get("price"))
            + " / event_ns=" + str((item.get("at") or {}).get("event_ns")) for item in exits) or "OPEN / UNKNOWN"
        rendered.append("<tr>" + "".join("<td>" + text(value) + "</td>" for value in (
            identity["trading_day"], identity["symbol"], identity["broker"], identity["account_id"],
            identity["flip_id"], row["mode"], row["model"], row["decision"], entry_facts, exit_facts,
            row.get("first_fill_ts"), row.get("vwap"), row.get("filled_qty"),
            row.get("remainder_cancel_reason"),
            reason_text,
            row["gross_pnl"], row.get("pnl_difference"), row.get("comparison"))) + "</tr>")
    if not rendered:
        rendered.append('<tr><td colspan="18">No simulation rows. Flip coverage is UNKNOWN, not NO_BUY.</td></tr>')
    return ('<section id="atr-simulation" aria-labelledby="atr-simulation-title">'
        '<h2 id="atr-simulation-title">SIMULATION - NOT REAL TRADES</h2>'
        '<p>Separate process and evidence ledger. Never included in live PnL. '
        'Retained LIVE facts are shown only for comparison; unknown values stay unknown.</p>'
        '<p>SIM: one position, current running VWAP anchors target/stop/floor; '
        '120 seconds starts at the first fill and never resets. Whole filled quantity exits; '
        'unfilled remainder cancels at first exit or next SELL, with no reopening. '
        'Fields below are engine receipts, not per-lot exit calculations.</p>'
        '<div style="overflow-x:auto"><table><thead><tr>'
        + "".join("<th>" + name + "</th>" for name in (
            "Day", "Symbol", "Broker", "Account", "Flip", "Mode", "Fill model", "Entry decision", "Entry facts", "Exit facts",
            "First fill (event ns)", "Current VWAP", "Filled quantity", "Remainder cancel reason",
            "Exit reason", "Known gross PnL", "Difference", "Comparison"))
        + "</tr></thead><tbody>" + "".join(rendered) + "</tbody></table></div>"
        + '<p>Coverage: ' + text(view["coverage_status"]) + "; truncated: " + text(view["truncated"])
        + ". Known-row subtotals are not full-population totals.</p></section>")

