"""Head-to-head: PRICE two-tier vs ATR/behavior sizing on gated intrabar-2%."""
import json, statistics
SD = r"C:/Users/kkvkr/AppData/Local/Temp/claude/C--Users-kkvkr/67e00b05-ff08-4181-8729-0b38cee38319/scratchpad"
d = json.load(open(f"{SD}/orb_exit_sweep_gated.json"))
cl = [r for r in d["rows"] if r["metrics"]]
atrs = sorted(r["metrics"]["atr_pct5"] for r in cl); VOL_LO = atrs[int(0.33*len(atrs))]
active = [r for r in cl if r["metrics"]["atr_pct5"] >= VOL_LO]
ER_HI = statistics.median([r["metrics"]["er"] for r in active])
def beh(m): return "slow" if m["atr_pct5"] < VOL_LO else ("grinding" if m["er"] >= ER_HI else "volatile")
for r in cl:
    r["b"] = beh(r["metrics"]); r["price"] = r["metrics"]["price"]
    r["ps"] = r["configs"]["t2_hnone"]["ib"] / 5.0     # per-share pnl (qty-invariant)
    r["hi"] = r["b"] in ("volatile", "grinding")

def qty_flat(r): return 5
def qty_price(r): return 10 if r["price"] >= 5 else 20          # price two-tier
def qty_atr(r):   return 20 if r["hi"] else 10                  # ATR two-tier (same 10/20, by ATR)
def qty_edge(r):  return 20 if r["hi"] else 0                   # ATR edge-only (slow excluded)

def stats(rule):
    vals = [r["ps"] * rule(r) for r in cl if rule(r) > 0]       # drop excluded (qty0)
    p = sorted(vals); n = len(p)
    win = sum(1 for x in p if x > 0.005)/n*100
    dtop = sum(p) - max(p, key=abs)
    avgq = sum(rule(r) for r in cl if rule(r) > 0)/n
    return dict(n=n, tot=sum(p), med=statistics.median(p), win=win, dtop=dtop, avgq=avgq)

print(f"=== sizing head-to-head (gated intrabar-2%, n={len(cl)} classifiable) ===")
print(f"  {'rule':<24}{'n':>4}{'avgQ':>6}{'total':>9}{'median':>8}{'win%':>6}{'drop-top':>10}")
for lbl, rule in [("flat qty5", qty_flat), ("PRICE two-tier 10/20", qty_price),
                  ("ATR two-tier 10/20", qty_atr), ("ATR edge-only (slow=0)", qty_edge)]:
    s = stats(rule)
    print(f"  {lbl:<24}{s['n']:>4}{s['avgq']:>6.1f}{s['tot']:>+9.1f}{s['med']:>+8.2f}{s['win']:>5.0f}%{s['dtop']:>+10.1f}")

# does ATR avoid amplifying the cheap-slow losers? track the slow names & their qty per rule
print("\n--- SLOW names: qty & pnl under PRICE vs ATR sizing (the DSY question) ---")
print(f"  {'sym':<7}{'price':>6}{'ATR5%':>7}{'ps':>7}  {'price:qty->pnl':>18}{'ATR2t:qty->pnl':>18}{'edge:qty->pnl':>16}")
for r in sorted([r for r in cl if r["b"] == "slow"], key=lambda r: r["ps"]):
    def cell(rule):
        q = rule(r); return f"{q}->{r['ps']*q:+.2f}" if q > 0 else "excluded"
    print(f"  {r['configs'] and r['sym']:<7}{r['price']:>6.2f}{r['metrics']['atr_pct5']:>7.2f}{r['ps']:>+7.2f}  "
          f"{cell(qty_price):>18}{cell(qty_atr):>18}{cell(qty_edge):>16}")

# where the rules DISAGREE on who gets the big (20) size
print("\n--- who gets qty20? PRICE(<$5) vs ATR(high-ATR) — disagreements ---")
p20 = {r["sym"]+r["date"] for r in cl if qty_price(r) == 20}
a20 = {r["sym"]+r["date"] for r in cl if qty_atr(r) == 20}
only_price = [r for r in cl if (r["sym"]+r["date"]) in p20 - a20]
only_atr = [r for r in cl if (r["sym"]+r["date"]) in a20 - p20]
print(f"  qty20 only under PRICE (cheap but slow): {[(r['sym'],round(r['ps']*20,2)) for r in only_price]}")
print(f"  qty20 only under ATR (high-ATR but $5+): {[(r['sym'],round(r['ps']*20,2)) for r in only_atr]}")

# per-behavior median/share (rule-independent, per-share) for reference
print("\n--- per-share edge by behavior (rule-independent) ---")
for b in ["volatile","grinding","slow"]:
    g=[r["ps"] for r in cl if r["b"]==b]
    print(f"  {b:<9} n={len(g)} median/share {statistics.median(g):+.3f} win {sum(1 for x in g if x>0.005)/len(g)*100:.0f}%")
