"""Inert FIRSTBAR presentation port, not an engine-result or allocation adapter.

The caller projects canonical allocated results into this bounded display tree.
That projector is a separate dependency: a planner candidate cannot be passed as
a trade. No prices, exits, PnL, coverage, or skip decisions are reconstructed here.
Main supplies the unchanged LIVE row/totals formatters when mounting this port.
"""

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html import escape


DISPLAY_CONTRACT = "ATRSIM_FIRSTBAR_DISPLAY_V1"
IDENTITY_FIELDS = ("trading_day", "symbol", "broker", "account_id", "strategy",
                   "flip_id", "segment_id")
WORLDS = (("FIRSTBAR", "First-bar-high"),
          ("FIRSTBAR_X3", "First-bar-high + 2-min"))
COLUMNS = ("Ticker", "Path", "Qty", "Entry Time", "Entry", "Exit Time",
           "Exit", "P&L", "Exit Summary")
COLUMN_HEADER = '<tr><th>Ticker</th><th>Path</th><th style="text-align:right">Qty</th><th>Entry Time</th><th style="text-align:right">Entry</th><th>Exit Time</th><th style="text-align:right">Exit</th><th>P&amp;L</th><th>Exit Summary</th></tr>'
ROW_FIELDS = ("ticker", "path", "quantity", "entry_time", "entry_price",
              "exit_time", "exit_price", "pnl_label", "pnl_pct_label", "summary")
MAX_OUTCOMES = 10000
MAX_DISPLAY_ROWS = 25
MAX_TEXT = 2048


def _require(condition):
    if not condition:
        raise ValueError("firstbar_display_contract_required")


def _text(value):
    _require(isinstance(value, str) and 0 < len(value) <= MAX_TEXT)
    return value


def _sequence(value):
    _require(isinstance(value, (list, tuple)))
    return value


def firstbar_tables(view):
    """Copy presentation facts; shape checks do NOT certify engine provenance.

All outcomes in the known page remain accounted for: allocated trades are rows,
explicit skips are footer counts, and unresolved outcomes are UNKNOWN counts.
The canonical projector, not this formatter, proves allocation and skip coverage.
"""
    _require(isinstance(view, Mapping) and view.get("contract") == DISPLAY_CONTRACT)
    source = view.get("source")
    _require(isinstance(source, Mapping))
    for field in ("source_id", "policy_version", "engine_dependency_head", "population_label"):
        _text(source.get(field))
    _require(type(view.get("truncated")) is bool)
    _require(view.get("coverage_status") in ("COMPLETE", "UNKNOWN", "UNAVAILABLE"))
    outcomes = _sequence(view.get("outcomes"))
    _require(len(outcomes) <= MAX_OUTCOMES)
    totals = view.get("totals")
    _require(isinstance(totals, Mapping) and set(totals) == {mode for mode, _ in WORLDS})
    tables = {mode: {"mode": mode, "title": title, "rows": [],
                    "skip_counts": Counter(), "unknown_count": 0, "issues": []}
              for mode, title in WORLDS}
    seen, trades = set(), set()
    for outcome in outcomes:
        _require(isinstance(outcome, Mapping))
        mode = outcome.get("mode")
        _require(isinstance(mode, str) and mode in tables)
        identity = outcome.get("identity")
        _require(isinstance(identity, Mapping))
        identity_key = tuple(_text(identity.get(field)) for field in IDENTITY_FIELDS)
        model = _text(outcome.get("model"))
        key = (mode, model, identity_key, _text(outcome.get("outcome_id")))
        _require(key not in seen)
        seen.add(key)
        kind = outcome.get("kind")
        _require(kind in ("TRADE", "SKIP", "UNKNOWN"))
        table = tables[mode]
        issues = _sequence(outcome.get("issues", ()))
        _require(len(issues) <= 32)
        if issues:
            table["issues"].append({"outcome_id": outcome["outcome_id"],
                                    "values": tuple(_text(issue) for issue in issues)})
        if kind != "TRADE":
            _require("row" not in outcome and "accepted_quantity" not in outcome)
            if kind == "SKIP":
                table["skip_counts"][_text(outcome.get("reason"))] += 1
            else:
                table["unknown_count"] += 1
            continue
        quantity = outcome.get("accepted_quantity")
        _require(type(quantity) is int and quantity > 0)
        trade_id = _text(outcome.get("trade_id"))
        trade_key = (mode, model, identity_key, trade_id)
        _require(trade_key not in trades)
        trades.add(trade_key)
        _require(outcome.get("status") in ("CLOSED", "OPEN", "UNKNOWN"))
        refs = _sequence(outcome.get("allocation_refs"))
        _require(bool(refs))
        for reference in refs:
            _text(reference)
        row = outcome.get("row")
        _require(isinstance(row, Mapping))
        # A fixed display-key projection cannot smuggle unrelated live-row fields.
        copied = {field: _text(row.get(field)) for field in ROW_FIELDS}
        _require(copied["ticker"] == identity["symbol"])
        _require(copied["quantity"] in (str(quantity), format(quantity, ",")))
        _require(type(row.get("pnl_label_known")) is bool)
        copied["pnl_label_known"] = row["pnl_label_known"]
        _require(outcome["status"] == "CLOSED" or not copied["pnl_label_known"])
        if not copied["pnl_label_known"]:
            _require(copied["pnl_label"] == "UNKNOWN" and copied["pnl_pct_label"] == "UNKNOWN")
            _require(row.get("pnl") is None)
        else:
            _require(not isinstance(row.get("pnl"), bool) and isinstance(row.get("pnl"), (str, int, Decimal)))
            try:
                _require(Decimal(str(row["pnl"])).is_finite())
            except InvalidOperation as exc:
                raise ValueError("firstbar_display_contract_required") from exc
        # LIVE's formatter uses the copied numeric PnL for color, not economics.
        copied["pnl"] = row.get("pnl")
        table["rows"].append({"identity": deepcopy(dict(identity)),
            "trade_id": outcome["trade_id"], "model": model, "status": outcome["status"],
            "accepted_quantity": quantity, "allocation_refs": tuple(refs), "row": copied})
    output = []
    for mode, _ in WORLDS:
        table = tables[mode]
        total = totals[mode]
        _require(isinstance(total, Mapping))
        # Totals are supplied by the canonical read projection, never added here.
        _text(total.get("known_pnl_label"))
        _require(type(total.get("known_closed_trades")) is int and total["known_closed_trades"] >= 0)
        _require(total["known_closed_trades"] <= sum(
            row["status"] == "CLOSED" and row["row"]["pnl_label_known"] for row in table["rows"]))
        _require(type(total.get("complete_requested_window")) is bool)
        if total["complete_requested_window"]:
            _require(view["coverage_status"] == "COMPLETE" and not view["truncated"]
                     and table["unknown_count"] == 0
                     and total["known_closed_trades"] == len(table["rows"])
                     and all(row["status"] == "CLOSED" and row["row"]["pnl_label_known"]
                             for row in table["rows"]))
        if total["known_closed_trades"] == 0:
            _require(total["known_pnl_label"] == "UNKNOWN")
        table.update(source=deepcopy(dict(source)), truncated=view["truncated"],
            coverage_status=view["coverage_status"], totals=deepcopy(dict(total)),
            skip_counts=dict(table["skip_counts"]))
        output.append(table)
    return tuple(output)


