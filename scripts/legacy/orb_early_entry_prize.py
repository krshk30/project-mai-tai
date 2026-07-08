"""R&D: the 09:30-09:34 missed-breakout PRIZE (split by day character) + the pre-market ATR-seed test.

Part 1 (the decider): count/PNL the tick-entry breakouts in [09:30, 09:34) — the entries the causal
ATR gate misses (fail-closed until ~09:34) — SPLIT BY DAY CHARACTER (hot-open vs soft), because the
prize concentrates on flood days and averaging hides it. Restricted to HIGH-ATR name-days (what the
gate would recover) + all-names for context.

Part 2 (conditional): would a pre-market seed make the gate computable at 09:30 WITHOUT mislabeling?
Compare, per name, the gate DECISION three ways vs the full-window truth:
  premkt(09:00-09:30 bars)@09:30  vs  RTH(09:25-09:34 bars)@09:34 [current]  vs  full-window truth.

Uses the production OrbTickEntry engine (simulate_orb_tick_entry, gate off) for the entries.
"""
from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import simulate_orb_tick_entry
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import atr_pct5

_ET = ZoneInfo("America/New_York")
GATE = 4.3          # high-ATR gate threshold (ATR5% >=)
EARLY_MIN = 4       # [09:30, 09:34) — the fail-closed window


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def main():
    dates = [a for a in sys.argv[1:] if a.count("-") == 2 and a[:1].isdigit()]
    wdir = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--windows-dir=")), None)
    src = DbMarketDataSource(build_session_factory(get_settings()))
    rows = []       # per name-day
    for date in dates:
        y, mo, d = (int(x) for x in date.split("-"))
        obs, so, cut, end = _et(y, mo, d, 9, 25), _et(y, mo, d, 9, 30), _et(y, mo, d, 10, 0), _et(y, mo, d, 10, 10)
        premkt0 = _et(y, mo, d, 9, 0)
        early_end = so + timedelta(minutes=EARLY_MIN)
        wins = load_windows(f"{wdir}/windows_{date}.json") if wdir else {}
        for sym in src.orb_qualified_symbols(obs, end, min_trades=500):
            ewin = wins.get(sym, [])
            if not ewin:
                continue
            trades = src.trades(sym, obs, end)
            quotes = src.quotes(sym, obs, end)
            if len(trades) < 500 or len(quotes) < 50:
                continue
            bars = build_bars(trades, so)
            if len(bars) < 10:
                continue
            full_atr = atr_pct5(bars)                                  # truth (full ORB window)
            rth934 = atr_pct5(build_bars([t for t in trades if t.ts < _et(y, mo, d, 9, 34)], so))
            pm_tr = src.trades(sym, premkt0, so)
            premkt = atr_pct5(build_bars(pm_tr, so)) if len(pm_tr) >= 50 else None
            tk = simulate_orb_tick_entry(trades, quotes, gap_cap_pct=1.5, trail_pct=2.0, qty=5,
                                         observe_open=obs, session_open=so, cutoff=cut, capped=False,
                                         latency_s=3.0, entry_windows=ewin)   # gate OFF -> all breaks
            early = [t for t in tk if so <= t.entry_ts < early_end]
            rows.append({
                "date": date, "sym": sym, "full": full_atr, "rth934": rth934, "premkt": premkt,
                "n_trades": len(trades), "n_entry": len(tk), "pnl": sum(t.pnl for t in tk),
                "n_early": len(early), "pnl_early": sum(t.pnl for t in early),
                "hi": (full_atr is not None and full_atr >= GATE)})

    # ---- day character = ORB-window trade FLOOD per day (sum of loaded trades) ----
    day_trades = {}
    for r in rows:
        day_trades[r["date"]] = day_trades.get(r["date"], 0) + r["n_trades"]
    med_flood = statistics.median(day_trades.values())
    hot_days = {d for d, v in day_trades.items() if v >= med_flood}

    def blk(sub, label):
        n_early = sum(r["n_early"] for r in sub)
        pnl_early = sum(r["pnl_early"] for r in sub)
        pnl_tot = sum(r["pnl"] for r in sub)
        n_entry = sum(r["n_entry"] for r in sub)
        pct = (pnl_early / pnl_tot * 100) if pnl_tot else 0.0
        print(f"  {label:<26} name-days={len(sub):<3} early-entries={n_early:<3} early-P&L={pnl_early:+7.2f}"
              f"  (total entries={n_entry} P&L={pnl_tot:+.2f}; early = {pct:+.0f}% of total P&L)")

    print(f"\n{'='*96}\nPART 1 — 09:30-09:34 PRIZE, split by day character (hot vs soft open)\n{'='*96}")
    print("per-day flood (ORB-window trades) & prize:")
    print(f"  {'date':<11}{'flood':>10}{'hot?':>5}{'names':>7}{'early_n':>8}{'early_pnl':>10}{'day_pnl':>9}")
    for date in dates:
        sub = [r for r in rows if r["date"] == date]
        if not sub:
            continue
        print(f"  {date:<11}{day_trades[date]:>10}{('HOT' if date in hot_days else 'soft'):>5}{len(sub):>7}"
              f"{sum(r['n_early'] for r in sub):>8}{sum(r['pnl_early'] for r in sub):>+10.2f}{sum(r['pnl'] for r in sub):>+9.2f}")
    print(f"\nflood median = {med_flood:.0f} trades/day; HOT = >= median")
    print("\nHIGH-ATR name-days only (what the gate would recover):")
    hi = [r for r in rows if r["hi"]]
    blk([r for r in hi if r["date"] in hot_days], "HOT days, high-ATR")
    blk([r for r in hi if r["date"] not in hot_days], "SOFT days, high-ATR")
    print("\nALL name-days (context):")
    blk([r for r in rows if r["date"] in hot_days], "HOT days, all")
    blk([r for r in rows if r["date"] not in hot_days], "SOFT days, all")

    # ---- Part 2: seed agreement ----
    print(f"\n{'='*96}\nPART 2 — ATR seed agreement: does premkt@09:30 gate == RTH@09:34 gate (and truth)?\n{'='*96}")
    cov = [r for r in rows if r["premkt"] is not None and r["rth934"] is not None and r["full"] is not None]
    print(f"pre-market coverage: {len(cov)}/{len(rows)} name-days have usable premkt bars (>=50 trades 09:00-09:30)")

    def gate(v):
        return None if v is None else (v >= GATE)

    def agree(a_key, b_key, label):
        pairs = [(gate(r[a_key]), gate(r[b_key])) for r in cov]
        same = sum(1 for a, b in pairs if a == b)
        # confusion
        pp = sum(1 for a, b in pairs if a and b); nn = sum(1 for a, b in pairs if not a and not b)
        ab = sum(1 for a, b in pairs if a and not b); ba = sum(1 for a, b in pairs if not a and b)
        print(f"  {label:<34} agree {same}/{len(pairs)} ({same/len(pairs)*100:.0f}%)  "
              f"[both-hi {pp}, both-slow {nn}, {a_key}-only {ab}, {b_key}-only {ba}]")

    agree("premkt", "rth934", "premkt@09:30 vs RTH@09:34")
    agree("premkt", "full", "premkt@09:30 vs full-truth")
    agree("rth934", "full", "RTH@09:34 vs full-truth")

    print(f"\n{'='*96}\nPER NAME-DAY: ATR three ways (sorted by full ATR%)\n{'='*96}")
    print(f"  {'date':<11}{'sym':<6}{'full':>7}{'rth934':>8}{'premkt':>8}  {'premkt=rth?':>12}{'early_n':>8}")
    for r in sorted(cov, key=lambda r: r["full"] or 0, reverse=True):
        pm, r934 = gate(r["premkt"]), gate(r["rth934"])
        tag = "AGREE" if pm == r934 else "DISAGREE"
        pmv = f"{r['premkt']:.2f}" if r["premkt"] is not None else "  -"
        print(f"  {r['date']:<11}{r['sym']:<6}{r['full']:>7.2f}{r['rth934']:>8.2f}{pmv:>8}  {tag:>12}{r['n_early']:>8}")


if __name__ == "__main__":
    main()
