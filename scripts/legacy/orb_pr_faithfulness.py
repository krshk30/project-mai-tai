"""07-06/07-07 through the PR #403 production logic — (1) faithfulness vs the research sweep,
(2) per-trade detail for chart-validation. NOT new edge (these 2 days are in the 40-nd sample).

Runs, per ORB-qualified confirmed name:
  research    = simulate_intrabar (trail 2, gate off)              [the sweep baseline]
  prod_off    = simulate_orb_tick_entry (production engine, gate off)   [must == research, else BUG]
  full        = simulate_orb_tick_entry (atr_gate 4.3 + ungate 4min, causal bars)  [the PR config]
  failclosed  = simulate_orb_tick_entry (atr_gate 4.3, NO ungate)      [to show what ungate recovers]
Then labels each research trade: ungate-early(kept) / atr-PASS(kept) / atr-FAIL slow(dropped).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import simulate_intrabar, simulate_orb_tick_entry
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import atr_pct5

_ET = ZoneInfo("America/New_York")
GATE, TRAIL, UNGATE_MIN = 4.3, 2.0, 4


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def hhmmss(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


def sig(ts):
    return [(hhmmss(t.entry_ts), round(t.entry_price, 4), hhmmss(t.exit_ts) if t.exit_ts else None,
             round(t.exit_price, 4) if t.exit_price is not None else None, round(t.pnl, 2), t.exit_reason)
            for t in ts]


def main():
    dates = [a for a in sys.argv[1:] if a.count("-") == 2 and a[:1].isdigit()]
    wdir = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--windows-dir=")), None)
    src = DbMarketDataSource(build_session_factory(get_settings()))
    faithful_ok = faithful_tot = 0
    for date in dates:
        y, mo, d = (int(x) for x in date.split("-"))
        obs, so, cut, end = _et(y, mo, d, 9, 25), _et(y, mo, d, 9, 30), _et(y, mo, d, 10, 0), _et(y, mo, d, 10, 10)
        early_end = so + timedelta(minutes=UNGATE_MIN)
        wins = load_windows(f"{wdir}/windows_{date}.json") if wdir else {}
        print(f"\n{'='*104}\nDAY {date}\n{'='*104}")
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
            base = dict(gap_cap_pct=1.5, qty=5, observe_open=obs, session_open=so, cutoff=cut,
                        capped=False, latency_s=3.0, trail_pct=TRAIL, entry_windows=ewin)
            research = simulate_intrabar(trades, quotes, **base)
            prod_off = simulate_orb_tick_entry(trades, quotes, **base)
            full = simulate_orb_tick_entry(trades, quotes, atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60,
                                           bars=bars, **base)
            failclosed = simulate_orb_tick_entry(trades, quotes, atr_gate_pct=GATE, gate_after_secs=0.0,
                                                 bars=bars, **base)
            if not research and not full:
                continue
            faithful_tot += 1
            match = sig(research) == sig(prod_off)
            faithful_ok += 1 if match else 0
            full_atr = atr_pct5(bars)
            tag = "HIGH-ATR" if (full_atr and full_atr >= GATE) else "slow"
            full_ts = {round(t.entry_ts.timestamp()) for t in full}
            fc_ts = {round(t.entry_ts.timestamp()) for t in failclosed}
            print(f"\n{sym:<6} ATR%(full)={full_atr:.2f} [{tag}]  faithful={'MATCH' if match else '*** DIVERGENCE (BUG) ***'}"
                  f"  research={len(research)} full={len(full)} (failclosed={len(failclosed)})")
            if not match:
                print(f"   research: {sig(research)}\n   prod_off: {sig(prod_off)}")
            for t in research:
                ets = round(t.entry_ts.timestamp())
                early = t.entry_ts < early_end
                kept = ets in full_ts
                if early and kept:
                    lbl = "ungate-early KEPT (failclosed would DROP)" if ets not in fc_ts else "early KEPT"
                elif kept:
                    lbl = "atr-gate PASS (high-ATR) KEPT"
                else:
                    lbl = "atr-gate FAIL (slow) DROPPED"
                print(f"   {hhmmss(t.entry_ts)} entry {t.entry_price:.4f} -> {hhmmss(t.exit_ts)} exit "
                      f"{t.exit_price:.4f}  pnl {t.pnl:+.2f}  {t.exit_reason:<11}  [{lbl}]")
    print(f"\n{'='*104}\nFAITHFULNESS: {faithful_ok}/{faithful_tot} names match the research sweep exactly "
          f"{'(all good)' if faithful_ok == faithful_tot else '*** DIVERGENCE — investigate ***'}")


if __name__ == "__main__":
    main()