def render_firstbar_tables(view, *, render_row, render_totals):
    """Reuse caller-owned LIVE formatters on detached SIM-only presentation facts.

Formatters receive copies, no DB reader/service/repository is passed or opened.
The raw all-status API and existing VOLBODY section are not changed by this port.
"""
    tables = firstbar_tables(view)
    parts = ['<section data-atrsim-firstbar="true"><h2>SIMULATION - NOT REAL TRADES</h2>']
    for table in tables:
        source = table["source"]
        parts.append("<h3>" + escape(table["title"]) + "</h3><p>"
            + escape(source["population_label"]) + " | source " + escape(source["source_id"])
            + " | policy " + escape(source["policy_version"])
            + " | engine " + escape(source["engine_dependency_head"]) + "</p>")
        parts.append("<table><thead>" + COLUMN_HEADER + "</thead><tbody>")
        parts.extend(render_row(deepcopy(row["row"])) for row in table["rows"][:MAX_DISPLAY_ROWS])
        parts.append("</tbody></table>")
        parts.append(render_totals(deepcopy(table["totals"])))
        scope = "complete requested window" if table["totals"]["complete_requested_window"] else "known page only; not a global total"
        skips = ", ".join(reason + ": " + str(count) for reason, count in table["skip_counts"].items()) or "none in known page"
        parts.append("<p>Scope: " + escape(scope) + "; coverage " + escape(table["coverage_status"])
            + "; shown " + str(min(MAX_DISPLAY_ROWS, len(table["rows"]))) + " of " + str(len(table["rows"]))
            + " trade rows in known page"
            + "; truncated " + str(table["truncated"]).lower() + "; skipped: " + escape(skips)
            + "; UNKNOWN outcomes: " + str(table["unknown_count"]) + "</p>")
        for issue in table["issues"]:
            parts.append("<p>Receipt issues " + escape(issue["outcome_id"]) + ": "
                + escape("; ".join(issue["values"])) + "</p>")
    parts.append("</section>")
    return "".join(parts)
