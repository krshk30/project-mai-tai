"""Weekly P3-B ledger — June 8–12 2026, per-day mover selection, exit ladder.

Read-only. Settles whether P3-B's strong 06-12 result holds across a week or was
a good day. Per day: (1) confirm stored-bar completeness (skip/flag gaps), (2)
select the top-5 movers from that day's v2-scanner universe (reproducible
criterion: intraday range %), (3) run P3-B (touch+floor), P1-MACD, P2-VWAP through
the documented OMS exit ladder (qty 10). Markdown summary + full-row CSV.

⚠️ ANECDOTE-SCALE — 5 days, few names/day; directional, NOT statistical.
⚠️ IDEALIZED fills, no slippage/spread — and **P3-B trades the most → most fills →
most cost exposure**; the Phase-2 measured spread is decisive and could flip P3-B's
sign. ⚠️ BOTH-HIT AMBIGUITY → bounded (fav/adv), never point estimates.
⚠️ Models the OLD-bot exit ladder, NOT what v2 runs today (v2 has no exits — top item).

Selection criterion (reproducible): per day, from symbols with ≥30 schwab_1m_v2
stored bars after 11:00 UTC, the top 5 by intraday range %
((max_high − min_low)/min_low). Completeness gate: ≥330/390 RTH minutes covered.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import UTC, datetime

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atr_flip import ET, compute_atr_trail, fetch_day  # noqa: E402
from path3_backtest import extract_signals  # noqa: E402
from exit_ladder_rescore import macd_cross_below_series  # noqa: E402
from trade_ledger import VOL_FLOOR, et_hm, trade_rows  # noqa: E402

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient  # noqa: E402
from project_mai_tai.settings import Settings  # noqa: E402

DAYS = ["2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"]
TOP_N = 5
RTH_MIN_GATE = 330


def _dsn():
    return os.environ["MAI_TAI_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")


def day_universe(conn, day):
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT count(DISTINCT date_trunc('minute', bar_time))
                FILTER (WHERE bar_time >= '{day} 13:30+00' AND bar_time < '{day} 20:00+00')
                FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND bar_time::date='{day}'""")
        rth_cov = cur.fetchone()[0]
        cur.execute(
            f"""SELECT symbol, count(*) AS bars,
                   round(((max(high_price)-min(low_price))/NULLIF(min(low_price),0)*100)::numeric,1) AS range_pct
                FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2'
                  AND bar_time::date='{day}' AND bar_time >= '{day} 11:00+00'
                GROUP BY symbol HAVING count(*) >= 30 ORDER BY range_pct DESC LIMIT {TOP_N}""")
        movers = [{"sym": s, "bars": b, "range_pct": float(r)} for s, b, r in cur.fetchall()]
    return rth_cov, movers


