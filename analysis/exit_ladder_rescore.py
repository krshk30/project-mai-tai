"""Re-score: replay the system's EXIT LADDER over stored bars per entry path.

Read-only. Computes realized P&L per entry under the documented ladder
(docs/oms-exit-logic-reference.md) — the real apples-to-apples test, replacing
the flat +10% scalp lens. Paths: 1 (MACD Cross), 2 (VWAP Breakout), 3 (ATR flip:
A/B/C0.5/C1/C2/D). RAW + liquidity-floored.

⚠️ Models the OLD-bot ladder APPLIED to these entries — NOT what schwab_1m_v2 does
today (v2 runs NO managed exits; see docs/oms-exit-logic-reference.md §scope). A
positive result = "these entries + this ladder would pay", NOT "v2 is profitable".
⚠️ BOTH-HIT AMBIGUITY: when a scale tier and the stop/floor sit in one 1-min
candle, intrabar order is unknown → we report a BOUNDED range (favorable-first vs
adverse-first passes), never a point estimate (ticks resolve it in Phase 2).
⚠️ Idealized fills, and partials = MORE exit fills per trade = more cost surface;
Phase-2 measured spread is decisive. ⚠️ DIRECTIONAL, NOT STATISTICAL.

Ladder modeled: scale (NORMAL: +2%→50%, +4%-after→25% of remaining, fast +4%→75%),
floor ratchet (peak: 1%→BE, 2%→+0.5%, 3%→+1.5%, 4%+→trail peak−1.5%), hard stop
−1.5% (fixed), macd-cross-below tier exit on the remainder (bar close), precedence
hard>floor>scale>tier, no EOD flat (remainder exits at session close).
KNOWN v1 LIMITATION: the stoch tier-exit leg is NOT modeled (needs the strategy's
stoch-exit threshold); macd-cross-below (the common tier exit) is.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atr_flip import Bar, compute_atr_trail, fetch_day  # noqa: E402
from path3_backtest import et_date, extract_signals, load_path12  # noqa: E402

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient  # noqa: E402
from project_mai_tai.settings import Settings  # noqa: E402

STOP_PCT = 1.5
VOL_FLOOR = 5000


def _dsn():
    return os.environ["MAI_TAI_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")


def macd_cross_below_series(closes: list[float], fast=12, slow=26, signal=9) -> list[bool]:
    """Per-bar macd_cross_below (prev macd≥signal AND macd<signal). 1m MACD."""
    n = len(closes)
    out = [False] * n

    def ema_series(vals, p):
        if len(vals) < p:
            return []
        m = 2.0 / (p + 1)
        e = sum(vals[:p]) / p
        res = [(p - 1, e)]
        for i in range(p, len(vals)):
            e = (vals[i] - e) * m + e
            res.append((i, e))
        return res

    fast_s = dict(ema_series(closes, fast))
    slow_s = dict(ema_series(closes, slow))
    macd_idx = sorted(i for i in slow_s if i in fast_s)
    macd = {i: fast_s[i] - slow_s[i] for i in macd_idx}
    sig_pairs = ema_series([macd[i] for i in macd_idx], signal)  # indices into macd_idx
    sig = {macd_idx[j]: v for j, v in sig_pairs}
    valid = [i for i in macd_idx if i in sig]
    for a, b in zip(valid, valid[1:]):
        if macd[a] >= sig[a] and macd[b] < sig[b]:
            out[b] = True
    return out


def _floor_pct(peak: float) -> float:
    if peak >= 4.0:
        return peak - 1.5
    if peak >= 3.0:
        return 1.5
    if peak >= 2.0:
        return 0.5
    if peak >= 1.0:
        return 0.0
    return -999.0


def _scale_action(profit: float, done: set):
    if profit >= 4 and "FAST4" not in done and "PCT2" not in done:
        return ("FAST4", 0.75, 4.0)
    if profit >= 2 and "PCT2" not in done and "FAST4" not in done:
        return ("PCT2", 0.50, 2.0)
    if profit >= 4 and "PCT2" in done and "PCT4_AFTER2" not in done:
        return ("PCT4_AFTER2", 0.25, 4.0)
    return None


def simulate(entry: float, fwd: list[Bar], xbelow: list[bool], optimistic: bool) -> dict:
    """Return realized % return on the position + exit breakdown."""
    qty, peak, floor_pct = 1.0, 0.0, -999.0
    done: set = set()
    realized = 0.0
    breakdown = {"scale_pnl": 0.0, "exit": "session_end", "tiers": []}
    stop_profit = -STOP_PCT

    def apply_scales(high_profit):
        nonlocal qty, realized
        while qty > 1e-9:
            a = _scale_action(high_profit, done)
            if not a:
                break
            lvl, frac, trig = a
            sell = qty * frac
            realized += sell * trig                 # sold at +trig% (idealized)
            breakdown["scale_pnl"] += sell * trig
            breakdown["tiers"].append(lvl)
            qty -= sell
            done.add(lvl)

    def check_down(low_profit) -> bool:
        nonlocal qty, realized
        if floor_pct > -999 and low_profit <= floor_pct:
            realized += qty * floor_pct
            breakdown["exit"] = "floor"
            qty = 0.0
            return True
        if low_profit <= stop_profit:
            realized += qty * stop_profit
            breakdown["exit"] = "hard_stop"
            qty = 0.0
            return True
        return False

    for k, b in enumerate(fwd):
        hp = (b.high - entry) / entry * 100
        lp = (b.low - entry) / entry * 100
        cp = (b.close - entry) / entry * 100
        if optimistic:
            peak = max(peak, hp)
            floor_pct = max(floor_pct, _floor_pct(peak))
            apply_scales(hp)
            if check_down(lp):
                break
        else:
            if check_down(lp):
                break
            peak = max(peak, hp)
            floor_pct = max(floor_pct, _floor_pct(peak))
            apply_scales(hp)
        if qty > 1e-9 and k < len(xbelow) and xbelow[k]:
            realized += qty * cp
            breakdown["exit"] = "macd_below"
            qty = 0.0
            break
    if qty > 1e-9:
        realized += qty * ((fwd[-1].close - entry) / entry * 100 if fwd else 0.0)
    breakdown["realized_pct"] = round(realized, 3)
    return breakdown


def score(entry, fwd, xbelow) -> tuple[float, float, dict]:
    """(worst, best) realized% + the optimistic breakdown."""
    opt = simulate(entry, fwd, xbelow, True)
    pes = simulate(entry, fwd, xbelow, False)
    return pes["realized_pct"], opt["realized_pct"], opt


def agg(results: list[tuple[float, float, dict]]) -> dict:
    if not results:
        return {"n": 0}
    worst = sorted(r[0] for r in results)
    best = sorted(r[1] for r in results)
    mid = [(r[0] + r[1]) / 2 for r in results]
    n = len(results)
    ambiguous = sum(1 for r in results if abs(r[1] - r[0]) > 1e-6)
    exits: dict = {}
    for r in results:
        exits[r[2]["exit"]] = exits.get(r[2]["exit"], 0) + 1
    wins = sum(1 for m in mid if m > 0)
    return {
        "n": n,
        "exp_pct_worst": round(sum(worst) / n, 3),
        "exp_pct_best": round(sum(best) / n, 3),
        "median_mid_pct": round(sorted(mid)[n // 2], 3),
        "win_rate_mid": round(wins / n, 3),
        "avg_win_mid": round(sum(m for m in mid if m > 0) / max(1, wins), 3),
        "avg_loss_mid": round(sum(m for m in mid if m <= 0) / max(1, n - wins), 3),
        "ambiguous": ambiguous,
        "exit_mix": exits,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/exit_rescore.json")
    args = ap.parse_args()
    s = Settings()
    client = SchwabV2RestClient(s, on_chart_bar=lambda *a: None, on_quote=lambda *a: None)
    with psycopg.connect(_dsn()) as conn:
        path12 = load_path12(conn)
    universe = sorted(path12.keys())
    print(f"universe: {len(universe)} symbol-days")

    variants = [("A", None), ("B", None), ("C", 0.5), ("C", 1), ("C", 2), ("D", None)]
    vkey = lambda v, p: v if p is None else f"C{p}"
    res: dict = {vkey(v, p): {"raw": [], "floor": []} for v, p in variants}
    p12: dict = {"raw": [], "floor": [], "by_path": {}}
    bcache: dict = {}

    for (sym, day) in universe:
        if (sym, day) not in bcache:
            try:
                bcache[(sym, day)] = fetch_day(client, s, sym, day)
            except Exception:
                bcache[(sym, day)] = []
        bars = bcache[(sym, day)]
        if len(bars) < 40:
            continue
        xbelow = macd_cross_below_series([b.close for b in bars])
        rows = compute_atr_trail(bars)
        for (v, p) in variants:
            for (ei, ep) in extract_signals(bars, rows, v, p):
                sc = score(ep, bars[ei + 1:], xbelow[ei + 1:])
                res[vkey(v, p)]["raw"].append(sc)
                if bars[ei].volume > VOL_FLOOR:
                    res[vkey(v, p)]["floor"].append(sc)
        bidx = {b.ts: i for i, b in enumerate(bars)}
        for sig in path12[(sym, day)]:
            i = bidx.get(sig["bar_ms"])
            if i is None:
                continue
            sc = score(sig["entry"], bars[i + 1:], xbelow[i + 1:])
            p12["raw"].append(sc)
            if bars[i].volume > VOL_FLOOR:
                p12["floor"].append(sc)
            p12["by_path"].setdefault(sig["path"], []).append(sc)

    report = {
        "models": "OLD-bot exit ladder applied to these entries (NOT what v2 does today)",
        "path3": {k: {"raw": agg(c["raw"]), "floor": agg(c["floor"])} for k, c in res.items()},
        "path12_baseline": {"raw": agg(p12["raw"]), "floor": agg(p12["floor"]),
                            "by_path": {p: agg(v) for p, v in p12["by_path"].items()}},
    }
    json.dump(report, open(args.out, "w"), default=str, indent=2)

    def line(name, a):
        if not a.get("n"):
            print(f"  {name:14} n=0")
            return
        print(f"  {name:14} n={a['n']:<5} exp%={a['exp_pct_worst']:>6}..{a['exp_pct_best']:<6} "
              f"med={a['median_mid_pct']:>6}  win={a['win_rate_mid']:.0%}  "
              f"avgW/L={a['avg_win_mid']}/{a['avg_loss_mid']}  amb={a['ambiguous']}  {a['exit_mix']}")
    print("=== Realized % under the exit ladder (exp% = worst..best, ambiguous-bounded) ===")
    print("-- Path 1/2 baseline --")
    line("P1/2 raw", report["path12_baseline"]["raw"])
    line("P1/2 floor", report["path12_baseline"]["floor"])
    for p, v in report["path12_baseline"]["by_path"].items():
        line(p, v)
    print("-- Path 3 (floored) --")
    for k in res:
        line(k + " floor", report["path3"][k]["floor"])
    print("-- Path 3 (raw) --")
    for k in res:
        line(k + " raw", report["path3"][k]["raw"])
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
