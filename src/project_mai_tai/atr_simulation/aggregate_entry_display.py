"""Inert aggregate-entry table, separate from realized LIVE accounting.

The caller supplies independently projected display facts and the existing LIVE
row renderer. This module never opens storage, executes a model, or adds P&L.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html import escape

from .aggregate_entry_contract import LABEL, MODE, MODEL, VERSION


CONTRACT = "ATRSIM_AGGREGATE_ENTRY_DISPLAY_V1"
MAX_ROWS = 10000
SHOWN_ROWS = 25
ROW_FIELDS = ("ticker", "path", "quantity", "entry_time", "entry_price",
              "exit_time", "exit_price", "pnl_label", "pnl_pct_label", "summary")


def _text(value):
    if not isinstance(value, str) or not 0 < len(value) <= 2048:
        raise ValueError("bounded_aggregate_display_text_required")
    return value


def render_aggregate_entry_table(view, *, render_row, column_header, compact=False):
    """Only presentation: the canonical DB projector owns every result/status."""
    if (not isinstance(view, dict) or view.get("contract") != CONTRACT
            or view.get("mode") != MODE or view.get("model") != MODEL
            or view.get("version") != VERSION or view.get("history_label") != LABEL
            or view.get("read_status") not in {"AVAILABLE", "UNKNOWN", "UNAVAILABLE"}
            or type(view.get("truncated")) is not bool
            or not isinstance(view.get("rows"), list) or len(view["rows"]) > MAX_ROWS
            or type(view.get("unknown_count")) is not int or view["unknown_count"] < 0
            or view.get("global_pnl_qualified") is not False):
        raise ValueError("aggregate_display_contract_required")
    if view["read_status"] != "AVAILABLE" and (view["rows"] or view.get("known_pnl_label") != "UNKNOWN"):
        raise ValueError("unavailable_aggregate_cannot_present_trades_or_zero")
    source = _text(view.get("source_id"))
    subtotal = _text(view.get("known_pnl_label"))
    copied = []
    for row in view["rows"]:
        if not isinstance(row, dict) or type(row.get("pnl_label_known")) is not bool:
            raise ValueError("aggregate_display_row_required")
        projected = {key: _text(row.get(key)) for key in ROW_FIELDS}
        if not row["pnl_label_known"] and (
                projected["pnl_label"] != "UNKNOWN" or projected["pnl_pct_label"] != "UNKNOWN"
                or row.get("pnl") is not None):
            raise ValueError("unknown_aggregate_pnl_must_remain_unknown")
        if row["pnl_label_known"]:
            try:
                amount = row.get("pnl")
                if (isinstance(amount, bool) or not isinstance(amount, (str, int, Decimal))
                        or not Decimal(str(amount)).is_finite()):
                    raise ValueError("finite_aggregate_modeled_pnl_required")
            except InvalidOperation as error:
                raise ValueError("finite_aggregate_modeled_pnl_required") from error
        projected.update(pnl_label_known=row["pnl_label_known"], pnl=row.get("pnl"))
        copied.append(projected)
    if compact:
        from .cp_sim_display import panel
        return panel("Live replay", copied, render_row=render_row, marker="AGGREGATE_ENTRY",
            tooltip=source + "; " + LABEL + "; partial timing UNKNOWN; not actual realized P&L or a global total.")
    parts = ['<section data-aggregate-entry-sim="true"><h3>Live entries, modeled exits (same engine) · partial timing UNKNOWN</h3>',
        '<p>' + escape(LABEL) + '</p><p>Source: ' + escape(source)
        + '; storage: ' + escape(view["read_status"]) + '</p>',
        '<table><thead>' + column_header + '</thead><tbody>']
    parts.extend(render_row(deepcopy(row)) for row in copied[:SHOWN_ROWS])
    parts.extend(['</tbody></table><p>Known requested-page modeled P&amp;L: '
        + escape(subtotal) + '; not actual realized P&amp;L or a global total.</p>',
        '<p>Shown ' + str(min(SHOWN_ROWS, len(copied))) + ' of ' + str(len(copied))
        + '; UNKNOWN outcomes: ' + str(view["unknown_count"])
        + '; truncated: ' + str(view["truncated"]).lower() + '</p></section>'])
    return ''.join(parts)
