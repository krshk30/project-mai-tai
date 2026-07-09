"""Report the ATR naked test from atr_naked_run.py JSON. Per-trade table + totals for the three exit
configs (+2%, +3%, flip_only): net, win%, avg win, avg loss, payoff, expectancy, breakeven win%,
median, drop-one-name."""
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime

QTY = 10
CFGS = ["+2%", "+3%", "flip_only"]


def _t(iso):
    return datetime.fromisoformat(iso)


def _rows(data):
    """Flatten to per-(trade,config): dict with sym,date,entry,exit,pnl%,pnl$,hold,reason,mfe,mae,spread."""
    out = []
    for nd in data["name_days"]:
        for tr in nd["trades"]:
            ep = tr["entry_px"]
            for cfg in CFGS:
                ex = tr["exits"][cfg]
                pnl_pct = 100 * (ex["px"] - ep) / ep
                out.append({
                    "sym": nd["sym"], "date": nd["date"], "cfg": cfg,
                    "entry_t": _t(tr["entry_ts"]).strftime("%m-%d %H:%M"), "entry_px": ep,
                    "exit_t": _t(ex["ts"]).strftime("%H:%M"), "exit_px": ex["px"], "reason": ex["reason"],
                    "pnl_pct": pnl_pct, "pnl_d": QTY * (ex["px"] - ep),
                    "mfe": tr["mfe_pct"], "mae": tr["mae_pct"], "spread": tr.get("spread"),
                    "seg": tr.get("short_seg_bars"),
                    "hold_s": (_t(ex["ts"]) - _t(tr["entry_ts"])).total_seconds()})
    return out


def _totals(rows, cfg):
    r = [x for x in rows if x["cfg"] == cfg]
    if not r:
        return None
    pcts = [x["pnl_pct"] for x in r]
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]
    net = sum(x["pnl_d"] for x in r)
    aw = st.mean(wins) if wins else 0.0
    al = st.mean(losses) if losses else 0.0
    payoff = aw / abs(al) if al else float("inf")
    be = 100 * abs(al) / (aw + abs(al)) if (aw + abs(al)) else 0.0
    per_sym = defaultdict(float)
    for x in r:
        per_sym[x["sym"]] += x["pnl_d"]
    drop = max(per_sym.items(), key=lambda kv: kv[1]) if per_sym else (None, 0.0)
    n_targ = sum(x["reason"] == "TARGET" for x in r)
    return {"n": len(r), "net": net, "win": 100 * len(wins) / len(r), "aw": aw, "al": al,
            "payoff": payoff, "exp": st.mean(pcts), "be": be, "median": st.median(pcts),
            "drop_sym": drop[0], "drop_net": net - drop[1], "n_target": n_targ}


def main():
    data = json.load(open(sys.argv[1]))
    rows = _rows(data)
    ul = data.get("universe_log", [])
    print(f"# ATR NAKED — window {data.get('window')}, qty {data['qty']}")
    print(f"# universe (existing v2 pull): {sum(u['n'] for u in ul)} name-days over {len(ul)} days")
    for u in ul:
        print(f"#   {u['date']}: {u['symbols']}")
    uniq = sorted({nd['sym'] for nd in data['name_days']})
    print(f"# {len(uniq)} names traded: {uniq}\n")

    # per-trade table (one row per trade; the 3 configs shown side by side)
    print("=== PER-TRADE (entry once; exit under each config) ===")
    print(f"{'sym':<6}{'entry':<12}{'entryPx':>8}{'spr':>6}{'segBars':>7}{'MFE%':>7}{'MAE%':>7}   "
          f"{'+2%: exit/rsn/PnL%':<26}{'+3%: exit/rsn/PnL%':<26}{'flip: exit/rsn/PnL%':<26}")
    by_trade = defaultdict(dict)
    for x in rows:
        by_trade[(x["sym"], x["entry_t"])][x["cfg"]] = x
    for (sym, et), d in sorted(by_trade.items(), key=lambda kv: kv[0][1]):
        base = d["flip_only"]
        cells = ""
        for cfg in CFGS:
            x = d[cfg]
            cells += f"{x['exit_t']}/{x['reason'][:4]}/{x['pnl_pct']:+5.2f}   "
        sp = f"{base['spread']:.3f}" if base["spread"] is not None else "  -  "
        seg = f"{base['seg']}" if base["seg"] is not None else "EOD"
        print(f"{sym:<6}{et:<12}{base['entry_px']:>8.3f}{sp:>6}{seg:>7}{base['mfe']:>7.2f}"
              f"{base['mae']:>7.2f}   {cells}")

    print("\n=== TOTALS BY CONFIG ===")
    print(f"{'config':<10}{'n':>4}{'net$':>9}{'win%':>6}{'avgW%':>7}{'avgL%':>7}{'payoff':>7}"
          f"{'exp%':>7}{'BEwin%':>7}{'med%':>7}  {'nTarget':>7}   drop-one")
    for cfg in CFGS:
        s = _totals(rows, cfg)
        if not s:
            continue
        print(f"{cfg:<10}{s['n']:>4}{s['net']:>+9.2f}{s['win']:>6.1f}{s['aw']:>+7.2f}{s['al']:>+7.2f}"
              f"{s['payoff']:>7.2f}{s['exp']:>+7.3f}{s['be']:>7.1f}{s['median']:>+7.2f}  {s['n_target']:>7}   "
              f"-{s['drop_sym']} -> {s['drop_net']:+.2f}")

    # noise-vs-reversal: flip_only ATR_FLIP exits bucketed by short-segment length
    print("\n=== FLIP-SHORT EXIT QUALITY (flip_only, ATR_FLIP exits by short-segment length) ===")
    fo = [x for x in rows if x["cfg"] == "flip_only" and x["reason"] == "ATR_FLIP" and x["seg"] is not None]
    print(f"{'segBars':<10}{'n':>4}{'net$':>9}{'win%':>7}{'exp%':>8}   (short seg = flip fired on noise)")
    for lab, pred in (("1-2 (noise)", lambda v: v <= 2), ("3-5", lambda v: 3 <= v <= 5),
                      ("6+ (reversal)", lambda v: v >= 6)):
        g = [x for x in fo if pred(x["seg"])]
        if not g:
            continue
        pcts = [x["pnl_pct"] for x in g]
        print(f"{lab:<10}{len(g):>4}{sum(x['pnl_d'] for x in g):>+9.2f}"
              f"{100 * sum(p > 0 for p in pcts) / len(g):>7.1f}{st.mean(pcts):>+8.3f}")


if __name__ == "__main__":
    main()
