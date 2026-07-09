"""Light post-process of v2_rearm_ab_run.py's JSON: rearm=False vs True in three cuts
(full / Schwab-tradeable / restricted-only), each with net P&L, win%, median, per-name-day,
drop-one-name, and the Path A/B/reclaim split. Classification = DB-observed Schwab fill/reject
(06-24..07-08). The TRADEABLE cut gates the flip; FULL is broker-agnostic (restricted fills are
counterfactual — could not have filled on Schwab); RESTRICTED is the routing-cost case.

    python -m scripts.v2_rearm_ab_report v2_rearm_ab.json
"""
from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict

# DB-observed 06-24..07-08 (scripts/atr_name_class.py). MIXED (>=1 fill) -> tradeable.
TRADEABLE = {"CANF", "CELZ", "CLRO", "CWD", "FCUV", "FRTT", "INTZ", "KIDZ", "LGPS", "LHAI", "LUCY",
             "SKYQ", "TVRD", "VTAK"}
RESTRICTED = {"AZI", "BTCT", "BYAH", "CUPR", "DGNX", "DSY", "DXF", "EHGO", "IOTR", "JEM", "LGCL",
              "NVVE", "RPGL", "SDEV", "SDOT", "TC", "TDTH", "UPC"}


def _cls(sym):
    if sym in TRADEABLE:
        return "tradeable"
    if sym in RESTRICTED:
        return "restricted"
    return "unknown"


def _side_stats(nds, side):
    """nds = list of name-day dicts already filtered to a cut. Returns metrics for one side."""
    pnls, per_nd, per_sym, paths = [], {}, defaultdict(float), defaultdict(lambda: [0, 0.0])
    for nd in nds:
        s = 0.0
        for t in nd[side]:
            pnls.append(t["pnl"])
            s += t["pnl"]
            per_sym[nd["sym"]] += t["pnl"]
            paths[t["path"]][0] += 1
            paths[t["path"]][1] += t["pnl"]
        if nd[side]:
            per_nd[f"{nd['date']} {nd['sym']}"] = round(s, 2)
    net = sum(pnls)
    n = len(pnls)
    win = 100.0 * sum(p > 0 for p in pnls) / n if n else 0.0
    med = st.median(pnls) if pnls else 0.0
    # drop-one-name: net with each sym removed -> which single name most inflates the total
    drops = {sym: round(net - tot, 2) for sym, tot in per_sym.items()}
    worst_drop = min(drops.items(), key=lambda kv: kv[1]) if drops else (None, net)
    best_drop = max(drops.items(), key=lambda kv: kv[1]) if drops else (None, net)
    return {"net": round(net, 2), "n": n, "win": round(win, 1), "median": round(med, 3),
            "n_nd": len(per_nd), "per_nd": per_nd,
            "paths": {k: [v[0], round(v[1], 2)] for k, v in sorted(paths.items())},
            "drop_worst": worst_drop, "drop_best": best_drop}


def _cut(name_days, keep):
    nds = [nd for nd in name_days if keep(_cls(nd["sym"]))]
    off, on = _side_stats(nds, "off"), _side_stats(nds, "on")
    return {"off": off, "on": on, "delta_net": round(on["net"] - off["net"], 2),
            "delta_trades": on["n"] - off["n"], "n_names": len({nd["sym"] for nd in nds})}


def _print_cut(title, cut, note=""):
    print(f"\n{'='*92}\n{title}   ({cut['n_names']} names){('  — ' + note) if note else ''}\n{'='*92}")
    print(f"{'':10} {'net':>9} {'trades':>7} {'win%':>6} {'median':>8} {'name-days':>10}   paths(n,$)")
    for lab, s in (("rearm OFF", cut["off"]), ("rearm ON", cut["on"])):
        pstr = "  ".join(f"{k}:{v[0]}/{v[1]:+.2f}" for k, v in s["paths"].items())
        print(f"{lab:10} {s['net']:>+9.2f} {s['n']:>7} {s['win']:>6.1f} {s['median']:>+8.3f} "
              f"{s['n_nd']:>10}   {pstr}")
    print(f"{'DELTA':10} {cut['delta_net']:>+9.2f} {cut['delta_trades']:>+7}   (ON - OFF)")
    dw = cut["on"]["drop_worst"]
    if dw[0]:
        print(f"  drop-one (ON): remove {dw[0]} -> net {dw[1]:+.2f}  "
              f"(single-name dependence: {cut['on']['net']:+.2f} -> {dw[1]:+.2f})")


def main():
    if len(sys.argv) < 2:
        print("usage: v2_rearm_ab_report v2_rearm_ab.json")
        return
    data = json.load(open(sys.argv[1]))
    nd = data["name_days"]
    print(f"loaded {len(nd)} name-days, qty={data.get('qty')}, dates={data['dates'][0]}..{data['dates'][-1]}")
    unknown = sorted({x["sym"] for x in nd if _cls(x["sym"]) == "unknown"})
    if unknown:
        print(f"UNKNOWN names (no live emit record — Schwab stance unknown): {unknown}")
    _print_cut("CUT 1 — FULL universe (broker-agnostic edge)", _cut(nd, lambda c: True),
               "restricted fills are COUNTERFACTUAL (could not fill on Schwab)")
    _print_cut("CUT 2 — SCHWAB-TRADEABLE only  [GATES THE FLIP]", _cut(nd, lambda c: c == "tradeable"))
    _print_cut("CUT 3 — RESTRICTED only (Schwab routing cost; Webull-candidate)",
               _cut(nd, lambda c: c == "restricted"))
    if unknown:
        _print_cut("ADDENDUM — UNKNOWN (unclassified)", _cut(nd, lambda c: c == "unknown"))


if __name__ == "__main__":
    main()
