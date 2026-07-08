"""Two-tier price-scaled sizing on the VALIDATED gated intrabar-2% config.
Rule: price>=$5 -> qty10, price<$5 -> qty20 (baseline flat qty5). P&L linear in qty."""
import json, statistics
SD = r"C:/Users/kkvkr/AppData/Local/Temp/claude/C--Users-kkvkr/67e00b05-ff08-4181-8729-0b38cee38319/scratchpad"
d = json.load(open(f"{SD}/orb_exit_sweep_gated.json"))
cl = [r for r in d["rows"] if r["metrics"]]     # need price+behavior
# behavior classification (same rule as the sweep)
atrs = sorted(r["metrics"]["atr_pct5"] for r in cl); VOL_LO = atrs[int(0.33*len(atrs))]
active = [r for r in cl if r["metrics"]["atr_pct5"] >= VOL_LO]
ER_HI = statistics.median([r["metrics"]["er"] for r in active])
def beh(m): return "slow" if m["atr_pct5"] < VOL_LO else ("grinding" if m["er"] >= ER_HI else "volatile")

BASE_QTY = 5
for r in cl:
    r["ib5"] = r["configs"]["t2_hnone"]["ib"]           # intrabar-2% pnl at qty5
    r["price"] = r["metrics"]["price"]
    r["b"] = beh(r["metrics"])
    r["tier"] = "$5+" if r["price"] >= 5 else "<$5"
    r["qty"] = 10 if r["price"] >= 5 else 20
    r["scaled"] = r["ib5"] * (r["qty"] / BASE_QTY)      # two-tier pnl
    r["pershare"] = r["ib5"] / BASE_QTY                 # scale-invariant per-share

def block(vals):
    p = sorted(vals); n = len(p)
    win = sum(1 for x in p if x > 0.005)/n*100
    byabs = sorted(p, key=abs, reverse=True)
    return dict(n=n, tot=sum(p), med=statistics.median(p), win=win,
                dtop=sum(p)-max(p, key=abs), pos=sum(1 for x in p if x>0.005), neg=sum(1 for x in p if x<-0.005))

print(f"=== gated intrabar-2% price-scaled sizing === classifiable name-days={len(cl)}  (unclassifiable excluded: {len(d['rows'])-len(cl)})")
print(f"tier split by median price: $5+={sum(1 for r in cl if r['tier']=='$5+')}  <$5={sum(1 for r in cl if r['tier']=='<$5')}")

print("\n--- OVERALL: flat qty5 (baseline) vs two-tier (10/20) vs uniform qty10 (ref) ---")
for lbl, key in [("flat qty5 (baseline)", "ib5"), ("TWO-TIER 10/20", "scaled"), ("uniform qty10 (ref)", None)]:
    vals = [r["ib5"]*2 for r in cl] if key is None else [r[key] for r in cl]
    b = block(vals)
    print(f"  {lbl:<22} total {b['tot']:+8.1f}  median {b['med']:+7.2f}  win {b['win']:.0f}%  drop-top {b['dtop']:+7.1f}")

print("\n--- BY PRICE TIER (the key question: is each tier net +/-?) ---")
for tier, q in [("$5+", 10), ("<$5", 20)]:
    g = [r for r in cl if r["tier"] == tier]
    b5 = block([r["ib5"] for r in g])        # at qty5 (sign-defining)
    bq = block([r["scaled"] for r in g])     # at the tier qty
    sign = "NET POSITIVE" if b5["med"] > 0.005 else "NET NEGATIVE" if b5["med"] < -0.005 else "FLAT"
    print(f"  {tier} (qty{q}): n={b5['n']}  median/share {b5['med']/5:+.3f}  win {b5['win']:.0f}% ({b5['pos']}+/{b5['neg']}-)  "
          f"[{sign}]  ->  total@qty{q} {bq['tot']:+.1f}  median@qty{q} {bq['med']:+.2f}  drop-top {bq['dtop']:+.1f}")

print("\n--- PRICE TIER x BEHAVIOR (are the cheap 2x names high-ATR movers or slow drifters?) ---")
print(f"  {'':<6}{'volatile':>22}{'grinding':>22}{'slow':>22}")
for tier in ["$5+", "<$5"]:
    cells = ""
    for bb in ["volatile", "grinding", "slow"]:
        g = [r for r in cl if r["tier"] == tier and r["b"] == bb]
        if g:
            b5 = block([r["ib5"] for r in g])
            cells += f"{f'n={b5['n']} med/sh{b5['med']/5:+.2f}':>22}"
        else:
            cells += f"{'-':>22}"
    print(f"  {tier:<6}{cells}")

print("\n--- <$5 tier detail (each cheap name that gets qty20) ---")
sub = sorted([r for r in cl if r["tier"] == "<$5"], key=lambda r: r["ib5"])
print(f"  {'date':<11}{'sym':<6}{'price':>6}{'ATR5%':>7}{'beh':<10}{'pnl@qty5':>9}{'pnl@qty20':>10}")
for r in sub:
    print(f"  {r['date']:<11}{r['sym']:<6}{r['price']:>6.2f}{r['metrics']['atr_pct5']:>7.2f}{r['b']:<10}{r['ib5']:>+9.2f}{r['scaled']:>+10.2f}")
