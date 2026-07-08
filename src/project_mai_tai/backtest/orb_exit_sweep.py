"""DECISION-2 Part-2: ORB exit sweep — trail width × hard-stop-under-trail, both modes, per name,
classified by behavior, robustness-ready.

Trail: 2 / 3(live) / 4 / 5 % / ATR-adaptive (trail% = the name's period-14 ATR%).
Hard stop under the trail: none(live) / −2% / −3% fixed loss floor beneath the trail.
Modes: bar_close + intrabar. Feed = massive/stream (market_capture) per the ORB data-source
decision; honest fills; Webull 3s latency (baseline — Part 1 showed fade-slippage scales with it).

Classification (sweep-independent, reused from the ATR study): period-14 ATR% (volatility),
Kaufman ER = |net move|/total path (directionality), median price (price bucket). Dumps raw
metrics + per-config bc/ib P&L; buckets + robustness assigned in aggregation.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import statistics

from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import (
    simulate_bar_close,
    simulate_intrabar,
    simulate_orb_tick_entry,
)
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.backtest.v2_wait3break import bars_from_trades
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")


def classify_metrics(bars):
    """Window-appropriate (ORB window ~45min): period-5 ATR% (volatility) + Kaufman ER
    (directionality) + median price. Needs >=10 bars."""
    closes = [b.close for b in bars if b.close > 0]
    if len(closes) < 10:
        return None
    rows = compute_atr_trail(bars, period=5, factor=1.0)   # factor 1 -> loss == raw ATR5
    atrp = [rows[i]["loss"] / bars[i].close * 100 for i in range(len(bars))
            if rows[i]["loss"] is not None and bars[i].close > 0]
    if not atrp:
        return None
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    er = net / path if path > 0 else 0.0
    return {"atr_pct5": statistics.median(atrp), "er": er, "price": statistics.median(closes)}
TRAILS = ["2", "3", "4", "5", "atr"]   # atr = per-name period-14 ATR%
HARDS = ["none", "2", "3"]
GAP_CAP, QTY, LAT = 1.5, 5, 3.0


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def main():
    argv = sys.argv[1:]
    jsonp, wdir, engine = None, None, "research"
    for a in argv:
        if a.startswith("--json="):
            jsonp = a.split("=", 1)[1]
        elif a.startswith("--windows-dir="):
            wdir = a.split("=", 1)[1]
        elif a.startswith("--engine="):
            engine = a.split("=", 1)[1]     # research=simulate_intrabar; production=OrbTickEntry engine
    intrabar_fn = simulate_orb_tick_entry if engine == "production" else simulate_intrabar
    print(f"INTRABAR ENGINE = {engine} ({intrabar_fn.__name__})")
    dates = [a for a in argv if a.count("-") == 2 and a[:1].isdigit()]
    src = DbMarketDataSource(build_session_factory(get_settings()))
    out_rows = []
    for date in dates:
        y, mo, d = (int(x) for x in date.split("-"))
        obs, so, cut, end = _et(y, mo, d, 9, 25), _et(y, mo, d, 9, 30), _et(y, mo, d, 10, 0), _et(y, mo, d, 10, 10)
        wins_by_sym = load_windows(f"{wdir}/windows_{date}.json") if wdir else None
        syms = src.orb_qualified_symbols(obs, end, min_trades=500)
        gated = "confirmed-window GATED" if wdir else "NO window gate"
        print(f"DAY {date}: {len(syms)} ORB-qualified ({gated})", flush=True)
        base = dict(gap_cap_pct=GAP_CAP, qty=QTY, observe_open=obs, session_open=so,
                    cutoff=cut, capped=False, latency_s=LAT)
        for sym in syms:
            ewin = wins_by_sym.get(sym, []) if wdir else None
            if wdir and not ewin:
                continue  # name never scanner-confirmed that day -> live ORB couldn't trade it
            trades = src.trades(sym, obs, end)
            quotes = src.quotes(sym, obs, end)
            if len(trades) < 500 or len(quotes) < 50:
                continue
            bars = build_bars(trades, so)
            if len(bars) < 5:
                continue
            met = classify_metrics(bars_from_trades(trades))
            configs = {}
            traded = False
            for tw in TRAILS:
                if tw == "atr":
                    if not met:
                        continue
                    tp = met["atr_pct5"]
                else:
                    tp = float(tw)
                for hd in HARDS:
                    hs = None if hd == "none" else float(hd)
                    bc = simulate_bar_close(bars, quotes, trail_pct=tp, hard_stop_pct=hs, entry_windows=ewin, **base)
                    ib = intrabar_fn(trades, quotes, trail_pct=tp, hard_stop_pct=hs, entry_windows=ewin, **base)
                    configs[f"t{tw}_h{hd}"] = {
                        "bc": round(sum(t.pnl for t in bc), 3), "bcn": len(bc),
                        "ib": round(sum(t.pnl for t in ib), 3), "ibn": len(ib)}
                    traded = traded or bc or ib
            if not traded:
                continue
            out_rows.append({"date": date, "sym": sym, "metrics": met, "configs": configs})
            b = configs.get("t3_hnone", {})
            print(f"  {sym:<6} " + (f"atr5%={met['atr_pct5']:.2f} er={met['er']:.2f} price={met['price']:.2f}" if met else "met=NA")
                  + f" | live(t3,noHS) bc={b.get('bc',0):+.2f}({b.get('bcn',0)}) ib={b.get('ib',0):+.2f}({b.get('ibn',0)})", flush=True)
    payload = {"trails": TRAILS, "hards": HARDS, "qty": QTY, "lat": LAT, "dates": dates,
               "baseline": "t3_hnone", "rows": out_rows}
    if jsonp:
        json.dump(payload, open(jsonp, "w"))
        print(f"[json -> {jsonp}]  ({len(out_rows)} name-days)", flush=True)


if __name__ == "__main__":
    main()
