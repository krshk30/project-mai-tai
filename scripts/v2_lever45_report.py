"""Report levers 4 & 5 from v2_lever45_run.py JSON: for each entry (touch=current, break=confirmation)
x hard-stop level -> net%, win%, avg winner/loser, payoff, expectancy, and DROP-ONE (single-name
dependence). Tradeable universe only."""
import json
import statistics as st
import sys
from collections import defaultdict


def stats(nds, entry, stop):
    per_sym = defaultdict(list)
    for nd in nds:
        for t in nd[entry].get(stop, []):
            per_sym[nd["sym"]].append(t["pnl_pct"])
    pcts = [p for v in per_sym.values() for p in v]
    if not pcts:
        return None
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]
    net = sum(pcts)
    aw = st.mean(wins) if wins else 0.0
    al = st.mean(losses) if losses else 0.0
    net_by_sym = {s: sum(v) for s, v in per_sym.items()}
    drop = max(net_by_sym.items(), key=lambda kv: kv[1])          # biggest single contributor -> drop it
    return {"net": net, "n": len(pcts), "win": 100 * len(wins) / len(pcts),
            "aw": aw, "al": al, "payoff": (aw / abs(al)) if al else float("inf"),
            "exp": st.mean(pcts),
            "drop_name": drop[0], "drop_net": net - drop[1]}


def main():
    d = json.load(open(sys.argv[1]))
    nds = d["name_days"]
    names = sorted({nd["sym"] for nd in nds})
    print(f"tradeable name-days: {len(nds)}  names: {names}")
    for entry, title in (("touch", "TEST 5 — TOUCH entry (current), hard-stop sweep"),
                         ("break", "TEST 4 — BREAK-CONFIRMATION entry, hard-stop sweep")):
        print(f"\n{'='*100}\n{title}\n{'='*100}")
        print(f"{'stop':>6} {'net%':>9} {'n':>4} {'win%':>6} {'avgWin':>8} {'avgLoss':>8} "
              f"{'payoff':>7} {'exp%/tr':>8}   drop-one")
        for stop in ("1.5", "2.0", "3.0", "5.0", "none"):
            s = stats(nds, entry, stop)
            if not s:
                print(f"{stop:>6}  (no trades)")
                continue
            print(f"{stop:>6} {s['net']:>+9.2f} {s['n']:>4} {s['win']:>6.1f} {s['aw']:>+8.2f} "
                  f"{s['al']:>+8.2f} {s['payoff']:>7.2f} {s['exp']:>+8.3f}   "
                  f"-{s['drop_name']} -> {s['drop_net']:+.2f}")


if __name__ == "__main__":
    main()
