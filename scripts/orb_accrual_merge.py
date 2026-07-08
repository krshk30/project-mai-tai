"""ORB trail-study forward-accrual merger: fold each day's sweep rows into a master set and
recompute the RUNNING robustness of the headline config (intrabar 2% trail, no hard stop) vs
live bar-close 3% — median $/nd, win%, drop-top-3, split by behavior. Appends a one-line summary
to the accrual log each run so the sample's climb toward 15-20+ name-days is visible.

Usage: python orb_accrual_merge.py <DATA_DIR> <DATE_ET>   (called by orb_trail_accrual.sh)
Reads DATA_DIR/day_*.json (each an orb_exit_sweep --json output), writes DATA_DIR/master.json
and appends DATA_DIR/accrual_log.txt.
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys


def _classify(rows):
    cl = [r for r in rows if r.get("metrics")]
    if not cl:
        return cl, None, None
    atrs = sorted(r["metrics"]["atr_pct5"] for r in cl)
    vol_lo = atrs[int(0.33 * len(atrs))]
    active = [r for r in cl if r["metrics"]["atr_pct5"] >= vol_lo]
    er_hi = statistics.median([r["metrics"]["er"] for r in active]) if active else 0.0
    for r in cl:
        m = r["metrics"]
        r["b"] = "slow" if m["atr_pct5"] < vol_lo else ("grinding" if m["er"] >= er_hi else "volatile")
    return cl, vol_lo, er_hi


def _rob(group, cfg, mode):
    p = sorted(r["configs"][cfg][mode] for r in group if cfg in r["configs"])
    if not p:
        return None
    byabs = sorted(p, key=abs, reverse=True)
    d3 = sorted(byabs[3:])
    return {"n": len(p), "med": statistics.median(p),
            "win": sum(1 for x in p if x > 0.005) / len(p) * 100,
            "tot": sum(p), "med_d3": (statistics.median(d3) if d3 else statistics.median(p))}


def main():
    data_dir, date_et = sys.argv[1], sys.argv[2]
    # merge all day files, dedup by (date, sym) — later files win
    merged: dict[tuple, dict] = {}
    for f in sorted(glob.glob(os.path.join(data_dir, "day_*.json"))):
        try:
            payload = json.load(open(f))
        except (OSError, ValueError):
            continue
        for r in payload.get("rows", []):
            merged[(r["date"], r["sym"])] = r
    rows = list(merged.values())
    json.dump({"rows": rows, "n": len(rows)}, open(os.path.join(data_dir, "master.json"), "w"))

    cl, vol_lo, er_hi = _classify(rows)
    days = sorted({r["date"] for r in rows})
    ib2 = _rob(cl, "t2_hnone", "ib")
    bc3 = _rob(cl, "t3_hnone", "bc")
    lines = [f"=== ORB trail accrual @ {date_et} === days={len(days)} name-days(total)={len(rows)} classifiable={len(cl)}"]
    if ib2 and bc3:
        lines.append(f"  intrabar-2% : median {ib2['med']:+.3f}  win {ib2['win']:.0f}%  drop-top3-median {ib2['med_d3']:+.3f}  (n={ib2['n']}, total {ib2['tot']:+.1f})")
        lines.append(f"  barclose-3% : median {bc3['med']:+.3f}  win {bc3['win']:.0f}%  (live)")
        for b in ("volatile", "grinding", "slow"):
            g = [r for r in cl if r.get("b") == b]
            s = _rob(g, "t2_hnone", "ib")
            if s:
                lines.append(f"    ib2%/{b:<9}: median {s['med']:+.3f}  win {s['win']:.0f}%  drop-top3 {s['med_d3']:+.3f}  (n={s['n']})")
        target = "REACHED 15+" if len(cl) >= 15 else f"{max(0, 15 - len(cl))} more to 15"
        lines.append(f"  -> progress: {len(cl)} classifiable name-days ({target})")
    summary = "\n".join(lines)
    print(summary)
    with open(os.path.join(data_dir, "accrual_log.txt"), "a") as fh:
        fh.write(summary + "\n\n")


if __name__ == "__main__":
    main()
