"""Per-trade fill LEDGER — 3 symbols, 1 day, all entry paths, exit ladder applied.

Read-only. For VSME/CAST/BYAH on a day, replays each entry path through the
documented OMS exit ladder (docs/oms-exit-logic-reference.md) and emits ONE ROW
PER FILL EVENT (each scale partial + the final close), qty=10. Markdown + CSV.

This is a human-readable, TOS-cross-checkable ledger — an ANECDOTE (3 symbols, 1
day), NOT statistics. Entry prices match the ATR parity check / the stored v2
intents so they can be cross-checked against TOS.

⚠️ IDEALIZED fills — modeled price, NO slippage / NO spread. At qty 10 on sub-$3
names this is an UPPER BOUND; real costs (Phase 2) could change the sign.
⚠️ BOTH-HIT AMBIGUITY — when a scale tier and the stop/floor fall in one candle,
the trade is marked AMBIGUOUS and BOTH favorable-first and adverse-first totals are
shown (bounded), never a single guessed number.
⚠️ Models the OLD-bot ladder applied to these entries — NOT what v2 does today
(v2 runs no managed exits). v1 limitation: stoch tier-exit not modeled (macd is).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import UTC, datetime

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atr_flip import ET, Bar, compute_atr_trail, fetch_day  # noqa: E402
from path3_backtest import extract_signals  # noqa: E402
from exit_ladder_rescore import _floor_pct, _scale_action, macd_cross_below_series  # noqa: E402

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient  # noqa: E402
from project_mai_tai.settings import Settings  # noqa: E402

STOP_PCT = 1.5
QTY = 10
VOL_FLOOR = 5000


def _dsn():
    return os.environ["MAI_TAI_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")


def et_hm(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, UTC).astimezone(ET).strftime("%H:%M")


def simulate_fills(entry: float, fwd: list[Bar], xbelow: list[bool], optimistic: bool):
    """Return list of fill dicts: {et, price, shares, reason}."""
    remaining = QTY
    peak, floor_pct = 0.0, -999.0
    done: set = set()
    fills: list[dict] = []
    stop_price = entry * (1 - STOP_PCT / 100)

    def add(et, price, shares, reason):
        nonlocal remaining
        fills.append({"et": et, "price": round(price, 4), "shares": shares, "reason": reason})
        remaining -= shares

    def scales(hp, et):
        while remaining > 0:
            a = _scale_action(hp, done)
            if not a:
                break
            lvl, frac, trig = a
            sh = min(remaining, max(1, int(remaining * frac)))
            add(et, entry * (1 + trig / 100), sh, f"scale+{int(trig)}%")
            done.add(lvl)

    def down(lp, et) -> bool:
        if floor_pct > -999 and lp <= floor_pct:
            add(et, entry * (1 + floor_pct / 100), remaining,
                "floor@BE" if abs(floor_pct) < 1e-9 else f"floor+{floor_pct:.2f}%")
            return True
        if lp <= -STOP_PCT:
            add(et, stop_price, remaining, "hard-stop")
            return True
        return False

    for k, b in enumerate(fwd):
        hp, lp, et = (b.high - entry) / entry * 100, (b.low - entry) / entry * 100, et_hm(b.ts)
        if optimistic:
            peak = max(peak, hp)
            floor_pct = max(floor_pct, _floor_pct(peak))
            scales(hp, et)
            if down(lp, et):
                return fills
        else:
            if down(lp, et):
                return fills
            peak = max(peak, hp)
            floor_pct = max(floor_pct, _floor_pct(peak))
            scales(hp, et)
        if remaining > 0 and k < len(xbelow) and xbelow[k]:
            add(et, b.close, remaining, "tier-macd")
            return fills
    if remaining > 0 and fwd:
        add(et_hm(fwd[-1].ts), fwd[-1].close, remaining, "session-end")
    return fills


def trade_rows(path, sym, entry_idx, entry, bars, xbelow):
    """One trade -> ledger rows (favorable-first) + ambiguity bound + total."""
    fwd, xb = bars[entry_idx + 1:], xbelow[entry_idx + 1:]
    fav = simulate_fills(entry, fwd, xb, True)
    adv = simulate_fills(entry, fwd, xb, False)
    tot = lambda fl: sum(f["shares"] * (f["price"] - entry) for f in fl)
    fav_pnl, adv_pnl = tot(fav), tot(adv)
    ambiguous = abs(fav_pnl - adv_pnl) > 1e-6
    et_in = et_hm(bars[entry_idx].ts)
    rows, cum = [], 0.0
    for f in fav:
        pnl = f["shares"] * (f["price"] - entry)
        cum += pnl
        rows.append({
            "Path": path, "Symbol": sym, "EntryTime": et_in, "EntryPrice": round(entry, 4),
            "ExitTime": f["et"], "ExitPrice": f["price"], "Shares": f["shares"],
            "ExitReason": f["reason"] + (" *AMBIG" if ambiguous else ""),
            "PnL$": round(pnl, 4), "PnL%": round((f["price"] - entry) / entry * 100, 3),
            "CumTradePnL$": round(cum, 4),
        })
    return rows, {"fav": fav_pnl, "adv": adv_pnl, "ambiguous": ambiguous}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-06-12")
    ap.add_argument("--symbols", default="VSME,CAST,BYAH")
    ap.add_argument("--md", default="/tmp/ledger.md")
    ap.add_argument("--csv", default="/tmp/ledger.csv")
    args = ap.parse_args()
    s = Settings()
    client = SchwabV2RestClient(s, on_chart_bar=lambda *a: None, on_quote=lambda *a: None)
    symbols = [x.strip().upper() for x in args.symbols.split(",")]

    # Path 1/2 stored signals for these symbols/day
    p12: dict[tuple[str, str], list[dict]] = {}
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT ti.symbol, (ti.payload->'metadata'->>'bar_time_ms')::bigint,
                      (ti.payload->'metadata'->>'entry_price')::numeric,
                      ti.payload->'metadata'->>'path'
               FROM trade_intents ti JOIN strategies st ON st.id=ti.strategy_id
               WHERE st.code='schwab_1m_v2' AND ti.symbol = ANY(%s)
                 AND ti.payload->'metadata'->>'bar_time_ms' IS NOT NULL""",
            (symbols,))
        for sym, bar_ms, entry, path in cur.fetchall():
            d = datetime.fromtimestamp(int(bar_ms) / 1000, UTC).astimezone(ET).strftime("%Y-%m-%d")
            if d == args.day:
                p12.setdefault((sym, path or "?"), []).append({"bar_ms": int(bar_ms), "entry": float(entry)})

    all_rows = []
    summary = []   # (path, sym, n, total_fav, total_adv, wins, losses, avgW%, avgL%)
    for sym in symbols:
        bars = fetch_day(client, s, sym, args.day)
        if len(bars) < 12:
            continue
        xbelow = macd_cross_below_series([b.close for b in bars])
        rows_atr = compute_atr_trail(bars)
        bidx = {b.ts: i for i, b in enumerate(bars)}

        def run(path_label, entries):  # entries = list of (idx, entry_price)
            trades = []
            for (ei, ep) in entries:
                r, meta = trade_rows(path_label, sym, ei, ep, bars, xbelow)
                all_rows.extend(r)
                trades.append(meta)
            if trades:
                fav = [t["fav"] for t in trades]
                wins = sum(1 for f in fav if f > 0)
                wpct = [r["PnL%"] for r in all_rows if False]  # per-trade win% below
                # per-trade total %: use fav total / (entry*qty)? simpler: $-based
                summary.append({
                    "path": path_label, "sym": sym, "n": len(trades),
                    "total_fav$": round(sum(t["fav"] for t in trades), 2),
                    "total_adv$": round(sum(t["adv"] for t in trades), 2),
                    "wins": wins, "losses": len(trades) - wins,
                    "ambiguous": sum(1 for t in trades if t["ambiguous"]),
                })

        # Path 1 / Path 2 (stored)
        for path in ("MACD Cross", "VWAP Breakout"):
            ents = [(bidx[g["bar_ms"]], g["entry"]) for g in p12.get((sym, path), []) if g["bar_ms"] in bidx]
            run(f"P1-{path}" if path == "MACD Cross" else f"P2-{path}", ents)
        # Path 3 B (touch) + floor, and A (confirmed) + floor
        b_ents = [(ei, ep) for (ei, ep) in extract_signals(bars, rows_atr, "B") if bars[ei].volume > VOL_FLOOR]
        a_ents = [(ei, ep) for (ei, ep) in extract_signals(bars, rows_atr, "A") if bars[ei].volume > VOL_FLOOR]
        run("P3-B(touch,vol>5k)", b_ents)
        run("P3-A(flip,vol>5k)", a_ents)

    # ---- CSV ----
    cols = ["Path", "Symbol", "EntryTime", "EntryPrice", "ExitTime", "ExitPrice", "Shares",
            "ExitReason", "PnL$", "PnL%", "CumTradePnL$"]
    with open(args.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(all_rows)

    # ---- Markdown ----
    L = []
    L.append(f"# Per-trade Ledger — {args.symbols} — {args.day} (qty {QTY}/entry)\n")
    L.append("> **ANECDOTE, NOT STATISTICS** — 3 symbols, 1 day; for visual TOS cross-check, not a verdict.")
    L.append("> **IDEALIZED fills** — modeled price, NO slippage/spread; at qty 10 on sub-$3 names an UPPER BOUND (real costs, Phase 2, could flip the sign).")
    L.append("> **`*AMBIG`** rows: a scale tier + the stop/floor shared one 1-min candle → bounded; the per-trade/summary `fav$ / adv$` shows favorable-first vs adverse-first.")
    L.append("> Models the OLD-bot exit ladder applied to these entries — NOT what v2 does today (v2 has no exits). Entry prices match the ATR parity check / stored v2 intents. stoch tier-exit not modeled (macd is).\n")
    L.append("## Ledger (one row per fill)\n")
    L.append("| Path | Symbol | Entry ET | Entry $ | Exit ET | Exit $ | Sh | Exit Reason | P&L $ | P&L % | Cum trade $ |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in all_rows:
        L.append("| {Path} | {Symbol} | {EntryTime} | {EntryPrice} | {ExitTime} | {ExitPrice} | {Shares} | "
                 "{ExitReason} | {PnL$} | {PnL%} | {CumTradePnL$} |".format(**r))
    L.append("\n## Summary — per symbol × path\n")
    L.append("| Path | Symbol | Entries | Wins | Losses | Ambig | Total P&L (fav $) | Total P&L (adv $) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for s_ in summary:
        L.append(f"| {s_['path']} | {s_['sym']} | {s_['n']} | {s_['wins']} | {s_['losses']} | "
                 f"{s_['ambiguous']} | {s_['total_fav$']} | {s_['total_adv$']} |")
    L.append("\n## Grand summary — total P&L per path (all 3 symbols)\n")
    L.append("| Path | Entries | Total P&L (fav $) | Total P&L (adv $) |")
    L.append("|---|---|---|---|")
    bypath: dict = {}
    for s_ in summary:
        b = bypath.setdefault(s_["path"], {"n": 0, "fav": 0.0, "adv": 0.0})
        b["n"] += s_["n"]; b["fav"] += s_["total_fav$"]; b["adv"] += s_["total_adv$"]
    for p, b in bypath.items():
        L.append(f"| {p} | {b['n']} | {round(b['fav'],2)} | {round(b['adv'],2)} |")
    open(args.md, "w").write("\n".join(L) + "\n")
    print("\n".join(L[-(len(summary) + len(bypath) + 12):]))
    print(f"\nrows={len(all_rows)}  wrote {args.md} + {args.csv}")


if __name__ == "__main__":
    main()
