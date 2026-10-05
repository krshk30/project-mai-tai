"""Offline Step 0 projection, not a broker replay or a live implementation."""

import argparse
import json
from collections import Counter
from decimal import Decimal, ROUND_CEILING

from project_mai_tai.strategy_core.v2_entry_sizing import resting_wire_limit, sized_entry_quantity


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture")
    args = parser.parse_args()
    with open(args.capture) as handle:
        raw = json.load(handle)
    rows = []
    counts = Counter()
    for order in raw["queries"]["orders"]:
        md = order["payload"]
        line = Decimal(str(md["cw_flip_level"]))
        rule = line * (1 + Decimal(str(md["resting_offset_pct"])) / 100)
        stop = rule.quantize(Decimal("0.01") if rule >= 1 else Decimal("0.0001"), rounding=ROUND_CEILING)
        leg = "webull" if order["account"] == "live:orb" else "schwab"
        old_stop = Decimal(str(md["stop_price"]))
        old_limit = Decimal(str(md["limit_price"]))
        if leg == "webull":
            old_wire_stop = old_stop.quantize(Decimal("0.01") if old_stop >= 1 else Decimal("0.0001"), rounding="ROUND_HALF_UP")
        else:
            old_wire_stop = Decimal(f"{float(old_stop):.2f}" if old_stop > 1 else f"{float(old_stop):.4f}")
        band = Decimal(str(md["resting_band_pct"]))
        # Keep the current limit FORMULA, not the previous limit NUMBER. This
        # projection is stated explicitly; no limit behaviour is silently claimed.
        new_raw_limit = (stop * (1 + band / 100)).quantize(Decimal("0.0001"))
        native = str(md.get("native_oco_bracket", "false")).lower() == "true"
        old_wire_limit = resting_wire_limit(old_stop, old_limit, leg=leg, native_schwab_bracket=native)
        new_wire_limit = resting_wire_limit(stop, new_raw_limit, leg=leg, native_schwab_bracket=native)
        notional = Decimal(str(md.get("entry_notional_target_usd", 0)))
        quantity = int(Decimal(order["quantity"]))
        new_quantity = sized_entry_quantity(notional, new_wire_limit, quantity, 1000)
        bracket_changes = {}
        if native:
            old_anchor = Decimal(str(md.get("entry_price") or md.get("reference_price")))
            for key, multiplier in (("bracket_target_price", Decimal("1.05")), ("bracket_stop_price", Decimal("0.92"))):
                old_value = str(md.get(key, "UNMEASURED"))
                hypothetical = stop * multiplier
                new_value = f"{float(hypothetical):.2f}" if hypothetical > 1 else f"{float(hypothetical):.4f}"
                if old_value != "UNMEASURED" and Decimal(old_value) != Decimal(new_value):
                    bracket_changes[key] = {"old": old_value, "single_seam_only": new_value, "old_anchor": str(old_anchor)}
        rows.append({
            "order_id": order["id"], "client_order_id": order["client_order_id"],
            "symbol": order["symbol"], "account": order["account"], "status": order["status"],
            "submitted_at": order["submitted_at"], "line_recorded_four_decimals": str(line),
            "rule_reconstructed": str(rule), "old_wire_stop": str(old_wire_stop),
            "projected_wire_stop": str(stop), "old_wire_limit": str(old_wire_limit),
            "projected_wire_limit": str(new_wire_limit), "old_quantity": quantity,
            "projected_quantity": new_quantity, "notional_recorded": str(notional),
            "naive_single_seam_exit_changes": bracket_changes,
        })
        counts[leg + ":orders"] += 1
        counts[leg + ":filled"] += order["status"] == "filled"
        counts[leg + ":stop_up"] += stop > old_wire_stop
        counts[leg + ":limit_changed"] += new_wire_limit != old_wire_limit
        counts[leg + ":shares_changed"] += new_quantity != quantity
        counts[leg + ":naive_exit_changed"] += bool(bracket_changes)
    print(json.dumps({"read_at": raw["read_at"], "label": "four-decimal-line price projection; full-precision line and counterfactual fills UNMEASURED", "counts": dict(counts), "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
