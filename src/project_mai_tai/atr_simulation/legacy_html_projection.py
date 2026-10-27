"""Private HTML working projection, only after existing full stored validation.

Not a validation capability, raw API, read port, writer or cross-request cache.
"""

import math


LEGACY_RENDER_FIELDS = frozenset((
    "identity", "mode", "model", "decision", "source_id", "policy_version",
    "entries", "fills", "exits", "position", "position_contract", "coverage",
    "first_fill_ts", "vwap", "filled_qty", "remainder_cancel_reason",
    "gross_pnl", "pnl_difference", "comparison", "issues",
    "live_raw_reasons", "live_mapped_reasons", "clock_label",
    "condition_coverage", "execution_policy_label", "execution_policy", "live_lag_note",
))


def _check_owned_json_utf8(view):
    pending = [view]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if not str.isascii(value):
                str.encode(value, "utf-8", "strict")
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
        elif value is None or isinstance(value, (int, bool)):
            continue
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("nonfinite_owned_simulation_view")
        else:
            raise TypeError("normalized_owned_simulation_view_required")


def _legacy_html_projection(view):
    # Full stored normalization/validation and source guards MUST precede this.
    # Ensure unused metadata retains the old final strict UTF-8 refusal too.
    _check_owned_json_utf8(view)
    return {**view, "rows": [
        {key: value for key, value in row.items() if key in LEGACY_RENDER_FIELDS}
        for row in view["rows"]
    ]}