def day_path12(conn, day, symbols):
    out = {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ti.symbol, (ti.payload->'metadata'->>'bar_time_ms')::bigint,
                      (ti.payload->'metadata'->>'entry_price')::numeric,
                      ti.payload->'metadata'->>'path'
               FROM trade_intents ti JOIN strategies st ON st.id=ti.strategy_id
               WHERE st.code='schwab_1m_v2' AND ti.symbol = ANY(%s)
                 AND ti.payload->'metadata'->>'bar_time_ms' IS NOT NULL""", (symbols,))
        for sym, bar_ms, entry, path in cur.fetchall():
            d = datetime.fromtimestamp(int(bar_ms) / 1000, UTC).astimezone(ET).strftime("%Y-%m-%d")
            if d == day:
                out.setdefault((sym, path or "?"), []).append({"bar_ms": int(bar_ms), "entry": float(entry)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="/tmp/weekly_ledger.md")
    ap.add_argument("--csv", default="/tmp/weekly_ledger.csv")
    args = ap.parse_args()
    s = Settings()
    client = SchwabV2RestClient(s, on_chart_bar=lambda *a: None, on_quote=lambda *a: None)

    all_rows = []
    per_day = []                         # {day, cov, movers, cells:{path:{n,wins,losses,fav,adv,amb}}}
    PATHS = ["P3-B(touch,vol>5k)", "P1-MACD Cross", "P2-VWAP Breakout"]

    with psycopg.connect(_dsn()) as conn:
        for day in DAYS:
            cov, movers = day_universe(conn, day)
            syms = [m["sym"] for m in movers]
            complete = cov >= RTH_MIN_GATE
            p12 = day_path12(conn, day, syms)
            cells = {p: {"n": 0, "wins": 0, "losses": 0, "fav": 0.0, "adv": 0.0, "amb": 0} for p in PATHS}
            if complete:
                for sym in syms:
                    bars = fetch_day(client, s, sym, day)
                    if len(bars) < 40:
                        continue
                    xbelow = macd_cross_below_series([b.close for b in bars])
                    rows_atr = compute_atr_trail(bars)
                    bidx = {b.ts: i for i, b in enumerate(bars)}

                    def run(label, entries):
                        for (ei, ep) in entries:
                            r, meta = trade_rows(label, sym, ei, ep, bars, xbelow)
                            for row in r:
                                row["Day"] = day
                            all_rows.extend(r)
                            c = cells[label]
                            c["n"] += 1; c["fav"] += meta["fav"]; c["adv"] += meta["adv"]
                            c["amb"] += 1 if meta["ambiguous"] else 0
                            c["wins"] += 1 if meta["fav"] > 0 else 0
                            c["losses"] += 1 if meta["fav"] <= 0 else 0

                    run("P3-B(touch,vol>5k)",
                        [(ei, ep) for (ei, ep) in extract_signals(bars, rows_atr, "B") if bars[ei].volume > VOL_FLOOR])
                    run("P1-MACD Cross",
                        [(bidx[g["bar_ms"]], g["entry"]) for g in p12.get((sym, "MACD Cross"), []) if g["bar_ms"] in bidx])
                    run("P2-VWAP Breakout",
                        [(bidx[g["bar_ms"]], g["entry"]) for g in p12.get((sym, "VWAP Breakout"), []) if g["bar_ms"] in bidx])
            per_day.append({"day": day, "cov": cov, "complete": complete, "movers": movers, "cells": cells})

    # ---- CSV (every fill row) ----
    cols = ["Day", "Path", "Symbol", "EntryTime", "EntryPrice", "ExitTime", "ExitPrice", "Shares",
            "ExitReason", "PnL$", "PnL%", "CumTradePnL$"]
    with open(args.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(all_rows)

    # ---- Markdown ----
    L = ["# Weekly P3-B Ledger — June 8–12 2026 (qty 10/entry)\n"]
    L += ["> **ANECDOTE, NOT STATISTICS** — 5 days, top-5 movers/day; directional, not a verdict.",
          "> **IDEALIZED fills** (no slippage/spread). **P3-B trades the MOST → most fills → most cost exposure**; the Phase-2 measured spread is decisive and could flip P3-B's sign.",
          "> **`fav$ / adv$`** = both-hit ambiguity bound (favorable-first vs adverse-first), never a point estimate; ticks resolve in Phase 2.",
          "> Models the OLD-bot exit ladder applied to these entries — NOT what v2 runs today (v2 has no exits — TOP open item).",
          "> **Selection (reproducible):** per day, top-5 by intraday range % from the v2-scanner universe (≥30 bars after 11:00 UTC). **Completeness gate:** ≥330/390 RTH minutes.\n"]
    L.append("## Per-day summary\n")
    for d in per_day:
        flag = "✅ complete" if d["complete"] else f"⚠️ PARTIAL ({d['cov']}/390) — SKIPPED"
        movs = ", ".join(f"{m['sym']}({m['range_pct']:.0f}%)" for m in d["movers"])
        L.append(f"### {d['day']} — RTH {d['cov']}/390 {flag}")
        L.append(f"Movers (range%): {movs}\n")
        L.append("| Path | Entries | Wins | Losses | Ambig | P&L fav $ | P&L adv $ |")
        L.append("|---|---|---|---|---|---|---|")
        for p in PATHS:
            c = d["cells"][p]
            L.append(f"| {p} | {c['n']} | {c['wins']} | {c['losses']} | {c['amb']} | {round(c['fav'],2)} | {round(c['adv'],2)} |")
        L.append("")
    # grand total
    L.append("## Grand total — per path, all 5 days\n")
    L.append("| Path | Entries | Wins | Losses | Total P&L fav $ | Total P&L adv $ |")
    L.append("|---|---|---|---|---|---|")
    for p in PATHS:
        n = sum(d["cells"][p]["n"] for d in per_day)
        w_ = sum(d["cells"][p]["wins"] for d in per_day)
        l_ = sum(d["cells"][p]["losses"] for d in per_day)
        fav = sum(d["cells"][p]["fav"] for d in per_day)
        adv = sum(d["cells"][p]["adv"] for d in per_day)
        L.append(f"| {p} | {n} | {w_} | {l_} | {round(fav,2)} | {round(adv,2)} |")
    # P3-B consistency
    L.append("\n## P3-B daily consistency (the real question)\n")
    L.append("| Day | Entries | P&L fav $ | P&L adv $ |")
    L.append("|---|---|---|---|")
    pos_days = 0
    for d in per_day:
        c = d["cells"]["P3-B(touch,vol>5k)"]
        if c["fav"] > 0:
            pos_days += 1
        L.append(f"| {d['day']} | {c['n']} | {round(c['fav'],2)} | {round(c['adv'],2)} |")
    L.append(f"\n**P3-B positive (fav) on {pos_days}/5 days.** Full per-fill rows in the CSV ({len(all_rows)} rows).")
    open(args.md, "w").write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\nrows={len(all_rows)}  wrote {args.md} + {args.csv}")


if __name__ == "__main__":
    main()
