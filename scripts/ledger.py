#!/usr/bin/env python3
"""PER-TRADE LEDGER for one day. R&D, read-only. NO AGGREGATES BY DESIGN.

Purpose: the 292-trade aggregate is not proof. This emits every scanner-confirmed symbol for a
single day, in time order, TAKEN and SKIPPED, so each row can be validated against the TOS chart
and against broker/OMS actuals -- the same trade-by-trade method that caught the Polygon/Schwab
bar defect.

Deliberately prints no mean/median. The point is to read it row by row.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.proximity_sweep import (
    _in_entry_window,
    find_proximity_signals,
    simulate_cell_honest,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

ET = ZoneInfo("America/New_York")
BARS = Path("/var/lib/project-mai-tai/schwab_rest_bars")
PROX, FLOOR, STOP, LAT = 2.0, 2.0, -5.0, 1.0


def et(ms):
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(ET).strftime("%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-07-21")
    ap.add_argument("--out", default="/tmp/ledger")
    args = ap.parse_args()
    day = args.day

    settings = get_settings()
    sf = build_session_factory(settings)
    src = DbMarketDataSource(sf)

    # ---- scanner windows for the day
    with sf() as s:
        ev = s.execute(text("""
            SELECT symbol, event_type, event_at, confirm_path, rank_score, price,
                   day_volume, float_used, change_pct
            FROM scanner_confirmed_events
            WHERE trade_date = :d ORDER BY event_at
        """), {"d": day}).all()
        live = s.execute(text("""
            SELECT f.symbol, s2.code, f.side, f.quantity, f.price, f.filled_at, ba.name
            FROM fills f
            JOIN strategies s2 ON s2.id = f.strategy_id
            JOIN broker_accounts ba ON ba.id = f.broker_account_id
            WHERE ba.name LIKE 'live:%'
              AND (f.filled_at AT TIME ZONE 'America/New_York')::date = :d
            ORDER BY f.filled_at
        """), {"d": day}).all()

    confirms, drops = {}, {}
    meta = {}
    for sym, et_, at, path, rank, px, vol, flt, chg in ev:
        if et_ == "CONFIRM":
            confirms.setdefault(sym, at)
            meta.setdefault(sym, dict(path=path, rank=rank, price=px, vol=vol, flt=flt, chg=chg))
        elif sym in confirms:
            drops.setdefault(sym, at)

    live_by_sym = defaultdict(list)
    for sym, code, side, qty, px, at, acct in live:
        live_by_sym[sym].append(dict(code=code, side=side, qty=float(qty), px=float(px),
                                     at=at, acct=acct))

    day_dir = BARS / day
    rows = []
    funnel = defaultdict(int)

    for sym in sorted(confirms, key=lambda k: confirms[k]):
        funnel["confirmed"] += 1
        ca = confirms[sym]
        da = drops.get(sym, ca + timedelta(hours=8))
        m = meta.get(sym, {})
        base = {
            "symbol": sym,
            "confirm_et": ca.astimezone(ET).strftime("%H:%M:%S"),
            "drop_et": da.astimezone(ET).strftime("%H:%M:%S"),
            "window_min": round((da - ca).total_seconds() / 60, 1),
            "confirm_path": m.get("path") or "",
            "rank_score": m.get("rank") or "",
            "confirm_price": m.get("price") or "",
            "day_volume": m.get("vol") or "",
            "float": m.get("flt") or "",
            "change_pct": m.get("chg") or "",
            "spread_pct_at_confirm": "",
            "flip_et": "", "flip_price": "", "signal_et": "", "signal_prox_pct": "",
            "rule": f"chase prox{PROX}% floor{FLOOR}% stop{STOP}%",
            "entry_et": "", "entry_px": "", "exit_et": "", "exit_px": "", "exit_reason": "",
            "pnl_pct": "", "status": "", "skip_gate": "",
            "live_side": "", "live_submit_et": "", "live_px": "", "live_exit_px": "",
            "live_pnl_pct": "", "delta_vs_backtest": "", "reconciliation": "",
        }

        f = day_dir / f"{sym}.json"
        if not f.exists():
            base["status"] = "SKIPPED"; base["skip_gate"] = "no_schwab_bars"
            funnel["skip_no_bars"] += 1
            rows.append(base); continue
        cs = json.loads(f.read_text())
        bars = [Bar(ts=int(c["datetime"]), open=float(c["open"]), high=float(c["high"]),
                    low=float(c["low"]), close=float(c["close"]), volume=int(c["volume"]))
                for c in cs if float(c["close"]) > 0]
        if len(bars) < 80:
            base["status"] = "SKIPPED"; base["skip_gate"] = f"thin_bars({len(bars)})"
            funnel["skip_thin"] += 1
            rows.append(base); continue

        quotes = src.quotes(sym, ca - timedelta(minutes=5), da + timedelta(minutes=15))
        q0 = next((q for q in quotes if q.bid > 0 and q.ask > 0), None)
        if q0:
            base["spread_pct_at_confirm"] = round((q0.ask - q0.bid) / q0.bid * 100, 3)

        atr = compute_atr_trail(bars)
        cut = int(ca.timestamp() * 1000)
        for i, r in enumerate(atr):
            if bars[i].ts < cut:
                r["state"] = "warmup"

        flip = next(((bars[i].ts, atr[i].get("close")) for i in range(len(atr))
                     if atr[i].get("flip") == "BUY" and bars[i].ts >= cut), None)
        if flip:
            base["flip_et"], base["flip_price"] = et(flip[0]), flip[1]

        sigs = find_proximity_signals(atr, PROX)
        if not sigs:
            base["status"] = "SKIPPED"; base["skip_gate"] = "no_proximity_signal"
            funnel["skip_no_signal"] += 1
            rows.append(base); continue
        funnel["signals"] += len(sigs)

        in_win = [i for i in sigs if _in_entry_window(bars[i].ts)]
        if not in_win:
            base["status"] = "SKIPPED"; base["skip_gate"] = "out_of_window(07:00-16:30)"
            base["signal_et"] = et(bars[sigs[0]].ts)
            funnel["skip_out_of_window"] += len(sigs)
            rows.append(base); continue
        funnel["in_window"] += len(in_win)

        if not quotes:
            base["status"] = "SKIPPED"; base["skip_gate"] = "no_quotes_for_fill"
            base["signal_et"] = et(bars[in_win[0]].ts)
            funnel["skip_no_quotes"] += 1
            rows.append(base); continue

        trades = [t for t in simulate_cell_honest(bars, atr, quotes, symbol=sym, day=day,
                                                  threshold_pct=PROX, stop_pct=STOP,
                                                  floor_start_pct=FLOOR, latency_s=LAT)
                  if _in_entry_window(t.entry_ts)]
        if not trades:
            base["status"] = "SKIPPED"; base["skip_gate"] = "signal_but_no_fill"
            base["signal_et"] = et(bars[in_win[0]].ts)
            rows.append(base); continue

        lv = live_by_sym.get(sym, [])
        for t in trades:
            funnel["filled"] += 1
            row = dict(base)
            row.update({
                "signal_et": et(t.entry_ts),
                "signal_prox_pct": round(t.signal_prox_pct, 3) if t.signal_prox_pct else "",
                "entry_et": et(t.entry_ts + 60000 + int(LAT * 1000)),
                "entry_px": round(t.entry_price, 4),
                "exit_et": et(t.exit_ts), "exit_px": round(t.exit_price, 4),
                "exit_reason": t.reason, "pnl_pct": round(t.pnl_pct, 3), "status": "TAKEN",
            })
            if lv:
                buys = [x for x in lv if x["side"] == "buy"]
                sells = [x for x in lv if x["side"] == "sell"]
                if buys:
                    b = buys[0]
                    row["live_side"] = f'{b["code"]} buy {b["qty"]:g}'
                    row["live_submit_et"] = b["at"].astimezone(ET).strftime("%H:%M:%S")
                    row["live_px"] = round(b["px"], 4)
                    if sells:
                        sx = sells[-1]
                        row["live_exit_px"] = round(sx["px"], 4)
                        lp = (sx["px"] - b["px"]) / b["px"] * 100
                        row["live_pnl_pct"] = round(lp, 3)
                        row["delta_vs_backtest"] = round(lp - t.pnl_pct, 3)
                        row["reconciliation"] = (
                            "MATCH" if abs(b["px"] - t.entry_price) <= 0.01
                            else "RECONCILIATION MISS (entry px differs > 1c)")
                    else:
                        row["reconciliation"] = "live still open / no sell"
            else:
                row["reconciliation"] = "no live trade on this name"
            rows.append(row)

    out = Path(args.out)
    csv_path = out.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("=" * 120)
    print(f"PER-TRADE LEDGER — {day} (ET)   [NO AGGREGATES: read row by row]")
    print("=" * 120)
    print(f"  BAR SOURCE   : SCHWAB REST pricehistory 1-min  ({BARS}/{day})  -- NOT Polygon")
    print(f"  QUOTE SOURCE : market_capture_quotes (Polygon NBBO)  <-- fills are approximated from this;")
    print(f"                 the live bot fills at Schwab, so fill prices carry residual error")
    print(f"  FILL MODEL   : entry = observed ASK at signal-bar close + {LAT}s latency;")
    print(f"                 exit  = observed BID, market-on-touch (fills at the NEXT bid, capturing slip)")
    print(f"  RULE         : chase, proximity {PROX}%, floor ladder from +{FLOOR}%, stop {STOP}%,")
    print(f"                 window 07:00-16:30 ET, one entry per ATR short-segment")
    print(f"  NOT RECORDED : RVOL is not stored by the scanner; day_volume/float/change_pct shown instead")
    print()
    print("  COUNT FUNNEL FOR THIS DAY:")
    print(f"    scanner-confirmed symbols : {funnel['confirmed']}")
    print(f"      skipped, no schwab bars : {funnel['skip_no_bars']}")
    print(f"      skipped, thin bars      : {funnel['skip_thin']}")
    print(f"      skipped, no signal      : {funnel['skip_no_signal']}")
    print(f"    proximity signals raised  : {funnel['signals']}")
    print(f"      skipped, out-of-window  : {funnel['skip_out_of_window']}")
    print(f"    in-window signals         : {funnel['in_window']}")
    print(f"    FILLED (rows marked TAKEN): {funnel['filled']}")
    if funnel["signals"]:
        print(f"    skip ratio (this day)     : "
              f"{100*(funnel['signals']-funnel['filled'])/funnel['signals']:.1f}% of signals never traded")
    print(f"\n  CSV: {csv_path}\n")

    for r in rows:
        print("-" * 120)
        print(f"  {r['symbol']:<6} {r['status']:<8} confirm {r['confirm_et']} -> drop {r['drop_et']} "
              f"({r['window_min']}min)  path={r['confirm_path']} rank={r['rank_score']}")
        print(f"         scanner: price={r['confirm_price']} chg%={r['change_pct']} "
              f"dayvol={r['day_volume']} float={r['float']} spread%@confirm={r['spread_pct_at_confirm']}")
        print(f"         signal : ATR BUY flip {r['flip_et'] or '(none in window)'} @ {r['flip_price']}"
              f"   proximity-signal {r['signal_et'] or '-'} prox%={r['signal_prox_pct']}")
        if r["status"] == "TAKEN":
            print(f"         BACKTEST: entry {r['entry_et']} @ {r['entry_px']} -> "
                  f"exit {r['exit_et']} @ {r['exit_px']} [{r['exit_reason']}]  P&L {r['pnl_pct']}%")
        else:
            print(f"         SKIPPED BY GATE: {r['skip_gate']}")
        if r["reconciliation"]:
            print(f"         LIVE    : {r['live_side'] or '-'} submit={r['live_submit_et'] or '-'} "
                  f"px={r['live_px'] or '-'} exit={r['live_exit_px'] or '-'} "
                  f"P&L={r['live_pnl_pct'] or '-'}%  delta={r['delta_vs_backtest'] or '-'}  "
                  f"=> {r['reconciliation']}")
    print("-" * 120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
